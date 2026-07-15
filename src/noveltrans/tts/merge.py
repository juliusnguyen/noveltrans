"""Merge per-chapter audio into one (or several) files via ffmpeg.

The per-chapter WAV/MP3 files already sit in `exports/audio/`; this module joins a
selected set of them — all, a chapter range, or fixed-size batches — into either an
M4B audiobook (AAC with per-chapter markers) or a flat joined MP3.

The selection/metadata builders are pure (no ffmpeg) so they can be unit-tested; only
`merge_chapters` shells out.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from noveltrans.errors import TtsError
from noveltrans.models import Chapter
from noveltrans.tts.convert import ffmpeg_available  # noqa: F401 (re-exported for callers)


class MergeCancelled(Exception):
    """Raised when a merge is cancelled mid-ffmpeg (not a failure)."""


@dataclass
class MergeWindow:
    """A contiguous chapter-number span selected for one output file."""

    first_num: int  # 1-based chapter number of the first included chapter
    last_num: int
    chapters: list[Chapter] = field(default_factory=list)


def plan_merge_windows(
    chapters: list[Chapter],
    voice: str,
    mode: str,  # "all" | "range" | "batch"
    *,
    start: int | None = None,
    end: int | None = None,
    batch: int | None = None,
) -> list[MergeWindow]:
    """Group the chapters that have audio in `voice` into output windows.

    Ranges/batches are by 1-based chapter *number* (`index + 1`), so boundaries are
    predictable and a missing chapter doesn't shift later batches. Each window's
    first/last number reflect its actually-included chapters (no phantom span). Windows
    with no audio in range are omitted. Returns [] when nothing matches.
    """
    avail = sorted(
        (c for c in chapters if c.audio_path and c.audio_voice == voice),
        key=lambda c: c.index,
    )
    if not avail:
        return []

    def window(chs: list[Chapter]) -> MergeWindow:
        return MergeWindow(chs[0].index + 1, chs[-1].index + 1, chs)

    if mode == "range":
        lo, hi = int(start or 1), int(end or 0)
        sel = [c for c in avail if lo <= c.index + 1 <= hi]
        return [window(sel)] if sel else []

    if mode == "batch":
        size = int(batch or 0)
        if size < 1:
            raise ValueError("batch size must be >= 1")
        max_num = avail[-1].index + 1
        windows: list[MergeWindow] = []
        lo = 1
        while lo <= max_num:
            hi = lo + size - 1
            sel = [c for c in avail if lo <= c.index + 1 <= hi]
            if sel:
                windows.append(window(sel))
            lo += size
        return windows

    # "all"
    return [window(avail)]


def chapter_marker_title(chapter: Chapter) -> str:
    """Bookmark label for a chapter — matches the text its audio was voiced from."""
    if chapter.audio_source == "original":
        return chapter.title
    return chapter.translated_title or chapter.title


def build_concat_list(paths: list[Path | str]) -> str:
    """ffmpeg concat-demuxer list body (single-quoted, escaped, one file per line)."""
    lines = []
    for path in paths:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def _escape_ffmetadata(value: str) -> str:
    for ch in ("\\", "=", ";", "#"):
        value = value.replace(ch, "\\" + ch)
    return value.replace("\n", " ")


@dataclass
class MergeSegment:
    path: Path
    seconds: float
    title: str


def build_chapter_metadata(segments: list[MergeSegment]) -> str:
    """ffmetadata document with one [CHAPTER] per segment, offsets in ms (TIMEBASE 1/1000)."""
    out = [";FFMETADATA1"]
    start_ms = 0
    for seg in segments:
        end_ms = start_ms + int(round(seg.seconds * 1000))
        out += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={_escape_ffmetadata(seg.title)}",
        ]
        start_ms = end_ms
    return "\n".join(out) + "\n"


def merge_chapters(
    segments: list[MergeSegment],
    out_path: Path,
    fmt: str,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Concatenate `segments` into `out_path`.

    fmt "m4b" → AAC with chapter markers; "mp3" → flat joined MP3. `cancelled` is polled
    while ffmpeg runs so a long merge can be interrupted (raises MergeCancelled). Raises
    TtsError on a missing ffmpeg or a non-zero exit.
    """
    if not segments:
        raise TtsError("Không có chương nào có audio để ghép.")
    if cancelled is not None and cancelled():
        raise MergeCancelled()
    out_path = Path(out_path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="noveltrans-merge-"))
    list_file = tmp_dir / "list.txt"
    meta_file = tmp_dir / "chapters.txt"
    err_file = tmp_dir / "err.txt"
    list_file.write_text(build_concat_list([s.path for s in segments]), encoding="utf-8")

    if fmt == "m4b":
        meta_file.write_text(build_chapter_metadata(segments), encoding="utf-8")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-i", str(meta_file),
            "-map", "0:a", "-map_metadata", "1",
            "-c:a", "aac", "-b:a", "96k",
            str(out_path),
        ]
    else:  # mp3 — flat join, no chapter markers
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:a", "libmp3lame", "-b:a", "96k",
            str(out_path),
        ]

    total = sum(s.seconds for s in segments)
    deadline = time.monotonic() + max(1800, int(total))  # encode is faster than realtime
    try:
        # Popen + stderr→file (not a pipe) so a chatty ffmpeg can't fill a pipe buffer
        # and deadlock, while we poll for cancellation.
        with open(err_file, "w", encoding="utf-8") as err:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
            except FileNotFoundError as exc:
                raise TtsError("Không tìm thấy ffmpeg — cài ffmpeg để ghép audio.") from exc
            while True:
                try:
                    proc.wait(timeout=0.3)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled is not None and cancelled():
                        _terminate(proc)
                        raise MergeCancelled()
                    if time.monotonic() > deadline:
                        _terminate(proc)
                        raise TtsError("ffmpeg quá thời gian khi ghép — thử lô nhỏ hơn.")
        if proc.returncode != 0:
            detail = err_file.read_text(encoding="utf-8", errors="replace").strip()[-300:]
            raise TtsError(f"ffmpeg trả lỗi (mã {proc.returncode}): {detail}")
        return out_path
    finally:
        for f in (list_file, meta_file, err_file):
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()

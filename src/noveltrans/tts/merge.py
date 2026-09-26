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
from noveltrans.runtime_env import no_console_kwargs
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
    predictable and a MISSING chapter (deleted, or never scraped) doesn't shift later
    batches — see `_group_windows`. Each window's first/last number reflect its
    actually-included chapters (no phantom span). Windows with no audio in range are
    omitted. Returns [] when nothing matches.

    A DISABLED chapter (feature 098) is different from a missing one: it still exists,
    it just must not occupy a grid slot in "batch" mode — the window reaches past its
    number to still collect `batch` chapters. See `_group_windows`'s `disabled_numbers`.
    """
    avail = sorted(
        (c for c in chapters if c.enabled and c.audio_path and c.audio_voice == voice),
        key=lambda c: c.index,
    )
    disabled_numbers = {c.index + 1 for c in chapters if not c.enabled}
    return _group_windows(avail, disabled_numbers, mode, start=start, end=end, batch=batch)


def plan_source_windows(
    releases: list,
    mode: str,  # "all" | "range" | "batch"
    *,
    start: int | None = None,
    end: int | None = None,
    batch: int | None = None,
) -> list[MergeWindow]:
    """Group downloaded SOURCE AUDIO releases into output windows.

    The counterpart of `plan_merge_windows` for the site's own audio edition. Two
    differences, both structural rather than cosmetic:

    * There is no voice to filter on — a release is not synthesized — so availability is
      simply "the file is on disk".
    * Numbering counts RELEASES, not chapters: `SourceAudio.index` is its position in the
      manifest, so "phần 1..N" runs 1..21 for a novel with 21 releases rather than jumping
      with the chapter numbers the volumes happen to cover.

    Everything downstream is shared: a release satisfies the same narrow protocol
    (`audio_path`, `audio_seconds`, `audio_source`, `title`, `translated_title`) that
    `chapter_marker_title` and the renderers read off a Chapter.

    Releases have no `enabled` concept (they're a different edition, not 1:1 with
    chapters) — an empty `disabled_numbers`, unlike `plan_merge_windows`.
    """
    avail = sorted((r for r in releases if r.audio_path), key=lambda r: r.index)
    return _group_windows(avail, set(), mode, start=start, end=end, batch=batch)


def _batch_window_end(lo: int, size: int, max_num: int, disabled_numbers: set[int]) -> int:
    """The last chapter NUMBER a fresh batch window starting at `lo` claims.

    Walks numbers one at a time from `lo`, counting each toward the window's `size`-sized
    quota UNLESS it's in `disabled_numbers` — a disabled chapter's number is skipped over
    entirely rather than counted (feature 098), while a number with no chapter at all
    (deleted, or never scraped) still counts, so a hole never shrinks or shifts a window
    (see `_group_windows`). Shared with `plan_locked_video_windows`, which only needs this
    for windows NOT already frozen by `committed`/`read_manual_windows`.
    """
    hi = lo
    counted = 0
    while counted < size and hi <= max_num:
        if hi not in disabled_numbers:
            counted += 1
        hi += 1
    return hi - 1


def _group_windows(
    avail: list,
    disabled_numbers: set[int],
    mode: str,
    *,
    start: int | None = None,
    end: int | None = None,
    batch: int | None = None,
) -> list[MergeWindow]:
    """Slice an already-filtered, index-sorted list into windows. Shared by both planners.

    Works on anything carrying `.index`; the callers differ only in what they consider
    available, which is why the filter is theirs and the slicing is here.

    In "batch" mode, a window's `[lo, hi]` number span is still walked one integer at a
    time starting from 1 (as it always was — a MISSING chapter number, deleted or never
    scraped, still fills a slot and leaves a hole, never shrinking or shifting a window).
    The one thing that changed (feature 098): a number in `disabled_numbers` does not
    count towards the window's `batch`-sized quota, so the window's `hi` reaches past it
    to still collect `batch` real slots — a disabled chapter is skipped over, not just
    left empty.
    """
    if not avail:
        return []

    def window(items: list) -> MergeWindow:
        return MergeWindow(items[0].index + 1, items[-1].index + 1, items)

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
            hi = _batch_window_end(lo, size, max_num, disabled_numbers)
            sel = [c for c in avail if lo <= c.index + 1 <= hi]
            if sel:
                windows.append(window(sel))
            lo = hi + 1
        return windows

    # "all"
    return [window(avail)]


def part_number(
    first_num: int, batch: int | None, disabled_numbers: set[int] | None = None,
) -> int:
    """Which part a window is, derived from the chapter it starts at.

    **Not** the window's position in the list being rendered. That was the old rule and it
    was wrong twice over:

    * Re-rendering one part on its own gave it a one-item list, so `i + 1` made it "Phần 1"
      while its filename kept the real chapter range — a part covering chapters 1381-1400
      was uploaded titled "Phần 1", cover and all.
    * `plan_merge_windows` omits a batch whose chapters have no audio, so a single gap
      shifted every later part's number down by one, no re-render needed.

    Batches sit on a fixed grid starting at chapter 1, so the slot a window *starts* in is
    its part number no matter what else is selected or missing. A window never spans a slot
    boundary (its chapters are picked within one), so the start alone decides it.

    `batch` under 1 (or None) means the caller has no batch grid — a custom range, where
    there is no meaningful part number — and falls back to 1 rather than raising.

    A DISABLED chapter (feature 098) doesn't occupy a grid slot at all — see
    `_group_windows`. `disabled_numbers`, when given, is the set of disabled 1-based
    chapter numbers; the part number is then `first_num - 1` MINUS however many of those
    disabled numbers fall before it, divided by the batch size — the same "skip over,
    don't shift" rule `_group_windows` applies when building the window itself. Omitted
    (the default), behavior is unchanged: every existing caller with nothing ever
    disabled keeps the exact old formula.
    """
    size = int(batch or 0)
    if size < 1:
        return 1
    if not disabled_numbers:
        return (int(first_num) - 1) // size + 1
    skipped = sum(1 for n in disabled_numbers if n < int(first_num))
    rank = (int(first_num) - 1) - skipped
    return rank // size + 1


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
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=err, **no_console_kwargs()
                )
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

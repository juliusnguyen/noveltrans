"""Render per-chapter audio into a video: background image + audio + burned-in titles.

A close cousin of `tts/merge.py` — it reuses that module's chapter selection
(`plan_merge_windows`), segment type (`MergeSegment`), concat list, and the ffmpeg
Popen/poll/cancel/deadline pattern. What's new here:

  * `build_ass_subtitles` — an ASS subtitle document that shows the novel title for the
    whole video plus the current chapter title, timed to each chapter's audio boundary.
  * `build_youtube_description` — a companion text file where each chapter is a clickable
    YouTube timestamp (`0:00`, `2:05`, …), so viewers can jump to a chapter.
  * `render_video` — the ffmpeg call: loop the background image, mux the concatenated
    chapter audio, burn the ASS titles, end when the audio ends.

The builders are pure (no ffmpeg) so they unit-test without a render; only `render_video`
shells out. Chapter title text is Vietnamese; the bundled Noto Sans font (assets/) renders
its tone marks. A video made from the *original* Chinese audio would show CJK titles as
boxes — the bundled font is Latin/Vietnamese only (documented limit).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from importlib import resources
from pathlib import Path

from noveltrans.errors import TtsError
from noveltrans.tts.convert import ffmpeg_available  # noqa: F401 (re-exported for callers)

# Reused verbatim from merge — the selection, segment type, concat list, and cancel.
from noveltrans.tts.merge import (  # noqa: F401
    MergeCancelled,
    MergeSegment,
    MergeWindow,
    _terminate,
    build_concat_list,
    chapter_marker_title,
    plan_merge_windows,
)

FONT_NAME = "Noto Sans"  # the bundled TTF's internal family name (assets/NotoSans-Regular.ttf)


@contextmanager
def font_dir_context():
    """Yield a real filesystem path to the bundled-font directory.

    Uses importlib.resources so it works both from source and inside the frozen .app,
    where the asset may be extracted from an archive. ffmpeg/libass need a real path.
    """
    with resources.as_file(resources.files("noveltrans.tts").joinpath("assets")) as p:
        yield p


# -- audio duration probing ---------------------------------------------------

def _probe_duration(path: Path | str) -> float:
    """Real duration (seconds) of an audio file via ffprobe. 0.0 if it can't be read."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _with_real_durations(segments: list[MergeSegment]) -> list[MergeSegment]:
    """Return `segments` with each `seconds` set to the file's actual duration.

    The subtitle/timestamp timing must match the audio the video actually plays — the
    stored `audio_seconds` can be 0 (audio made before durations were tracked) or stale,
    which would collapse the subtitle events to zero length and make them invisible.
    Falls back to the stored value when a probe fails.
    """
    timed = []
    for seg in segments:
        probed = _probe_duration(seg.path)
        timed.append(replace(seg, seconds=probed) if probed > 0 else seg)
    return timed


# -- timing helpers -----------------------------------------------------------

def _ass_time(total_seconds: float) -> str:
    """Format seconds as an ASS timestamp `H:MM:SS.cc` (centiseconds)."""
    cs = int(round(total_seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _yt_timestamp(total_seconds: float) -> str:
    """Format seconds as a YouTube description timestamp: `M:SS` (or `H:MM:SS` past 1h)."""
    total = int(total_seconds)  # YouTube timestamps are whole seconds
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# -- ASS subtitle document ----------------------------------------------------

_ASS_STYLES = (
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    # BorderStyle=4 + a translucent BackColour (&H80…) draws a dark scrim box behind the
    # text so it stays legible over any background image.
    "Style: Novel,{font},{nsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
    "0,0,0,0,100,100,0,0,4,0,0,8,80,80,60,1\n"
    "Style: Chapter,{font},{csize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
    "0,0,0,0,100,100,0,0,4,0,0,2,80,80,120,1\n"
)

_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "WrapStyle: 2\n"
    "ScaledBorderAndShadow: yes\n"
    "PlayResX: {w}\n"
    "PlayResY: {h}\n\n"
)

_ASS_EVENTS_HEADER = (
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)

_TRAILING_BACKSLASH = re.compile(r"\\+$")


def _escape_ass(text: str) -> str:
    """Make arbitrary title text safe inside an ASS Dialogue Text field.

    `{`/`}` open/close ASS override blocks and a literal newline ends the event, so a
    stray one in a chapter title would corrupt the line or inject a tag. Commas are safe
    — Text is the final field, so libass takes the rest of the line verbatim.
    """
    text = text.replace("{", "(").replace("}", ")")
    text = text.replace("\r\n", "\\N").replace("\r", "\\N").replace("\n", "\\N")
    return _TRAILING_BACKSLASH.sub("", text)  # a lone trailing backslash is meaningless


def build_ass_subtitles(
    segments: list[MergeSegment],
    novel_title: str,
    *,
    width: int = 1920,
    height: int = 1080,
    font_name: str = FONT_NAME,
    novel_font_px: int = 56,
    chapter_font_px: int = 72,
) -> str:
    """An ASS document: the novel title for the whole video + one event per chapter.

    Chapter events are timed by the cumulative sum of `MergeSegment.seconds` — the same
    offset math as merge's chapter markers, so titles change exactly at the audio
    boundaries.
    """
    total = sum(s.seconds for s in segments)
    out = [
        _ASS_HEADER.format(w=width, h=height),
        _ASS_STYLES.format(font=font_name, nsize=novel_font_px, csize=chapter_font_px),
        "\n",
        _ASS_EVENTS_HEADER,
        f"Dialogue: 0,{_ass_time(0)},{_ass_time(total)},Novel,,0,0,0,,{_escape_ass(novel_title)}\n",
    ]
    start = 0.0
    for seg in segments:
        end = start + seg.seconds
        out.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Chapter,,0,0,0,,"
            f"{_escape_ass(seg.title)}\n"
        )
        start = end
    return "".join(out)


# -- YouTube description ------------------------------------------------------

def build_youtube_description(segments: list[MergeSegment], novel_title: str) -> str:
    """A YouTube description whose chapter timestamps become clickable jump markers.

    YouTube turns a description into video chapters when it finds timestamps that start
    at `0:00`, ascend, and number at least 3 — so the first chapter here is always `0:00`.
    Each line is `<timestamp> <chapter title>`; the title header lines above are ignored
    by YouTube's parser but read nicely for a human.
    """
    lines = []
    if novel_title.strip():
        lines.append(novel_title.strip())
        lines.append("")
    lines.append("Mục lục chương:")
    start = 0.0
    for seg in segments:
        lines.append(f"{_yt_timestamp(start)} {seg.title}")
        start += seg.seconds
    return "\n".join(lines) + "\n"


# -- the ffmpeg render --------------------------------------------------------

def _run_ffmpeg(
    cmd: list[str],
    err_file: Path,
    cancelled: Callable[[], bool] | None,
    deadline: float,
    what: str,
) -> None:
    """Run one ffmpeg command, polling for cancel/deadline. Raises on failure.

    stderr goes to a file (not a pipe) so a chatty ffmpeg can't fill a pipe buffer and
    deadlock while we poll — the same pattern as merge_chapters.
    """
    with open(err_file, "w", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
        except FileNotFoundError as exc:
            raise TtsError("Không tìm thấy ffmpeg — cài ffmpeg để tạo video.") from exc
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
                    raise TtsError(f"ffmpeg quá thời gian khi {what} — thử lô nhỏ hơn.")
    if proc.returncode != 0:
        detail = err_file.read_text(encoding="utf-8", errors="replace").strip()[-300:]
        raise TtsError(f"ffmpeg trả lỗi (mã {proc.returncode}) khi {what}: {detail}")


def _filtergraph(width: int, height: int, subs_path: Path, font_dir: Path) -> str:
    """Blurred-fill background (any aspect → no black bars) with the ASS titles burned in.

    [bg] the image scaled to COVER the frame, blurred + darkened; [fg] the image scaled to
    FIT (undistorted) centred over it; then burn the subtitles.
    """
    subs = str(subs_path).replace("\\", "/").replace(":", r"\:")
    fonts = str(font_dir).replace("\\", "/").replace(":", r"\:")
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:2,eq=brightness=-0.25[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"subtitles='{subs}':fontsdir='{fonts}'[v]"
    )


def render_video(
    segments: list[MergeSegment],
    image_path: Path,
    out_path: Path,
    font_dir: Path,
    novel_title: str,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 12,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Render `segments` into an MP4 at `out_path`; also write the YouTube description.

    The background `image_path` fills the frame (blurred fill), the concatenated chapter
    audio plays, and the ASS chapter titles are burned in. `-shortest` ends the video
    with the audio. A `<out>.txt` description with clickable timestamps is written next
    to the video. Raises TtsError on ffmpeg failure, MergeCancelled if cancelled.
    """
    if not segments:
        raise TtsError("Không có chương nào có audio để tạo video.")
    if cancelled is not None and cancelled():
        raise MergeCancelled()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="noveltrans-video-"))
    list_file = tmp_dir / "list.txt"
    subs_file = tmp_dir / "subs.ass"
    audio_file = tmp_dir / "audio.m4a"
    err_file = tmp_dir / "err.txt"

    # Time the titles by the audio the video actually plays, not the (possibly 0/stale)
    # stored durations — otherwise the subtitle events collapse to zero length.
    segments = _with_real_durations(segments)
    total = sum(s.seconds for s in segments)
    deadline = time.monotonic() + max(3600, int(total) * 3)  # encode is slower than merge

    try:
        list_file.write_text(build_concat_list([s.path for s in segments]), encoding="utf-8")
        subs_file.write_text(
            build_ass_subtitles(segments, novel_title, width=width, height=height),
            encoding="utf-8",
        )

        # 1) Concatenate the per-chapter audio into one AAC track so -shortest and the
        #    muxed duration are exact regardless of the inputs' codecs (WAV/MP3 mixed).
        _run_ffmpeg(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c:a", "aac", "-b:a", "96k", str(audio_file)],
            err_file, cancelled, deadline, "ghép âm thanh",
        )

        # 2) Loop the image, burn the titles, mux the audio, end with the audio.
        _run_ffmpeg(
            ["ffmpeg", "-y",
             "-loop", "1", "-framerate", str(fps), "-i", str(image_path),
             "-i", str(audio_file),
             "-filter_complex", _filtergraph(width, height, subs_file, font_dir),
             "-map", "[v]", "-map", "1:a",
             "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", str(fps),
             "-c:a", "copy", "-shortest", str(out_path)],
            err_file, cancelled, deadline, "tạo video",
        )

        # Companion YouTube description with clickable chapter timestamps.
        out_path.with_suffix(".txt").write_text(
            build_youtube_description(segments, novel_title), encoding="utf-8"
        )
        return out_path
    finally:
        for f in (list_file, subs_file, audio_file, err_file):
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()

"""Render per-chapter audio into a video: background image + audio + burned-in titles.

A close cousin of `tts/merge.py` — it reuses that module's chapter selection
(`plan_merge_windows`), segment type (`MergeSegment`), concat list, and the ffmpeg
Popen/poll/cancel/deadline pattern. What's new here:

  * `build_ass_subtitles` — an ASS subtitle document that shows the novel title for the
    whole video plus the current chapter title, timed to each chapter's audio boundary.
  * `build_youtube_description` — a companion text file where each chapter is a clickable
    YouTube timestamp (`0:00`, `2:05`, …), so viewers can jump to a chapter.
  * `render_video` — bakes three `player_skin` layers (the static skin: pastel gradient +
    the chosen photo framed on the left + an empty progress track; a vinyl disc labelled
    with the bundled logo; a playhead dot), then the ffmpeg call loops them and animates:
    the vinyl spins, an audio-driven bar spectrum plays, the playhead slides along the
    track with real progress, the chapter audio is muxed, the ASS titles are burned, and it
    ends when the audio ends.

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
from noveltrans.tts.player_skin import (
    PlayerLayout,
    build_knob,
    build_player_skin,
    build_vinyl,
)

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
VINYL_LABEL = "vinyl_label.png"  # bundled logo used as the record's centre label (assets/)
# One vinyl revolution every N seconds. A real 33⅓rpm record spins ~1.8s/rev, which
# strobes at video frame rates; a slower turn reads calmly as "playing".
_VINYL_SPIN_SECONDS = 8.0

# Quality/speed presets for the video export. Lower fidelity → faster encode. `speed` is the
# approximate encode rate (× real time) on Apple Silicon — measured for "high" (~3×), scaled
# for the rest — used only to show a render-time estimate before starting. `spin_vinyl=False`
# drops the per-frame rotate (the biggest filter cost), so the disc is static.
# `speed` values are measured on Apple Silicon (high ~3.0×, fast ~10.6×, fastest ~17.1×
# realtime), rounded down a touch so the shown estimate is conservative under thermal load.
VIDEO_QUALITY_PRESETS: dict[str, dict] = {
    "high":        {"width": 1920, "height": 1080, "fps": 25, "spin_vinyl": True,  "speed": 3.0},
    # Full 1080p but with a static disc — dropping the per-frame rotate (the biggest filter
    # cost) makes it markedly faster than "high" while keeping the resolution.
    "high_static": {"width": 1920, "height": 1080, "fps": 25, "spin_vinyl": False, "speed": 5.0},
    "fast":        {"width": 1280, "height": 720,  "fps": 25, "spin_vinyl": True,  "speed": 9.0},
    "fastest":     {"width": 1280, "height": 720,  "fps": 15, "spin_vinyl": False, "speed": 15.0},
}
DEFAULT_VIDEO_QUALITY = "high"


def video_preset(key: str) -> dict:
    """Return the preset params for `key`, falling back to the default if unknown."""
    return VIDEO_QUALITY_PRESETS.get(key, VIDEO_QUALITY_PRESETS[DEFAULT_VIDEO_QUALITY])


# Selectable title fonts — all bundled in assets/ (OFL, full Vietnamese coverage). libass
# resolves a style by its `family` name among the TTFs in `fontsdir`, so `family` MUST equal
# the TTF's name-table family (guarded by a test). `noto_sans`'s family == FONT_NAME, so the
# default keeps the original behaviour.
VIDEO_FONTS: dict[str, dict[str, str]] = {
    "noto_sans":  {"label": "Noto Sans (mặc định)", "file": "NotoSans-Regular.ttf",       "family": "Noto Sans"},
    "be_vietnam": {"label": "Be Vietnam Pro",        "file": "BeVietnamPro-Regular.ttf",    "family": "Be Vietnam Pro"},
    "nunito":     {"label": "Nunito (bo tròn)",      "file": "Nunito-Regular.ttf",          "family": "Nunito"},
    "montserrat": {"label": "Montserrat",            "file": "Montserrat-Regular.ttf",      "family": "Montserrat"},
    "lora":       {"label": "Lora (serif)",          "file": "Lora-Regular.ttf",            "family": "Lora"},
    "playfair":   {"label": "Playfair Display (serif)", "file": "PlayfairDisplay-Regular.ttf", "family": "Playfair Display"},
}
DEFAULT_VIDEO_FONT = "noto_sans"


def video_font(key: str) -> dict[str, str]:
    """Return the font descriptor for `key`, falling back to the default if unknown."""
    return VIDEO_FONTS.get(key, VIDEO_FONTS[DEFAULT_VIDEO_FONT])


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
    # Colours are ASS &HAABBGGRR. The player's "now playing" block: the Novel title is the
    # muted 'album' line, the Chapter title the bold 'track' line. Alignment 8 (top-center) +
    # MarginL≈right-column-start frames both into the right column (over the gradient, not the
    # photo). The palette adapts to the backdrop: dark text + no outline over a light skin;
    # light text + a dark outline over a dark chosen background (see `_text_palette`).
    "Style: Novel,{font},{nsize},{npri},&H000000FF,{ocol},&H00000000,"
    "0,0,0,0,100,100,0,0,1,{ow},1,8,{mL},{mR},{nmargin},1\n"
    "Style: Chapter,{font},{csize},{cpri},&H000000FF,{ocol},&H30000000,"
    "1,0,0,0,100,100,0,0,1,{ow},1,8,{mL},{mR},{cmargin},1\n"
)

# Below this backdrop luminance (0..255) the titles flip to light-on-dark.
_TEXT_LUMA_THRESHOLD = 140


def _ass_colour(rgb: tuple[int, int, int], alpha: int = 0) -> str:
    """An ASS colour literal `&HAABBGGRR` from an (r, g, b) tuple."""
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


# Dark text + white(unused, outline 0) over the light pastel skin — the original look.
_LIGHT_BG_TEXT = {"npri": "&H00A06B8A", "cpri": "&H00502A55", "ocol": "&H00FFFFFF", "ow": 0}
# Light text + a dark outline so it reads over a dark/mid chosen background.
_DARK_BG_TEXT = {
    "npri": _ass_colour((214, 202, 230)),  # soft light lavender (album line)
    "cpri": _ass_colour((246, 246, 250)),  # near-white (track line)
    "ocol": _ass_colour((18, 16, 26)),     # dark edge for legibility on mid-tones
    "ow": 2,
}


def _text_palette(bg_color: tuple[int, int, int] | None) -> dict:
    """Pick the title colour palette for a backdrop: light-on-dark when `bg_color` is dark,
    else the default dark-on-light. `None` (default gradient) always uses dark-on-light."""
    if bg_color is None:
        return _LIGHT_BG_TEXT
    r, g, b = bg_color
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    return _DARK_BG_TEXT if luma < _TEXT_LUMA_THRESHOLD else _LIGHT_BG_TEXT

# WrapStyle 0 = smart auto-wrapping: a long title breaks onto balanced lines that fit within
# the right column (PlayResX − MarginL − MarginR), so it never overflows the frame or spills
# left onto the photo. (Was 2 = no wrapping, which let long chapter titles run off-frame.)
# We still emit hard breaks as `\N` in `_escape_ass`, which break under any wrap style.
_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "WrapStyle: 0\n"
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
    bg_color: tuple[int, int, int] | None = None,
) -> str:
    """An ASS document: the novel title for the whole video + one event per chapter.

    Chapter events are timed by the cumulative sum of `MergeSegment.seconds` — the same
    offset math as merge's chapter markers, so titles change exactly at the audio
    boundaries. Each chapter title fades in/out (`\\fad`) so it changes smoothly; the
    novel title stays solid the whole video. Both titles form the player's "now playing"
    block in the right column (`PlayerLayout` fixes the placement): the novel title is the
    muted album line above, the chapter title the bold track line below it. The title colours
    follow `bg_color` — light-on-dark over a dark chosen background, dark-on-light otherwise.
    """
    lay = PlayerLayout.of(width, height)
    palette = _text_palette(bg_color)
    total = sum(s.seconds for s in segments)
    out = [
        _ASS_HEADER.format(w=width, h=height),
        _ASS_STYLES.format(
            font=font_name, nsize=lay.novel_font_px, csize=lay.chapter_font_px,
            mL=lay.text_margin_l, mR=lay.text_margin_r,
            nmargin=lay.novel_margin_v, cmargin=lay.chapter_margin_v,
            npri=palette["npri"], cpri=palette["cpri"],
            ocol=palette["ocol"], ow=palette["ow"],
        ),
        "\n",
        _ASS_EVENTS_HEADER,
        f"Dialogue: 0,{_ass_time(0)},{_ass_time(total)},Novel,,0,0,0,,{_escape_ass(novel_title)}\n",
    ]
    start = 0.0
    for seg in segments:
        end = start + seg.seconds
        # The \fad override is added OUTSIDE the escaped title, so a title's own braces
        # (already neutralised by _escape_ass) can't break out of or corrupt the fade.
        out.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Chapter,,0,0,0,,"
            f"{{\\fad(400,400)}}{_escape_ass(seg.title)}\n"
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


# -- rich per-part title & description (feature 025) --------------------------

DEFAULT_VIDEO_CREDIT = "Fox Novel"


def video_part_name(slug: str, first_num: int, last_num: int, *, whole_novel: bool = False) -> str:
    """The output file name for one part video — the single source of truth for naming.

    A single whole-novel video is just `{slug}.mp4`; every windowed part is
    `{slug}-{first:04d}-{last:04d}.mp4`. The GUI uses this to tell which parts already exist.
    """
    if whole_novel:
        return f"{slug}.mp4"
    return f"{slug}-{first_num:04d}-{last_num:04d}.mp4"


def build_upload_title(vn_title: str, part_num: int | None) -> str:
    """The video title: `{vn_title} - Phần {N}`, or just `vn_title` for a whole-novel video.

    `part_num is None` means the single video covers the whole novel (no part split), so the
    "- Phần N" suffix is omitted.
    """
    vn_title = (vn_title or "").strip()
    if part_num is None:
        return vn_title
    return f"{vn_title} - Phần {part_num}"


def _chapter_timestamp_lines(segments: list[MergeSegment]) -> list[str]:
    """`<timestamp> <title>` lines for the chapter table; first is always `0:00`."""
    lines = []
    start = 0.0
    for seg in segments:
        lines.append(f"{_yt_timestamp(start)} {seg.title}")
        start += seg.seconds
    return lines


def build_video_description(
    segments: list[MergeSegment],
    *,
    original_title: str,
    vn_title: str,
    original_author: str,
    vn_author: str,
    total_chapters: int,
    credit: str = DEFAULT_VIDEO_CREDIT,
) -> str:
    """A YouTube description for one part: header block + clickable chapter table + credit.

    Shape (see 025.00-PROMPT.md)::

        Tên truyện: {original_title} — "{vn_title}"
        Tác giả: {original_author} "{vn_author}"
        Số chương: {total_chapters}

        Mục lục chương:
        0:00 {ch1}
        ...

        Tạo bởi: {credit}

    The `Tác giả:` line drops the trailing quoted Vietnamese clause when `vn_author` is empty
    (older projects translated before the field existed), rather than printing empty quotes.
    Chapter timestamps come from the cumulative `MergeSegment.seconds`, so pass segments that
    already carry real durations (the caller runs `_with_real_durations` first).
    """
    original_title = (original_title or "").strip()
    vn_title = (vn_title or "").strip()
    original_author = (original_author or "").strip()
    vn_author = (vn_author or "").strip()

    if vn_author:
        author_line = f'Tác giả: {original_author} "{vn_author}"'
    else:
        author_line = f"Tác giả: {original_author}"

    lines = [
        f'Tên truyện: {original_title} — "{vn_title}"',
        author_line,
        f"Số chương: {total_chapters}",
        "",
        "Mục lục chương:",
        *_chapter_timestamp_lines(segments),
        "",
        f"Tạo bởi: {credit}",
    ]
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


# All chapter PCM is normalised to this before concatenation, so the raw byte stream is
# uniform (a hard requirement for headerless concat) regardless of each file's own format.
_PCM_RATE = 48000
_PCM_ARGS = ("-f", "s16le", "-ac", "1", "-ar", str(_PCM_RATE))


def _concat_audio(
    paths: list[Path | str],
    audio_file: Path,
    err_file: Path,
    cancelled: Callable[[], bool] | None,
    deadline: float,
) -> None:
    """Concatenate chapter audio into one AAC track WITHOUT the concat demuxer.

    Why not `-f concat`: that demuxer accumulates each input's timestamps, and (at least
    in the ffmpeg build shipped here) overflows a signed 32-bit sample counter at
    2**31 / 48000 Hz ≈ 12.4 hours — past which it emits a wrapped, non-monotonic DTS and
    the muxer simply stops advancing, silently truncating any longer "toàn bộ" video to
    ~12.4h. Instead we decode each file independently to raw 48 kHz mono PCM (headerless,
    so it carries no timestamps at all) and stream the samples into a single encoder that
    stamps one fresh monotonic timeline — correct for any total length.

    The decoders run one at a time, each writing straight into the encoder's stdin, so the
    encoder consumes concurrently and the pipe never deadlocks. Honours cancel/deadline.
    """
    with open(err_file, "w", encoding="utf-8") as err:
        try:
            enc = subprocess.Popen(
                ["ffmpeg", "-y", *_PCM_ARGS, "-i", "pipe:0",
                 "-c:a", "aac", "-b:a", "96k", str(audio_file)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
            )
        except FileNotFoundError as exc:
            raise TtsError("Không tìm thấy ffmpeg — cài ffmpeg để tạo video.") from exc

        def _poll(proc: subprocess.Popen, *others: subprocess.Popen) -> None:
            """Wait for `proc`, tearing everything down on cancel/deadline."""
            while True:
                try:
                    proc.wait(timeout=0.3)
                    return
                except subprocess.TimeoutExpired:
                    if cancelled is not None and cancelled():
                        for p in (proc, *others):
                            _terminate(p)
                        raise MergeCancelled()
                    if time.monotonic() > deadline:
                        for p in (proc, *others):
                            _terminate(p)
                        raise TtsError("ffmpeg quá thời gian khi ghép âm thanh — thử lô nhỏ hơn.")

        try:
            for path in paths:
                if enc.poll() is not None:
                    break  # encoder exited early (e.g. disk full) — reported below
                dec = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), *_PCM_ARGS, "pipe:1"],
                    stdout=enc.stdin, stderr=subprocess.DEVNULL,
                )
                _poll(dec, enc)
                if dec.returncode != 0:
                    _terminate(enc)
                    raise TtsError(
                        f"ffmpeg không giải mã được audio chương ({Path(path).name}) khi ghép."
                    )
            if enc.stdin:
                enc.stdin.close()  # EOF → the encoder finalises the file
            _poll(enc)
        finally:
            if enc.stdin and not enc.stdin.closed:
                enc.stdin.close()

    if enc.returncode != 0:
        detail = err_file.read_text(encoding="utf-8", errors="replace").strip()[-300:]
        raise TtsError(f"ffmpeg trả lỗi (mã {enc.returncode}) khi ghép âm thanh: {detail}")


def _filtergraph(width: int, height: int, subs_path: Path, font_dir: Path,
                 total_seconds: float, spin_vinyl: bool = True) -> str:
    """Overlay the animated bits onto the pre-baked player artwork.

    Inputs: 0 = the static skin (`build_player_skin`: gradient, framed photo, empty
    progress track), 1 = the audio, 2 = the vinyl disc, 3 = the playhead knob. Here we add
    the moving parts:
      * the vinyl (input 2): when `spin_vinyl`, `rotate`d by an angle that grows with time
        so it spins in place (`ow=iw:oh=ih` keeps the frame; `fillcolor=none` keeps corners
        clear); otherwise overlaid statically — skipping the per-frame rotate, the single
        biggest filter cost, for a much faster encode;
      * the `showfreqs` bar spectrum (from the audio) in the right column (purple, so it
        reads over the light skin — no `rate=` option, it animates at the output `-r`);
      * the playhead knob (input 3) slid along the track, its x a linear function of
        `t / total` so it tracks real playback progress;
      * the burned-in ASS titles.
    """
    lay = PlayerLayout.of(width, height)
    subs = str(subs_path).replace("\\", "/").replace(":", r"\:")
    fonts = str(font_dir).replace("\\", "/").replace(":", r"\:")
    total = max(total_seconds, 0.001)  # guard the knob's t/total against a zero divide
    knob_x = f"{lay.track_x}+(t/{total})*{lay.track_w}-{lay.knob_half}"
    knob_y = lay.track_y - lay.knob_half
    if spin_vinyl:
        vinyl = (
            f"[2:v]format=rgba,rotate=a='2*PI*t/{_VINYL_SPIN_SECONDS}':fillcolor=none:"
            f"ow=iw:oh=ih[vin];"
            f"[0:v][vin]overlay={lay.vinyl_x}:{lay.vinyl_y}[s1];"
        )
    else:
        vinyl = f"[0:v][2:v]overlay={lay.vinyl_x}:{lay.vinyl_y}[s1];"  # static, no rotate
    return (
        f"[1:a]showfreqs=s={lay.bars_w}x{lay.bars_h}:mode=bar:ascale=sqrt:fscale=log:"
        f"win_size=2048:colors=0x8a52c8[viz];"
        f"{vinyl}"
        f"[s1][viz]overlay={lay.bars_x}:{lay.bars_y}[s2];"
        f"[s2][3:v]overlay=x='{knob_x}':y={knob_y}[s3];"
        f"[s3]subtitles='{subs}':fontsdir='{fonts}'[v]"
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
    fps: int = 25,
    spin_vinyl: bool = True,
    font_name: str = FONT_NAME,
    bg_color: tuple[int, int, int] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Render `segments` into an MP4 at `out_path`; also write the YouTube description.

    The `image_path` is framed on the LEFT of a "music player" skin (pastel gradient); on
    the RIGHT: a spinning vinyl (its centre label is the bundled logo), the novel + chapter
    titles as the "now playing" text (chapter fading on change), an audio-driven bar
    spectrum, and a real progress bar whose playhead slides with playback. The concatenated
    chapter audio plays and `-shortest` ends the video with it. A `<out>.txt` description
    with clickable timestamps is written next to the video. Raises TtsError on ffmpeg
    failure, MergeCancelled if cancelled.
    """
    if not segments:
        raise TtsError("Không có chương nào có audio để tạo video.")
    if cancelled is not None and cancelled():
        raise MergeCancelled()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="noveltrans-video-"))
    subs_file = tmp_dir / "subs.ass"
    audio_file = tmp_dir / "audio.m4a"
    skin_file = tmp_dir / "skin.png"
    vinyl_file = tmp_dir / "vinyl.png"
    knob_file = tmp_dir / "knob.png"
    err_file = tmp_dir / "err.txt"

    # Time the titles by the audio the video actually plays, not the (possibly 0/stale)
    # stored durations — otherwise the subtitle events collapse to zero length.
    segments = _with_real_durations(segments)
    total = sum(s.seconds for s in segments)
    # The waveform makes this motion video (not a still), so the encode is slower than
    # 019's stillimage pass — give it more headroom.
    deadline = time.monotonic() + max(3600, int(total) * 6)

    try:
        subs_file.write_text(
            build_ass_subtitles(segments, novel_title, width=width, height=height,
                                font_name=font_name, bg_color=bg_color),
            encoding="utf-8",
        )
        # Bake the three artwork layers once; ffmpeg loops each and animates them: the
        # static skin (backdrop), the vinyl disc (spun), and the playhead knob (slid).
        lay = PlayerLayout.of(width, height)
        build_player_skin(image_path, skin_file, width=width, height=height, bg_color=bg_color)
        build_vinyl(font_dir / VINYL_LABEL, vinyl_file, size=lay.vinyl_size)
        build_knob(knob_file, radius=lay.knob_r)

        # 1) Concatenate the per-chapter audio into one AAC track (mixed WAV/MP3 inputs ok).
        #    NOT via the concat demuxer — its 32-bit timestamp counter overflows at ~12.4h
        #    and would truncate a full-novel video; _concat_audio decodes per file instead.
        _concat_audio([s.path for s in segments], audio_file, err_file, cancelled, deadline)

        # 2) Loop the skin/vinyl/knob, spin the vinyl + slide the playhead + draw the bars,
        #    burn the titles, mux the audio, end with it. No -tune stillimage: it animates.
        _run_ffmpeg(
            ["ffmpeg", "-y",
             "-loop", "1", "-framerate", str(fps), "-i", str(skin_file),
             "-i", str(audio_file),
             "-loop", "1", "-i", str(vinyl_file),
             "-loop", "1", "-i", str(knob_file),
             "-filter_complex", _filtergraph(width, height, subs_file, font_dir, total, spin_vinyl),
             "-map", "[v]", "-map", "1:a",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(fps),
             "-c:a", "copy", "-shortest", str(out_path)],
            err_file, cancelled, deadline, "tạo video",
        )

        # Companion YouTube description with clickable chapter timestamps.
        out_path.with_suffix(".txt").write_text(
            build_youtube_description(segments, novel_title), encoding="utf-8"
        )
        return out_path
    finally:
        for f in (subs_file, audio_file, skin_file, vinyl_file, knob_file, err_file):
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()


# -- preview frame ------------------------------------------------------------

_PREVIEW_TOTAL = 3.0   # synthetic "video length" (s) so the playhead sits partway along
_PREVIEW_GRAB_T = 1.0  # grab the frame at t=1s (knob ~1/3 across; rotate/bars settled)
_PREVIEW_FPS = 25


def render_preview_frame(
    image_path: Path,
    out_png: Path,
    font_dir: Path,
    novel_title: str,
    sample_chapter_title: str,
    *,
    width: int = 1920,
    height: int = 1080,
    spin_vinyl: bool = True,
    font_name: str = FONT_NAME,
    bg_color: tuple[int, int, int] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Render ONE still PNG of the music-player video — a fast preview, no encode/audio.

    Bakes the same skin/vinyl/knob and reuses the exact `_filtergraph` as a real render, so
    the preview is WYSIWYG (layout, photo, titles, font). Needs no chapter audio: a short
    pink-noise `lavfi` source drives `showfreqs` so the bars look alive (silence would render
    empty), and a single frame is grabbed at `_PREVIEW_GRAB_T` with a synthetic total so the
    playhead sits partway along the track. Raises TtsError on ffmpeg failure.
    """
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="noveltrans-preview-"))
    subs_file = tmp_dir / "subs.ass"
    skin_file = tmp_dir / "skin.png"
    vinyl_file = tmp_dir / "vinyl.png"
    knob_file = tmp_dir / "knob.png"
    err_file = tmp_dir / "err.txt"
    deadline = time.monotonic() + 120

    try:
        lay = PlayerLayout.of(width, height)
        # One synthetic chapter spanning the whole preview so both titles show at grab time.
        sample = [MergeSegment(path="", seconds=_PREVIEW_TOTAL, title=sample_chapter_title)]
        subs_file.write_text(
            build_ass_subtitles(sample, novel_title, width=width, height=height,
                                font_name=font_name, bg_color=bg_color),
            encoding="utf-8",
        )
        build_player_skin(image_path, skin_file, width=width, height=height, bg_color=bg_color)
        build_vinyl(font_dir / VINYL_LABEL, vinyl_file, size=lay.vinyl_size)
        build_knob(knob_file, radius=lay.knob_r)

        _run_ffmpeg(
            ["ffmpeg", "-y",
             "-loop", "1", "-framerate", str(_PREVIEW_FPS), "-i", str(skin_file),
             "-f", "lavfi", "-t", "2",
             "-i", f"anoisesrc=color=pink:amplitude=0.5:sample_rate={_PCM_RATE}",
             "-loop", "1", "-i", str(vinyl_file),
             "-loop", "1", "-i", str(knob_file),
             "-filter_complex",
             _filtergraph(width, height, subs_file, font_dir, _PREVIEW_TOTAL, spin_vinyl),
             "-map", "[v]", "-ss", str(_PREVIEW_GRAB_T), "-frames:v", "1", "-update", "1",
             str(out_png)],
            err_file, cancelled, deadline, "tạo ảnh xem trước",
        )
        return out_png
    finally:
        for f in (subs_file, skin_file, vinyl_file, knob_file, err_file):
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()

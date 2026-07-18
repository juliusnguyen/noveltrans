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
    # Colours are ASS &HAABBGGRR. Over the light pastel skin the titles sit in the right
    # column (over the gradient, not the photo), so no scrim is needed — BorderStyle=1 with
    # Outline=0 draws plain text. The player's "now playing" block: the Novel title is the
    # muted 'album' line (grey-purple), the Chapter title the bold 'track' line (dark
    # purple). Alignment 8 (top-center) + MarginL≈right-column-start frames both into the
    # right column; a soft shadow lifts them off the gradient.
    "Style: Novel,{font},{nsize},&H00A06B8A,&H000000FF,&H00FFFFFF,&H00000000,"
    "0,0,0,0,100,100,0,0,1,0,1,8,{mL},{mR},{nmargin},1\n"
    "Style: Chapter,{font},{csize},&H00502A55,&H000000FF,&H00FFFFFF,&H30000000,"
    "1,0,0,0,100,100,0,0,1,0,1,8,{mL},{mR},{cmargin},1\n"
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
) -> str:
    """An ASS document: the novel title for the whole video + one event per chapter.

    Chapter events are timed by the cumulative sum of `MergeSegment.seconds` — the same
    offset math as merge's chapter markers, so titles change exactly at the audio
    boundaries. Each chapter title fades in/out (`\\fad`) so it changes smoothly; the
    novel title stays solid the whole video. Both titles form the player's "now playing"
    block in the right column (`PlayerLayout` fixes the placement): the novel title is the
    muted album line above, the chapter title the bold track line below it.
    """
    lay = PlayerLayout.of(width, height)
    total = sum(s.seconds for s in segments)
    out = [
        _ASS_HEADER.format(w=width, h=height),
        _ASS_STYLES.format(
            font=font_name, nsize=lay.novel_font_px, csize=lay.chapter_font_px,
            mL=lay.text_margin_l, mR=lay.text_margin_r,
            nmargin=lay.novel_margin_v, cmargin=lay.chapter_margin_v,
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
                 total_seconds: float) -> str:
    """Overlay the animated bits onto the pre-baked player artwork.

    Inputs: 0 = the static skin (`build_player_skin`: gradient, framed photo, empty
    progress track), 1 = the audio, 2 = the vinyl disc, 3 = the playhead knob. Here we add
    the four moving parts:
      * the vinyl (input 2) `rotate`d by an angle that grows with time, so it spins in
        place (`ow=iw:oh=ih` keeps the frame; `fillcolor=none` keeps the corners clear);
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
    return (
        f"[1:a]showfreqs=s={lay.bars_w}x{lay.bars_h}:mode=bar:ascale=sqrt:fscale=log:"
        f"win_size=2048:colors=0x8a52c8[viz];"
        f"[2:v]format=rgba,rotate=a='2*PI*t/{_VINYL_SPIN_SECONDS}':fillcolor=none:"
        f"ow=iw:oh=ih[vin];"
        f"[0:v][vin]overlay={lay.vinyl_x}:{lay.vinyl_y}[s1];"
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
            build_ass_subtitles(segments, novel_title, width=width, height=height),
            encoding="utf-8",
        )
        # Bake the three artwork layers once; ffmpeg loops each and animates them: the
        # static skin (backdrop), the vinyl disc (spun), and the playhead knob (slid).
        lay = PlayerLayout.of(width, height)
        build_player_skin(image_path, skin_file, width=width, height=height)
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
             "-filter_complex", _filtergraph(width, height, subs_file, font_dir, total),
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

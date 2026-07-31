"""Subtitle cues timed from the TTS run itself, and the `.srt` a part video ships with.

`synthesize_chapter` splits a chapter into chunks and concatenates them with a fixed gap,
so every chunk's exact start and end is known while the audio is being built — and was
being thrown away. Capturing it gives sentence-level subtitles with **no speech
recognition**, and with authoritative text rather than an ASR guess at Vietnamese TTS.

Two things would silently ruin sync, and both are handled here rather than left to callers
to remember:

  * `AudioWorker._apply_speed` rescales the rendered audio with `apply_tempo` and reports
    `seconds / speed`. Cues are captured in *pre-speed* time, so they need the identical
    division — see `scale_cues`.
  * A part is several chapters concatenated by `_concat_audio`, which inserts no gaps
    between them. A chapter's offset inside the part is therefore the cumulative sum of the
    preceding chapters' durations — the same arithmetic `build_ass_subtitles` uses for the
    on-screen chapter titles, so the two agree with each other by construction.

Cues live in `<audio stem>.cues.json`, beside the chapter's audio rather than the part's
video: re-voicing one chapter must invalidate exactly its own cues, and parts are re-cut
freely whenever the batch size changes.

Pure apart from the two file helpers, so the timing arithmetic unit-tests without an
engine, ffmpeg or a project.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from noveltrans.tts.clean import clean_for_tts

_CUES_EXT = ".cues.json"
_CUES_VERSION = 1


@dataclass(frozen=True)
class Cue:
    """One spoken chunk: when it starts, when it ends, and what was said."""

    start: float
    end: float
    text: str


def format_srt_time(seconds: float) -> str:
    """`HH:MM:SS,mmm` — SRT's comma decimal separator, not WebVTT's dot.

    Negatives clamp to zero: a rounding error or a bad scale factor should produce a cue
    at the start of the video, not a timestamp SRT parsers reject outright.
    """
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(cues: Iterable[Cue]) -> str:
    """An SRT document: numbered blocks from 1, separated by blank lines.

    Empty-text cues are dropped rather than emitted as blank subtitles — a chunk can clean
    down to nothing (see feature 038's punctuation-only lines), and a numbered block with
    no text makes some players flash an empty caption box.
    """
    blocks = []
    index = 1
    for cue in cues:
        text = (cue.text or "").strip()
        if not text:
            continue
        blocks.append(
            f"{index}\n"
            f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}\n"
            f"{text}\n"
        )
        index += 1
    return "\n".join(blocks)


def scale_cues(cues: Iterable[Cue], factor: float) -> list[Cue]:
    """Multiply every cue's timing by `factor`.

    The `_apply_speed` correction. `apply_tempo` at speed 1.25 makes the audio 1/1.25 as
    long, so the caller passes `1 / speed`. Without this every subtitle drifts further out
    of sync the longer the chapter runs — the failure this whole module is most exposed to.
    """
    factor = float(factor)
    return [Cue(c.start * factor, c.end * factor, c.text) for c in cues]


def shift_cues(cues: Iterable[Cue], offset: float) -> list[Cue]:
    """Move every cue later by `offset` seconds — a chapter's position inside a part."""
    return [Cue(c.start + offset, c.end + offset, c.text) for c in cues]


def cues_path(audio_path: Path | str) -> Path:
    """`<stem>.cues.json`, beside the chapter's audio file."""
    audio_path = Path(audio_path)
    return audio_path.parent / (audio_path.stem + _CUES_EXT)


def write_cues(audio_path: Path | str, cues: Iterable[Cue], *, seconds: float) -> Path:
    """Persist a chapter's cues. `seconds` is the audio's final duration.

    The duration is stored alongside so a reader can tell "these cues describe this audio"
    from "this audio was re-rendered and the cues are stale" — the check that stops a
    re-voiced chapter from shipping subtitles for the previous take.
    """
    path = cues_path(audio_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _CUES_VERSION,
        "seconds": round(float(seconds), 3),
        "cues": [[round(c.start, 3), round(c.end, 3), c.text] for c in cues],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def read_cues(audio_path: Path | str) -> tuple[list[Cue], float]:
    """`(cues, seconds)` for a chapter, or `([], 0.0)` when there are none.

    Never raises. A missing file is the normal case for any chapter voiced before this
    feature existed, and a corrupt one must not fail a video render — the worst outcome of
    reading it as absent is a part without subtitles, which is exactly where we started.
    """
    path = cues_path(audio_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], 0.0
    if not isinstance(data, dict):
        return [], 0.0
    out: list[Cue] = []
    for row in data.get("cues") or []:
        try:
            start, end, text = row[0], row[1], row[2]
            out.append(Cue(float(start), float(end), str(text)))
        except (TypeError, ValueError, IndexError):
            continue
    try:
        seconds = float(data.get("seconds") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    return out, seconds


def part_cues(segments) -> tuple[list[Cue], int, int]:
    """`(cues in part time, chapters with cues, chapters total)` for one part's segments.

    The single definition of "where does this chapter start inside the part", shared by the
    `.srt` sidecar and the burned-in subtitles (feature 041). Two independently-derived
    timings for the same words would be free to drift apart, and a burned line disagreeing
    with the sidecar is near-impossible to spot and miserable to debug.

    A chapter without cues contributes nothing but **still advances the offset**, so the
    chapters after it stay in sync — that is what makes a partially-covered part useful
    rather than misleading.

    Offsets come from each segment's `seconds`. `render_video` runs `_with_real_durations`
    before calling this, so those are ffprobe's real file durations, not stored estimates.
    """
    segments = list(segments)
    all_cues: list[Cue] = []
    covered = 0
    offset = 0.0
    for segment in segments:
        cues, _seconds = read_cues(segment.path)
        if cues:
            covered += 1
            all_cues.extend(shift_cues(cues, offset))
        offset += float(getattr(segment, "seconds", 0.0) or 0.0)
    return all_cues, covered, len(segments)


def part_srt(segments) -> tuple[str, int, int]:
    """`(srt text, chapters with cues, chapters total)` — the sidecar written beside the mp4."""
    cues, covered, total = part_cues(segments)
    return build_srt(cues), covered, total


# -- backfilling cues for audio voiced before the capture existed (042) -------
#
# The pipeline writes exactly `gap_seconds` of TRUE DIGITAL SILENCE between chunks
# (`np.zeros`), so the boundaries are physically present in every file already on disk.
# That makes this a *recovery*, not an estimate: the chunk list is re-derivable exactly
# (pure functions over the same text and settings), and the gaps say where each one lands.
#
# The alternative — distributing a chapter's duration across chunks in proportion to their
# length — drifts by tens of seconds mid-chapter, because speech rate is not uniform. It is
# not offered.

_SILENCE_RE = re.compile(
    r"silence_(start|end):\s*(-?[\d.]+)", re.IGNORECASE
)
# Thresholds tried in order, quietest first. The inter-chunk gaps are `np.zeros` — true
# digital silence — while a natural pause in speech is quiet but NOT zero, so the
# threshold is what separates them. Measured against a chapter whose real cues were known:
#   -50 dB -> 98 silences (catches quiet speech)      -70 dB -> 76  EXACT
#   -60 dB -> 77 silences                              -80 dB -> 76  EXACT
# The ladder exists because a lossy format re-encodes digital silence as near-silence, so
# the right threshold depends on the file, not on a constant we can pick in advance.
_SILENCE_THRESHOLDS_DB = (-90, -80, -70, -60, -50)
def detect_silences(
    audio_path: Path | str, *, min_seconds: float, noise_db: int = -80
) -> list[tuple[float, float]]:
    """`(start, end)` of every silence at least `min_seconds` long. `[]` if ffmpeg can't.

    One audio-only ffmpeg pass — fast even on a 20-minute chapter, and it reads the file
    that already exists rather than regenerating anything.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(audio_path), "-af",
             f"silencedetect=noise={noise_db}dB:d={max(0.05, min_seconds):.3f}",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    starts: list[float] = []
    ends: list[float] = []
    for kind, value in _SILENCE_RE.findall(result.stderr or ""):
        try:
            seconds = float(value)
        except ValueError:
            continue
        (starts if kind.lower() == "start" else ends).append(seconds)
    return list(zip(starts, ends))


def find_chunk_gaps(
    audio_path: Path | str, *, want: int, gap: float
) -> list[tuple[float, float]]:
    """The `want` inter-chunk gaps in this file, or `[]` if they can't be identified.

    Tries progressively louder thresholds and accepts the FIRST that yields exactly `want`
    silences. The expected count is known from the text, so it is used as a calibration
    signal rather than only as a safety check — which is what makes this work across
    formats and speeds instead of depending on a constant chosen in advance.

    Filtering by silence *length* was tried and abandoned: `silencedetect` reports a region
    slightly inside the gap (speech trails off around it), so a real 0.348 s gap is never
    reported as 0.348 s and a length filter rejects every one of them.
    """
    for noise_db in _SILENCE_THRESHOLDS_DB:
        found = detect_silences(
            audio_path, min_seconds=max(0.05, gap * 0.5), noise_db=noise_db
        )
        if len(found) == want:
            return found
    return []


def expected_gap(gap_seconds: float, speed: float) -> float:
    """The gap length as it exists in a FINISHED file.

    `apply_tempo` rescales the whole chapter, silence included, so a 0.4 s gap written at
    speed 1.15 is 0.348 s on disk. Detecting with the unscaled value would miss every gap
    in a sped-up file — the setting most likely to be non-default.
    """
    speed = float(speed) or 1.0
    return float(gap_seconds) / speed


def chunks_for_text(title: str, body: str, *, clean: bool, extra_remove: str,
                    max_chars: int, min_chars: int) -> list[str]:
    """Re-derive the chunks a chapter was synthesised from.

    Deterministic: the same pure functions `synthesize_chapter` runs, in the same order.
    If the user has since changed a cleaning or chunking setting the list will differ from
    what was actually spoken — which is precisely why `backfill_cues` refuses to guess when
    the count doesn't match the audio.
    """
    from noveltrans.tts.base import merge_short_chunks, split_sentences

    text = f"{title}\n\n{body}" if title else body
    if clean:
        text = clean_for_tts(text, extra_remove)
    return merge_short_chunks(split_sentences(text, max_chars), min_chars, max_chars)


def backfill_cues(
    audio_path: Path | str,
    title: str,
    body: str,
    *,
    duration: float,
    gap_seconds: float,
    speed: float = 1.0,
    clean: bool = True,
    extra_remove: str = "",
    max_chars: int = 400,
    min_chars: int = 30,
) -> list[Cue] | None:
    """Recover a chapter's cues from its existing audio, or `None` if it can't be trusted.

    Returns `None` — never a best guess — unless the number of detected gaps is exactly
    one fewer than the number of chunks. That equality is the whole safety argument: it
    means the silence pattern in the file matches the text we think produced it. Anything
    else (settings changed since, a pause inside a chunk that reads as silence, a chapter
    edited after voicing) breaks the equality, and skipping one chapter is far better than
    shipping subtitles that are confidently wrong.
    """
    chunks = chunks_for_text(
        title, body, clean=clean, extra_remove=extra_remove,
        max_chars=max_chars, min_chars=min_chars,
    )
    if not chunks or duration <= 0:
        return None
    if len(chunks) == 1:
        return [Cue(0.0, float(duration), chunks[0])]

    gap = expected_gap(gap_seconds, speed)
    gaps = find_chunk_gaps(audio_path, want=len(chunks) - 1, gap=gap)
    if len(gaps) != len(chunks) - 1:
        return None

    cues: list[Cue] = []
    start = 0.0
    for index, chunk in enumerate(chunks):
        end = gaps[index][0] if index < len(gaps) else float(duration)
        cues.append(Cue(start, max(start, end), chunk))
        if index < len(gaps):
            start = gaps[index][1]
    return cues

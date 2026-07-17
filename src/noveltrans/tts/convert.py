"""Audio format conversion via ffmpeg (optional — WAV needs nothing)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from noveltrans.errors import TtsError


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffmpeg_has_encoder(name: str) -> bool:
    """True if this ffmpeg build ships the given audio encoder (e.g. 'aac' for M4B)."""
    if not ffmpeg_available():
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines())


def convert_to_mp3(wav_path: Path, bitrate: str = "96k") -> Path:
    """Convert a WAV to MP3 next to it, delete the WAV, return the MP3 path."""
    mp3_path = wav_path.with_suffix(".mp3")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-b:a", bitrate, str(mp3_path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise TtsError("Không tìm thấy ffmpeg — cài ffmpeg hoặc chọn định dạng WAV.") from exc
    except subprocess.TimeoutExpired as exc:
        raise TtsError("ffmpeg quá 600s không xong — chương quá dài?") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-300:]
        raise TtsError(f"ffmpeg trả lỗi (mã {result.returncode}): {detail}")
    wav_path.unlink(missing_ok=True)
    return mp3_path


def _atempo_filters(tempo: float) -> list[float]:
    """Decompose `tempo` into atempo factors each within ffmpeg's [0.5, 2.0] range,
    whose product is `tempo`.

    ffmpeg's atempo caps a single filter at 0.5–2.0; larger changes chain filters.
    e.g. 2.5 -> [2.0, 1.25], 0.25 -> [0.5, 0.5], 1.5 -> [1.5], 1.0 -> [1.0]. Within the
    app's 0.5–2.0 slider this is always one factor, but the general form keeps a wider
    range safe to add later.
    """
    if tempo <= 0:
        raise ValueError(f"tempo must be positive, got {tempo}")
    factors: list[float] = []
    remaining = tempo
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return factors


def apply_tempo(wav_path: Path, tempo: float) -> Path:
    """Time-scale a WAV in place via ffmpeg atempo (pitch-preserving). Returns the path.

    `tempo == 1.0` is a no-op (no ffmpeg call). Other values run atempo into a temp file
    that then replaces the original. Duration scales by exactly 1/tempo, so callers can
    rescale a known duration without probing.
    """
    if tempo == 1.0:
        return wav_path
    chain = ",".join(f"atempo={f:g}" for f in _atempo_filters(tempo))
    tmp_path = wav_path.with_name(f"{wav_path.stem}.tempo{wav_path.suffix}")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-filter:a", chain, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise TtsError("Không tìm thấy ffmpeg — cài ffmpeg hoặc đặt tốc độ về 1.0×.") from exc
    except subprocess.TimeoutExpired as exc:
        raise TtsError("ffmpeg quá 600s không xong khi đổi tốc độ — chương quá dài?") from exc
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        detail = (result.stderr or "").strip()[-300:]
        raise TtsError(f"ffmpeg đổi tốc độ lỗi (mã {result.returncode}): {detail}")
    tmp_path.replace(wav_path)
    return wav_path

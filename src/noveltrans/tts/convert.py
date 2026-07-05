"""Audio format conversion via ffmpeg (optional — WAV needs nothing)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from noveltrans.errors import TtsError


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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

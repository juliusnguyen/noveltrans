"""Audio format conversion via ffmpeg (optional — WAV needs nothing)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from noveltrans.errors import TtsError
from noveltrans.runtime_env import no_console_kwargs


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
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **no_console_kwargs(),
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
            encoding="utf-8",
            errors="replace",
            timeout=600,
            **no_console_kwargs(),
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
            encoding="utf-8",
            errors="replace",
            timeout=600,
            **no_console_kwargs(),
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


def probe_duration(path: Path | str) -> float:
    """Real duration (seconds) of an audio file via ffprobe. 0.0 if it can't be read.

    Lives here rather than in `video.py` because the audio downloader needs it too and
    must not import the video stack. `video._probe_duration` is an alias onto this.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nokey=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            **no_console_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


class DownloadCancelled(Exception):
    """Raised when a media download is cancelled mid-transfer (not a failure).

    The partial `.part` file is deliberately left on disk so the next run resumes it.
    """


# Big enough that a 200 MB file is not 200k iterations, small enough that `cancelled`
# is still polled several times a second on a slow line.
_DL_CHUNK = 1 << 18  # 256 KiB


def download_media(
    url: str,
    dest: Path,
    *,
    cookies: str = "",
    headers: dict | None = None,
    cancelled=None,
    on_progress=None,
) -> Path:
    """Stream `url` to `dest`, resuming a previous partial transfer when possible.

    Written for the sizes this actually sees: the reference novel's 21 audio files total
    1.7 GB, the largest single file is 203 MB. So the body is streamed to a `.part`
    sibling in chunks and renamed into place only once complete — never buffered in RAM,
    and never left looking finished when it is not. A truncated file that *looks* like
    finished audio is the worst outcome here, because feature 059's guards then protect
    it from being replaced.

    Resume uses a `Range` request when a `.part` is already present. Servers that ignore
    `Range` answer 200 instead of 206; that is detected and the file restarted rather
    than appended to, which would corrupt it.

    `on_progress(done_bytes, total_bytes)` is called as chunks land (`total` is 0 when the
    server does not say). `cancelled()` is polled between chunks and raises
    `DownloadCancelled`. Raises `TtsError` on any HTTP or disk failure.
    """
    import requests

    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if url.lower().split("?")[0].endswith(".m3u8"):
        # No HLS has ever been observed from the one site this serves; a clear refusal
        # beats an ffmpeg remux path that nothing exercises and no test can pin.
        raise TtsError("Audio dạng HLS (.m3u8) chưa được hỗ trợ tải về.")

    resume_from = part.stat().st_size if part.exists() else 0
    request_headers = {
        "Accept": "*/*",
        # The media host serves these anonymously; the cookie is sent only because a
        # future gate would need it, and it costs nothing today.
        **({"Cookie": cookies} if cookies else {}),
        **(headers or {}),
    }
    if resume_from:
        request_headers["Range"] = f"bytes={resume_from}-"

    try:
        response = requests.get(url, headers=request_headers, stream=True, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise TtsError(f"Không tải được audio: {exc}") from exc

    # 206 honours the resume; a 200 to a Range request means the server sent the whole
    # file, so anything already on disk has to go or the two halves would be concatenated.
    append = resume_from > 0 and response.status_code == 206
    if resume_from and not append:
        resume_from = 0

    total = 0
    length = response.headers.get("content-length")
    if length and length.isdigit():
        total = int(length) + resume_from

    done = resume_from
    try:
        with open(part, "ab" if append else "wb") as handle:
            for chunk in response.iter_content(chunk_size=_DL_CHUNK):
                if cancelled is not None and cancelled():
                    handle.flush()
                    raise DownloadCancelled()
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total)
    except DownloadCancelled:
        raise
    except requests.RequestException as exc:
        # MUST precede OSError: `requests.RequestException` subclasses `IOError`, so an
        # `except OSError` above this line silently swallows every network failure and
        # reports a dropped connection as a disk error. Keep the .part either way — the
        # next run resumes from where this one stopped.
        raise TtsError(f"Mất kết nối khi tải audio: {exc}") from exc
    except OSError as exc:
        raise TtsError(f"Không ghi được file audio: {exc}") from exc
    finally:
        response.close()

    if total and done != total:
        raise TtsError(f"Tải thiếu dữ liệu ({done}/{total} byte) — thử lại để tải tiếp.")
    if done == 0:
        part.unlink(missing_ok=True)
        raise TtsError("Máy chủ trả về file audio rỗng.")

    part.replace(dest)
    return dest

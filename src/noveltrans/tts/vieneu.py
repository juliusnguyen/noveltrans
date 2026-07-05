"""VieNeu-TTS engine — local Vietnamese TTS, torch-free ONNX runtime.

https://github.com/pnnbao97/VieNeu-TTS — `pip install vieneu`. The ~334 MB
model auto-downloads from HuggingFace on first load.
"""

from __future__ import annotations

from pathlib import Path

from noveltrans.errors import TtsError
from noveltrans.tts.base import TtsEngine

# (label, voice_id) pairs, shown before the model is loaded — verified against
# vieneu 3.0.11's list_preset_voices(); the model may add more after load()
PRESET_VOICES = [
    ("Ngọc Lan — nữ, giọng dịu dàng", "Ngọc Lan"),
    ("Mỹ Duyên — nữ, giọng mượt mà", "Mỹ Duyên"),
    ("Trúc Ly — nữ, giọng trẻ trung", "Trúc Ly"),
    ("Ngọc Linh — nữ, giọng tươi sáng", "Ngọc Linh"),
    ("Gia Bảo — nam, giọng mượt mà", "Gia Bảo"),
    ("Thái Sơn — nam, giọng chắc khỏe", "Thái Sơn"),
    ("Đức Trí — nam, giọng rõ ràng", "Đức Trí"),
    ("Xuân Vĩnh — nam, giọng vui tươi", "Xuân Vĩnh"),
    ("Trọng Hữu — nam, giọng uyên bác", "Trọng Hữu"),
    ("Bình An — nam, giọng điềm đạm", "Bình An"),
]

INSTALL_HINT = (
    "Chưa cài VieNeu-TTS — chạy: uv pip install --python .venv/bin/python vieneu "
    "(hoặc pip install 'noveltrans[tts]')."
)


class VieneuEngine(TtsEngine):
    name = "vieneu"
    display_name = "VieNeu-TTS (local)"
    sample_rate = 48000

    def __init__(self, voice: str = ""):
        self.voice = (voice or "").strip()
        self._tts = None

    def load(self) -> None:
        try:
            from vieneu import Vieneu
        except ImportError as exc:
            raise TtsError(INSTALL_HINT) from exc
        try:
            self._tts = Vieneu()
        except Exception as exc:
            raise TtsError(f"Không khởi tạo được VieNeu-TTS: {exc}") from exc

    def _require_loaded(self):
        if self._tts is None:
            raise TtsError("Engine chưa load() — lỗi lập trình.")
        return self._tts

    def list_voices(self) -> list[tuple[str, str]]:
        if self._tts is None:
            return list(PRESET_VOICES)
        try:
            presets = self._tts.list_preset_voices()  # (label, voice_id) pairs
            return [
                (p[0], p[1]) if isinstance(p, (tuple, list)) else (str(p), str(p))
                for p in presets
            ]
        except Exception:
            return list(PRESET_VOICES)

    def synthesize(self, text: str):
        tts = self._require_loaded()
        try:
            if self.voice:
                return tts.infer(text, voice=self.voice)
            return tts.infer(text)
        except Exception as exc:
            raise TtsError(f"VieNeu-TTS lỗi khi đọc đoạn văn: {exc}") from exc

    def save_wav(self, samples, out_path: Path) -> None:
        tts = self._require_loaded()
        try:
            tts.save(samples, str(out_path))
        except Exception as exc:
            raise TtsError(f"Không ghi được file audio {out_path.name}: {exc}") from exc

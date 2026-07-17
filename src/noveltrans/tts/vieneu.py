"""VieNeu-TTS engine — local Vietnamese TTS, torch-free ONNX runtime.

https://github.com/pnnbao97/VieNeu-TTS — `pip install vieneu`. The ~334 MB
model auto-downloads from HuggingFace on first load.
"""

from __future__ import annotations

import logging
from pathlib import Path

from noveltrans.errors import TtsError
from noveltrans.tts.base import TtsEngine

logger = logging.getLogger(__name__)

# (label, voice_id) pairs, shown before the model is loaded. These mirror the
# v3turbo build's list_preset_voices() output (label = "{name} — {description}").
# It's a best-effort snapshot: load() reconciles self.voice against the real list
# the model reports, so a drift here can no longer crash synthesis.
PRESET_VOICES = [
    ("Minh Đức — Nam · Bắc · Phong cách tin tức", "Minh Đức"),
    ("Phạm Tuyên — Nam · Bắc · Phong cách tự nhiên", "Phạm Tuyên"),
    ("Thái Sơn — Nam · Nam · Phong cách kể chuyện", "Thái Sơn"),
    ("Xuân Vĩnh — Nam · Nam · Phong cách tự nhiên", "Xuân Vĩnh"),
    ("Thanh Bình — Nam · Bắc · Phong cách kể chuyện", "Thanh Bình"),
    ("Trúc Ly — Nữ · Bắc · Phong cách tự nhiên", "Trúc Ly"),
    ("Ngọc Linh — Nữ · Bắc · Phong cách kể chuyện", "Ngọc Linh"),
    ("Đoan Trang — Nữ · Bắc · Phong cách tự nhiên", "Đoan Trang"),
    ("Mai Anh — Nữ · Bắc · Phong cách tin tức", "Mai Anh"),
    ("Thục Đoan — Nữ · Nam · Phong cách kể chuyện", "Thục Đoan"),
    ("Minh Triết — Nam · Nam · Phong cách tin tức", "Minh Triết"),
    ("Thùy Dung — Nữ · Nam · Phong cách tin tức", "Thùy Dung"),
    ("Quang Sơn — Nam · Trung · Phong cách tự nhiên", "Quang Sơn"),
    ("Ngọc Trân — Nữ · Trung · Phong cách tự nhiên", "Ngọc Trân"),
]

INSTALL_HINT = (
    "Chưa cài VieNeu-TTS — chạy: uv pip install --python .venv/bin/python vieneu "
    "(hoặc pip install 'noveltrans[tts]')."
)


class VieneuEngine(TtsEngine):
    name = "vieneu"
    display_name = "VieNeu-TTS (local)"
    sample_rate = 48000

    def __init__(
        self, voice: str = "", temperature: float | None = None, precision: str = "int8"
    ):
        self.voice = (voice or "").strip()
        # None = pass nothing to infer() → the model's own default. Keeps byte-for-byte
        # parity with pre-018 behaviour, which never passed a temperature.
        self.temperature = temperature
        # ONNX/CPU graph: "int8" (fast, default) or "fp32" (higher quality, slower and a
        # larger one-time download). Set at model construction, not per-chunk.
        self.precision = precision
        # Set by load() when the requested voice was substituted; a human-readable
        # notice the caller can surface (empty means the voice was used as-is).
        self.voice_notice = ""
        self._tts = None

    def load(self) -> None:
        try:
            from vieneu import Vieneu
        except ImportError as exc:
            raise TtsError(INSTALL_HINT) from exc
        try:
            self._tts = Vieneu(precision=self.precision)
        except Exception as exc:
            raise TtsError(f"Không khởi tạo được VieNeu-TTS: {exc}") from exc
        self._resolve_voice()

    def _resolve_voice(self) -> None:
        """Pin self.voice to a voice the loaded model actually offers.

        Voice names drift between vieneu builds; passing a stale name to infer()
        raises. Resolve once here (not per-chunk) so synthesize() can never fail
        on an unknown voice: fall back to the model's default, else the first
        available voice, else "" (which lets the model pick its own default).
        """
        if not self.voice:
            return
        try:
            available = [vid for _, vid in self.list_voices()]
        except Exception:
            return  # can't determine — leave self.voice untouched, degrade safely
        if not available or self.voice in available:
            return
        fallback = getattr(self._tts, "_default_voice", None)
        if fallback not in available:
            fallback = available[0]
        self.voice_notice = (
            f"Giọng '{self.voice}' không còn khả dụng — dùng '{fallback}' thay thế."
        )
        logger.warning(self.voice_notice)
        self.voice = fallback

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
        # Only include kwargs that are set, so an unset temperature passes nothing and
        # the model uses its own default (exact pre-018 behaviour).
        kwargs = {}
        if self.voice:
            kwargs["voice"] = self.voice
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        try:
            return tts.infer(text, **kwargs)
        except Exception as exc:
            raise TtsError(f"VieNeu-TTS lỗi khi đọc đoạn văn: {exc}") from exc

    def save_wav(self, samples, out_path: Path) -> None:
        tts = self._require_loaded()
        try:
            tts.save(samples, str(out_path))
        except Exception as exc:
            raise TtsError(f"Không ghi được file audio {out_path.name}: {exc}") from exc

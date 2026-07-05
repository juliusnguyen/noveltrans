"""TTS engine registry."""

from __future__ import annotations

from noveltrans.errors import TtsError
from noveltrans.tts.base import TtsEngine

TTS_ENGINE_NAMES = {"vieneu": "VieNeu-TTS (local)"}


def get_tts_engine(name: str, *, voice: str = "") -> TtsEngine:
    """Build a TTS engine by name. Imports lazily — the heavy TTS dependency
    is optional and only needed when audio generation is actually used."""
    if name == "vieneu":
        from noveltrans.tts.vieneu import VieneuEngine

        return VieneuEngine(voice=voice)
    raise TtsError(f"Unknown TTS engine: {name!r}")

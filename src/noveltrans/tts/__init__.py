"""TTS engine registry."""

from __future__ import annotations

from noveltrans.errors import TtsError
from noveltrans.tts.base import TtsEngine

TTS_ENGINE_NAMES = {"vieneu": "VieNeu-TTS (local)"}


def get_tts_engine(
    name: str,
    *,
    voice: str = "",
    temperature: float | None = None,
    precision: str = "int8",
    style: str = "",
) -> TtsEngine:
    """Build a TTS engine by name. Imports lazily — the heavy TTS dependency
    is optional and only needed when audio generation is actually used.

    `temperature` (None = the model's own default) sets VieNeu's expressiveness;
    `precision` ("int8" = fast, "fp32" = higher quality) selects its ONNX/CPU graph;
    `style` ("" = the model default) sets the reading style independent of voice.
    """
    if name == "vieneu":
        from noveltrans.tts.vieneu import VieneuEngine

        return VieneuEngine(
            voice=voice, temperature=temperature, precision=precision, style=style
        )
    raise TtsError(f"Unknown TTS engine: {name!r}")

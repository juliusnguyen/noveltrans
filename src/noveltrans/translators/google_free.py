"""Free Google Translate engine via deep-translator (no API key)."""

from __future__ import annotations

import time

from deep_translator import GoogleTranslator

from noveltrans.translators.base import Translator

# NovelTrans language codes -> Google codes
_GOOGLE_LANGS = {"zh": "zh-CN", "vi": "vi", "en": "en"}


class GoogleFreeTranslator(Translator):
    name = "google"
    display_name = "Google Translate (miễn phí)"
    # The free endpoint sends text in the URL; CJK chars URL-encode to 9 bytes
    # each, so requests start failing around ~2000 Chinese characters.
    max_chunk_chars = 1500
    request_delay = 1.0  # be polite: the free endpoint rate-limits aggressively

    def __init__(self, request_delay: float = 1.0):
        self.request_delay = request_delay
        self._last_request_at = 0.0

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_at = time.monotonic()

        engine = GoogleTranslator(
            source=_GOOGLE_LANGS.get(source, source),
            target=_GOOGLE_LANGS.get(target, target),
        )
        result = engine.translate(text)
        return result or ""

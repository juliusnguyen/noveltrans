"""Claude API translation engine — best quality for literary text."""

from __future__ import annotations

import anthropic

from noveltrans.errors import TranslateError
from noveltrans.translators.base import Translator

_LANG_NAMES = {"vi": "Vietnamese", "en": "English"}

_SYSTEM_PROMPT = (
    "You are a professional literary translator of Chinese web novels. "
    "Translate the Chinese text the user sends into {language}. "
    "Keep the paragraph breaks exactly as in the source. "
    "Translate character names consistently and keep the novel's tone. "
    "{name_rule}"
    "The text may be a whole chapter or just a short fragment such as a chapter "
    "title — translate exactly what is given; NEVER ask for more text and NEVER "
    "remark that content seems missing. "
    "Translate every word — leave NO Chinese characters in the output. "
    "Output ONLY the translation — no notes, no explanations, no preamble."
)

_NAME_RULES = {
    "vi": (
        "Render ALL Chinese person and place names in Sino-Vietnamese (Hán-Việt) "
        "reading, never pinyin — e.g. 傅清詞 -> Phó Thanh Từ, 江妤 -> Giang Dư. "
    ),
    "en": "Render Chinese person names in standard pinyin without tone marks. ",
}


class ClaudeTranslator(Translator):
    name = "claude"
    display_name = "Claude API"
    # A whole chapter usually fits one request; chunk only very long ones.
    max_chunk_chars = 12000
    supports_completion = True

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        if not api_key:
            raise TranslateError(
                "Chưa có Claude API key — điền vào phần Cài đặt (Settings) trước."
            )
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        language = _LANG_NAMES.get(target, target)
        system = _SYSTEM_PROMPT.format(
            language=language, name_rule=_NAME_RULES.get(target, "")
        )
        result = self.complete(text, system=system)
        if not result:
            raise TranslateError("Claude returned an empty translation")
        return result

    def complete(self, prompt: str, *, system: str = "") -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=8192,
                system=system or "You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError as exc:
            raise TranslateError(f"Claude API key không hợp lệ: {exc}") from exc
        parts = [block.text for block in response.content if block.type == "text"]
        return "".join(parts).strip()

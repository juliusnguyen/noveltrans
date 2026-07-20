"""LM Studio translator — a local OpenAI-compatible server, no key, no cloud.

Talks to LM Studio's built-in server (Developer -> Start Server), default
http://127.0.0.1:1234. The model can be picked explicitly; otherwise the
first model the server has loaded is used.
"""

from __future__ import annotations

import re

import requests

from noveltrans.errors import TranslateError
from noveltrans.translators.base import Translator

DEFAULT_LMSTUDIO_URL = "http://127.0.0.1:1234"

_LANG_NAMES = {"vi": "Vietnamese", "en": "English"}

_NAME_RULES = {
    "vi": (
        "Render ALL Chinese person and place names in Sino-Vietnamese (Hán-Việt) "
        "reading, never pinyin — e.g. 傅清辭 -> Phó Thanh Từ, 江妤 -> Giang Dư. "
    ),
    "en": "Render Chinese person names in standard pinyin without tone marks. ",
}

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


def list_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """IDs of the models the LM Studio server has available ([] on any failure)."""
    base_url = (base_url or "").strip().rstrip("/") or DEFAULT_LMSTUDIO_URL
    try:
        response = requests.get(f"{base_url}/v1/models", timeout=timeout)
        data = response.json().get("data", [])
    except (requests.RequestException, ValueError):
        return []
    return [m.get("id", "") for m in data if m.get("id")]


class LmStudioTranslator(Translator):
    name = "lmstudio"
    display_name = "LM Studio (local)"
    # local models often run with an 8k context window; stay well under it
    max_chunk_chars = 4000
    supports_completion = True

    def __init__(self, base_url: str = DEFAULT_LMSTUDIO_URL, model: str = "",
                 timeout: float = 600.0):
        self.base_url = (base_url or "").strip().rstrip("/") or DEFAULT_LMSTUDIO_URL
        self.model = (model or "").strip()
        self.timeout = timeout

    def _resolve_model(self) -> str:
        """The configured model, or the first one the server has loaded."""
        if self.model:
            return self.model
        models = list_models(self.base_url)
        if not models:
            raise TranslateError(
                f"LM Studio tại {self.base_url} không có model nào — mở LM Studio, "
                "load một model rồi bật server (Developer → Start Server)."
            )
        self.model = models[0]
        return self.model

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        system = _SYSTEM_PROMPT.format(
            language=_LANG_NAMES.get(target, target),
            name_rule=_NAME_RULES.get(target, ""),
        )
        return self.complete(text, system=system)

    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.3) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self._resolve_model(),
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        try:
            response = requests.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TranslateError(
                f"Không kết nối được LM Studio tại {self.base_url} — kiểm tra đã bật "
                "server chưa (Developer → Start Server) và địa chỉ Reachable at."
            ) from exc
        if response.status_code != 200:
            detail = response.text.strip()[:300]
            raise TranslateError(f"LM Studio trả lỗi HTTP {response.status_code}: {detail}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TranslateError(
                f"Phản hồi LM Studio không đúng định dạng: {response.text.strip()[:200]}"
            ) from exc
        result = (content or "").strip()
        # reasoning models (Qwen3…) may prepend a think block — keep only the answer
        result = re.sub(r"^<think>.*?</think>\s*", "", result, flags=re.DOTALL).strip()
        if not result:
            raise TranslateError("LM Studio không trả về nội dung dịch.")
        return result

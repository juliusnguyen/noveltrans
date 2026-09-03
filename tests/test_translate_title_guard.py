"""Feature 076 — `Translator._safe_title`, at the ABC so every engine inherits it.

The defect was engine-agnostic even though it was reported against the Claude CLI:
`translate_chapter` sent the chapter TITLE through the prompt written for a chapter BODY,
so a bare `第127章` invited the model to ask for the missing content. These tests pin the
guard where it actually lives, using stub engines rather than a real CLI.
"""

from __future__ import annotations

import pytest

from noveltrans.errors import TranslateError
from noveltrans.translators.base import Translator

REFUSAL = (
    "Xin lỗi, bạn chưa cung cấp nội dung chương 127 để dịch. Vui lòng dán văn bản tiếng "
    "Trung cần dịch, tôi sẽ dịch sang tiếng Việt theo phong cách văn học chuyên nghiệp."
)


class ScriptedTranslator(Translator):
    """Returns the next scripted output per call and records what it was asked."""

    name = "scripted"
    retry_delay = 0.0

    def __init__(self, outputs: list[str], default: str = "Bản dịch."):
        self.seen: list[str] = []
        self._outputs = list(outputs)
        self._default = default

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        self.seen.append(text)
        return self._outputs.pop(0) if self._outputs else self._default


class BrokenTranslator(Translator):
    """An engine that is genuinely down — every call raises."""

    name = "broken"
    retry_delay = 0.0

    def __init__(self):
        self.calls = 0

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        self.calls += 1
        raise TranslateError("hết hạn mức (quota)")


class TestNumericTitleSkipsTheEngine:
    def test_a_bare_title_never_reaches_the_model(self):
        engine = ScriptedTranslator([])
        title, _ = engine.translate_chapter("第127章", "正文。")
        assert title == "Chương 127"
        assert engine.seen == ["正文。"]  # the body, and only the body

    def test_it_saves_one_engine_call_per_chapter(self):
        # The point is not only correctness: novels numbered this way paid for a request
        # that could never return anything but "Chương N".
        engine = ScriptedTranslator([])
        engine.translate_chapter("第90章", "正文。")
        assert len(engine.seen) == 1

    def test_a_title_with_real_text_still_goes_to_the_model(self):
        engine = ScriptedTranslator(["Chương 1: Trùng sinh"])
        title, _ = engine.translate_chapter("第1章 重生", "正文。")
        assert title == "Chương 1: Trùng sinh"
        assert engine.seen[0] == "第1章 重生"


class TestImplausibleReplyIsRejected:
    def test_a_refusal_is_retried_then_replaced(self):
        # A title with real text, so the local shortcut does not apply and the guard has
        # to catch the reply itself. Retried because the failure is non-deterministic.
        engine = ScriptedTranslator([REFUSAL, "Chương 268: Xin lỗi nhé máy liên lạc"])
        title, _ = engine.translate_chapter("第268章 對不起通訊器先生", "正文。")
        assert title == "Chương 268: Xin lỗi nhé máy liên lạc"

    def test_a_refusal_every_time_falls_back_to_the_source_title(self):
        engine = ScriptedTranslator([REFUSAL, REFUSAL, REFUSAL, REFUSAL])
        title, body = engine.translate_chapter("第268章 對不起通訊器先生", "正文。")
        assert title == "第268章 對不起通訊器先生"  # untranslated beats a fabricated one
        assert REFUSAL not in title
        assert body  # and the body still came through

    def test_a_numeric_source_falls_back_to_the_local_form(self):
        # Unreachable through translate_chapter (the shortcut fires first), but _safe_title
        # is the shared entry point and must not depend on that ordering.
        engine = ScriptedTranslator([REFUSAL, REFUSAL])
        assert engine._safe_title("第127章", "zh", "vi") == "Chương 127"

    def test_a_title_plus_body_reply_is_trimmed_to_its_first_line(self):
        leak = "Chương 196: Ôn Thục Nghi bùng nổ\n" + "Trước cửa thư viện, gió thổi. " * 8
        engine = ScriptedTranslator([leak, leak])
        title, _ = engine.translate_chapter("第196章 暴走的溫淑儀（求月票）", "正文。")
        assert title == "Chương 196: Ôn Thục Nghi bùng nổ"


class TestTheBodyIsNeverLostToATitle:
    def test_an_engine_error_on_the_title_does_not_end_the_chapter(self):
        # The body call right after this raises the real error, so swallowing it here
        # hides nothing — it only stops a title problem posing as a chapter failure.
        engine = BrokenTranslator()
        with pytest.raises(TranslateError, match="quota"):
            engine.translate_chapter("第1章 重生", "正文。")

    def test_a_body_that_translates_survives_a_hopeless_title(self):
        engine = ScriptedTranslator([REFUSAL, REFUSAL, REFUSAL, REFUSAL], default="Nội dung.")
        title, body = engine.translate_chapter("第268章 對不起通訊器先生", "正文。")
        assert body == "Nội dung."
        assert title == "第268章 對不起通訊器先生"

    def test_an_empty_title_is_still_left_empty(self):
        engine = ScriptedTranslator([], default="Nội dung.")
        title, body = engine.translate_chapter("", "正文。")
        assert title == ""
        assert body == "Nội dung."


class TestRefusalInABody:
    def test_a_body_refusal_is_retried_not_saved(self):
        engine = ScriptedTranslator([REFUSAL, "Nội dung thật."])
        _, body = engine.translate_chapter("", "正文。")
        assert body == "Nội dung thật."

    def test_a_body_that_only_ever_refuses_fails_the_chapter(self):
        # Loud beats silent: a refusal saved as the body would be exported into the EPUB.
        engine = ScriptedTranslator([REFUSAL] * 6, default=REFUSAL)
        with pytest.raises(TranslateError):
            engine.translate_chapter("", "正文。")

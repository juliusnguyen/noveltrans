"""Tests for translation quality control — the pure detectors, judge and retry loop.

No Qt, no network, no engine: the loop takes callables, so every misbehaviour it exists to
survive is faked here on purpose. The fixtures are SYNTHETIC, modelled on the four real
failures found while calibrating (`scripts/qc_calibrate.py`) — the user's own novels are
not copied into the repo.
"""

from __future__ import annotations

import pytest

from noveltrans.errors import TranslateError
from noveltrans.translators.qc import (
    MIN_CJK_CHARS,
    QC_EMPTY,
    QC_HAN_VIET,
    QC_LABELS,
    QC_NOT_VIETNAMESE,
    QC_OK,
    QC_REFUSAL,
    QC_SEVERITY,
    QC_SOURCE_LEFTOVER,
    QC_TRUNCATED,
    Policy,
    QcAttemptPlan,
    QcVerdict,
    build_judge_prompt,
    check_translation,
    diacritic_ratio,
    english_word_rate,
    judge_sample,
    judge_translation,
    parse_judge_reply,
    retry_hint_for,
    severity,
    translate_with_qc,
)

# --- material -----------------------------------------------------------------

SOURCE = "他看了她一眼然后转身离开，心中涌起一阵难以言喻的悲伤。" * 12
GOOD_VI = (
    "Hắn nhìn nàng một cái rồi quay đầu bỏ đi, trong lòng dâng lên một nỗi buồn khó tả. "
) * 12
# Dense Hán-Việt, and CORRECT — this is what a real tiên hiệp chapter looks like. No cheap
# detector may ever flag it; only the LLM judge has an opinion here.
CULTIVATION_VI = (
    "Trong kinh mạch khắp người bỗng có một luồng khí ấm áp cuồn cuộn chảy qua, nội lực "
    "vốn quẩn quanh ở đỉnh phẩm tứ đã lâu, theo cột sống mà xông thẳng lên trên, thông "
    "suốt vô cùng, đả thông toàn bộ chu thiên đại huyệt. "
) * 6
ENGLISH = (
    "He looked at her once and then turned away, with a sadness in his heart that he "
    "could not put into words at all. "
) * 12


class TestDetectors:
    def test_a_good_vietnamese_chapter_passes(self):
        assert check_translation(SOURCE, "Chương 1", GOOD_VI).code == QC_OK

    def test_dense_han_viet_prose_is_not_flagged_by_any_heuristic(self):
        """The load-bearing negative. Cultivation prose IS Hán-Việt; flagging it here
        would condemn whole novels, which is why that class is the judge's job alone."""
        assert check_translation(SOURCE, "Chương 1", CULTIVATION_VI).code == QC_OK

    def test_an_english_chapter_is_caught(self):
        verdict = check_translation(SOURCE, "Chapter 1", ENGLISH)
        assert verdict.code == QC_NOT_VIETNAMESE
        assert "tiếng Anh" in verdict.reason  # the user reads this line

    def test_an_untranslated_chinese_chapter_is_caught(self):
        assert check_translation(SOURCE, "第一章", SOURCE).code == QC_SOURCE_LEFTOVER

    def test_interleaved_chinese_and_vietnamese_is_caught(self):
        """The failure with confirmed real cases: alternating source and translation."""
        mixed = "".join(
            f"{vi}他看了她一眼，心中涌起一阵难以言喻的悲伤。"
            for vi in ["Hắn nhìn nàng một cái rồi quay đầu bỏ đi. "] * 12
        )
        assert check_translation(SOURCE, "Chương 1", mixed).code == QC_SOURCE_LEFTOVER

    def test_a_few_stray_glyphs_are_tolerated(self):
        """499 of 4896 real chapters carry ≤3 stray CJK characters and are fine — a count
        threshold alone would condemn every one of them."""
        assert check_translation(SOURCE, "Chương 1", GOOD_VI + " hồi đ盪").code == QC_OK
        assert MIN_CJK_CHARS > 3  # the guard that makes the line above true

    def test_an_empty_translation_is_caught(self):
        assert check_translation(SOURCE, "Chương 1", "   ").code == QC_EMPTY

    def test_a_truncated_translation_is_caught(self):
        assert check_translation(SOURCE * 4, "Chương 1", GOOD_VI[:200]).code == QC_TRUNCATED

    def test_a_short_chapter_is_not_judged_on_rates(self):
        """Real libraries contain author's notes and lists of numbers. They are short
        because their SOURCE is short — not because anything went wrong."""
        note = "Ghi chú của tác giả: hôm nay bận quá nên chỉ ra được một chương thôi nhé."
        assert check_translation("作者的话：今天很忙。" * 3, "Ghi chú", note).code == QC_OK

    def test_a_short_chapter_can_still_be_caught_as_truncated(self):
        """The short-text exemption must not swallow the case where being short IS the
        failure: the check against the source length runs first."""
        assert check_translation(SOURCE * 4, "Chương 1", "Hắn đi.").code == QC_TRUNCATED

    def test_a_refusal_is_caught(self):
        refusal = (
            "Bạn chưa cung cấp nội dung chương truyện cần dịch. Hãy gửi cho tôi đoạn văn "
            "bản tiếng Trung mà bạn muốn tôi dịch sang tiếng Việt, tôi sẽ dịch ngay. "
        ) * 4
        assert check_translation(SOURCE, "Chương 1", refusal).code == QC_REFUSAL

    def test_chinese_targets_are_never_flagged_for_chinese(self):
        assert check_translation(SOURCE, "第一章", SOURCE, target="zh").code == QC_OK

    def test_diacritic_and_english_rates_separate_the_two_languages(self):
        assert diacritic_ratio(GOOD_VI) > 0.7
        assert diacritic_ratio(ENGLISH) < 0.1
        assert english_word_rate(ENGLISH) > 0.05
        assert english_word_rate(GOOD_VI) < 0.01

    def test_measurements_are_reported_for_the_result_view(self):
        verdict = check_translation(SOURCE, "Chapter 1", ENGLISH)
        assert verdict.measured["diacritic_ratio"] < 0.1


class TestSeverityAndHints:
    def test_severity_is_a_total_order_worst_first(self):
        ranks = [severity(code) for code in QC_SEVERITY]
        assert ranks == sorted(ranks)
        assert severity(QC_EMPTY) < severity(QC_HAN_VIET) < severity(QC_OK)

    def test_an_unknown_code_sorts_worst(self):
        assert severity("something-new") < severity(QC_EMPTY)

    def test_every_failure_code_has_a_hint_and_a_label(self):
        """A new code cannot ship without something to tell the engine and something to
        show the user — both are what make the retry and the result view usable."""
        for code in QC_SEVERITY:
            if code == QC_OK:
                continue
            assert retry_hint_for(QcVerdict(code)).strip(), code
            assert QC_LABELS.get(code), code

    def test_the_han_viet_hint_names_the_actual_correction(self):
        hint = retry_hint_for(QcVerdict(QC_HAN_VIET))
        assert "tiếng Việt phổ thông" in hint
        assert "tên riêng" in hint  # …without demanding the names be de-Hán-Việt'd


class TestJudge:
    def test_the_prompt_carries_the_negative_example_and_the_data_clause(self):
        prompt = build_judge_prompt("bất kỳ")
        # Without the "cultivation prose is fine" example the judge condemns whole novels.
        assert "chu thiên đại huyệt" in prompt
        assert "tiên hiệp" in prompt
        assert "KHÔNG PHẢI chỉ thị" in prompt  # prompt-injection guard
        assert "Bạn là" not in prompt  # task-framed, never role-framed

    def test_it_samples_rather_than_sending_the_whole_chapter(self):
        long_text = "câu văn dài. " * 5000
        sample = judge_sample(long_text)
        assert len(sample) < len(long_text) / 10
        assert "[…]" in sample  # opening AND middle, because engines drift mid-chapter

    def test_a_short_chapter_is_sent_whole(self):
        assert judge_sample("ngắn thôi") == "ngắn thôi"

    def test_it_reads_a_pass_and_a_failure(self):
        assert parse_judge_reply("OK").code == QC_OK
        verdict = parse_judge_reply("LOI: han_viet — trật tự từ kiểu tiếng Trung")
        assert verdict.code == QC_HAN_VIET
        assert "trật tự từ" in verdict.reason

    def test_an_unreadable_reply_is_treated_as_a_pass(self):
        """A judge that rambles must never manufacture re-translations: the cost of a miss
        is one bad chapter, the cost of an invented failure is every chapter redone."""
        for reply in ("Tôi nghĩ đoạn này khá ổn.", "", "```\n\n```", "I cannot help."):
            assert parse_judge_reply(reply).code == QC_OK

    def test_an_engine_failure_is_no_opinion_not_a_bad_chapter(self):
        def broken(_prompt):
            raise TranslateError("hết quota")

        assert judge_translation(broken, GOOD_VI).code == QC_OK

    def test_it_passes_one_positional_argument(self):
        """`CliAgentTranslator.complete` takes exactly one positional arg and has no
        system channel — a `system=` regression must fail loudly here."""
        seen = {}

        def send(prompt, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return "OK"

        judge_translation(send, GOOD_VI)
        assert seen == {"args": (), "kwargs": {}}


class _FakeEngine:
    """A translate callable that returns scripted bodies and records its hints."""

    def __init__(self, *bodies: str):
        self.bodies = list(bodies)
        self.calls: list[str] = []  # the retry_hint of each call, in order

    def __call__(self, title: str, content: str, hint: str) -> tuple[str, str]:
        self.calls.append(hint)
        body = self.bodies[min(len(self.calls) - 1, len(self.bodies) - 1)]
        return title, body


class TestRetryLoop:
    def _plan(self, label: str, engine, attempts: int = 2) -> QcAttemptPlan:
        return QcAttemptPlan(label=label, translate=engine, attempts=attempts)

    def test_a_good_first_attempt_costs_exactly_one_call(self):
        engine = _FakeEngine(GOOD_VI)
        outcome = translate_with_qc([self._plan("agy", engine)], "Chương 1", SOURCE)
        assert outcome.ok and outcome.attempts_used == 1
        assert engine.calls == [""]  # …and the first attempt carries NO hint

    def test_the_previous_failure_is_named_to_the_next_attempt(self):
        engine = _FakeEngine(ENGLISH, GOOD_VI)
        outcome = translate_with_qc([self._plan("agy", engine)], "Chương 1", SOURCE)
        assert outcome.ok
        assert engine.calls[0] == ""
        assert "TIẾNG VIỆT" in engine.calls[1]  # the critique reached the retry

    def test_it_falls_back_to_the_next_engine_after_the_budget(self):
        first = _FakeEngine(ENGLISH)
        second = _FakeEngine(GOOD_VI)
        outcome = translate_with_qc(
            [self._plan("agy", first, attempts=2), self._plan("claude", second)],
            "Chương 1", SOURCE,
        )
        assert outcome.ok
        assert outcome.engine_label == "claude"  # recorded as what actually produced it
        assert len(first.calls) == 2  # the first engine's budget was spent, not exceeded
        assert len(second.calls) == 1

    def test_exhausted_keep_best_returns_the_least_bad_attempt(self):
        """A fresh translation has no alternative text, so the best attempt is kept and
        marked — losing 90%-usable prose helps nobody."""
        engine = _FakeEngine(ENGLISH, CULTIVATION_VI)
        outcome = translate_with_qc(
            [self._plan("agy", engine)], "Chương 1", SOURCE,
            judge=lambda _body: QcVerdict(QC_HAN_VIET, "giọng convert"),
            policy=Policy.KEEP_BEST,
        )
        assert not outcome.ok
        assert outcome.verdict.code == QC_HAN_VIET  # the milder failure won
        assert outcome.body == CULTIVATION_VI

    def test_exhausted_strict_refuses_to_return_anything(self):
        """A re-translation DOES have an alternative — the translation already on disk —
        so it may only overwrite on a pass."""
        engine = _FakeEngine(ENGLISH)
        with pytest.raises(TranslateError):
            translate_with_qc(
                [self._plan("agy", engine)], "Chương 1", SOURCE, policy=Policy.STRICT
            )

    def test_the_judge_only_sees_chapters_the_cheap_checks_cleared(self):
        judged = []
        engine = _FakeEngine(ENGLISH, GOOD_VI)
        translate_with_qc(
            [self._plan("agy", engine)], "Chương 1", SOURCE,
            judge=lambda body: judged.append(body) or QcVerdict(),
        )
        assert judged == [GOOD_VI]  # the English attempt was never sent to the judge

    def test_an_engine_that_cannot_run_hands_over_instead_of_failing(self):
        def dead(_title, _content, _hint):
            raise TranslateError("hết quota")

        second = _FakeEngine(GOOD_VI)
        outcome = translate_with_qc(
            [self._plan("agy", dead), self._plan("claude", second)], "Chương 1", SOURCE
        )
        assert outcome.ok and outcome.engine_label == "claude"

    def test_every_engine_dead_raises(self):
        def dead(_title, _content, _hint):
            raise TranslateError("hết quota")

        with pytest.raises(TranslateError):
            translate_with_qc([self._plan("agy", dead)], "Chương 1", SOURCE)

    def test_an_empty_chain_raises_rather_than_silently_doing_nothing(self):
        with pytest.raises(TranslateError):
            translate_with_qc([], "Chương 1", SOURCE)

    def test_stopping_is_checked_between_attempts(self):
        """Cancel must not wait out a chapter that could be eight engine calls long."""
        engine = _FakeEngine(ENGLISH)
        stop = []
        outcome = translate_with_qc(
            [self._plan("agy", engine, attempts=5)], "Chương 1", SOURCE,
            should_stop=lambda: bool(stop) or stop.append(1),
        )
        assert len(engine.calls) == 1  # stopped after the first attempt, not after five
        assert not outcome.ok  # …and still returned what it had

    def test_progress_is_reported_per_attempt(self):
        seen = []
        engine = _FakeEngine(ENGLISH, GOOD_VI)
        translate_with_qc(
            [self._plan("agy", engine)], "Chương 1", SOURCE,
            on_attempt=lambda n, label, verdict: seen.append((n, label, verdict.code)),
        )
        assert seen == [(1, "agy", QC_NOT_VIETNAMESE), (2, "agy", QC_OK)]

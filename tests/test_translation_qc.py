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
    NO_HINT_CODES,
    QC_EMPTY,
    QC_HAN_VIET,
    QC_LABELS,
    QC_NAME_DRIFT,
    QC_NAME_VARIANT,
    QC_NOT_VIETNAMESE,
    QC_OK,
    QC_REFUSAL,
    QC_SEVERITY,
    QC_SOURCE_LEFTOVER,
    QC_TITLE_UNTRANSLATED,
    QC_TRUNCATED,
    Policy,
    QcAttemptPlan,
    QcVerdict,
    build_judge_prompt,
    build_name_profile,
    check_name_consistency,
    check_title,
    check_translation,
    diacritic_ratio,
    fix_name_variants,
    english_word_rate,
    judge_sample,
    judge_translation,
    parse_judge_reply,
    propose_name_fixes,
    retranslate_title_with_qc,
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


class TestTitleCheck:
    """Feature 094 — a good body under a heading still in Chinese passed QC, because QC
    never read the title. `_safe_title` saves the source title when the engine fails."""

    def test_a_source_title_saved_back_unchanged_is_caught(self):
        verdict = check_translation(SOURCE, "第12章 夜雨孤灯", GOOD_VI)
        assert verdict.code == QC_TITLE_UNTRANSLATED
        assert "第12章 夜雨孤灯" in verdict.reason  # the user sees which heading

    def test_a_half_translated_title_is_caught(self):
        verdict = check_translation(SOURCE, "Chương 12: Cái chết của Lý Phàm之死", GOOD_VI)
        assert verdict.code == QC_TITLE_UNTRANSLATED

    def test_a_short_chapter_does_not_excuse_its_title(self):
        """The short-body exemption skips RATE checks; a heading is structural."""
        note = "Ghi chú của tác giả: hôm nay bận quá nên chỉ ra được một chương thôi nhé."
        assert check_translation("作者的话：今天很忙。" * 3, "作者的话", note).code == QC_TITLE_UNTRANSLATED

    def test_a_body_failure_is_reported_before_the_title(self):
        assert check_translation(SOURCE, "第一章", ENGLISH).code == QC_NOT_VIETNAMESE

    def test_a_translated_or_empty_title_passes(self):
        assert check_title("Chương 1757: Cuối cùng giải được bí ẩn Sơn Hải").ok
        assert check_title("").ok

    def test_chinese_targets_keep_chinese_titles(self):
        assert check_title("第一章", target="zh").ok

    def test_the_retry_loop_retries_a_chapter_whose_title_came_back_chinese(self):
        titles = iter(["第12章 夜雨孤灯", "Chương 12: Đèn cô độc đêm mưa"])
        calls = []

        def engine(_title, _content, hint):
            calls.append(hint)
            return next(titles), GOOD_VI

        outcome = translate_with_qc(
            [QcAttemptPlan(label="agy", translate=engine)], "第12章 夜雨孤灯", SOURCE
        )
        assert outcome.ok and outcome.title == "Chương 12: Đèn cô độc đêm mưa"
        assert calls == ["", ""]  # no hint: it would reach the body prompt, not the title


class TestNameCheck:
    """Feature 086 — the gap that let a chapter pass with the wrong character name.

    Every other check asks about language and length, and a mis-spelled name is perfectly
    good Vietnamese of the right length.
    """

    def _body(self, name: str) -> str:
        return f"{name} nhìn nàng một cái rồi quay đầu bỏ đi, trong lòng buồn khó tả. " * 12

    GLOSSARY = {"尹志平": "Doãn Chí Bình"}

    def test_the_right_name_passes(self):
        verdict = check_translation(
            "尹志平" * 40, "Chương 1", self._body("Doãn Chí Bình"), glossary=self.GLOSSARY
        )
        assert verdict.code == QC_OK

    def test_a_changed_name_is_caught(self):
        """The reported case: 尹志平 came back as "Yin Chí Bình" — pinyin surname, Hán-Việt
        given name — and QC passed the chapter."""
        verdict = check_translation(
            "尹志平" * 40, "Chương 1", self._body("Yin Chí Bình"), glossary=self.GLOSSARY
        )
        assert verdict.code == QC_NAME_DRIFT
        assert "Doãn Chí Bình" in verdict.reason  # tells the user what to look for

    def test_i_and_y_are_the_same_name(self):
        """Vietnamese writes Lý or Lí, Kỳ or Kì, and both are correct. Treating them as
        different flagged 95% of chapters in a real library."""
        verdict = check_translation(
            "李莫愁" * 40, "Chương 1", self._body("Lí Mạc Sầu"),
            glossary={"李莫愁": "Lý Mạc Sầu"},
        )
        assert verdict.code == QC_OK

    def test_it_works_on_an_already_substituted_source(self):
        """A live translation passes source text in which the glossary has ALREADY replaced
        the Chinese, so the name is matched under either spelling."""
        verdict = check_translation(
            "Doãn Chí Bình đi tới. " * 40, "Chương 1", self._body("Yin Chí Bình"),
            glossary=self.GLOSSARY,
        )
        assert verdict.code == QC_NAME_DRIFT

    def test_a_name_the_chapter_never_mentions_is_not_required(self):
        verdict = check_translation(
            "他走了。" * 60, "Chương 1", self._body("Ai đó"), glossary=self.GLOSSARY
        )
        assert verdict.code == QC_OK

    def test_one_mention_is_enough(self):
        # A translation may use a pronoun after the first mention.
        body = "Doãn Chí Bình đi tới. " + "Hắn nhìn nàng rồi quay đầu bỏ đi buồn bã. " * 20
        assert check_translation("尹志平" * 40, "C1", body, glossary=self.GLOSSARY).code == QC_OK

    def test_no_glossary_means_no_name_check(self):
        # The behaviour before this feature, preserved for every caller that passes none.
        assert check_translation(
            "尹志平" * 40, "Chương 1", self._body("Yin Chí Bình")
        ).code == QC_OK


class TestNameConsistency:
    """The check that actually catches a wrong name: the novel disagreeing with itself.

    It asks about CONSISTENCY, not correctness, which is what makes it usable — comparing
    chapters against the auto-detected glossary instead flagged 38-94% of them per novel,
    because a detected "name" that is not a person can never appear in any translation.
    A non-name has no competing spellings, so it never has a minority variant.
    """

    READING = "Doãn Chí Bình"

    def _novel(self, canonical_uses: int = 60) -> list[str]:
        return [f"Hôm nay {self.READING} đi tới. Hắn nhìn quanh." for _ in range(canonical_uses)]

    def _profile(self, extra: list[str] | None = None):
        return build_name_profile(self._novel() + (extra or []), [self.READING])

    def test_the_odd_chapter_out_is_found(self):
        """The reported case: one chapter says "Yin Chí Bình" where 14838 others say
        "Doãn Chí Bình", and every other QC check passes it."""
        odd = "Hôm nay Yin Chí Bình đi tới. Hắn nhìn quanh."
        verdict = check_name_consistency(odd, self._profile([odd]))
        assert verdict.code == QC_NAME_VARIANT
        assert "doãn chí bình" in verdict.reason  # tells the user what it should be

    def test_the_usual_spelling_passes(self):
        assert check_name_consistency(self._novel()[0], self._profile()).code == QC_OK

    def test_an_ordinary_word_before_the_name_is_not_a_spelling(self):
        """"theo Chí Bình" is "follow Chí Bình" — lowercase, so not a name."""
        text = "Hắn đi theo Chí Bình ra ngoài."
        assert check_name_consistency(text, self._profile([text])).code == QC_OK

    def test_a_sentence_initial_capital_is_not_a_spelling(self):
        """Every word is capitalised at the start of a sentence, so a capital there is
        orthography and says nothing. Ignoring this was worth 3 points of false positives."""
        text = "Còn Chí Bình thì sao? Anh ấy đã đi rồi."
        assert check_name_consistency(text, self._profile([text])).code == QC_OK

    def test_an_honorific_is_not_a_spelling(self):
        """"Tiểu Chí Bình" is "little Chí Bình", an address, not a different name."""
        text = "Hôm nay Tiểu Chí Bình đi tới đây."
        assert check_name_consistency(text, self._profile([text])).code == QC_OK

    def test_an_established_alternative_is_left_alone(self):
        """A spelling used through a fifth of the novel is a legacy convention, not a slip;
        flagging those would bury the real mistakes."""
        common = ["Hôm nay Duẫn Chí Bình đi tới. Hắn nhìn quanh." for _ in range(20)]
        verdict = check_name_consistency(common[0], self._profile(common))
        assert verdict.code == QC_OK

    def test_a_name_the_novel_barely_uses_is_not_profiled(self):
        """Two mentions cannot establish "how this novel spells it", and one early chapter
        must not define the canon for a whole book."""
        profile = build_name_profile(self._novel(3), [self.READING])
        assert profile.empty

    def test_a_two_syllable_name_is_not_profiled(self):
        """With a one-syllable stem ("Quách Tĩnh" → "Tĩnh") every other name ending in that
        syllable collides with it."""
        profile = build_name_profile(["Quách Tĩnh đi tới."] * 60, ["Quách Tĩnh"])
        assert profile.empty

    def test_no_profile_means_no_check(self):
        assert check_name_consistency("bất kỳ điều gì", None).code == QC_OK
        assert check_name_consistency("bất kỳ điều gì", build_name_profile([], [])).code == QC_OK

    def test_it_runs_inside_check_translation(self):
        # ONE odd mention among ordinary prose: enough words for the rate-based checks to
        # apply, and rare enough novel-wide to read as a slip rather than a convention.
        odd = (
            "Hôm nay Yin Chí Bình đi tới đó. "
            + "Hắn nhìn quanh rồi lặng lẽ quay đầu bỏ đi trong lòng buồn khó tả. " * 12
        )
        verdict = check_translation(
            "尹志平" * 40, "Chương 1", odd, profile=self._profile([odd])
        )
        assert verdict.code == QC_NAME_VARIANT


class TestNameFix:
    """Feature 095 — propose odd spellings for review, then apply only the approved ones."""

    READING = "Doãn Chí Bình"
    YIN = ("chí bình", "iin")  # a proposal key: folded stem and folded prefix

    def _profile(self, *extra: str, reading: str = READING, usual: str = READING):
        # 200 uses: a slip used twice in one chapter must stay under `VARIANT_SHARE` (2%),
        # or it is an established alternative and — correctly — not a mistake to fix.
        novel = [f"Hôm nay {usual} đi tới. Hắn nhìn quanh." for _ in range(200)]
        return build_name_profile(novel + list(extra), [reading])

    def test_an_approved_slip_becomes_the_usual_spelling(self):
        odd = "Hôm nay Yin Chí Bình đi tới, rồi Yin Chí Bình ngồi xuống."
        fixed, changes = fix_name_variants(odd, self._profile(odd), approved={self.YIN})
        assert fixed == "Hôm nay Doãn Chí Bình đi tới, rồi Doãn Chí Bình ngồi xuống."
        assert changes == ["Yin Chí Bình → Doãn Chí Bình"]  # shown to the user
        assert check_name_consistency(fixed, self._profile(odd)).code == QC_OK

    def test_nothing_is_written_that_was_not_approved(self):
        """THE rule. On a real library most proposed pairs were false positives."""
        odd = "Hôm nay Yin Chí Bình đi tới."
        assert fix_name_variants(odd, self._profile(odd), approved=set()) == (odd, [])
        other = {("chí bình", "lê")}
        assert fix_name_variants(odd, self._profile(odd), approved=other) == (odd, [])

    def test_approval_is_required_not_defaulted(self):
        with pytest.raises(TypeError):
            fix_name_variants("Hôm nay Yin Chí Bình đi.", self._profile())

    def test_proposals_group_a_slip_across_chapters_with_an_example(self):
        a = "Hôm nay Yin Chí Bình đi tới."
        b = "Rồi Yin Chí Bình lại tới, và Yin Chí Bình ngồi."
        proposals = propose_name_fixes([(3, a), (7, b)], self._profile(a, b))
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.key == self.YIN
        assert (proposal.wrong, proposal.right) == ("Yin Chí Bình", "Doãn Chí Bình")
        assert proposal.occurrences == 3 and proposal.chapters == (3, 7)
        assert "Yin Chí Bình" in proposal.example  # enough context to judge it by

    def test_proposals_are_exactly_what_a_full_approval_would_change(self):
        odd = "Yin Chí Bình đứng dậy. Hôm nay Yin Chí Bình đi tới."
        profile = self._profile(odd)
        keys = {p.key for p in propose_name_fixes([(0, odd)], profile)}
        assert fix_name_variants(odd, profile, approved=keys)[0].count("Doãn") == 2

    def test_it_writes_the_novels_own_spelling_not_the_folded_one(self):
        """Names are COMPARED with i/y folded, but "Lí" must never be WRITTEN where the
        novel says "Lý" — that would trade one inconsistency for another."""
        odd = "Hôm nay Lê Mạc Sầu đi tới."
        profile = self._profile(odd, reading="Lí Mạc Sầu", usual="Lý Mạc Sầu")
        keys = {p.key for p in propose_name_fixes([(0, odd)], profile)}
        assert fix_name_variants(odd, profile, approved=keys)[0] == "Hôm nay Lý Mạc Sầu đi tới."

    def test_a_sentence_start_is_fixed_when_the_chapter_uses_that_spelling(self):
        """Otherwise the fix leaves it, and the re-check — which ignores sentence starts —
        calls the chapter clean with the wrong name still in it."""
        odd = "Yin Chí Bình đứng dậy. Hôm nay Yin Chí Bình đi tới."
        fixed, _ = fix_name_variants(odd, self._profile(odd), approved={self.YIN})
        assert "Yin" not in fixed

    def test_an_ordinary_word_at_a_sentence_start_is_not_proposed(self):
        """Even when a stray mid-sentence capital has made "Còn" a known minority spelling
        novel-wide, THIS chapter never uses it mid-sentence — so "as for Chí Bình" stays."""
        profile = self._profile("Hắn gọi Còn Chí Bình tới.")
        assert ("chí bình", "còn") in profile.minority  # the trap is armed
        assert propose_name_fixes([(0, "Còn Chí Bình thì sao? Hắn đi ra.")], profile) == []

    def test_an_established_alternative_is_not_proposed(self):
        common = ["Hôm nay Duẫn Chí Bình đi tới."] * 20
        assert propose_name_fixes([(0, common[0])], self._profile(*common)) == []

    def test_honorifics_and_lowercase_words_are_not_proposed(self):
        profile = self._profile("Hôm nay Yin Chí Bình đi tới.")
        assert propose_name_fixes([(0, "Hắn đi theo Chí Bình, gọi Tiểu Chí Bình.")], profile) == []

    def test_no_profile_proposes_and_changes_nothing(self):
        text = "Hôm nay Yin Chí Bình đi."
        assert propose_name_fixes([(0, text)], None) == []
        assert fix_name_variants(text, None, approved={self.YIN}) == (text, [])
        assert fix_name_variants("", self._profile(), approved={self.YIN}) == ("", [])


class TestFastPrefixMatching:
    """`_prefix_matches` walks back from `str.find` instead of running a regex at every
    character — 126 s to profile a 1793-chapter novel before. It must not change a result."""

    def test_it_agrees_with_the_regex_it_replaced(self):
        import re

        from noveltrans.translators.qc import _HONORIFICS, _SENTENCE_END, _prefix_matches

        def by_regex(text, stem):
            found = []
            for m in re.compile(r"([^\W\d_]+)\s+" + re.escape(stem), re.IGNORECASE).finditer(text):
                word = m.group(1)
                if not word[:1].isupper() or word.casefold() in _HONORIFICS:
                    continue
                before = text[: m.start(1)].rstrip(" ")
                found.append((m.start(1), m.end(1), word, not before or before[-1] in _SENTENCE_END))
            return found

        text = (
            "Yin Chí Bình đứng dậy. Hôm nay  Doãn Chí Bình đi tới, gọi Tiểu Chí Bình.\n"
            "“Lê Chí Bình!” theo Chí Bình 3Yin Chí Bình, CHÍ BÌNH, Cao\tChí Bình chí bình\n"
            "Ông Chí Bìnhxyz Chí Bình. Chí Bình Chí Bình Chí Bình Xa  Chí Bình"
        )
        assert list(_prefix_matches(text, "chí bình")) == by_regex(text, "chí bình")


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
            assert QC_LABELS.get(code), code
            if code in NO_HINT_CODES:
                assert retry_hint_for(QcVerdict(code)) == "", code
                continue
            assert retry_hint_for(QcVerdict(code)).strip(), code

    def test_a_name_variant_ranks_beside_the_glossary_name_failure(self):
        assert severity(QC_NAME_VARIANT) == severity(QC_NAME_DRIFT) + 1

    def test_a_bad_title_ranks_below_every_body_failure(self):
        """`KEEP_BEST` must never give up a good body to get a clean heading."""
        assert severity(QC_HAN_VIET) < severity(QC_TITLE_UNTRANSLATED) < severity(QC_OK)

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


class TestTitleOnlyLoop:
    """Feature 094 — `retranslate_title_with_qc`, the loop behind title-only re-translation."""

    def _plan(self, label, replies, attempts=2):
        calls = []

        def translate_title(title):
            calls.append(title)
            reply = replies[min(len(calls) - 1, len(replies) - 1)]
            if isinstance(reply, Exception):
                raise reply
            return reply

        def no_body(*_args):
            raise AssertionError("a title-only run must never translate a body")

        return QcAttemptPlan(label, no_body, attempts, translate_title), calls

    def test_a_good_title_costs_one_call(self):
        plan, calls = self._plan("agy", ["Chương 12: Đèn cô độc"])
        outcome = retranslate_title_with_qc([plan], "第12章 孤灯")
        assert outcome.ok and outcome.title == "Chương 12: Đèn cô độc"
        assert outcome.engine_label == "agy" and calls == ["第12章 孤灯"]

    def test_it_retries_then_falls_back_to_the_next_engine(self):
        first, first_calls = self._plan("agy", ["第12章 孤灯"], attempts=2)
        second, _ = self._plan("claude", ["Chương 12: Đèn cô độc"])
        outcome = retranslate_title_with_qc([first, second], "第12章 孤灯")
        assert outcome.ok and outcome.engine_label == "claude"
        assert len(first_calls) == 2 and outcome.attempts_used == 3

    def test_exhaustion_returns_no_title_never_a_chinese_one(self):
        plan, _ = self._plan("agy", ["第12章 孤灯"])
        outcome = retranslate_title_with_qc([plan], "第12章 孤灯")
        assert not outcome.ok and outcome.title == ""
        assert outcome.verdict.code == QC_TITLE_UNTRANSLATED

    def test_an_engine_error_moves_on_instead_of_raising(self):
        dead, dead_calls = self._plan("claude", [TranslateError("hết quota")])
        good, _ = self._plan("agy", ["Chương 12: Đèn cô độc"])
        outcome = retranslate_title_with_qc([dead, good], "第12章 孤灯")
        assert outcome.ok and len(dead_calls) == 1  # one error ends that engine's turn

    def test_an_empty_reply_is_not_a_fix(self):
        plan, _ = self._plan("agy", [""])
        assert not retranslate_title_with_qc([plan], "第12章 孤灯").ok

    def test_a_plan_without_a_title_callable_is_skipped(self):
        bodyless = QcAttemptPlan("google", lambda *_a: ("", ""))
        good, _ = self._plan("agy", ["Chương 12"])
        assert retranslate_title_with_qc([bodyless, good], "第12章 孤灯").engine_label == "agy"

    def test_stopping_writes_nothing(self):
        plan, calls = self._plan("agy", ["Chương 12"])
        outcome = retranslate_title_with_qc([plan], "第12章 孤灯", should_stop=lambda: True)
        assert not outcome.ok and calls == []

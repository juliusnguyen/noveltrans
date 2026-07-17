"""Tests for the pre-TTS text cleaner. Pure — no engine, no model."""

from __future__ import annotations

from noveltrans.tts.base import split_sentences
from noveltrans.tts.clean import clean_for_tts

# A real paragraph of Vietnamese prose: tone marks, đ, digits, dialogue, punctuation.
VIETNAMESE = (
    "Diệp Vân mỉm cười: “Ngươi nghĩ ta sợ sao?” Hắn bước tới, ánh mắt lạnh lẽo. "
    "Năm 1234, tại thành Lạc Dương, một trận chiến kinh thiên động địa đã nổ ra!"
)


class TestPreservesVietnamese:
    def test_clean_prose_survives_unchanged(self):
        # The single most important property: real Vietnamese in, same text out.
        assert clean_for_tts(VIETNAMESE) == VIETNAMESE

    def test_tone_marks_and_special_letters_kept(self):
        text = "ằẳẵắặ ộ ữ đ Đ ươ ạ"
        assert clean_for_tts(text) == text

    def test_digits_and_prosody_punctuation_kept(self):
        text = "Chương 12: “A, b; c!” Thật sao? Đúng… (rồi)."
        assert clean_for_tts(text) == text

    def test_is_idempotent(self):
        once = clean_for_tts(VIETNAMESE)
        assert clean_for_tts(once) == once


class TestStripsSpecials:
    def test_removes_emoji(self):
        assert clean_for_tts("Xin chào 😀🎉 các bạn") == "Xin chào các bạn"

    def test_removes_decorative_symbols(self):
        assert clean_for_tts("★ Chương 1 ★") == "Chương 1"
        assert clean_for_tts("【Hồi 1】※ Mở đầu") == "Hồi 1 Mở đầu"

    def test_removes_leftover_cjk(self):
        assert clean_for_tts("Diệp Vân 叶云 đến") == "Diệp Vân đến"

    def test_removes_zero_width_without_splitting_the_word(self):
        # A zero-width joiner inside a word must vanish, not become a space.
        assert clean_for_tts("Xin​chào") == "Xinchào"

    def test_removes_markdown_remnants(self):
        assert clean_for_tts("*in đậm* và _nghiêng_ và `mã`") == "in đậm và nghiêng và mã"

    def test_dropped_symbol_does_not_merge_neighbouring_words(self):
        assert clean_for_tts("A★B") == "A B"


class TestDashes:
    def test_dialogue_dashes_normalised_to_hyphen(self):
        assert clean_for_tts("— Xin chào") == "- Xin chào"
        assert clean_for_tts("A – B — C") == "A - B - C"


class TestFullwidthPunctuation:
    # Chinese-sourced text leaks fullwidth punctuation through translation; it must be
    # normalised to ASCII (which carries the pause), not stripped (which loses it).
    def test_sentence_and_clause_marks_normalised(self):
        assert clean_for_tts("Thật sao？ Đúng！ Rồi，đi。") == "Thật sao? Đúng! Rồi,đi."

    def test_colon_semicolon_and_ideographic_comma(self):
        assert clean_for_tts("A：b；c、d") == 'A:b;c,d'

    def test_corner_and_fullwidth_brackets_become_quotes_and_parens(self):
        assert clean_for_tts("「trích」（chú）") == '"trích"(chú)'

    def test_a_fullwidth_ender_is_a_real_sentence_boundary(self):
        # The whole point: a leftover ！ becomes an ASCII ender the chunker splits on,
        # not a gap. split_sentences packs greedily, so force a split with a small limit.
        cleaned = clean_for_tts("Câu một！Câu hai。")
        assert cleaned == "Câu một!Câu hai."  # both enders normalised, nothing dropped
        assert len(split_sentences(cleaned, max_chars=10)) == 2  # a usable boundary


class TestWhitespace:
    def test_paragraph_breaks_preserved(self):
        # \n\n must survive — split_sentences relies on it.
        assert clean_for_tts("Đoạn một.\n\nĐoạn hai.") == "Đoạn một.\n\nĐoạn hai."

    def test_runs_of_spaces_collapsed(self):
        assert clean_for_tts("A    B") == "A B"

    def test_blank_run_capped_at_one_blank_line(self):
        # 4 newlines collapse to a single blank line, not a growing gap of silence.
        assert clean_for_tts("A.\n\n\n\nB.") == "A.\n\nB."

    def test_spaces_around_newlines_trimmed(self):
        assert clean_for_tts("A.  \n  B.") == "A.\nB."

    def test_empty_and_symbol_only_clean_to_empty(self):
        assert clean_for_tts("") == ""
        assert clean_for_tts("   ") == ""
        assert clean_for_tts("★☆※") == ""


class TestDownstreamChunkingSurvives:
    def test_cleaning_does_not_break_sentence_splitting(self):
        raw = "Câu một! Câu hai? Câu ba… “Trích dẫn.”\n\nĐoạn mới 中文 emoji😀 ở đây."
        cleaned = clean_for_tts(raw)
        chunks = split_sentences(cleaned, max_chars=400)
        assert chunks  # still chunks
        assert not any("中" in c or "😀" in c for c in chunks)  # specials gone
        # sentence enders survived, so splitting still sees sentence boundaries
        assert any(c.endswith(("!", "?", "…", ".", "”")) for c in chunks)

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


class TestExtraRemove:
    def test_strips_kept_punctuation_the_user_lists(self):
        # The point of the setting: remove things the whitelist normally keeps.
        assert clean_for_tts("Câu (một) hai.", extra_remove="()") == "Câu một hai."

    def test_removes_each_listed_character(self):
        # Removed chars become a space (like any dropped symbol), so words don't merge.
        out = clean_for_tts("nói “xin chào” nhé", extra_remove="“”")
        assert "“" not in out and "”" not in out
        assert out == "nói xin chào nhé"

    def test_no_op_for_already_stripped_characters(self):
        # ★ is gone with or without listing it — listing it changes nothing.
        assert clean_for_tts("x ★ y", extra_remove="★") == clean_for_tts("x ★ y")

    def test_empty_extra_remove_is_the_default_behaviour(self):
        assert clean_for_tts("Bình thường (rồi).", extra_remove="") == clean_for_tts(
            "Bình thường (rồi)."
        )

    def test_whitespace_in_the_list_is_ignored(self):
        # A stray space in the setting must not delete every space in the text.
        assert clean_for_tts("A B C", extra_remove="   ") == "A B C"

    def test_matches_the_cleaned_form_not_the_raw_form(self):
        # Fullwidth ！ is normalised to ! first, so listing "!" removes it.
        assert clean_for_tts("Đi nào！", extra_remove="!") == "Đi nào"


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


class TestPunctuationOnlyLinesBecomeSilence:
    """Feature 038 — a line with nothing speakable on it is a *beat*, not a sound.

    Reported: a `“…”` line rendered by Ngọc Linh came out as a weird noise. It reached the
    engine because every character in it is in the keep-set, `split_sentences` gave it its
    own chunk, and feature 028's `merge_short_chunks` glued that 3-char chunk onto the
    neighbouring sentence for the voice to pronounce.
    """

    # the excerpt exactly as reported
    REPORTED = (
        "“Tên bây giờ khó nghe quá.”\n\n"
        "“…”\n\n"
        "Một vấn đề hoàn toàn không nên xuất hiện trong trường hợp này."
    )

    def test_the_reported_ellipsis_line_is_gone(self):
        out = clean_for_tts(self.REPORTED)
        assert "…" not in out
        assert out == (
            "“Tên bây giờ khó nghe quá.”\n\n"
            "Một vấn đề hoàn toàn không nên xuất hiện trong trường hợp này."
        )

    def test_an_ellipsis_inside_a_sentence_is_untouched(self):
        """**The anti-regression.** Mid-sentence ellipsis is prosody, not silence —
        turning it into a pause would be a worse bug than the one being fixed."""
        text = "Anh ta ngập ngừng… rồi im lặng."
        assert clean_for_tts(text) == text

    def test_a_sentence_ending_in_an_ellipsis_survives(self):
        text = "Hắn không nói gì cả…"
        assert clean_for_tts(text) == text

    def test_silent_beats_of_every_common_shape_are_dropped(self):
        """The shapes these novels actually use for a wordless reply."""
        for beat in ("“…”", "……", '"..."', "“?”", "“!”", "- - -", "***", "( )", "?!"):
            out = clean_for_tts(f"Trước.\n\n{beat}\n\nSau.")
            assert out == "Trước.\n\nSau.", f"{beat!r} survived as {out!r}"

    def test_a_digit_only_line_is_speech_and_stays(self):
        """Digits are pronounceable — a bare chapter number must still be read."""
        assert clean_for_tts("Trước.\n\n1\n\nSau.") == "Trước.\n\n1\n\nSau."

    def test_a_single_letter_line_stays(self):
        assert clean_for_tts("Trước.\n\nA\n\nSau.") == "Trước.\n\nA\n\nSau."

    def test_a_line_emptied_by_extra_remove_is_dropped_too(self):
        """Ordering: the check runs after `extra_remove`, so a line that setting empties
        is caught as well."""
        assert clean_for_tts("Trước.\n\n(A)\n\nSau.", "A") == "Trước.\n\nSau."

    def test_a_chapter_of_nothing_but_beats_cleans_to_empty(self):
        """Feeds the existing "Chương không có nội dung để đọc." guard rather than
        handing the engine a page of punctuation."""
        assert clean_for_tts("“…”\n\n“?”\n\n***") == ""

    def test_dropping_a_beat_leaves_a_paragraph_break_not_a_join(self):
        """The break is what `synthesize_chapter` renders as real silence, so the beat
        the author wrote survives *as* silence."""
        out = clean_for_tts("Trước.\n\n“…”\n\nSau.")
        assert "\n\n" in out
        assert "Trước. Sau." not in out

    def test_the_beat_no_longer_reaches_the_engine(self):
        """End to end through the real chunker, including 028's merge step — which is what
        actually handed the ellipsis to the voice."""
        from noveltrans.tts.base import merge_short_chunks

        chunks = merge_short_chunks(
            split_sentences(clean_for_tts(self.REPORTED), 400), 30, 400
        )
        assert not any("…" in c for c in chunks)

    def test_a_beat_between_normal_length_paragraphs_still_yields_a_pause(self):
        """With paragraphs above 028's 30-char merge floor the gap survives as a real
        pause — two chunks, so `synthesize_chapter` puts silence between them."""
        from noveltrans.tts.base import merge_short_chunks

        src = (
            "“Tên bây giờ khó nghe quá, ta đã nói với ngươi bao nhiêu lần rồi hả?”\n\n"
            "“…”\n\n"
            "Một vấn đề hoàn toàn không nên xuất hiện trong trường hợp này, hắn nghĩ."
        )
        chunks = merge_short_chunks(split_sentences(clean_for_tts(src), 400), 30, 400)
        assert len(chunks) == 2

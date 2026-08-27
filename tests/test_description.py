"""Feature 065 — fitting a YouTube description into 5000 characters, and the short form."""

from __future__ import annotations

from noveltrans.tts.description import (
    TRUNCATION_PREFIX,
    YOUTUBE_DESCRIPTION_CHAR_LIMIT,
    build_short_description,
    build_shorten_prompt,
    clamp_description,
    description_length,
    fit_description,
    indexed_chapter_count,
    looks_generated,
    parse_shortened_titles,
    short_chapter_label,
    split_chapter_number,
    was_truncated,
)

BEFORE = ['Tên truyện: 穿书反派 — "Xuyên sách phản diện"', "Số chương: 199", "", "Mục lục chương:"]
AFTER = ["", "Tạo bởi: Fox Novel"]


def _lines(n: int, width: int = 45) -> list[str]:
    return [f"{i}:00 Chương {i}: {'ả' * width}" for i in range(1, n + 1)]


class TestDescriptionLength:
    def test_ascii_is_a_plain_character_count(self):
        assert description_length("abcde") == 5

    def test_vietnamese_diacritics_count_one_each(self):
        assert description_length("Chương") == 6

    def test_emoji_counts_two_like_studios_counter(self):
        # Studio counts JS String.length (UTF-16 units), where an emoji is a surrogate pair
        assert description_length("🐉") == 2
        assert len("🐉") == 1  # ...and Python disagrees, which is the whole point

    def test_empty_and_none_are_zero(self):
        assert description_length("") == 0
        assert description_length(None) == 0


class TestFitDescription:
    def test_returns_the_text_unchanged_when_everything_fits(self):
        chapters = _lines(3)
        text, dropped = fit_description(BEFORE, chapters, AFTER)
        assert dropped == 0
        assert text == "\n".join([*BEFORE, *chapters, *AFTER]) + "\n"

    def test_never_exceeds_the_limit(self):
        text, dropped = fit_description(BEFORE, _lines(500), AFTER)
        assert description_length(text) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT
        assert dropped > 0

    def test_keeps_a_prefix_starting_at_the_first_chapter(self):
        chapters = _lines(500)
        text, _ = fit_description(BEFORE, chapters, AFTER)
        assert chapters[0] in text

    def test_stops_at_the_first_line_that_does_not_fit(self):
        # A hole in the middle of a chapter index reads as a bug — the loop breaks, it
        # does not skip the fat line and carry on the way parse_tags does.
        chapters = ["0:00 A", "1:00 " + "x" * 6000, "2:00 C"]
        text, dropped = fit_description(BEFORE, chapters, AFTER)
        assert "0:00 A" in text
        assert "2:00 C" not in text
        assert dropped == 2

    def test_marker_reports_the_real_dropped_count(self):
        text, dropped = fit_description(BEFORE, _lines(500), AFTER)
        assert f"còn {dropped} chương nữa" in text

    def test_marker_is_the_last_line_of_the_chapter_block(self):
        text, _ = fit_description(BEFORE, _lines(500), AFTER)
        lines = text.splitlines()
        marker = next(i for i, ln in enumerate(lines) if ln.startswith(TRUNCATION_PREFIX))
        assert lines[marker + 1:] == AFTER

    def test_mandatory_lines_all_survive(self):
        text, _ = fit_description(BEFORE, _lines(500), AFTER)
        for line in [*BEFORE, *AFTER]:
            assert line in text.splitlines()

    def test_no_marker_when_nothing_was_dropped(self):
        text, dropped = fit_description(BEFORE, _lines(3), AFTER)
        assert dropped == 0
        assert TRUNCATION_PREFIX not in text

    def test_zero_chapters_kept_still_emits_the_marker(self):
        text, dropped = fit_description(BEFORE, _lines(5), AFTER, max_chars=200)
        assert dropped == 5
        assert "còn 5 chương nữa" in text

    def test_header_alone_over_the_limit_is_still_clamped(self):
        # "never exceeds the cap" beats "never touch the header": a description YouTube
        # truncates wherever it likes is strictly worse than one we shortened on purpose.
        text, _ = fit_description(["x" * 6000], _lines(3), AFTER)
        assert description_length(text) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT

    def test_no_chapter_lines_at_all(self):
        text, dropped = fit_description(BEFORE, [], AFTER)
        assert dropped == 0
        assert TRUNCATION_PREFIX not in text

    def test_marker_reservation_uses_the_worst_case_digit_count(self):
        # dropped falls from 4 digits to 3 across this range; the reservation is made at
        # the widest, so a narrower real marker can only leave slack, never overflow.
        for count in (999, 1000, 1001):
            text, _ = fit_description(BEFORE, _lines(count), AFTER)
            assert description_length(text) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT


class TestClampDescription:
    def test_under_the_limit_is_returned_unchanged(self):
        assert clamp_description("abc\n") == "abc\n"

    def test_cuts_on_a_line_boundary(self):
        text = "\n".join("x" * 40 for _ in range(10)) + "\n"
        out = clamp_description(text, max_chars=100)
        assert description_length(out) <= 100
        assert out.endswith("\n")
        assert all(len(line) == 40 for line in out.splitlines())

    def test_falls_back_to_a_hard_cut_without_a_newline(self):
        out = clamp_description("x" * 500, max_chars=100)
        assert out == "x" * 100

    def test_never_splits_a_surrogate_pair(self):
        out = clamp_description("🐉" * 100, max_chars=51)
        assert description_length(out) <= 51
        assert out == "🐉" * 25  # 25 emoji = 50 units; the 26th would need 2 more


class TestWasTruncated:
    def test_true_for_a_fitted_description(self):
        text, _ = fit_description(BEFORE, _lines(500), AFTER)
        assert was_truncated(text)

    def test_false_for_an_untruncated_one(self):
        text, _ = fit_description(BEFORE, _lines(3), AFTER)
        assert not was_truncated(text)


class TestIndexedChapterCount:
    def test_counts_the_listed_timestamp_lines(self):
        text, _ = fit_description(BEFORE, _lines(3), AFTER)
        assert indexed_chapter_count(text) == 3

    def test_a_trimmed_index_still_reports_the_whole_span(self):
        # listed + the marker's own N — this is what keeps a trimmed index from looking
        # like a part that lost chapters
        text, dropped = fit_description(BEFORE, _lines(500), AFTER)
        assert dropped > 0
        assert indexed_chapter_count(text) == 500

    def test_handles_hour_long_timestamps(self):
        assert indexed_chapter_count("1:01:40 C2\n") == 1


class TestSplitChapterNumber:
    def test_vietnamese_prefix_with_colon(self):
        assert split_chapter_number("Chương 12: Nhặt được chậu rách") == (
            12, "Nhặt được chậu rách"
        )

    def test_prefix_without_a_separator(self):
        assert split_chapter_number("Chương 7 Mở đầu") == (7, "Mở đầu")

    def test_leading_zeros(self):
        assert split_chapter_number("Chương 007: X") == (7, "X")

    def test_bare_numbered_title(self):
        assert split_chapter_number("Chương 12") == (12, "")

    def test_abbreviated_prefix(self):
        assert split_chapter_number("Ch. 3 - Mở đầu") == (3, "Mở đầu")

    def test_cjk_prefix(self):
        assert split_chapter_number("第12章 破境") == (12, "破境")

    def test_bare_title_returns_none_and_is_untouched(self):
        # Inventing a number here would renumber the index — worse than a long description
        assert split_chapter_number("Nhặt được chậu rách") == (
            None, "Nhặt được chậu rách"
        )

    def test_a_title_that_merely_contains_the_word_chuong_is_not_stripped(self):
        assert split_chapter_number("Bí mật của chương trình") == (
            None, "Bí mật của chương trình"
        )

    def test_a_title_starting_with_a_similar_word_is_not_stripped(self):
        assert split_chapter_number("Chỉ 5 phút") == (None, "Chỉ 5 phút")


class TestShortChapterLabel:
    def test_formats_as_c_dot_n(self):
        assert short_chapter_label(12) == "C.12"

    def test_none_gives_an_empty_label(self):
        assert short_chapter_label(None) == ""


class TestBuildShortenPrompt:
    def test_numbers_every_title(self):
        prompt = build_shorten_prompt(["Mở đầu", "Kết thúc"])
        assert "1. Mở đầu" in prompt
        assert "2. Kết thúc" in prompt

    def test_states_the_expected_line_count(self):
        assert "ĐÚNG 2 dòng" in build_shorten_prompt(["a", "b"])


class TestParseShortenedTitles:
    def test_strips_numbering_and_returns_one_per_original(self):
        out, ok = parse_shortened_titles("1. Mở đầu\n2. Kết thúc", ["a", "b"])
        assert ok
        assert out == ["Mở đầu", "Kết thúc"]

    def test_ignores_a_preamble_line(self):
        out, ok = parse_shortened_titles(
            "Đây là danh sách:\n1. Mở đầu\n2. Kết thúc", ["a", "b"]
        )
        assert ok
        assert out == ["Mở đầu", "Kết thúc"]

    def test_accepts_an_unnumbered_reply(self):
        out, ok = parse_shortened_titles("Mở đầu\nKết thúc", ["a", "b"])
        assert ok
        assert out == ["Mở đầu", "Kết thúc"]

    def test_line_count_mismatch_falls_back_to_the_originals(self):
        # A list short by one would attach every title to the wrong timestamp — never
        # salvaged by pairing up whatever happens to line up.
        out, ok = parse_shortened_titles("1. Mở đầu", ["a", "b"])
        assert not ok
        assert out == ["a", "b"]

    def test_blank_reply_falls_back(self):
        assert parse_shortened_titles("", ["a", "b"]) == (["a", "b"], False)


class TestBuildShortDescription:
    def _entries(self, n: int = 3):
        return [(f"{i}:00", f"C.{i}", f"Tên chương {i}") for i in range(1, n + 1)]

    def test_drops_title_author_and_credit(self):
        text, _d, _e = build_short_description(self._entries(), total_chapters=199)
        assert "Tên truyện" not in text
        assert "Tác giả" not in text
        assert "Tạo bởi" not in text

    def test_keeps_the_chapter_index_header(self):
        text, _d, _e = build_short_description(self._entries(), total_chapters=199)
        assert "Mục lục chương:" in text

    def test_keeps_the_chapter_count_line(self):
        # Not in the "drop these" list the feature asked for, and it's ~15 characters —
        # pinned so removing it has to be a deliberate change.
        text, _d, _e = build_short_description(self._entries(), total_chapters=199)
        assert "Số chương: 199" in text

    def test_first_timestamp_is_still_0_00(self):
        entries = [("0:00", "C.1", "A"), ("5:51", "C.2", "B")]
        text, _d, _e = build_short_description(entries, total_chapters=2)
        assert "\n0:00 C.1 A\n" in text

    def test_lines_are_timestamp_label_title(self):
        text, _d, _e = build_short_description(self._entries(1), total_chapters=1)
        assert "1:00 C.1 Tên chương 1" in text

    def test_missing_label_does_not_leave_a_double_space(self):
        text, _d, _e = build_short_description([("0:00", "", "Mở đầu")], total_chapters=1)
        assert "0:00 Mở đầu" in text
        assert "  " not in text

    def test_still_respects_the_char_limit(self):
        entries = [(f"{i}:00", f"C.{i}", "ả" * 60) for i in range(1, 500)]
        text, dropped, _e = build_short_description(entries, total_chapters=499)
        assert description_length(text) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT
        assert dropped > 0

    def test_is_much_shorter_than_the_full_builder(self):
        from noveltrans.tts.merge import MergeSegment
        from noveltrans.tts.video import build_video_description

        segments = [
            MergeSegment(path="", seconds=300, title=f"Chương {i}: Tên chương dài {i}")
            for i in range(1, 61)
        ]
        full = build_video_description(
            segments, original_title="穿书反派", vn_title="Xuyên sách phản diện",
            original_author="远赴人间", vn_author="Lữ khách", total_chapters=199,
        )
        short, _d, _e = build_short_description(
            [(f"{i}:00", f"C.{i}", f"Tên {i}") for i in range(1, 61)],
            total_chapters=199,
        )
        assert description_length(short) < description_length(full) / 2


class TestShortDescriptionExtras:
    """The header/credit lines are offered back — but only when they are free."""

    EXTRAS_BEFORE = ['Tên truyện: 穿书反派 — "Xuyên sách phản diện"', "Tác giả: 远赴人间"]
    EXTRAS_AFTER = ["", "Tạo bởi: Fox Novel"]

    def _build(self, n=3, **kw):
        entries = [(f"{i}:00", f"C.{i}", f"Tên {i}") for i in range(1, n + 1)]
        return build_short_description(
            entries, total_chapters=199,
            extras_before=self.EXTRAS_BEFORE, extras_after=self.EXTRAS_AFTER, **kw
        )

    def test_extras_are_added_when_there_is_room(self):
        text, dropped, kept = self._build()
        assert kept and dropped == 0
        assert text.startswith("Tên truyện: ")
        assert "Tác giả: 远赴人间" in text
        assert text.rstrip().endswith("Tạo bởi: Fox Novel")

    def test_the_index_still_comes_with_them(self):
        text, _, _ = self._build()
        assert "Mục lục chương:" in text
        assert "1:00 C.1 Tên 1" in text

    def test_extras_are_dropped_rather_than_cost_a_chapter(self):
        # Shortening exists to fit more chapters — the header is never bought with one.
        text, _dropped, kept = self._build(n=3, max_chars=90)
        assert not kept
        assert "Tên truyện" not in text
        assert "Tạo bởi" not in text

    def test_dropping_the_extras_keeps_more_chapters(self):
        entries = [(f"{i}:00", f"C.{i}", "ả" * 30) for i in range(1, 200)]
        common = dict(entries=entries, total_chapters=199)
        bare, bare_dropped, _ = build_short_description(**common)
        with_extras, extras_dropped, kept = build_short_description(
            **common, extras_before=self.EXTRAS_BEFORE, extras_after=self.EXTRAS_AFTER
        )
        assert not kept  # they weren't free at this size...
        assert with_extras == bare  # ...so the bare form is what came back
        assert extras_dropped == bare_dropped

    def test_no_extras_asked_for_means_none_kept(self):
        entries = [("0:00", "C.1", "Mở đầu")]
        text, _, kept = build_short_description(entries, total_chapters=1)
        assert not kept
        assert "Tên truyện" not in text

    def test_extras_never_bust_the_limit(self):
        text, _, _ = self._build(n=300)
        assert description_length(text) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT

    def test_the_header_matches_the_full_builders_spelling(self):
        """Same helper, so a shortened description with room reads identically up top."""
        from noveltrans.tts.video import description_header_lines

        lines = description_header_lines(
            original_title="穿书反派", vn_title="Xuyên sách phản diện",
            original_author="远赴人间", vn_author="",
        )
        text, _, kept = build_short_description(
            [("0:00", "C.1", "Mở đầu")], total_chapters=199, extras_before=lines
        )
        assert kept
        assert text.startswith(lines[0] + "\n" + lines[1] + "\n")


class TestDescriptionHeaderLines:
    def test_quotes_the_vietnamese_author(self):
        from noveltrans.tts.video import description_header_lines

        lines = description_header_lines(
            original_title="A", vn_title="B", original_author="C", vn_author="D"
        )
        assert lines == ['Tên truyện: A — "B"', 'Tác giả: C "D"']

    def test_empty_vn_author_drops_the_quoted_clause(self):
        from noveltrans.tts.video import description_header_lines

        lines = description_header_lines(
            original_title="A", vn_title="B", original_author="C", vn_author=""
        )
        assert lines[1] == "Tác giả: C"


class TestLooksGenerated:
    def _full(self, chapters: int = 3) -> str:
        from noveltrans.tts.merge import MergeSegment
        from noveltrans.tts.video import build_video_description

        return build_video_description(
            [MergeSegment(path="", seconds=300, title=f"Chương {i}") for i in range(chapters)],
            original_title="穿书反派", vn_title="Xuyên sách phản diện",
            original_author="远赴人间", vn_author="Lữ khách", total_chapters=199,
        )

    def test_true_for_a_real_generated_description(self):
        assert looks_generated(self._full())

    def test_true_for_a_truncated_generated_description(self):
        text = self._full(500)
        assert was_truncated(text)
        assert looks_generated(text)

    def test_false_for_the_ai_shortened_form(self):
        # The data-loss guard: an AI-shortened description's titles cannot be rebuilt from
        # the database, so the resync must never mistake one for a regenerable sidecar.
        short, _d, _e = build_short_description(
            [("0:00", "C.1", "Mở đầu")], total_chapters=199
        )
        assert not looks_generated(short)

    def test_false_for_arbitrary_text(self):
        assert not looks_generated("chỉ là ghi chú của tôi")

    def test_false_when_the_credit_line_is_missing(self):
        text = self._full().replace("Tạo bởi: Fox Novel", "").strip() + "\n"
        assert not looks_generated(text)

    def test_false_for_empty_text(self):
        assert not looks_generated("")

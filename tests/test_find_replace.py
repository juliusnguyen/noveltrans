"""Tests for the pure find-and-replace core — no GUI, no storage."""

from __future__ import annotations

from noveltrans.find_replace import (
    FIELD_CONTENT,
    FIELD_TITLE,
    FIELD_TRANSLATED,
    FIELD_TRANSLATED_TITLE,
    apply_to_text,
    chapter_count,
    scan,
    total_matches,
)
from noveltrans.models import Chapter


def _chapter(index: int, **fields) -> Chapter:
    return Chapter(index=index, title=fields.pop("title", f"Chương {index + 1}"),
                   url=f"https://x/{index}", **fields)


class TestApplyToText:
    def test_basic_replace_and_count(self):
        assert apply_to_text("a cat and a cat", "cat", "dog", False) == ("a dog and a dog", 2)

    def test_no_match_leaves_text_untouched(self):
        assert apply_to_text("hello", "xyz", "q", False) == ("hello", 0)

    def test_empty_search_is_a_noop(self):
        # Replacing "nothing" is meaningless — must not explode or match between chars.
        assert apply_to_text("hello", "", "X", False) == ("hello", 0)

    def test_case_insensitive_by_default(self):
        assert apply_to_text("Bob and BOB and bob", "bob", "Ann", False) == (
            "Ann and Ann and Ann",
            3,
        )

    def test_case_sensitive_matches_exact_case_only(self):
        assert apply_to_text("Bob and BOB and bob", "bob", "Ann", True) == (
            "Bob and BOB and Ann",
            1,
        )

    def test_replacement_containing_search_does_not_loop(self):
        # "cat" -> "cats" must replace once per match, not re-scan the inserted text.
        assert apply_to_text("cat", "cat", "cats", False) == ("cats", 1)

    def test_backreference_syntax_is_inserted_literally(self):
        # The lambda replacer is the whole point: a bare-string re.sub would read
        # \g<0> / \1 as group references (and raise on a nonexistent group).
        assert apply_to_text("x", "x", r"\g<0>", True) == (r"\g<0>", 1)
        assert apply_to_text("x", "x", r"a\1b", True) == (r"a\1b", 1)

    def test_backslashes_survive(self):
        assert apply_to_text("path", "path", r"C:\temp", True) == (r"C:\temp", 1)


class TestScan:
    def test_returns_only_chapters_with_matches_in_order(self):
        chapters = [
            _chapter(0, translated="Lâm Phong đến"),
            _chapter(1, translated="không có ai"),
            _chapter(2, translated="Lâm Phong lại đến"),
        ]
        matches = scan(chapters, "Lâm Phong", "Diệp Vân", [FIELD_TRANSLATED], case_sensitive=False)
        assert [m.index for m in matches] == [0, 2]  # ch.1 (no match) excluded
        assert matches[1].count == 1
        assert matches[1].changes[0].new == "Diệp Vân lại đến"

    def test_sums_counts_across_selected_fields(self):
        ch = _chapter(0, title="Lâm Phong", translated_title="Lâm Phong dịch",
                      content="Lâm Phong", translated="Lâm Phong Lâm Phong")
        matches = scan(
            [ch], "Lâm Phong", "X",
            [FIELD_TITLE, FIELD_CONTENT, FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE],
            case_sensitive=False,
        )
        assert len(matches) == 1
        # 1 (title) + 1 (content) + 2 (translated) + 1 (translated_title) = 5
        assert matches[0].count == 5
        assert {c.field for c in matches[0].changes} == {
            FIELD_TITLE, FIELD_CONTENT, FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE
        }

    def test_field_selection_excludes_unselected_columns(self):
        ch = _chapter(0, content="Lâm Phong", translated="Lâm Phong")
        # Only scan the translated body — the identical hit in `content` must be ignored.
        matches = scan([ch], "Lâm Phong", "X", [FIELD_TRANSLATED], case_sensitive=False)
        assert matches[0].count == 1
        assert [c.field for c in matches[0].changes] == [FIELD_TRANSLATED]

    def test_empty_search_or_no_fields_returns_nothing(self):
        ch = _chapter(0, translated="Lâm Phong")
        assert scan([ch], "", "X", [FIELD_TRANSLATED], case_sensitive=False) == []
        assert scan([ch], "Lâm Phong", "X", [], case_sensitive=False) == []

    def test_label_omits_a_blank_title(self):
        ch = _chapter(0, title="", translated="Lâm Phong")
        matches = scan([ch], "Lâm Phong", "X", [FIELD_TRANSLATED], case_sensitive=False)
        assert matches[0].label == "Chương 1"  # no trailing " — "

    def test_helpers(self):
        chapters = [
            _chapter(0, translated="a a a"),
            _chapter(1, translated="a"),
            _chapter(2, translated="none"),
        ]
        matches = scan(chapters, "a", "b", [FIELD_TRANSLATED], case_sensitive=True)
        assert total_matches(matches) == 4
        assert chapter_count(matches) == 2

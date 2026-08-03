"""Feature 025 — YouTube tag builders + the LLM-engine `complete()` capability."""

from __future__ import annotations

import pytest

from noveltrans.errors import TranslateError
from noveltrans.tts.tags import (
    YOUTUBE_TAG_CHAR_LIMIT,
    build_tags_prompt,
    build_thumbnail_image_prompt,
    format_tags,
    parse_tags,
)


class TestParseTags:
    def test_splits_and_strips(self):
        assert parse_tags("a, b ,  c") == ["a", "b", "c"]

    def test_dedupes_case_insensitively_keeping_first(self):
        assert parse_tags("Truyện, truyện, TRUYỆN") == ["Truyện"]

    def test_handles_newlines_and_bullets(self):
        assert parse_tags("- a\n2. b\n* c") == ["a", "b", "c"]

    def test_strips_quotes_and_hashes(self):
        assert parse_tags('"a", #b, \'c\'') == ["a", "b", "c"]

    def test_collapses_internal_whitespace(self):
        assert parse_tags("truyện   audio") == ["truyện audio"]

    def test_first_tag_alone_has_no_separator_cost(self):
        # "abcde" alone costs just its own length (5) — no ", " separator yet.
        assert parse_tags("abcde", max_total_chars=5) == ["abcde"]

    def test_every_tag_after_the_first_costs_two_extra_for_the_separator(self):
        # "abcde" (5) fits; adding ", fg" costs 2 + 2 = 4 more, totalling 9 — exactly the
        # length of "abcde, fg". A budget one short of that must drop the second tag.
        assert parse_tags("abcde, fg", max_total_chars=9) == ["abcde", "fg"]
        assert parse_tags("abcde, fg", max_total_chars=8) == ["abcde"]

    def test_single_oversized_tag_is_skipped_not_fatal(self):
        # A tag too long for the whole budget on its own is dropped; scanning continues.
        assert parse_tags("wayyyyytoolong, ok", max_total_chars=5) == ["ok"]

    def test_first_multiword_tag_pays_quote_cost_but_not_separator(self):
        # "aa bb" alone (first tag, no separator yet): 5 (length) + 2 (multi-word "quote"
        # cost) = 7. This is deliberate double-counting headroom — see the module docstring
        # for why a naive char-count model let real tags exceed YouTube's actual 500 budget.
        assert parse_tags("aa bb", max_total_chars=7) == ["aa bb"]
        assert parse_tags("aa bb", max_total_chars=6) == []

    def test_multiword_tag_pays_both_quote_and_separator_cost(self):
        # "aa bb" as the SECOND tag: 5 (length) + 2 (multi-word) + 2 (", " separator) = 9.
        # "c" alone costs 1.
        assert parse_tags("c, aa bb", max_total_chars=10) == ["c", "aa bb"]
        assert parse_tags("c, aa bb", max_total_chars=9) == ["c"]

    def test_trims_to_budget_without_exceeding(self):
        many = ", ".join(f"tag{i}" for i in range(300))
        kept = parse_tags(many, max_total_chars=20)
        assert len(format_tags(kept)) <= 20  # the exact string this app types/writes
        assert kept  # some kept

    def test_default_budget_is_500(self):
        assert YOUTUBE_TAG_CHAR_LIMIT == 500

    def test_empty_input(self):
        assert parse_tags("") == []


class TestBuildTagsPrompt:
    def test_includes_title_and_no_diacritic_instruction(self):
        prompt = build_tags_prompt(vn_title="Người hầu phản diện xuyên sách")
        assert "Người hầu phản diện xuyên sách" in prompt
        assert "KHÔNG DẤU" in prompt

    def test_includes_format_tag_hints(self):
        prompt = build_tags_prompt(vn_title="X")
        assert "truyện audio" in prompt
        assert "review truyện" in prompt

    def test_mentions_the_500_char_budget(self):
        assert "500" in build_tags_prompt(vn_title="X")


class TestFormatTags:
    def test_joins_with_comma_space(self):
        assert format_tags(["a", "b", "c"]) == "a, b, c"


class TestBuildThumbnailImagePrompt:
    def test_includes_title_and_asks_for_english(self):
        prompt = build_thumbnail_image_prompt(vn_title="Người hầu phản diện xuyên sách")
        assert "Người hầu phản diện xuyên sách" in prompt
        assert "TIẾNG ANH" in prompt

    def test_forbids_text_in_the_image(self):
        prompt = build_thumbnail_image_prompt(vn_title="X")
        assert "watermark" in prompt or "không có chữ" in prompt

    def test_includes_description_context(self):
        prompt = build_thumbnail_image_prompt(vn_title="X", vn_description="Một câu chuyện tu tiên.")
        assert "tu tiên" in prompt


class TestEngineCompletionCapability:
    def test_google_cannot_complete(self):
        from noveltrans.translators.google_free import GoogleFreeTranslator

        assert GoogleFreeTranslator.supports_completion is False

    def test_llm_engines_support_completion(self):
        from noveltrans.translators.claude import ClaudeTranslator
        from noveltrans.translators.cli_agent import CliAgentTranslator
        from noveltrans.translators.lmstudio import LmStudioTranslator

        assert CliAgentTranslator.supports_completion is True
        assert ClaudeTranslator.supports_completion is True
        assert LmStudioTranslator.supports_completion is True

    def test_base_complete_rejects_by_default(self):
        # Google inherits the default complete(), which must refuse
        from noveltrans.translators.google_free import GoogleFreeTranslator

        engine = GoogleFreeTranslator()
        with pytest.raises((NotImplementedError, TranslateError)):
            engine.complete("hello")

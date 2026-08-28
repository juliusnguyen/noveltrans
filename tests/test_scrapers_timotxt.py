"""Feature 070 — the timotxt.com adapter.

Two load-bearing tests here, guarding two different silent-corruption paths.

`TestAdapterWiring::test_the_landing_page_is_never_requested_for_chapters` is the first.
timotxt's landing page carries a `開始閱讀` link plus the ~12 newest chapters **newest
first**, and says nothing about being partial — a 376-chapter novel renders as 13 links that
look entirely normal. `ChapterRef.index` is dense and positional and `replace_toc` preserves
content across re-scans, so accepting that list would file chapter 20 at index 1 and chapter
1 at index 0, and a later good scan would rewrite the titles while leaving the wrong bodies
in place. If anyone ever adds a landing-page fallback, that test is what fails.

`TestDeobfuscation` is the second. Chapter bodies arrive with 2-5% of their characters
swapped for visually-similar Hangul syllables, a different random subset every request. The
decoder must undo that without ever being able to touch clean prose — under-correct, never
over-correct — so the negative tests there matter more than the positive ones.

Fixtures are hand-built to the structure measured on the live site (the `name=` OpenGraph
tags, the recent block sitting first in `/dir`, the mid-prose ad slots, the unclassed 溫馨提示
notice) with invented filler text — so the traps are encoded deliberately rather than
captured by luck, and no novel text lives in this repo.
"""

from __future__ import annotations

import pytest
import responses

from noveltrans.errors import ObfuscatedContentError, ScrapeError
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.timotxt import (
    ORIGIN,
    _RESIDUE_REFETCH_MAX,
    _SUBSTITUTIONS,
    TimotxtAdapter,
    bodies_align,
    book_id,
    chapter_numbers,
    deobfuscate,
    dir_url,
    merge_bodies,
    needs_refetch,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    read_url,
    residual_hangul,
    stated_total,
)
from tests.conftest import load_fixture

BID = "2608569069"
LANDING = f"{ORIGIN}/{BID}/"
DIR_URL = f"{ORIGIN}/{BID}/dir"
CHAPTER_URL = f"{ORIGIN}/{BID}/1.html"
ALL_FORMS = [
    LANDING,
    f"https://timotxt.com/{BID}/",
    DIR_URL,
    CHAPTER_URL,
    f"http://timotxt.com/{BID}/12.html",
]


def fx(name: str) -> str:
    return load_fixture("timotxt", name)


def make_adapter() -> TimotxtAdapter:
    return TimotxtAdapter(HttpClient())


class TestUrlDerivation:
    def test_the_book_id_is_read_from_every_url_form(self):
        assert {book_id(u) for u in ALL_FORMS} == {BID}

    def test_every_form_canonicalises_to_one_read_url(self):
        """`Library.find_by_url` is exact string equality, so the landing page, /dir and a
        chapter URL must not become three projects for one novel."""
        assert {read_url(u) for u in ALL_FORMS} == {LANDING}

    def test_the_origin_is_pinned_not_taken_from_the_pasted_url(self):
        assert read_url(f"http://timotxt.com/{BID}/9.html") == LANDING

    def test_the_dir_url_is_derived_from_any_form(self):
        assert {dir_url(u) for u in ALL_FORMS} == {DIR_URL}

    @pytest.mark.parametrize("url", ["", "https://www.timotxt.com/", "not a url"])
    def test_a_non_novel_url_raises(self, url):
        with pytest.raises(ScrapeError):
            book_id(url)


class TestMetadata:
    def test_the_name_attribute_og_tags_are_read(self):
        """This site declares OpenGraph with `name=`, not the standard `property=`. A
        `property=`-only reader finds nothing and silently falls through."""
        meta = parse_metadata(fx("index.html"), LANDING, "timotxt")
        assert meta.title == "測試小說"
        assert meta.author == "測試作者"
        assert meta.site == "timotxt"
        assert meta.source_lang == "zh"
        assert meta.url == LANDING

    def test_the_description_comes_from_the_intro_not_the_truncated_meta_tag(self):
        meta = parse_metadata(fx("index.html"), LANDING, "timotxt")
        assert not meta.description.endswith("…"), "took the SEO-truncated meta tag"
        assert len(meta.description) > 40

    def test_the_cover_is_the_og_image(self):
        meta = parse_metadata(fx("index.html"), LANDING, "timotxt")
        assert meta.cover_url.startswith("https://i1.timotxt.com/")
        assert meta.cover_url.endswith(".jpg")

    def test_the_title_and_author_fall_back_to_the_visible_headings(self):
        meta = parse_metadata(fx("index_no_og.html"), LANDING, "timotxt")
        assert meta.title == "測試小說"
        assert meta.author == "測試作者"

    def test_no_title_anywhere_raises(self):
        with pytest.raises(ScrapeError, match="Novel title not found"):
            parse_metadata(fx("index_no_title.html"), LANDING, "timotxt")

    def test_metadata_is_never_run_through_the_deobfuscator(self):
        """Titles and descriptions come back clean from this site (measured); decoding them
        would be an unforced error with nothing to gain."""
        markup = fx("index.html").replace("測試小說", "測試놖說")
        meta = parse_metadata(markup, LANDING, "timotxt")
        assert "놖" in meta.title


class TestStatedTotal:
    def test_the_total_comes_off_the_latest_chapter_url(self):
        assert stated_total(fx("index.html")) == 20

    def test_the_total_survives_the_latest_chapter_url_disappearing(self):
        """Two independent signals — delete either and the other still answers."""
        markup = fx("index.html").replace(
            '<meta name="og:novel:latest_chapter_url" '
            f'content="https://www.timotxt.com/{BID}/20.html" />',
            "",
        )
        assert stated_total(markup) == 20  # via og:novel:latest_chapter_name

    def test_markup_claiming_nothing_states_nothing(self):
        assert stated_total("<html></html>") is None


class TestChapterList:
    def test_the_duplicate_recent_block_is_not_emitted(self):
        """The /dir page holds 23 anchors for 20 chapters — the recent block is repeated."""
        refs = parse_chapter_list(fx("dir.html"), DIR_URL)
        assert len(refs) == 20
        assert len(refs) != 23

    def test_refs_are_ascending_with_dense_positional_indexes(self):
        """The recent block sits FIRST on the page and runs backwards, so document order
        would put chapter 20 at index 0."""
        refs = parse_chapter_list(fx("dir.html"), DIR_URL)
        assert [r.index for r in refs] == list(range(20))
        assert chapter_numbers(refs) == list(range(1, 21))
        assert refs[0].url.endswith("/1.html")
        assert refs[-1].url.endswith("/20.html")

    def test_titles_keep_the_sites_own_text(self):
        refs = parse_chapter_list(fx("dir.html"), DIR_URL)
        assert refs[0].title == "第1章 測試章節1"

    def test_a_css_refresh_that_drops_the_all_class_still_parses(self):
        refs = parse_chapter_list(fx("dir_no_all_class.html"), DIR_URL)
        assert chapter_numbers(refs) == list(range(1, 21))

    def test_an_unparseable_page_raises(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html><body>nothing</body></html>", DIR_URL)


class TestChapterContent:
    def test_paragraphs_are_separated_by_blank_lines(self):
        body = parse_chapter(fx("chapter.html"), "第1章", CHAPTER_URL)
        paragraphs = body.split("\n\n")
        assert len(paragraphs) == 6
        assert all(p.strip() for p in paragraphs)
        assert "\n\n\n" not in body

    def test_the_mid_prose_ad_block_is_gone_without_eating_prose(self):
        body = parse_chapter(fx("chapter.html"), "第1章", CHAPTER_URL)
        assert "clickforceads" not in body and "holmesmind" not in body
        paragraphs = body.split("\n\n")
        # the ad sat between paragraphs 2 and 3; they must still be adjacent
        assert paragraphs[1].startswith("他") and paragraphs[2].startswith("風")

    def test_the_unclassed_site_notice_is_not_a_paragraph(self):
        """The one piece of chrome no class-based rule can name — it is why extraction
        takes direct-child <p> of div.content rather than every <p> inside it."""
        body = parse_chapter(fx("chapter.html"), "第1章", CHAPTER_URL)
        assert "溫馨提示" not in body
        assert "改版" not in body

    def test_the_breadcrumb_and_nav_never_leak_in(self):
        body = parse_chapter(fx("chapter.html"), "第1章", CHAPTER_URL)
        for chrome in ("首页", "上一章", "下一章", "目錄", "設定"):
            assert chrome not in body

    def test_the_heading_is_not_the_first_line(self):
        """h1.imgtext lives outside div.content, so it is excluded structurally."""
        body = parse_chapter(fx("chapter.html"), "第1章", CHAPTER_URL)
        assert not body.startswith("第1章")

    def test_a_repeated_title_line_is_dropped_at_most_once(self):
        body = parse_chapter(fx("chapter_title_echo.html"), "第1章", CHAPTER_URL)
        assert not body.startswith("第1章")
        assert len(body.split("\n\n")) == 6

    def test_a_missing_container_raises(self):
        with pytest.raises(ScrapeError, match="Chapter content not found"):
            parse_chapter("<html><body>gone</body></html>", "第1章", CHAPTER_URL)

    def test_a_container_holding_only_chrome_raises_a_distinct_error(self):
        with pytest.raises(ScrapeError, match="Chapter content is empty"):
            parse_chapter(fx("chapter_empty.html"), "第1章", CHAPTER_URL)

    def test_failures_never_return_empty_content(self):
        """An empty string would be saved as a downloaded chapter and never retried."""
        for markup in ("<html></html>", fx("chapter_empty.html")):
            with pytest.raises(ScrapeError) as excinfo:
                parse_chapter(markup, "第1章", CHAPTER_URL)
            assert not isinstance(excinfo.value, ObfuscatedContentError)


class TestDeobfuscation:
    def test_the_table_is_one_to_one_and_keyed_only_on_hangul(self):
        from noveltrans.scrapers.timotxt import _SUBSTITUTIONS

        assert all("가" <= k <= "힣" for k in _SUBSTITUTIONS)
        assert all(len(k) == 1 and len(v) == 1 for k, v in _SUBSTITUTIONS.items())
        # no key is also a value, so applying the table twice is a no-op by construction
        assert not set(_SUBSTITUTIONS) & set(_SUBSTITUTIONS.values())

    def test_substituted_characters_are_restored(self):
        assert deobfuscate("놖놛늀놅") == "我他就的"

    def test_clean_prose_is_returned_byte_identical(self):
        """Only Hangul syllables are keys, so Chinese, Latin, digits and punctuation are
        untouchable however incomplete the table is."""
        clean = "他走了，Chapter 12，還有 3 個人。“真的嗎？”"
        assert deobfuscate(clean) == clean

    def test_it_is_idempotent(self):
        once = deobfuscate("놖看著놛，늀這樣。")
        assert deobfuscate(once) == once

    def test_it_preserves_length_and_paragraph_structure(self):
        """TTS chunking and the exporters key off character offsets and blank lines."""
        text = "놖走進院子。\n\n놛沒有說話。"
        out = deobfuscate(text)
        assert len(out) == len(text)
        assert out.count("\n\n") == text.count("\n\n")

    def test_an_unmapped_glyph_survives_verbatim_and_does_not_raise(self):
        """Under-correct, never over-correct: the worst case is the do-nothing baseline."""
        body = parse_chapter(fx("chapter_unknown_glyph.html"), "第1章", CHAPTER_URL)
        assert "뷁" in body
        assert residual_hangul(body) == 1

    def test_the_chapter_body_is_decoded(self):
        body = parse_chapter(fx("chapter.html"), "第1章", CHAPTER_URL)
        assert residual_hangul(body) == 0
        assert body.startswith("我走進院子")
        assert "他沒有說話" in body

    def test_the_reported_missing_mapping_is_covered(self):
        """Feature 071's regression. `꿫` was absent from the 070 table, so it survived into
        a stored chapter and the translator turned it into a plausible-looking Han character
        that appears nowhere in the source."""
        assert "꿫" in _SUBSTITUTIONS
        assert deobfuscate("꿫") == "仍"

    def test_a_wholly_obfuscated_body_raises(self):
        """Past the alarm ratio the scheme itself has changed and nothing can be trusted."""
        markup = fx("chapter.html").replace("놖走進院子，看見一棵老樹。", "뷁" * 40)
        with pytest.raises(ObfuscatedContentError):
            parse_chapter(markup, "第1章", CHAPTER_URL)


class TestRegistry:
    def test_the_adapter_is_registered(self):
        """The one line whose omission ships silently — every timotxt URL would report
        "Chưa hỗ trợ trang web này"."""
        assert TimotxtAdapter in ADAPTERS

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_every_paste_form_is_recognised(self, url):
        assert TimotxtAdapter.matches(url)
        assert isinstance(adapter_for_url(url, HttpClient()), TimotxtAdapter)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.timotxt.com/",
            "https://www.timotxt.com/bookstack/",
            "https://i1.timotxt.com/thumb/120x160/20251031/180656906994.jpg",
            "https://i1.timotxt.com/images/timo.png",
        ],
    )
    def test_non_novel_urls_are_ignored(self, url):
        assert not TimotxtAdapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://sto9.com/book/13908/index.html",
            "https://twkan.com/book/114283/index.html",
            "https://www.69shuba.com/book/59024/",
        ],
    )
    def test_it_does_not_steal_other_sites_urls(self, url):
        assert not TimotxtAdapter.matches(url)

    def test_no_other_adapter_claims_a_timotxt_url(self):
        """Every registered pattern is host-anchored; this proves it stays that way."""
        claimants = [a for a in ADAPTERS if a.matches(CHAPTER_URL)]
        assert claimants == [TimotxtAdapter]

    def test_the_content_is_not_a_translation(self):
        assert TimotxtAdapter.content_is_translated is False


class TestAdapterWiring:
    @responses.activate
    def test_the_landing_page_is_never_requested_for_chapters(self):
        """**The load-bearing test.** A landing-page fallback would return 5 refs, newest
        first, putting chapter 20 at index 0 and chapter 1 nowhere near its own row."""
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, DIR_URL, body=fx("dir.html"))

        refs = make_adapter().fetch_chapter_list(CHAPTER_URL)

        assert len(refs) == 20
        assert refs[0].url.endswith("/1.html")
        assert chapter_numbers(refs) == list(range(1, 21))

    @responses.activate
    def test_a_dead_dir_page_fails_rather_than_falling_back(self):
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, DIR_URL, status=404)

        with pytest.raises(ScrapeError):
            make_adapter().fetch_chapter_list(CHAPTER_URL)

    @responses.activate
    def test_a_dir_page_holding_only_the_recent_block_is_refused(self):
        """3 refs numbered 18-20 is not a prefix — it must never be saved."""
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, DIR_URL, body=fx("dir_only_recent.html"))

        with pytest.raises(ScrapeError, match="không liền mạch"):
            make_adapter().fetch_chapter_list(CHAPTER_URL)

    @responses.activate
    def test_a_gap_in_the_numbering_is_refused(self):
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, DIR_URL, body=fx("dir_gap.html"))

        with pytest.raises(ScrapeError) as excinfo:
            make_adapter().fetch_chapter_list(CHAPTER_URL)
        assert "19" in str(excinfo.value) and "20" in str(excinfo.value)

    @responses.activate
    def test_a_scan_costs_two_requests_and_reuses_the_cached_landing_page(self):
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, DIR_URL, body=fx("dir.html"))

        adapter = make_adapter()
        adapter.fetch_metadata(CHAPTER_URL)
        adapter.fetch_chapter_list(CHAPTER_URL)
        adapter.fetch_metadata(CHAPTER_URL)  # cached — no second landing fetch

        assert len(responses.calls) == 2
        assert [c.request.url for c in responses.calls] == [LANDING, DIR_URL]

    @responses.activate
    def test_a_short_but_contiguous_dir_is_used_and_reported(self):
        """A prefix is safe — replace_toc just extends it — but never silently."""
        short = fx("dir.html")
        for n in range(13, 21):
            short = short.replace(
                f'<li><a href="/{BID}/{n}.html">第{n}章 測試章節{n}</a></li>\n', ""
            )
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, DIR_URL, body=short)

        adapter = make_adapter()
        messages: list[str] = []
        adapter.on_status = messages.append
        refs = adapter.fetch_chapter_list(CHAPTER_URL)

        assert chapter_numbers(refs) == list(range(1, 13))
        assert any("12/20" in m for m in messages)

    @responses.activate
    def test_a_dead_landing_page_does_not_fail_a_good_dir_scan(self):
        """The cross-check is an upgrade, not a safety requirement: contiguity already
        proves the list is a dense prefix from chapter 1."""
        responses.add(responses.GET, LANDING, status=404)
        responses.add(responses.GET, DIR_URL, body=fx("dir.html"))

        refs = make_adapter().fetch_chapter_list(CHAPTER_URL)

        assert chapter_numbers(refs) == list(range(1, 21))

    @responses.activate
    def test_a_chapter_fetches_decodes_and_extracts_end_to_end(self):
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter.html"))

        body = make_adapter().fetch_chapter(
            parse_chapter_list(fx("dir.html"), DIR_URL)[0]
        )

        assert body.startswith("我走進院子")
        assert residual_hangul(body) == 0
        assert "溫馨提示" not in body

    @responses.activate
    def test_residual_hangul_is_reported_not_swallowed(self):
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_unknown_glyph.html"))

        adapter = make_adapter()
        messages: list[str] = []
        adapter.on_status = messages.append
        body = adapter.fetch_chapter(parse_chapter_list(fx("dir.html"), DIR_URL)[0])

        assert "뷁" in body, "the chapter is still returned"
        assert any("chưa giải mã" in m for m in messages)


@pytest.mark.live
class TestLive:
    """Hits timotxt.com. Deselected by default; run with `pytest -m live`."""

    URL = f"https://www.timotxt.com/{BID}/"

    def test_metadata(self):
        meta = make_adapter().fetch_metadata(self.URL)
        assert meta.title and meta.author
        assert meta.source_lang == "zh"
        assert meta.url == read_url(self.URL)

    def test_the_full_list_is_fetched_not_the_landing_excerpt(self):
        refs = make_adapter().fetch_chapter_list(self.URL)
        assert len(refs) >= 376, "got the 12-newest excerpt"
        assert chapter_numbers(refs) == list(range(1, len(refs) + 1))

    def test_the_first_chapter_extracts_cleanly(self):
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        body = adapter.fetch_chapter(refs[0])
        assert len(body) > 1000
        assert "溫馨提示" not in body and "clickforceads" not in body

    def test_the_deobfuscation_table_has_not_drifted(self):
        """The drift detector. The table is empirical, so the failure mode that matters is
        the site rotating to a different one — which shows up as unmapped Hangul.

        Measures a SINGLE draw via `parse_chapter`, deliberately not `fetch_chapter`.
        Since feature 071 the adapter re-fetches until residue is gone, so going through it
        would make this pass no matter how far the table had drifted — the detector would
        still be green and would be testing nothing at all.
        """
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        for ref in refs[:5]:
            body = parse_chapter(adapter.client.get_html(ref.url), ref.title, ref.url)
            assert residual_hangul(body) <= 3, f"{ref.title}: table may have drifted"

    def test_the_refetch_clears_residue(self):
        """The end-to-end proof of feature 071: whatever one draw leaves behind, the
        adapter's retry recovers."""
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        for ref in refs[:3]:
            assert residual_hangul(adapter.fetch_chapter(ref)) == 0, ref.title

    def test_single_draw_residue_stays_rare(self):
        """The retry-cost monitor. Every request the retry spends is the table being
        incomplete, so if this number climbs, re-run scripts/build_timotxt_table.py."""
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        total = sum(
            residual_hangul(parse_chapter(adapter.client.get_html(r.url), r.title, r.url))
            for r in refs[:5]
        )
        assert total < 10, f"{total} undecoded characters across 5 chapters — table is thin"


class TestPositionalMerge:
    """Feature 071 — recovering characters the table missed by comparing two draws.

    The site re-randomises which characters it scrambles on every response, so two draws
    of one chapter are the same text garbled in different places. Neither is "the good
    one", which is why the merge is positional rather than a choice between bodies.
    """

    def test_a_character_garbled_in_a_is_taken_from_b(self):
        assert merge_bodies("뷁走了", "我走了") == "我走了"

    def test_a_character_garbled_in_b_keeps_a(self):
        """The merge is not "take B" — A is the accumulator."""
        assert merge_bodies("我走了", "뷁走了") == "我走了"

    def test_a_character_garbled_in_both_survives_verbatim(self):
        """Never invented. An unmapped glyph neither draw resolved stays as it is."""
        assert merge_bodies("뷁走了", "뷁走了") == "뷁走了"

    def test_a_clean_character_is_never_replaced_by_a_differing_one(self):
        """The never-over-correct guarantee, and the merge's counterpart to
        `test_clean_prose_is_returned_byte_identical`. Only a residual Hangul syllable is
        ever substituted, so a wrong second draw cannot damage a readable first one."""
        assert merge_bodies("他走了", "她走了") == "他走了"

    def test_it_is_not_pick_the_cleaner_body(self):
        """A dirty at one position, B dirty at another — the result is clean and equals
        neither input. Choosing the better of two bodies could never produce this."""
        a, b = "뷁走了，他來了", "我走了，뷂來了"
        merged = merge_bodies(a, b)
        assert merged == "我走了，他來了"
        assert merged != a and merged != b

    def test_mismatched_paragraph_counts_are_not_merged(self):
        a = "뷁走了\n\n他來了"
        assert merge_bodies(a, "我走了") == a

    def test_an_equal_count_but_different_length_is_not_merged(self):
        """Same paragraph count is not enough — the substitution is strictly 1:1, so a
        length change means the chapter itself was edited between the two requests."""
        a = "뷁走了\n\n他來了"
        assert merge_bodies(a, "我走了吧\n\n他來了") == a

    def test_alignment_is_the_gate(self):
        assert bodies_align("abc\n\ndef", "xyz\n\nuvw")
        assert not bodies_align("abc\n\ndef", "abcd\n\ndef")
        assert not bodies_align("abc\n\ndef", "abc")

    def test_structure_is_preserved(self):
        a, b = "뷁走了\n\n他來了", "我走了\n\n他來了"
        merged = merge_bodies(a, b)
        assert len(merged) == len(a)
        assert merged.count("\n\n") == a.count("\n\n")

    def test_two_identical_clean_bodies_merge_to_a_no_op(self):
        assert merge_bodies("我走了", "我走了") == "我走了"

    def test_needs_refetch_is_one_character_not_a_threshold(self):
        """One undecoded glyph is what caused this report — there is no safe amount."""
        assert needs_refetch("我走了뷁") is True
        assert needs_refetch("我走了") is False


class TestResidueRefetch:
    """The adapter re-fetches only when the decoder left something behind."""

    def _adapter(self) -> TimotxtAdapter:
        # delay_seconds=0 or three draws cost 4.5s of real sleep (test_scrapers_medoctruyen).
        return TimotxtAdapter(HttpClient(delay_seconds=0))

    def _ref(self):
        return parse_chapter_list(fx("dir.html"), DIR_URL)[0]

    @responses.activate
    def test_a_clean_chapter_still_costs_exactly_one_request(self):
        """The test that fails if anyone makes the merge the default path."""
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter.html"))

        body = self._adapter().fetch_chapter(self._ref())

        assert len(responses.calls) == 1
        assert residual_hangul(body) == 0

    @responses.activate
    def test_residue_cleared_by_the_second_draw(self):
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_residue_a.html"))
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_residue_b.html"))

        adapter = self._adapter()
        messages: list[str] = []
        adapter.on_status = messages.append
        body = adapter.fetch_chapter(self._ref())

        assert len(responses.calls) == 2
        assert residual_hangul(body) == 0
        assert body.startswith("我走進院子")
        assert not any("⚠️" in m for m in messages), "cleared — nothing to warn about"

    @responses.activate
    def test_residue_in_every_draw_is_capped_and_reported(self):
        for _ in range(4):
            responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_residue_a.html"))

        adapter = self._adapter()
        messages: list[str] = []
        adapter.on_status = messages.append
        body = adapter.fetch_chapter(self._ref())

        assert len(responses.calls) == 1 + _RESIDUE_REFETCH_MAX
        assert residual_hangul(body) == 1, "the chapter is still returned, not dropped"
        assert any("chưa giải mã được" in m for m in messages)

    @responses.activate
    def test_a_misaligned_second_draw_stops_immediately(self):
        """The chapter changed between requests — do not guess-align, and do not spend a
        third request: the accumulator is anchored on draw 1, so later draws misalign too."""
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_residue_a.html"))
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_residue_misaligned.html"))

        adapter = self._adapter()
        body = adapter.fetch_chapter(self._ref())

        assert len(responses.calls) == 2, "did not stop after the misaligned draw"
        assert body.startswith("뷁走進院子"), "draw 1 returned verbatim"

    @responses.activate
    def test_a_failed_retry_keeps_the_draw_we_already_have(self):
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter_residue_a.html"))
        responses.add(responses.GET, CHAPTER_URL, status=404)

        adapter = self._adapter()
        body = adapter.fetch_chapter(self._ref())

        assert body.startswith("뷁走進院子")
        assert residual_hangul(body) == 1

    @responses.activate
    def test_an_obfuscated_first_draw_raises_with_one_request(self):
        """The alarm path must not be swallowed by the retry. A refactor that wrapped the
        whole thing in one `except ScrapeError` would turn the loudest failure silent."""
        markup = fx("chapter.html").replace("놖走進院子，看見一棵老樹。", "뷁" * 40)
        responses.add(responses.GET, CHAPTER_URL, body=markup)

        with pytest.raises(ObfuscatedContentError):
            self._adapter().fetch_chapter(self._ref())

        assert len(responses.calls) == 1

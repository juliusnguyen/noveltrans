"""Feature 062 — the sto9.com adapter.

The load-bearing test is `test_a_truncated_page_with_a_dead_fragment_raises`. sto9's
chapter-list page ships ~35 of a novel's chapters behind a `LoadMore()` button and looks
completely normal doing it — and those entries are not a prefix, they jump from chapter 15
to chapter 167. Saving that list would file late chapters under early `idx` values, and
`replace_toc` preserves content across re-scans, so the wrong body would survive under a
corrected title. The adapter must therefore refuse rather than fall back.

Fixtures are hand-built to the structure measured on the live site (class names, the
unclosed `<li>`s, the mid-prose ad slots, the `&emsp;` indents) with invented filler text —
so the traps are encoded deliberately rather than captured by luck, and no novel text
lives in this repo.
"""

from __future__ import annotations

import pytest
import responses

from noveltrans.errors import ObfuscatedContentError, RateLimitedError, ScrapeError
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.sto9 import (
    ORIGIN,
    Sto9Adapter,
    book_id,
    chapterlist_url,
    detail_url,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    read_url,
    stated_total,
)
from tests.conftest import load_fixture

BID = "13908"
READ_URL = f"https://sto9.com/book/{BID}/index.html"
DETAIL_URL = f"https://sto9.com/book/{BID}.html"
AJAX_URL = f"https://sto9.com/ajax_novels/chapterlist/{BID}.html"
CHAPTER_URL = f"https://sto9.com/txt/{BID}/7671958.html"

# Every form a user might realistically paste.
ALL_FORMS = (DETAIL_URL, READ_URL, CHAPTER_URL)

CH_TITLE = "第一章 測試章節1"


def fx(name: str) -> str:
    return load_fixture("sto9", name)


def make_adapter() -> Sto9Adapter:
    return Sto9Adapter(HttpClient(delay_seconds=0))


def capture_status(adapter: Sto9Adapter) -> list[str]:
    messages: list[str] = []
    adapter.on_status = messages.append
    return messages


class TestUrlDerivation:
    def test_the_book_id_is_read_from_every_url_form(self):
        assert [book_id(u) for u in ALL_FORMS] == [BID, BID, BID]

    def test_a_non_book_url_raises(self):
        with pytest.raises(ScrapeError):
            book_id("https://sto9.com/novels/class/2_1.html")
        with pytest.raises(ScrapeError):
            book_id("")

    def test_every_form_canonicalises_to_one_read_url(self):
        """The property the whole library keys off — find_by_url is string equality, so
        three paste forms must not become three projects."""
        assert {read_url(u) for u in ALL_FORMS} == {READ_URL}

    def test_the_derived_pages_are_stable_across_forms(self):
        assert {detail_url(u) for u in ALL_FORMS} == {DETAIL_URL}
        assert {chapterlist_url(u) for u in ALL_FORMS} == {AJAX_URL}

    def test_the_origin_is_pinned_not_taken_from_the_pasted_url(self):
        # http:// and a www. host must still land on the one canonical string.
        assert read_url(f"http://www.sto9.com/book/{BID}.html") == READ_URL


class TestMetadata:
    def test_the_opengraph_fields_are_read(self):
        meta = parse_metadata(fx("book.html"), READ_URL, "sto9")
        assert meta.title == "劍影孤舟"  # traditional characters round-trip unmangled
        assert meta.author == "無名氏"
        assert meta.description.startswith("這是一段測試用的簡介文字")
        assert meta.site == "sto9"
        assert meta.source_lang == "zh"

    def test_the_stored_url_is_canonical_from_every_form(self):
        assert {parse_metadata(fx("book.html"), u, "sto9").url for u in ALL_FORMS} == {READ_URL}

    def test_the_placeholder_cover_is_treated_as_absent(self):
        """Stored, it renders as a *broken* cover in the EPUB and the video thumbnail."""
        assert parse_metadata(fx("book.html"), READ_URL, "sto9").cover_url == ""

    def test_a_real_cover_is_kept(self):
        # Both directions: a one-sided test passes with the placeholder check deleted.
        cover = parse_metadata(fx("book_cover.html"), READ_URL, "sto9").cover_url
        assert cover.endswith(f"/{BID}s.jpg")

    def test_the_title_falls_back_to_the_visible_book_box(self):
        meta = parse_metadata(fx("book_no_og.html"), READ_URL, "sto9")
        assert meta.title == "劍影孤舟"
        assert meta.author == "無名氏"  # read off the 作者： row

    def test_no_title_anywhere_raises(self):
        with pytest.raises(ScrapeError, match="Novel title not found"):
            parse_metadata(fx("book_no_title.html"), READ_URL, "sto9")


class TestChapterList:
    def test_the_fragment_parses_despite_unclosed_list_items(self):
        """lxml recovers the CMS's unclosed `<li>`s — which is why this is a parse and
        not a regex over the raw markup."""
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        assert len(refs) == 20

    def test_indexes_are_dense_zero_based_and_independent_of_data_num(self):
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        assert [r.index for r in refs] == list(range(20))
        assert refs[0].url == CHAPTER_URL

    def test_titles_keep_the_sites_own_text_minus_the_trailing_space(self):
        # Titles are persisted, exported into the EPUB and read aloud by TTS, so nothing
        # beyond the stray whitespace is normalised away.
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        assert refs[0].title == "第1章 測試章節1"
        assert refs[19].title == "第20章 測試章節20"

    def test_the_truncated_page_parses_but_is_short_and_gappy(self):
        """Exactly the trap: 9 plausible entries whose numbering jumps 5 -> 17."""
        refs = parse_chapter_list(fx("index.html"), ORIGIN)
        assert len(refs) == 9
        assert refs[5].title == "第17章 測試章節17"  # ...sitting at index 5

    def test_the_stated_total_comes_off_the_loadmore_button(self):
        assert stated_total(fx("index.html")) == 20

    def test_the_stated_total_survives_the_button_disappearing(self):
        """Two independent signals — delete either and this fails."""
        markup = fx("index.html").replace('id="loadmore"', 'id="somethingelse"')
        assert stated_total(markup) == 20  # via max(data-num)

    def test_a_complete_page_states_no_more_than_it_shows(self):
        # No #loadmore, and the highest data-num equals the entry count — so nothing
        # signals truncation and the page is safe to use.
        refs = parse_chapter_list(fx("index_full.html"), ORIGIN)
        assert len(refs) == 4
        assert stated_total(fx("index_full.html")) == len(refs)

    def test_markup_claiming_nothing_states_nothing(self):
        assert stated_total("<html><body></body></html>") is None

    def test_an_unparseable_list_raises(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html></html>", ORIGIN)


class TestChapterContent:
    def body(self) -> str:
        return parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)

    def test_paragraphs_are_separated_by_blank_lines(self):
        paragraphs = self.body().split("\n\n")
        assert len(paragraphs) == 6
        assert paragraphs[0].startswith("這是第一段測試文字")

    def test_the_heading_is_not_the_first_line(self):
        assert not self.body().startswith(CH_TITLE)

    def test_the_indent_entities_leave_no_residue(self):
        # &emsp; decodes to U+2003, which strip() already removes — pinning that so
        # nobody "fixes" it with a replace that would also eat in-line em-spaces.
        assert " " not in self.body()
        assert "&emsp;" not in self.body()

    def test_the_mid_prose_ad_slots_are_gone_without_eating_prose(self):
        body = self.body()
        assert "loadAdv" not in body
        # The ad sat between these two; both must survive, and adjacently.
        assert "遠處傳來一陣腳步聲" in body
        assert "他抬頭看了看天色" in body
        paragraphs = body.split("\n\n")
        assert paragraphs.index("遠處傳來一陣腳步聲，越來越近。") == 2

    def test_the_sibling_page_chrome_never_leaks_in(self):
        body = self.body()
        for chrome in ("下一章", "上一章", "目錄", "書籤", "別的推薦書名"):
            assert chrome not in body

    def test_a_stray_triple_break_makes_no_empty_paragraph(self):
        assert "\n\n\n" not in self.body()
        assert all(p.strip() for p in self.body().split("\n\n"))

    def test_a_repeated_title_line_is_dropped_at_most_once(self):
        """Bias hard to under-strip: a duplicated heading is cosmetic, an eaten opening
        line is data loss nobody notices."""
        body = parse_chapter(fx("chapter_title_echo.html"), CH_TITLE, CHAPTER_URL)
        paragraphs = body.split("\n\n")
        assert paragraphs[0] == CH_TITLE  # the second copy survives
        assert paragraphs.count(CH_TITLE) == 1

    def test_a_missing_container_raises(self):
        with pytest.raises(ScrapeError, match="Chapter content not found"):
            parse_chapter("<html><body><p>nothing here</p></body></html>", CH_TITLE, CHAPTER_URL)

    def test_a_container_holding_only_chrome_raises_a_distinct_error(self):
        """A different message on purpose — the two failures need different fixes."""
        with pytest.raises(ScrapeError, match="Chapter content is empty"):
            parse_chapter(fx("chapter_empty.html"), CH_TITLE, CHAPTER_URL)

    def test_failures_never_return_empty_content(self):
        """`save_content(idx, "")` satisfies pending_download's `content = ''` check
        forever after, so the chapter re-queues every run with no error recorded."""
        for markup in ("<html></html>", fx("chapter_empty.html")):
            with pytest.raises(ScrapeError):
                parse_chapter(markup, CH_TITLE, CHAPTER_URL)

    def test_the_error_is_neither_obfuscation_nor_throttling(self):
        # RateLimitedError would cost 8 retries x 60 s per unparseable chapter, and
        # ObfuscatedContentError is reserved for content that cannot be *decoded*.
        with pytest.raises(ScrapeError) as excinfo:
            parse_chapter(fx("chapter_empty.html"), CH_TITLE, CHAPTER_URL)
        assert not isinstance(excinfo.value, (ObfuscatedContentError, RateLimitedError))


class TestRegistry:
    def test_every_paste_form_is_recognised(self):
        assert all(Sto9Adapter.matches(u) for u in ALL_FORMS)

    def test_non_novel_urls_are_ignored(self):
        for url in (
            "https://sto9.com/",
            "https://sto9.com/novels/class/2_1.html",
            f"https://sto9.com/txt/{BID}/end.html",  # the last chapter's 下一章 sentinel
        ):
            assert not Sto9Adapter.matches(url)

    def test_it_does_not_steal_69shubas_chapter_urls(self):
        """Both sites use /txt/<id>/<cid>; without the host anchor whichever imported
        first would silently win."""
        assert not Sto9Adapter.matches("https://www.69shuba.com/txt/59024/12345")

    def test_the_adapter_is_registered(self):
        # The one line whose omission ships silently: every sto9 URL would report
        # "Chưa hỗ trợ trang web này".
        assert Sto9Adapter in ADAPTERS

    def test_urls_resolve_to_this_adapter_and_69shuba_still_resolves_to_its_own(self):
        client = HttpClient(delay_seconds=0)
        assert isinstance(adapter_for_url(READ_URL, client), Sto9Adapter)
        other = adapter_for_url("https://www.69shuba.com/book/59024/", client)
        assert other is not None and other.name == "69shuba"

    def test_the_content_is_not_a_translation(self):
        assert Sto9Adapter.content_is_translated is False


class TestAdapterWiring:
    @responses.activate
    def test_a_scan_reads_the_page_then_the_fragment(self):
        responses.add(responses.GET, READ_URL, body=fx("index.html"))
        responses.add(responses.GET, AJAX_URL, body=fx("chapterlist.html"))

        refs = make_adapter().fetch_chapter_list(READ_URL)

        assert len(refs) == 20  # the complete list, not the page's 9
        assert [c.request.url for c in responses.calls] == [READ_URL, AJAX_URL]

    @responses.activate
    def test_metadata_pasted_as_a_chapter_url_still_reads_the_detail_page(self):
        responses.add(responses.GET, DETAIL_URL, body=fx("book.html"))

        meta = make_adapter().fetch_metadata(CHAPTER_URL)

        assert responses.calls[0].request.url == DETAIL_URL
        assert meta.url == READ_URL

    @responses.activate
    def test_a_full_scan_costs_three_requests(self):
        responses.add(responses.GET, DETAIL_URL, body=fx("book.html"))
        responses.add(responses.GET, READ_URL, body=fx("index.html"))
        responses.add(responses.GET, AJAX_URL, body=fx("chapterlist.html"))

        adapter = make_adapter()
        adapter.fetch_metadata(CHAPTER_URL)
        adapter.fetch_chapter_list(CHAPTER_URL)
        adapter.fetch_metadata(CHAPTER_URL)  # cached — no second detail fetch

        assert len(responses.calls) == 3

    @responses.activate
    def test_a_truncated_page_with_a_dead_fragment_raises(self):
        """★ The load-bearing test. If someone "improves" the adapter by falling back to
        the page, this is what fails: 9 wrongly-numbered chapters would be saved as if
        they were the novel."""
        responses.add(responses.GET, READ_URL, body=fx("index.html"))
        responses.add(responses.GET, AJAX_URL, status=404)

        with pytest.raises(ScrapeError) as excinfo:
            make_adapter().fetch_chapter_list(READ_URL)

        message = str(excinfo.value)
        assert "9/20" in message  # names both numbers, so it reads as a diagnosis
        assert "sto9" in message

    @responses.activate
    def test_a_short_fragment_is_used_but_never_silently(self):
        responses.add(responses.GET, READ_URL, body=fx("index.html"))
        responses.add(responses.GET, AJAX_URL, body=fx("chapterlist_short.html"))

        adapter = make_adapter()
        messages = capture_status(adapter)
        refs = adapter.fetch_chapter_list(READ_URL)

        # Contiguous from chapter 1, so it is safe to store — a re-scan just extends it.
        assert len(refs) == 12
        assert refs[0].title == "第1章 測試章節1"
        assert any("12/20" in m for m in messages)

    @responses.activate
    def test_a_complete_page_is_a_safe_fallback_when_the_fragment_dies(self):
        """The one branch where using the page is legitimate: nothing claims truncation."""
        responses.add(responses.GET, READ_URL, body=fx("index_full.html"))
        responses.add(responses.GET, AJAX_URL, status=404)

        adapter = make_adapter()
        messages = capture_status(adapter)
        refs = adapter.fetch_chapter_list(READ_URL)

        assert len(refs) == 4
        assert messages  # still reported, never silent

    @responses.activate
    def test_nothing_anywhere_raises(self):
        responses.add(responses.GET, READ_URL, body="<html><body></body></html>")
        responses.add(responses.GET, AJAX_URL, status=404)

        with pytest.raises(ScrapeError, match="Chapter list not found"):
            make_adapter().fetch_chapter_list(READ_URL)

    @responses.activate
    def test_a_chapter_fetches_and_extracts_end_to_end(self):
        responses.add(responses.GET, AJAX_URL, body=fx("chapterlist.html"))
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter.html"))

        body = make_adapter().fetch_chapter(refs[0])

        assert body.startswith("這是第一段測試文字")
        assert "loadAdv" not in body


@pytest.mark.live
class TestLive:
    """Drift detector against the real site. Deselected by default."""

    URL = "https://sto9.com/book/13908/index.html"

    def test_metadata(self):
        meta = make_adapter().fetch_metadata(self.URL)
        assert meta.title
        assert meta.source_lang == "zh"
        assert meta.url == self.URL

    def test_the_full_chapter_list_is_fetched_not_the_pages_excerpt(self):
        """The single assertion that catches a regression to the truncated page."""
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        assert len(refs) > 150
        page = adapter.client.get_html(read_url(self.URL))
        assert len(refs) >= (stated_total(page) or 0)

    def test_the_first_and_last_chapters_both_extract(self):
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        for ref in (refs[0], refs[-1]):
            body = adapter.fetch_chapter(ref)
            assert len(body) > 1000
            assert "loadAdv" not in body
            # The last chapter's 下一章 points at the end.html sentinel — it must not
            # have been followed or leaked into the body.
            assert "下一章" not in body

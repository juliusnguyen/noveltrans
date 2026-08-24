"""Feature 061 — the book.qq.com adapter.

The load-bearing test is `test_a_paid_chapter_is_refused_not_extracted`. A paid chapter
returns HTTP 200 with a teaser, so nothing about the response says the fetch failed; if
that teaser were saved it would satisfy `pending_download`'s `content = ''` check forever
after and flow on into translation, TTS, video and the EPUB with nothing looking wrong.

Fixtures are hand-built to the structure measured on the live site (class names, nesting,
and the Nuxt 2 payload encoding) with invented filler text — so the traps are encoded
deliberately rather than captured by luck, and no novel text lives in this repo.
"""

from __future__ import annotations

import pytest
import responses

from noveltrans.errors import AuthRequiredError, DailyLimitError, RateLimitedError, ScrapeError
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.bookqq import (
    BookqqAdapter,
    book_id,
    chapter_entries,
    chapter_number,
    chapter_url,
    detail_url,
    is_paywalled,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
)
from tests.conftest import load_fixture

BID = "58625737"
DETAIL_URL = f"https://book.qq.com/book-detail/{BID}"
CHAPTER_URL = f"https://book.qq.com/book-read/{BID}/3"


def detail() -> str:
    return load_fixture("bookqq", "detail.html")


def free_chapter() -> str:
    return load_fixture("bookqq", "chapter_free.html")


def paid_chapter() -> str:
    return load_fixture("bookqq", "chapter_paid.html")


def make_adapter() -> BookqqAdapter:
    return BookqqAdapter(HttpClient(delay_seconds=0))


class TestPayload:
    def test_every_entry_is_read_in_order(self):
        entries = chapter_entries(detail())
        assert len(entries) == 8
        assert [e["cid"] for e in entries] == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_single_letter_identifiers_resolve_through_the_argument_table(self):
        # `free:b` / `purchased:a` are identifiers, not literals; a=0 and b=1 come from
        # the IIFE's positional arguments.
        entries = chapter_entries(detail())
        assert entries[0]["free"] == 1
        assert entries[0]["purchased"] == 0
        assert entries[4]["free"] == 0

    def test_an_escaped_quote_in_a_chapter_name_survives(self):
        # A `chapterName:"(.*?)"` regex truncates here and silently drops the rest of the
        # chapter list — which is why this is a reader, not a regex.
        entries = chapter_entries(detail())
        assert entries[2]["chapterName"] == '第三章 "客" 至'
        assert len(entries) == 8  # nothing after the escape was lost

    def test_a_purchased_paid_chapter_is_representable(self):
        entries = chapter_entries(detail())
        assert entries[7]["free"] == 0 and entries[7]["purchased"] == 1

    def test_an_unreadable_payload_returns_empty_rather_than_raising(self):
        assert chapter_entries("<html><body>no payload here</body></html>") == []
        assert chapter_entries("") == []

    def test_a_renamed_chapter_key_returns_empty(self):
        assert chapter_entries(load_fixture("bookqq", "detail_no_payload.html")) == []


class TestChapterList:
    def test_refs_are_dense_zero_based_and_positional(self):
        refs, _ = parse_chapter_list(detail(), DETAIL_URL)
        assert [r.index for r in refs] == list(range(8))
        assert all(chapter_number(r.url) == r.index + 1 for r in refs)
        assert refs[0].url == f"https://book.qq.com/book-read/{BID}/1"

    def test_paid_chapters_are_listed_not_filtered(self):
        """A TOC whose length varied with entitlement would re-map existing `idx` values
        onto different content on the next `replace_toc`. That is corruption."""
        refs, entries = parse_chapter_list(detail(), DETAIL_URL)
        assert len(refs) == len(entries) == 8
        assert sum(1 for e in entries if not e["free"]) == 3  # and they are all present

    def test_titles_carry_no_lock_decoration(self):
        # Titles are persisted, exported into the EPUB and read aloud by TTS.
        refs, _ = parse_chapter_list(detail(), DETAIL_URL)
        assert all("🔒" not in r.title and "VIP" not in r.title for r in refs)

    def test_a_free_chapter_after_a_paid_run_keeps_its_position(self):
        refs, entries = parse_chapter_list(detail(), DETAIL_URL)
        assert entries[6]["free"] == 1  # chapter 7, after the paid 5 and 6
        assert refs[6].index == 6
        assert chapter_number(refs[6].url) == 7

    def test_the_html_toc_carries_the_scan_when_the_payload_cannot(self):
        refs, entries = parse_chapter_list(
            load_fixture("bookqq", "detail_no_payload.html"), DETAIL_URL
        )
        assert len(refs) == 8  # scan survives
        assert entries == []  # …without the free/purchased flags

    def test_neither_path_finding_anything_raises(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html><body></body></html>", DETAIL_URL)


class TestMetadata:
    def test_fields_come_off_the_og_tags(self):
        meta = parse_metadata(detail(), DETAIL_URL)
        assert meta.title == "测试书名"
        assert meta.author == "测试作者"
        assert meta.description
        assert meta.cover_url.startswith("https://")

    def test_it_is_a_chinese_source(self):
        meta = parse_metadata(detail(), DETAIL_URL)
        assert meta.source_lang == "zh"
        assert meta.site == "bookqq"

    def test_the_url_is_canonicalised_from_either_paste_form(self):
        # find_by_url is exact string equality, so echoing the pasted URL would make a
        # detail link and a chapter link two projects for one novel.
        assert parse_metadata(detail(), DETAIL_URL).url == DETAIL_URL
        assert parse_metadata(detail(), CHAPTER_URL).url == DETAIL_URL

    def test_a_chinese_title_round_trips_unmangled(self):
        assert parse_metadata(detail(), DETAIL_URL).title == "测试书名"

    def test_no_title_anywhere_raises(self):
        with pytest.raises(ScrapeError, match="title not found"):
            parse_metadata("<html><head></head><body></body></html>", DETAIL_URL)


class TestChapterContent:
    def test_a_free_chapter_extracts_its_paragraphs(self):
        body = parse_chapter(free_chapter(), "第一章 山门", CHAPTER_URL)
        assert len(body.split("\n\n")) == 3
        assert "下一章" not in body  # nav chrome excluded

    def test_the_leading_title_line_is_dropped(self):
        body = parse_chapter(free_chapter(), "第一章 山门", CHAPTER_URL)
        assert not body.startswith("第一章 山门")

    def test_a_header_login_link_does_not_trip_the_gate(self):
        """登录 sits in the header of ordinary pages, so it can never be a trigger —
        using it would refuse all 226 chapters."""
        assert "登录" in free_chapter()
        assert is_paywalled(free_chapter()) is False

    def test_a_paid_chapter_is_refused_not_extracted(self):
        with pytest.raises(AuthRequiredError):
            parse_chapter(paid_chapter(), "第五章 峰回", CHAPTER_URL)

    def test_the_gate_runs_before_extraction(self):
        """The paid fixture demonstrably contains extractable text; it must still raise.
        A refactor that extracted first and judged after would save the teaser."""
        from bs4 import BeautifulSoup

        container = BeautifulSoup(paid_chapter(), "lxml").select_one("#article")
        assert [p.get_text(strip=True) for p in container.select("p") if p.get_text(strip=True)]
        with pytest.raises(AuthRequiredError):
            parse_chapter(paid_chapter(), "第五章 峰回", CHAPTER_URL)

    def test_the_toc_flags_alone_refuse_a_paid_chapter(self):
        # Even given free markup, the entry's own flags are enough to refuse.
        with pytest.raises(AuthRequiredError):
            parse_chapter(
                free_chapter(), "x", CHAPTER_URL, entry={"free": 0, "purchased": 0}
            )

    def test_a_purchased_chapter_passes_the_flag_gate(self):
        # Forward compatibility for cookie support: `free == 0 and purchased == 0` is a
        # conjunction precisely so a bought chapter falls through without a code change.
        body = parse_chapter(
            free_chapter(), "第一章 山门", CHAPTER_URL, entry={"free": 0, "purchased": 1}
        )
        assert body

    def test_length_is_never_a_refusal_criterion(self):
        """A genuinely short free chapter must extract, not be mistaken for a teaser."""
        short = free_chapter().replace(
            "<p>山门之外落着薄雪，石阶一路向上，看不见尽头。</p>", ""
        ).replace("<p>他把手里的伞收起来，抖了抖上面的雪，抬头望了一眼。</p>", "")
        body = parse_chapter(short, "第一章 山门", CHAPTER_URL)
        assert body and len(body) < 40

    def test_a_missing_container_raises_a_different_error(self):
        with pytest.raises(ScrapeError, match="content not found"):
            parse_chapter("<html><body><p>x</p></body></html>", "t", CHAPTER_URL)

    def test_an_empty_container_raises_rather_than_returning_empty(self):
        """Returning "" would leave the chapter pending forever with no error recorded."""
        with pytest.raises(ScrapeError, match="content is empty"):
            parse_chapter(
                '<html><body><div id="article"></div></body></html>', "t", CHAPTER_URL
            )

    def test_the_paywall_error_is_not_retried_or_fatal(self):
        """RateLimitedError would be retried 8× at 60 s — ~20 hours over 149 chapters.
        DailyLimitError would break the batch and lose the later free chapters."""
        assert issubclass(AuthRequiredError, ScrapeError)
        assert not issubclass(AuthRequiredError, RateLimitedError)
        assert not issubclass(AuthRequiredError, DailyLimitError)


class TestUrlDerivation:
    def test_book_id_reads_both_forms(self):
        assert book_id(DETAIL_URL) == BID
        assert book_id(CHAPTER_URL) == BID

    def test_detail_url_normalises_both_forms(self):
        assert detail_url(DETAIL_URL) == detail_url(CHAPTER_URL) == DETAIL_URL

    def test_chapter_url_and_number_round_trip(self):
        assert chapter_number(chapter_url(BID, 7)) == 7

    def test_a_non_book_url_raises(self):
        with pytest.raises(ScrapeError):
            book_id("https://book.qq.com/category/wuxia")


class TestRegistry:
    def test_it_matches_both_url_forms(self):
        assert BookqqAdapter.matches(DETAIL_URL)
        assert BookqqAdapter.matches(CHAPTER_URL)

    def test_it_ignores_other_pages_and_hosts(self):
        assert not BookqqAdapter.matches("https://book.qq.com/")
        assert not BookqqAdapter.matches("https://book.qq.com/category/wuxia")
        assert not BookqqAdapter.matches("https://www.69shuba.com/book/59024/")

    def test_adapter_for_url_resolves_to_this_adapter(self):
        assert isinstance(adapter_for_url(CHAPTER_URL, HttpClient(delay_seconds=0)), BookqqAdapter)

    def test_the_adapter_is_in_the_default_registry(self):
        """A forgotten line in `_import_adapters()` ships silently — every URL would just
        report "Chưa hỗ trợ trang web này"."""
        assert BookqqAdapter in ADAPTERS

    def test_content_is_not_flagged_as_pre_translated(self):
        """Flipping this would land Chinese in `translated` and mark chapters already
        translated, so the user's Vietnamese output would be Chinese."""
        assert BookqqAdapter.content_is_translated is False


class TestAdapterWiring:
    @responses.activate
    def test_metadata_and_chapter_list_share_one_request(self):
        responses.get(DETAIL_URL, body=detail())
        adapter = make_adapter()
        meta = adapter.fetch_metadata(DETAIL_URL)
        refs = adapter.fetch_chapter_list(DETAIL_URL)
        assert meta.title == "测试书名"
        assert len(refs) == 8
        assert len(responses.calls) == 1

    @responses.activate
    def test_scanning_from_a_chapter_url_fetches_the_detail_page(self):
        responses.get(DETAIL_URL, body=detail())
        assert make_adapter().fetch_metadata(CHAPTER_URL).title == "测试书名"
        assert responses.calls[0].request.url == DETAIL_URL

    @responses.activate
    def test_the_free_paid_split_is_reported_at_scan_time(self):
        responses.get(DETAIL_URL, body=detail())
        adapter = make_adapter()
        seen: list[str] = []
        adapter.on_status = seen.append
        adapter.fetch_chapter_list(DETAIL_URL)
        assert seen and "trả phí" in seen[0]
        # 2, not 3: chapter 8 is free=0 but purchased=1, so it IS downloadable. The
        # count is "neither free nor bought", which is what the user cannot get.
        assert "2/8" in seen[0]
        assert "6 chương" in seen[0]

    @responses.activate
    def test_a_paid_chapter_costs_no_request_after_a_scan(self):
        responses.get(DETAIL_URL, body=detail())
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(DETAIL_URL)
        before = len(responses.calls)
        with pytest.raises(AuthRequiredError):
            adapter.fetch_chapter(refs[4])  # chapter 5: free=0, purchased=0
        assert len(responses.calls) == before  # refused without spending a request

    @responses.activate
    def test_a_free_chapter_is_fetched_and_extracted(self):
        responses.get(DETAIL_URL, body=detail())
        responses.get(chapter_url(BID, 1), body=free_chapter())
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(DETAIL_URL)
        assert len(adapter.fetch_chapter(refs[0]).split("\n\n")) == 3

    @responses.activate
    def test_an_unavailable_toc_still_lets_the_page_gate_decide(self):
        """Best-effort G1: a detail page we cannot fetch must never fail a chapter the
        page-level gate would have let through."""
        responses.get(DETAIL_URL, status=500)
        responses.get(chapter_url(BID, 1), body=free_chapter())
        adapter = make_adapter()
        ref = parse_chapter_list(detail(), DETAIL_URL)[0][0]
        assert adapter.fetch_chapter(ref)  # no TOC, free page → still extracted


@pytest.mark.live
class TestLive:
    """Drift detector against the real site. Excluded by default (`-m 'not live'`)."""

    def test_metadata_and_toc(self):
        adapter = make_adapter()
        meta = adapter.fetch_metadata(DETAIL_URL)
        assert meta.title and meta.source_lang == "zh"
        assert len(adapter.fetch_chapter_list(DETAIL_URL)) > 200

    def test_the_first_chapter_is_free_and_the_last_is_not(self):
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(DETAIL_URL)
        assert len(adapter.fetch_chapter(refs[0])) > 2000
        with pytest.raises(AuthRequiredError):
            adapter.fetch_chapter(refs[-1])

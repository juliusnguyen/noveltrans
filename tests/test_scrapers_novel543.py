"""Feature 081 — the novel543.com adapter.

Four tests carry this module, and each pins a failure that looks like nothing:

`test_the_duplicate_newest_block_never_reaches_the_list` — `/dir` ships a newest-first
duplicate block ahead of the real `ul.all` (12 entries live, 7 in the fixture, so no
hardcoded count can pass by luck). A flat scrape over-counts AND puts the last chapters at
the front; `ChapterRef.index` is dense and positional and `replace_toc` preserves content
across re-scans, so a later good scan would rewrite the titles while leaving the wrong
bodies in place.

`test_a_paginated_chapter_fetches_both_halves` / `test_an_unpaginated_chapter_fetches_
exactly_one_page` — the `(N/M)` suffix appears on some chapters and not others. Assuming one
page loses half of most chapters silently; assuming two 404s or duplicates on the rest.

`test_the_next_chapter_link_is_never_followed` — on the last sub-page 下一章 points at the
next *chapter*, and on an unpaginated chapter it points at the page itself. A chain that
runs off the end of a chapter is the bqg5-mobile trap, and `replace_toc` makes it permanent.

`test_the_latest_chapter_url_is_never_fetched` — the site advertises a chapter URL that is a
**404**. `timotxt.stated_total` reads exactly that tag and `bqg5` fetches it as a
cross-check, so this is the test that stops the obvious "harmonise the siblings" edit.

The browser is faked throughout — no test here launches Chrome or touches the network.

Fixtures are hand-built to the structure measured on the live site (the `all` class, the
variable-length duplicate block, drifting 章 numbers, mid-prose ad slots, the rotating
溫馨提示 notice in its unclassed `<div>`, and the truncated `meta[name=description]`) with
invented filler text — so the traps are encoded deliberately rather than captured by luck,
and no novel text lives in this repo.
"""

from __future__ import annotations

import pytest

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSessionError
from noveltrans.config import DEFAULT_REQUEST_DELAY
from noveltrans.errors import ObfuscatedContentError, RateLimitedError, ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.novel543 import (
    ORIGIN,
    _MIN_DELAY_SECONDS,
    Novel543Adapter,
    book_id,
    chapter_positions,
    dir_url,
    page_count,
    page_position,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    read_url,
    stated_total,
    sub_page_url,
)
from tests.conftest import load_fixture

BID = "0802691255"
INNER = "8096"
N = 20  # chapters in the fixture novel

READ_URL = f"https://www.novel543.com/{BID}/"
DIR_URL = f"https://www.novel543.com/{BID}/dir"
BARE_URL = f"https://www.novel543.com/{BID}"
NO_WWW_URL = f"https://novel543.com/{BID}/"

# Every form a user might realistically paste. All five must fold to ONE identity string.
ALL_FORMS = (BARE_URL, READ_URL, NO_WWW_URL, DIR_URL, f"{ORIGIN}/{BID}/{INNER}_12.html")

TITLE = "星河歸客"
AUTHOR = "無名氏"
COVER = "https://i2.novel543.com/thumb/120x160/20260511/100459125587.jpg"

# The 404 URL the site advertises: no internal id. Nothing may ever fetch this.
ADVERTISED_LATEST = f"{ORIGIN}/{BID}/{N}.html"


def ch_url(position: int, page: int = 1) -> str:
    if page == 1:
        return f"{ORIGIN}/{BID}/{INNER}_{position}.html"
    return f"{ORIGIN}/{BID}/{INNER}_{position}_{page}.html"


def ch_number(position: int) -> int:
    """The fixture's 章 number for a position — deliberately drifting past 10."""
    return position if position <= 10 else position - 1


def ch_title(position: int) -> str:
    return f"第{ch_number(position)}章 星圖上的第{position}個座標"


def fx(name: str) -> str:
    return load_fixture("novel543", name)


class _FakeSession:
    """Stands in for BrowserSession: serves fixtures by URL, records what was asked."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []
        self.closed = False

    def get_html(self, url: str) -> str:
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"adapter fetched an unexpected URL: {url}")
        return self.pages[url]

    def close(self) -> None:
        self.closed = True


def make_adapter(**overrides: str) -> tuple[Novel543Adapter, _FakeSession]:
    pages = {
        READ_URL: fx("detail.html"),
        DIR_URL: fx("dir.html"),
        ch_url(1): fx("chapter_p1of2.html"),
        ch_url(1, 2): fx("chapter_p2of2.html"),
        ch_url(12): fx("chapter_single.html"),
        # Present but must NEVER be requested while fetching chapter 1 — the last
        # sub-page's 下一章 points straight at it.
        ch_url(2): fx("chapter_two_p1of2.html"),
    }
    pages.update(overrides)
    adapter = Novel543Adapter(HttpClient(delay_seconds=0))
    session = _FakeSession(pages)
    adapter._session = session  # never launches a browser
    return adapter, session


def capture_status(adapter: Novel543Adapter) -> list[str]:
    messages: list[str] = []
    adapter.on_status = messages.append
    return messages


def ref_for(position: int) -> ChapterRef:
    return ChapterRef(index=position - 1, title=ch_title(position), url=ch_url(position))


class TestUrlDerivation:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_book_id_from_every_paste_form(self, url):
        assert book_id(url) == BID

    def test_book_id_from_a_sub_page_url(self):
        assert book_id(ch_url(4, 2)) == BID

    @pytest.mark.parametrize("url", ["", "https://example.com/1234", "not a url"])
    def test_book_id_raises_on_a_non_novel_url(self, url):
        with pytest.raises(ScrapeError):
            book_id(url)

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_every_paste_form_folds_to_one_identity(self, url):
        # `Library.find_by_url` is exact string equality, so anything else here makes one
        # novel several projects, each with its own translation progress.
        assert read_url(url) == READ_URL

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_dir_url_from_every_paste_form(self, url):
        assert dir_url(url) == DIR_URL

    def test_sub_page_url_is_built_from_the_stem(self):
        assert sub_page_url(ch_url(1), 2) == ch_url(1, 2)
        assert sub_page_url(ch_url(120), 3) == ch_url(120, 3)

    def test_sub_page_url_refuses_a_sub_page_as_its_input(self):
        # Otherwise "page 3 of page 2" would silently produce 8096_1_2_3.html.
        with pytest.raises(ScrapeError):
            sub_page_url(ch_url(1, 2), 3)

    def test_sub_page_url_refuses_a_non_chapter_url(self):
        with pytest.raises(ScrapeError):
            sub_page_url(DIR_URL, 2)


class TestMetadata:
    def test_reads_the_opengraph_block(self):
        meta = parse_metadata(fx("detail.html"), BARE_URL, "novel543")
        assert meta.title == TITLE
        assert meta.author == AUTHOR
        assert meta.site == "novel543"
        assert meta.source_lang == "zh"

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_url_is_always_canonical(self, url):
        assert parse_metadata(fx("detail.html"), url, "novel543").url == READ_URL

    def test_the_description_is_the_intro_not_the_truncated_meta_tag(self):
        # The meta tag is SEO copy wrapped around a blurb CUT OFF with an ellipsis, so no
        # prefix-stripping recovers the tail. This fails the moment anyone swaps it in.
        description = parse_metadata(fx("detail.html"), BARE_URL, "novel543").description
        assert "無彈窗最新章節" not in description
        assert not description.endswith("…")
        assert description.endswith("到底留下了什麼。")
        # Longer than what the meta tag holds — the tail the truncation threw away.
        assert len(description) > 90

    def test_og_tags_are_read_from_name_as_well_as_property(self):
        # timotxt — the same CMS — uses `name=`. Both spellings must work.
        markup = fx("detail.html").replace('<meta name="og:', '<meta property="og:')
        assert parse_metadata(markup, BARE_URL, "novel543").title == TITLE

    def test_falls_back_to_the_detail_section(self):
        meta = parse_metadata(fx("detail_no_og.html"), BARE_URL, "novel543")
        assert meta.title == TITLE
        assert meta.author == AUTHOR
        assert meta.cover_url == COVER

    def test_raises_when_no_title_anywhere(self):
        with pytest.raises(ScrapeError, match="title"):
            parse_metadata(fx("detail_no_title.html"), BARE_URL, "novel543")

    def test_cover_url_is_stored_unmodified(self):
        # Asserted explicitly so that anyone adding a placeholder-blanking rule has to add
        # its fixture too, rather than silently eating a real cover.
        assert parse_metadata(fx("detail.html"), BARE_URL, "novel543").cover_url == COVER


class TestChapterList:
    def test_the_duplicate_newest_block_never_reaches_the_list(self):
        refs = parse_chapter_list(fx("dir.html"), BID, DIR_URL)
        assert len(refs) == N  # not N + 7
        assert len({r.url for r in refs}) == N
        assert refs[0].title == ch_title(1)
        assert refs[0].url == ch_url(1)
        assert refs[-1].url == ch_url(N)

    def test_index_is_dense_and_positional(self):
        refs = parse_chapter_list(fx("dir.html"), BID, DIR_URL)
        assert [r.index for r in refs] == list(range(N))

    def test_positions_come_from_the_url_not_the_title(self):
        # The fixture's 章 numbers drift past position 10, exactly as the live site's do
        # (position 439 is 第438章). Nothing may refuse a list over that.
        refs = parse_chapter_list(fx("dir.html"), BID, DIR_URL)
        assert refs[-1].title.startswith(f"第{N - 1}章")
        assert chapter_positions(refs) == list(range(1, N + 1))

    def test_a_foreign_books_anchor_is_not_a_chapter(self):
        refs = parse_chapter_list(fx("dir_foreign_anchor.html"), BID, DIR_URL)
        assert not any("0999999999" in r.url for r in refs)

    def test_a_sub_page_anchor_is_not_a_chapter(self):
        # A sub-page is part of a chapter, never a chapter of its own.
        refs = parse_chapter_list(fx("dir_foreign_anchor.html"), BID, DIR_URL)
        assert len(refs) == N
        assert not any(r.url == ch_url(4, 2) for r in refs)

    def test_the_flat_fallback_comes_out_ascending(self):
        # The recent block leads the page and runs backwards, so document order would put
        # the last chapters at the front. Sorting by position is what makes this safe.
        refs = parse_chapter_list(fx("dir_no_all_class.html"), BID, DIR_URL)
        assert len(refs) == N
        assert chapter_positions(refs) == list(range(1, N + 1))
        assert refs[0].url == ch_url(1)

    def test_stated_total_reads_the_info_section(self):
        assert stated_total(fx("dir.html")) == N

    def test_stated_total_is_none_when_nothing_is_claimed(self):
        assert stated_total(fx("detail.html")) is None

    def test_raises_when_there_is_no_list(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list(fx("chapter_empty.html"), BID, DIR_URL)

    def test_the_detail_pages_start_reading_button_is_chapter_shaped(self):
        """Why `fetch_chapter_list` never falls back to the detail page.

        The 開始閱讀 button is a real `/<bid>/<inner>_1.html` anchor, so the flat fallback
        would happily read the detail page as a ONE-chapter novel — and `[1]` is
        contiguous, so the guard downstream could not catch it either. `fetch_chapter_list`
        fetches `/dir` and nothing else, which is what makes that unreachable.
        """
        refs = parse_chapter_list(fx("detail.html"), BID, READ_URL)
        assert [r.url for r in refs] == [ch_url(1)]
        assert chapter_positions(refs) == [1]


class TestContiguityGuard:
    """The check that carries this module — see `fetch_chapter_list`."""

    def test_a_gap_in_the_positions_raises(self):
        adapter, _ = make_adapter(**{DIR_URL: fx("dir_gap.html")})
        with pytest.raises(ScrapeError) as excinfo:
            adapter.fetch_chapter_list(BARE_URL)
        assert "19" in str(excinfo.value) and "20" in str(excinfo.value)

    def test_a_list_that_is_only_the_recent_block_raises(self):
        # This is the failure that would file chapter 20's body under index 0 — and
        # `replace_toc` would make it permanent.
        adapter, _ = make_adapter(**{DIR_URL: fx("dir_only_recent.html")})
        with pytest.raises(ScrapeError, match="không liền mạch"):
            adapter.fetch_chapter_list(BARE_URL)

    def test_a_short_but_contiguous_list_is_kept_and_reported(self):
        # Contiguity already proves it is a prefix, so a later re-scan just extends it.
        # Usable, but never silently — `stated_total` is a report, not a gate.
        short = fx("dir.html").replace("章節： 20", "章節： 40")
        adapter, _ = make_adapter(**{DIR_URL: short})
        messages = capture_status(adapter)
        refs = adapter.fetch_chapter_list(BARE_URL)
        assert len(refs) == N
        assert any("20/40" in m for m in messages)

    def test_a_list_longer_than_the_stated_total_is_not_refused(self):
        # The bqg5 lesson: a stated number never gets to refuse a provably-good list.
        stale = fx("dir.html").replace("章節： 20", "章節： 12")
        adapter, _ = make_adapter(**{DIR_URL: stale})
        assert len(adapter.fetch_chapter_list(BARE_URL)) == N


class TestPageCount:
    def test_a_paginated_chapter_reports_its_page_count(self):
        assert page_count(fx("chapter_p1of2.html"), ch_url(1)) == 2
        assert page_position(fx("chapter_p1of2.html")) == (1, 2)
        assert page_position(fx("chapter_p2of2.html")) == (2, 2)

    def test_an_unpaginated_chapter_reports_one(self):
        assert page_count(fx("chapter_single.html"), ch_url(12)) == 1
        assert page_position(fx("chapter_single.html")) is None

    def test_a_page_with_no_heading_raises_rather_than_reading_one(self):
        # An <h1> that exists without a suffix means one page. An <h1> that has VANISHED
        # means the layout changed, and reading that as "one page" would silently drop the
        # second half of every paginated chapter on the site.
        with pytest.raises(ScrapeError, match="no heading"):
            page_count(fx("chapter_no_heading.html"), ch_url(1))

    def test_the_title_is_a_second_signal_when_the_h1_is_gone(self):
        markup = fx("chapter_p1of2.html").replace(f"<h1>{ch_title(1)} (1/2)</h1>", "")
        assert page_count(markup, ch_url(1)) == 2

    def test_an_absurd_page_count_raises(self):
        markup = fx("chapter_p1of2.html").replace("(1/2)", "(1/9999)")
        with pytest.raises(ScrapeError, match="Implausible"):
            page_count(markup, ch_url(1))

    def test_a_bracketed_number_inside_a_title_is_not_a_page_suffix(self):
        # The regex is end-anchored, so only the trailing fraction counts.
        markup = fx("chapter_p1of2.html").replace(
            f"<h1>{ch_title(1)} (1/2)</h1>", "<h1>第5章 (3/4)的祕密 (1/2)</h1>"
        )
        assert page_position(markup) == (1, 2)

    def test_full_width_parentheses_are_accepted(self):
        markup = fx("chapter_p1of2.html").replace("(1/2)", "（1/2）")
        assert page_position(markup) == (1, 2)


class TestChapterContent:
    def test_paragraphs_are_separated_by_blank_lines(self):
        body = parse_chapter(fx("chapter_p1of2.html"), ch_title(1), ch_url(1))
        assert "\n\n" in body
        assert body.startswith("港務官把航行紀錄")

    def test_ad_slots_are_dropped_without_eating_prose(self):
        # The slots sit BETWEEN paragraphs, so a blanket <div> decompose would split the
        # prose. The surrounding paragraphs must survive and stay adjacent.
        body = parse_chapter(fx("chapter_p1of2.html"), ch_title(1), ch_url(1))
        assert "adBlock" not in body and "gadBlock" not in body
        assert "港務官把航行紀錄推到燈下，紙面上的座標一行行浮現出來。\n\n那些數字他認得" in body

    def test_the_rotating_notice_is_dropped_structurally_not_by_string(self):
        # The two sub-pages carry DIFFERENTLY worded notices, so nothing here can be
        # passing by matching a literal string.
        for name, title in (
            ("chapter_p1of2.html", ch_title(1)),
            ("chapter_p2of2.html", ""),
            ("chapter_single.html", ch_title(12)),
        ):
            body = parse_chapter(fx(name), title, ch_url(1))
            assert "溫馨提示" not in body

    def test_navigation_never_leaks_into_the_body(self):
        body = parse_chapter(fx("chapter_p1of2.html"), ch_title(1), ch_url(1))
        assert "下一章" not in body and "目錄" not in body

    def test_an_empty_p_does_not_become_a_blank_paragraph(self):
        body = parse_chapter(fx("chapter_p1of2.html"), ch_title(1), ch_url(1))
        assert "\n\n\n" not in body
        assert not body.endswith("\n")

    def test_no_indent_characters_survive(self):
        body = parse_chapter(fx("chapter_p1of2.html"), ch_title(1), ch_url(1))
        assert "　" not in body and "\xa0" not in body

    def test_a_title_echo_is_dropped(self):
        body = parse_chapter(fx("chapter_title_echo.html"), ch_title(12), ch_url(12))
        assert not body.startswith(ch_title(12))

    def test_a_non_matching_title_never_strips_the_first_line(self):
        body = parse_chapter(fx("chapter_p1of2.html"), "完全不同的標題", ch_url(1))
        assert body.startswith("港務官把航行紀錄")

    def test_an_empty_title_never_strips_the_first_line(self):
        # `fetch_chapter` passes "" for every sub-page, so this is the sub-page path.
        body = parse_chapter(fx("chapter_p2of2.html"), "", ch_url(1, 2))
        assert body.startswith("第二天清晨")

    def test_raises_when_the_container_is_missing(self):
        with pytest.raises(ScrapeError, match="Chapter content not found"):
            parse_chapter(fx("detail.html"), "", ch_url(1))

    def test_raises_when_the_body_is_empty(self):
        with pytest.raises(ScrapeError, match="Chapter content is empty"):
            parse_chapter(fx("chapter_empty.html"), "", ch_url(1))

    def test_empty_content_is_not_reported_as_obfuscated(self):
        # Nothing on this site is obfuscated — that is timotxt's problem, on the same CMS.
        # ObfuscatedContentError has GUI meaning and would offer a repair that cannot help.
        with pytest.raises(ScrapeError) as excinfo:
            parse_chapter(fx("chapter_empty.html"), "", ch_url(1))
        assert not isinstance(excinfo.value, ObfuscatedContentError)
        assert not isinstance(excinfo.value, RateLimitedError)


class TestRegistry:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_matches_every_paste_form(self, url):
        assert Novel543Adapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.novel543.com/",
            "https://www.novel543.com/bookstack/",
            "https://www.novel543.com/ranking.html",
            COVER,  # the image host must not be claimed
        ],
    )
    def test_rejects_non_novel_urls(self, url):
        assert not Novel543Adapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.timotxt.com/2608569069/",
            "https://www.timotxt.com/2608569069/12.html",
            "https://twkan.com/book/114283.html",
            "https://twkan.com/txt/114283/57238545",
            "https://www.sto9.com/book/12345/",
            "https://69shuba.cx/book/54321.htm",
            "https://m.bqg5.com/4_4217/",
        ],
    )
    def test_does_not_steal_a_neighbours_urls(self, url):
        assert not Novel543Adapter.matches(url)

    def test_neighbours_still_resolve_to_their_own_adapters(self):
        # timotxt especially: the same CMS and the same /<10-digit id>/ shape, differing
        # only by host. ADAPTERS is first-match-wins by import order, so a loose pattern
        # here would steal them silently.
        client = HttpClient(delay_seconds=0)
        for url, expected in (
            ("https://www.timotxt.com/2608569069/", "timotxt"),
            ("https://twkan.com/book/114283.html", "twkan"),
            (BARE_URL, "novel543"),
        ):
            adapter = adapter_for_url(url, client)
            assert adapter is not None and adapter.name == expected

    def test_the_adapter_is_registered(self):
        assert Novel543Adapter in ADAPTERS

    def test_adapter_for_url_resolves_to_this_adapter(self):
        adapter = adapter_for_url(BARE_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, Novel543Adapter)

    def test_content_is_not_pre_translated(self):
        # Flipping this would land Chinese in `translated` and mark chapters done.
        assert Novel543Adapter.content_is_translated is False


class TestAdapterWiring:
    def test_a_paginated_chapter_fetches_both_halves(self):
        adapter, session = make_adapter()
        body = adapter.fetch_chapter(ref_for(1))
        assert session.requested == [ch_url(1), ch_url(1, 2)]
        # Both halves, in order.
        assert body.index("港務官把航行紀錄") < body.index("第二天清晨")

    def test_an_unpaginated_chapter_fetches_exactly_one_page(self):
        adapter, session = make_adapter()
        adapter.fetch_chapter(ref_for(12))
        assert session.requested == [ch_url(12)]

    def test_the_next_chapter_link_is_never_followed(self):
        # Chapter 2's page IS in the fake's map, and the last sub-page's 下一章 points at
        # it. Following that link is how a chapter silently absorbs the next one.
        adapter, session = make_adapter()
        adapter.fetch_chapter(ref_for(1))
        assert ch_url(2) not in session.requested

    def test_a_sub_page_that_is_really_page_one_again_raises(self):
        """★ Measured on the live site, both shapes, and neither is an error response.

        An out-of-range sub-page serves PAGE 1 AGAIN with a 200 — `/8096_1_3.html` came
        back as page 1 of chapter 1 still carrying "(1/2)". It parses perfectly as prose,
        so without the `(k/M)` self-check it would be appended into the middle of the
        chapter, silently and permanently.
        """
        adapter, _ = make_adapter(**{ch_url(1, 2): fx("chapter_p1of2.html")})
        with pytest.raises(ScrapeError, match="sai trang"):
            adapter.fetch_chapter(ref_for(1))

    def test_a_sub_page_with_no_suffix_at_all_raises(self):
        # The other measured shape: `/8096_120_2.html`, for a chapter that has no second
        # page, returned page 1 of chapter 120 with no suffix at all. `page_position` is
        # None there, which must still not compare equal to (2, 2).
        adapter, _ = make_adapter(**{ch_url(1, 2): fx("chapter_single.html")})
        with pytest.raises(ScrapeError, match="sai trang"):
            adapter.fetch_chapter(ref_for(1))

    def test_a_failing_sub_page_never_saves_half_a_chapter(self):
        adapter, _ = make_adapter(**{ch_url(1, 2): fx("chapter_empty.html")})
        with pytest.raises(ScrapeError):
            adapter.fetch_chapter(ref_for(1))

    def test_the_latest_chapter_url_is_never_fetched(self):
        # The fake has no page for the advertised (404) URL, so requesting it would raise
        # AssertionError. This is the guard against transplanting timotxt's stated_total.
        adapter, session = make_adapter()
        adapter.fetch_metadata(BARE_URL)
        adapter.fetch_chapter_list(BARE_URL)
        assert ADVERTISED_LATEST not in session.requested

    def test_metadata_always_hits_the_detail_page(self):
        adapter, session = make_adapter()
        meta = adapter.fetch_metadata(f"{ORIGIN}/{BID}/{INNER}_12.html")
        assert session.requested == [READ_URL]
        assert meta.title == TITLE

    def test_the_detail_page_is_fetched_at_most_once(self):
        adapter, session = make_adapter()
        adapter.fetch_metadata(BARE_URL)
        adapter.fetch_metadata(READ_URL)
        assert session.requested.count(READ_URL) == 1

    def test_a_scan_costs_two_navigations(self):
        adapter, session = make_adapter()
        adapter.fetch_metadata(BARE_URL)
        adapter.fetch_chapter_list(BARE_URL)
        assert session.requested == [READ_URL, DIR_URL]

    def test_chapters_are_never_read_from_the_detail_page(self):
        adapter, session = make_adapter()
        refs = adapter.fetch_chapter_list(BARE_URL)
        assert session.requested == [DIR_URL]
        assert len(refs) == N


class TestBrowserPath:
    def test_constructing_never_launches_a_browser(self):
        adapter = Novel543Adapter(HttpClient(delay_seconds=0))
        assert adapter._session is None

    def test_close_is_safe_before_any_fetch(self):
        Novel543Adapter(HttpClient(delay_seconds=0)).close()  # must not raise

    def test_close_releases_the_session_and_is_idempotent(self):
        adapter, session = make_adapter()
        adapter.close()
        adapter.close()
        assert session.closed and adapter._session is None

    def test_one_session_is_reused_across_a_chapters_sub_pages(self):
        adapter, session = make_adapter()
        adapter.fetch_chapter(ref_for(1))
        assert adapter._session is session

    @staticmethod
    def _record_session(monkeypatch) -> dict:
        seen: dict = {}

        class _Recorder:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def get_html(self, url):
                return fx("detail.html")

        monkeypatch.setattr("noveltrans.scrapers.novel543.BrowserSession", _Recorder)
        return seen

    def test_a_slower_configured_delay_is_honoured(self, monkeypatch):
        seen = self._record_session(monkeypatch)
        Novel543Adapter(HttpClient(delay_seconds=5.0)).fetch_metadata(BARE_URL)
        assert seen["delay_seconds"] == 5.0
        # Headless is fingerprinted by Cloudflare and does not clear the challenge.
        assert seen["headless"] is False

    def test_the_app_default_delay_is_raised_to_this_sites_floor(self, monkeypatch):
        # Measured: at 1.5s a 14-chapter walk loses 2 chapters to challenges that never
        # clear; at 3.0s it loses none. The app's default is 1.5s, so without this floor a
        # 439-chapter novel would strand a double-digit percentage of chapters as errors.
        seen = self._record_session(monkeypatch)
        Novel543Adapter(HttpClient(delay_seconds=DEFAULT_REQUEST_DELAY)).fetch_metadata(BARE_URL)
        assert seen["delay_seconds"] == _MIN_DELAY_SECONDS
        assert _MIN_DELAY_SECONDS > DEFAULT_REQUEST_DELAY

    def test_the_user_is_warned_before_chrome_appears(self, monkeypatch):
        monkeypatch.setattr(
            "noveltrans.scrapers.novel543.BrowserSession",
            lambda **_kw: type("S", (), {"get_html": lambda _s, _u: fx("detail.html")})(),
        )
        adapter = Novel543Adapter(HttpClient(delay_seconds=0))
        messages = capture_status(adapter)
        adapter.fetch_metadata(BARE_URL)
        assert any("Cloudflare" in m for m in messages)

    def test_a_missing_browser_says_how_to_install_one(self):
        adapter, _ = make_adapter()

        def boom(_url):
            raise BrowserUnavailableError("no playwright")

        adapter._session.get_html = boom
        with pytest.raises(ScrapeError) as excinfo:
            adapter.fetch_metadata(BARE_URL)
        assert "Chrome" in str(excinfo.value)
        assert "playwright install" in str(excinfo.value)

    def test_a_dead_session_says_to_retry(self):
        adapter, _ = make_adapter()

        def boom(_url):
            raise BrowserSessionError("window closed")

        adapter._session.get_html = boom
        with pytest.raises(ScrapeError) as excinfo:
            adapter.fetch_metadata(BARE_URL)
        assert "Thử tải lại" in str(excinfo.value)


@pytest.fixture(scope="class")
def live_adapter():
    """One browser session for the whole live class.

    Class-scoped: on this site a fresh adapter means a fresh Chrome and a fresh Cloudflare
    clearance, so per-test construction would pay for both several times over. Reusing one
    session is also what the download path really does.

    The delay passed here is the app's default; `_MIN_DELAY_SECONDS` raises it. That is
    deliberate — this fixture exercises the same floor a real user gets.
    """
    adapter = Novel543Adapter(HttpClient(delay_seconds=DEFAULT_REQUEST_DELAY))
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.fixture(scope="class")
def live_refs(live_adapter):
    """The TOC, fetched once for the whole class.

    Every live test below needs it, and this site charges a challenge for pressure — see
    `_MIN_DELAY_SECONDS`. Re-fetching per test was measurably enough to start failing runs.
    """
    return live_adapter.fetch_chapter_list(TestLive.URL)


@pytest.mark.live
class TestLive:
    """Drift detector against the real site. Deselected by default; launches a browser."""

    URL = "https://www.novel543.com/0802691255"  # the bare form a user actually pastes

    def test_metadata(self, live_adapter):
        meta = live_adapter.fetch_metadata(self.URL)
        assert meta.title
        assert meta.author
        assert meta.source_lang == "zh"
        assert meta.url == "https://www.novel543.com/0802691255/"
        # The truncated-meta-tag detector.
        assert "無彈窗最新章節" not in meta.description
        assert not meta.description.endswith("…")

    def test_the_chapter_list_is_complete_and_contiguous(self, live_refs):
        assert len(live_refs) > 400
        assert chapter_positions(live_refs) == list(range(1, len(live_refs) + 1))
        assert len({r.url for r in live_refs}) == len(live_refs)
        assert all(r.title for r in live_refs)
        assert live_refs[0].title.startswith("第")

    def test_the_advertised_latest_chapter_url_is_still_a_404(self, live_adapter):
        """The drift detector for the trap that shapes `stated_total`.

        If the site ever fixes that tag this turns red, and the comment explaining why we
        ignore it can be revisited deliberately instead of quietly rotting.
        """
        url = f"{ORIGIN}/{BID}/439.html"
        with pytest.raises(ScrapeError):
            parse_chapter(live_adapter._get_html(url), "", url)

    def test_a_paginated_chapter_comes_back_whole(self, live_adapter, live_refs):
        ref = live_refs[0]
        first_page = live_adapter._get_html(ref.url)
        assert page_count(first_page, ref.url) == 2  # measured: chapter 1 is (1/2)

        body = live_adapter.fetch_chapter(ref)
        assert len(body) > 1000
        assert "溫馨提示" not in body
        assert "下一章" not in body
        assert "(1/2)" not in body
        # The direct proof that the second half arrived, not just page 1 twice.
        assert len(body) > len(parse_chapter(first_page, ref.title, ref.url))

    def test_an_unpaginated_chapter_comes_back_whole(self, live_adapter, live_refs):
        ref = live_refs[119]  # measured: no (N/M) suffix
        assert page_count(live_adapter._get_html(ref.url), ref.url) == 1
        body = live_adapter.fetch_chapter(ref)
        assert len(body) > 1000
        assert "溫馨提示" not in body

    def test_the_pagination_distribution_has_not_flipped(self, live_adapter, live_refs):
        """Both page counts must still occur.

        If every chapter suddenly reports one page, either the suffix moved or the site
        stopped paginating — either way this adapter's most complex path has gone dark and
        nothing else would notice. Four samples, not more: every extra one is a navigation
        into a host that answers pressure with a challenge.
        """
        sampled = {
            page_count(live_adapter._get_html(live_refs[i].url), live_refs[i].url)
            for i in (2, 119, 300, len(live_refs) - 1)
        }
        assert sampled == {1, 2}

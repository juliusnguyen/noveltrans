"""Feature 063 — the twkan.com adapter.

Three tests carry this module, and each pins a failure that looks like nothing:

`test_the_phantom_bookmark_li_is_not_a_chapter` — twkan wraps its hidden bookmark widget
in an `li[data-num]`, in the same `<ul>` as the chapters. sto9's selector matches it and
files a titleless ref at index 0, shifting every real chapter by one.

`test_the_phantom_li_does_not_become_the_total_on_a_short_novel` — the same widget's
`data-num="7"` poisons `stated_total`'s `max(data-num)` signal, so a *complete* 4-chapter
novel looks truncated and gets refused, quoting a total that appears nowhere on the site.

`test_a_truncated_page_with_a_dead_fragment_raises` — twkan's chapter-list page ships ~35
of a novel's chapters behind a `LoadMore()` button and looks completely normal doing it,
and those entries are not a prefix (they jump from chapter 15 to 168). Saving that list
would file late chapters under early `idx` values, and `replace_toc` preserves content
across re-scans, so the wrong body would survive under a corrected title. The adapter must
refuse rather than fall back.

The browser is faked throughout — no test here launches Chrome or touches the network.

Fixtures are hand-built to the structure measured on the live site (class names, the
phantom bookcase `li`, the mid-prose ad slots inside `#txtcontent0`, the `&emsp;` indents,
the site's own inconsistent `第N 章` / `第N章` spacing, and the DOM-serialised fragment
with closed `<li>`s that the browser path actually returns) with invented filler text — so
the traps are encoded deliberately rather than captured by luck, and no novel text lives
in this repo.
"""

from __future__ import annotations

import pytest

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSessionError
from noveltrans.errors import ObfuscatedContentError, RateLimitedError, ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.twkan import (
    ORIGIN,
    TwkanAdapter,
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

BID = "114283"
READ_URL = f"https://twkan.com/book/{BID}/index.html"
DETAIL_URL = f"https://twkan.com/book/{BID}.html"
AJAX_URL = f"https://twkan.com/ajax_novels/chapterlist/{BID}.html"
# NOTE: no .html suffix. That is the twkan shape, and it is the one place sto9's
# code would have silently failed.
CHAPTER_URL = f"https://twkan.com/txt/{BID}/57238545"

# Every form a user might realistically paste.
ALL_FORMS = (DETAIL_URL, READ_URL, CHAPTER_URL)

TITLE = "星河歸客"
CH_TITLE = "第1 章 測試章節1"


def fx(name: str) -> str:
    return load_fixture("twkan", name)


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


def make_adapter(**overrides: str) -> tuple[TwkanAdapter, _FakeSession]:
    pages = {
        DETAIL_URL: fx("book.html"),
        READ_URL: fx("index.html"),
        AJAX_URL: fx("chapterlist.html"),
        CHAPTER_URL: fx("chapter.html"),
    }
    pages.update(overrides)
    adapter = TwkanAdapter(HttpClient(delay_seconds=0))
    session = _FakeSession(pages)
    adapter._session = session  # never launches a browser
    return adapter, session


def capture_status(adapter: TwkanAdapter) -> list[str]:
    messages: list[str] = []
    adapter.on_status = messages.append
    return messages


class TestUrlDerivation:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_book_id_from_every_paste_form(self, url):
        assert book_id(url) == BID

    def test_book_id_from_a_suffixless_chapter_url(self):
        # Its own test on purpose: twkan chapter URLs carry NO .html, so any regex
        # written as /txt/(\d+)/\d+\.html would match nothing here.
        assert book_id(f"https://twkan.com/txt/{BID}/57238545") == BID

    def test_book_id_raises_on_a_non_book_url(self):
        with pytest.raises(ScrapeError):
            book_id("https://twkan.com/novels/class/3_1.html")

    def test_book_id_raises_on_empty(self):
        with pytest.raises(ScrapeError):
            book_id("")

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_read_url_folds_every_form_to_one_canonical_string(self, url):
        # Library.find_by_url is exact string equality — without this, three paste
        # forms become three projects for one novel.
        assert read_url(url) == READ_URL

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_detail_and_chapterlist_urls(self, url):
        assert detail_url(url) == DETAIL_URL
        assert chapterlist_url(url) == AJAX_URL

    def test_origin_is_pinned_not_echoed(self):
        assert read_url(f"http://www.twkan.com/book/{BID}.html") == READ_URL


class TestMetadata:
    def test_reads_the_opengraph_block(self):
        meta = parse_metadata(fx("book.html"), READ_URL, "twkan")
        assert meta.title == TITLE
        assert meta.author == "無名氏"
        assert meta.site == "twkan"
        assert meta.source_lang == "zh"

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_url_is_always_canonical(self, url):
        assert parse_metadata(fx("book.html"), url, "twkan").url == READ_URL

    def test_description_has_its_literal_br_text_unescaped(self):
        # og:description arrives as an ATTRIBUTE VALUE carrying literal "<br />" text,
        # so BeautifulSoup never sees those as tags. sto9 needs no such helper; twkan does.
        desc = parse_metadata(fx("book.html"), READ_URL, "twkan").description
        assert "<br" not in desc
        assert "\n" in desc  # fails if _clean_description is replaced by a bare strip
        assert "\n\n\n" not in desc
        assert desc == desc.strip()

    def test_cover_url_is_stored_unmodified(self):
        # Asserted explicitly: no placeholder-blanking rule exists (none has been
        # observed on this site), so anyone adding one must add its fixture too rather
        # than silently blanking real covers.
        cover = parse_metadata(fx("book.html"), READ_URL, "twkan").cover_url
        assert cover == f"https://twkan.com/files/article/image/114/{BID}/{BID}s.jpg"

    def test_falls_back_to_the_visible_book_box(self):
        meta = parse_metadata(fx("book_no_og.html"), READ_URL, "twkan")
        assert meta.title == TITLE
        assert meta.author == "無名氏"

    def test_raises_when_no_title_anywhere(self):
        with pytest.raises(ScrapeError, match="Novel title not found"):
            parse_metadata(fx("book_no_title.html"), READ_URL, "twkan")


class TestChapterList:
    def test_the_ajax_fragment_parses_completely(self):
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        assert len(refs) == 20
        assert [r.index for r in refs] == list(range(20))

    def test_urls_are_absolute_and_carry_no_html_suffix(self):
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        assert refs[0].url == CHAPTER_URL
        assert not any(r.url.endswith(".html") for r in refs)

    def test_titles_keep_the_sites_own_inconsistent_spacing(self):
        # Asserted verbatim so a future "tidy-up" normaliser fails here rather than
        # silently rewriting titles that are persisted, exported and read aloud.
        refs = parse_chapter_list(fx("chapterlist.html"), ORIGIN)
        assert refs[0].title == "第1 章 測試章節1"
        assert refs[3].title == "第4章 測試章節4"
        assert refs[19].title == "第 20 章 測試章節20"

    def test_the_phantom_bookmark_li_is_not_a_chapter(self):
        # ★ The TOC page wraps `<a href="#" id="bookcase">` in an li[data-num], inside
        # the same <ul> as the chapters. sto9's `li[data-num] a[href]` matches it and
        # shifts every real chapter down by one. Delete the href filter and this fails.
        refs = parse_chapter_list(fx("index.html"), ORIGIN)
        assert len(refs) == 9
        assert refs[0].title == CH_TITLE
        assert all(r.title for r in refs)
        assert not any("#" in r.url for r in refs)

    def test_the_end_sentinel_is_not_a_chapter(self):
        markup = (
            f'<html><body><ul><li data-num="99">'
            f'<a href="https://twkan.com/txt/{BID}/end.html">下一章</a></li></ul></body></html>'
        )
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list(markup, ORIGIN)

    def test_raises_when_there_is_no_list(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html></html>", ORIGIN)

    def test_the_truncated_page_is_short_and_gappy(self):
        # Why it must never be stored: index 5 holds chapter 17's text.
        refs = parse_chapter_list(fx("index.html"), ORIGIN)
        assert refs[5].title == "第17 章 測試章節17"

    def test_stated_total_from_the_loadmore_button(self):
        assert stated_total(fx("index.html")) == 20

    def test_stated_total_falls_back_to_max_data_num(self):
        # The two-signal property: kill the button and the numbers still tell the truth.
        markup = fx("index.html").replace('id="loadmore"', 'id="loadmore-renamed"')
        assert stated_total(markup) == 20

    def test_the_phantom_li_does_not_become_the_total_on_a_short_novel(self):
        # ★ index_full.html is a COMPLETE 4-chapter novel with no #loadmore, so
        # max(data-num) is the only signal. Unfiltered, the phantom's data-num="7"
        # wins and the adapter refuses a complete list, quoting "4/7" — a total that
        # appears nowhere on the site.
        assert stated_total(fx("index_full.html")) == 4

    def test_stated_total_is_none_when_nothing_is_claimed(self):
        assert stated_total("<html><body></body></html>") is None


class TestChapterContent:
    def test_extracts_the_body_as_paragraphs(self):
        body = parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)
        paragraphs = body.split("\n\n")
        assert len(paragraphs) == 6
        assert paragraphs[0] == "測試段落一，這是虛構的填充文字。"

    def test_mid_prose_ad_slots_are_removed_without_eating_prose(self):
        # The ad divs sit INSIDE #txtcontent0, between paragraphs — so the two
        # paragraphs that surrounded one must survive AND still be adjacent.
        body = parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)
        assert "loadAdv" not in body
        paragraphs = body.split("\n\n")
        assert paragraphs[1] == "測試段落二，廣告區塊就排在這一行後面。"
        assert paragraphs[2] == "測試段落三，它必須緊接著段落二。"

    def test_sibling_chrome_is_absent(self):
        body = parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)
        for junk in ("上一章", "下一章", "目錄", "推薦", "作者：", "2026-07-27"):
            assert junk not in body

    def test_the_heading_is_not_the_first_line(self):
        body = parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)
        assert not body.startswith(CH_TITLE)

    def test_emsp_indents_are_stripped_without_a_blanket_replace(self):
        body = parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)
        assert " " not in body
        assert "&emsp;" not in body

    def test_no_blank_paragraphs_survive(self):
        body = parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)
        assert "\n\n\n" not in body
        assert all(p.strip() for p in body.split("\n\n"))

    def test_an_echoed_title_is_dropped_despite_different_spacing(self):
        # The fixture echoes "第1   章   測試章節1" while the TOC title is
        # "第1 章 測試章節1" — pins that the comparison is _norm-based, not exact.
        body = parse_chapter(fx("chapter_title_echo.html"), CH_TITLE, CHAPTER_URL)
        assert len(body.split("\n\n")) == 6
        assert body.startswith("測試段落一")

    def test_a_non_matching_title_never_strips_the_first_line(self):
        # The dangerous direction: over-stripping is silent data loss.
        body = parse_chapter(fx("chapter_title_echo.html"), "完全不同的標題", CHAPTER_URL)
        assert len(body.split("\n\n")) == 7

    def test_raises_when_the_container_is_missing(self):
        with pytest.raises(ScrapeError, match="Chapter content not found"):
            parse_chapter("<html><body><p>nope</p></body></html>", CH_TITLE, CHAPTER_URL)

    def test_raises_a_different_error_when_the_container_is_empty(self):
        with pytest.raises(ScrapeError, match="Chapter content is empty"):
            parse_chapter(fx("chapter_empty.html"), CH_TITLE, CHAPTER_URL)

    def test_empty_content_never_returns_a_blank_string(self):
        # save_content("") would leave the row pending forever with no error shown.
        with pytest.raises(ScrapeError) as excinfo:
            parse_chapter(fx("chapter_empty.html"), CH_TITLE, CHAPTER_URL)
        assert not isinstance(excinfo.value, ObfuscatedContentError | RateLimitedError)


class TestRegistry:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_matches_every_paste_form(self, url):
        assert TwkanAdapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://twkan.com/",
            "https://twkan.com/novels/class/3_1.html",
            f"https://twkan.com/txt/{BID}/end.html",  # the last chapter's 下一章 sentinel
        ],
    )
    def test_rejects_non_novel_urls(self, url):
        assert not TwkanAdapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://sto9.com/txt/13908/7671958.html",
            "https://www.69shuba.com/txt/59024/38369377",
        ],
    )
    def test_does_not_steal_a_neighbours_chapter_urls(self, url):
        # Three sites in this repo serve chapters at /txt/<id>/<cid>; ADAPTERS is
        # first-match-wins by import order, so an un-anchored pattern would be a silent,
        # order-dependent theft.
        assert not TwkanAdapter.matches(url)

    def test_neighbours_still_resolve_to_their_own_adapters(self):
        from noveltrans.scrapers.shuba69 import Shuba69Adapter
        from noveltrans.scrapers.sto9 import Sto9Adapter

        client = HttpClient(delay_seconds=0)
        assert isinstance(adapter_for_url("https://sto9.com/book/13908.html", client), Sto9Adapter)
        assert isinstance(
            adapter_for_url("https://www.69shuba.com/book/59024.htm", client), Shuba69Adapter
        )

    def test_the_adapter_is_registered(self):
        # The one line whose omission ships silently: every twkan URL would report
        # "Chưa hỗ trợ trang web này".
        assert TwkanAdapter in ADAPTERS

    def test_adapter_for_url_resolves_to_this_adapter(self):
        assert isinstance(adapter_for_url(READ_URL, HttpClient(delay_seconds=0)), TwkanAdapter)

    def test_content_is_not_pre_translated(self):
        # Flipping this would land Chinese in `translated` and mark chapters
        # already-translated, so the user's Vietnamese output would be Chinese.
        assert TwkanAdapter.content_is_translated is False


class TestAdapterWiring:
    def test_chapter_list_prefers_the_fragment_over_the_page(self):
        adapter, session = make_adapter()
        refs = adapter.fetch_chapter_list(READ_URL)
        assert len(refs) == 20  # not the page's 9
        assert session.requested == [READ_URL, AJAX_URL]

    def test_metadata_always_hits_the_detail_page(self):
        # The TOC page carries no OpenGraph tags at all, and neither do chapter pages.
        adapter, session = make_adapter()
        meta = adapter.fetch_metadata(CHAPTER_URL)
        assert session.requested == [DETAIL_URL]
        assert meta.title == TITLE
        assert meta.url == READ_URL

    def test_the_detail_page_is_fetched_at_most_once(self):
        adapter, session = make_adapter()
        adapter.fetch_metadata(READ_URL)
        adapter.fetch_metadata(DETAIL_URL)
        assert session.requested == [DETAIL_URL]

    def test_a_truncated_page_with_a_dead_fragment_raises(self):
        # ★ The load-bearing test. The page says 20 and holds 9, and the fragment is
        # unreachable — so there is no complete list. Falling back would file chapter
        # 17's text under index 5 and replace_toc would preserve it across re-scans.
        adapter, _session = make_adapter()
        del adapter._session.pages[AJAX_URL]
        adapter._session.get_html = _raising_for(adapter._session, AJAX_URL)
        with pytest.raises(ScrapeError) as excinfo:
            adapter.fetch_chapter_list(READ_URL)
        message = str(excinfo.value)
        assert "9/20" in message
        assert "twkan" in message

    def test_a_short_fragment_is_used_but_never_silently(self):
        adapter, _session = make_adapter(**{AJAX_URL: fx("chapterlist_short.html")})
        messages = capture_status(adapter)
        refs = adapter.fetch_chapter_list(READ_URL)
        assert len(refs) == 12
        assert any("12/20" in m for m in messages)

    def test_a_short_complete_novel_falls_back_to_the_page(self):
        # The only branch where a page fallback is safe — and it is only trustworthy
        # because stated_total filters the phantom <li> (see TestChapterList).
        adapter, _session = make_adapter(**{READ_URL: fx("index_full.html")})
        adapter._session.get_html = _raising_for(adapter._session, AJAX_URL)
        messages = capture_status(adapter)
        refs = adapter.fetch_chapter_list(READ_URL)
        assert len(refs) == 4
        assert any("4" in m for m in messages)

    def test_raises_when_neither_source_has_a_list(self):
        adapter, _session = make_adapter(**{READ_URL: "<html></html>"})
        adapter._session.get_html = _raising_for(adapter._session, AJAX_URL)
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            adapter.fetch_chapter_list(READ_URL)

    def test_fetch_chapter_end_to_end(self):
        adapter, _session = make_adapter()
        ref = ChapterRef(index=0, title=CH_TITLE, url=CHAPTER_URL)
        body = adapter.fetch_chapter(ref)
        assert "測試段落一，這是虛構的填充文字。" in body
        assert "loadAdv" not in body


def _raising_for(session: _FakeSession, dead_url: str):
    """Make one URL unreachable while the rest of the fake session keeps working."""
    original = session.get_html

    def get_html(url: str) -> str:
        if url == dead_url:
            raise ScrapeError("fragment unreachable", url)
        return original(url)

    return get_html


class TestBrowserPath:
    def test_constructing_never_launches_a_browser(self):
        assert TwkanAdapter(HttpClient(delay_seconds=0))._session is None

    def test_close_is_safe_before_any_fetch(self):
        TwkanAdapter(HttpClient(delay_seconds=0)).close()  # must not raise or launch

    def test_close_releases_the_session_and_is_idempotent(self):
        adapter, session = make_adapter()
        adapter.close()
        adapter.close()
        assert session.closed and adapter._session is None

    def test_one_session_is_reused_across_fetches(self):
        adapter, session = make_adapter()
        adapter.fetch_metadata(READ_URL)
        adapter.fetch_chapter_list(READ_URL)
        assert adapter._session is session  # not rebuilt per call

    def test_politeness_delay_is_taken_from_the_client(self, monkeypatch):
        # HttpClient's throttle is bypassed on this path, so the session must inherit
        # the configured delay or a 188-chapter batch would hammer a CF-protected host.
        built: dict = {}
        monkeypatch.setattr(
            "noveltrans.scrapers.twkan.BrowserSession",
            lambda **kw: built.update(kw) or _FakeSession({DETAIL_URL: "<html></html>"}),
        )
        adapter = TwkanAdapter(HttpClient(delay_seconds=2.5))
        with pytest.raises(ScrapeError):  # empty markup fails to parse; we want the kwargs
            adapter.fetch_metadata(READ_URL)
        assert built["delay_seconds"] == 2.5
        assert built["headless"] is False  # headless is fingerprinted; headed default

    def test_the_user_is_warned_before_chrome_appears(self, monkeypatch):
        def explode(**_kw):
            raise RuntimeError("launch")

        monkeypatch.setattr("noveltrans.scrapers.twkan.BrowserSession", explode)
        adapter = TwkanAdapter(HttpClient(delay_seconds=0))
        messages = capture_status(adapter)
        with pytest.raises(RuntimeError):
            adapter.fetch_metadata(READ_URL)
        assert messages and "Cloudflare" in messages[0]

    def test_a_missing_browser_says_how_to_install_one(self):
        adapter, session = make_adapter()

        def get_html(_url: str) -> str:
            raise BrowserUnavailableError("no playwright")

        session.get_html = get_html
        with pytest.raises(ScrapeError) as excinfo:
            adapter.fetch_metadata(READ_URL)
        message = str(excinfo.value)
        assert "Chrome" in message and "playwright install" in message

    def test_a_dead_session_says_to_retry(self):
        adapter, session = make_adapter()

        def get_html(_url: str) -> str:
            raise BrowserSessionError("window closed")

        session.get_html = get_html
        with pytest.raises(ScrapeError) as excinfo:
            adapter.fetch_metadata(READ_URL)
        message = str(excinfo.value)
        assert "Cloudflare" in message and "window closed" in message


@pytest.fixture(scope="class")
def live_adapter():
    """One browser session for the whole live class.

    Class-scoped, unlike sto9's per-test adapters: on this site a fresh adapter means a
    fresh Chrome, so per-test construction would launch (and have to clear Cloudflare)
    three times over. Reusing one session is also what the download path really does.
    """
    adapter = TwkanAdapter(HttpClient(delay_seconds=1.5))
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.mark.live
class TestLive:
    """Drift detector against the real site. Deselected by default; launches a browser."""

    URL = "https://twkan.com/book/114283.html"

    def test_metadata(self, live_adapter):
        meta = live_adapter.fetch_metadata(self.URL)
        assert meta.title
        assert meta.source_lang == "zh"
        assert meta.url == "https://twkan.com/book/114283/index.html"
        assert "<br" not in meta.description

    def test_the_full_chapter_list_is_not_the_truncated_page(self, live_adapter):
        refs = live_adapter.fetch_chapter_list(self.URL)
        # The truncated page holds ~36. Anything near that means the fragment broke.
        assert len(refs) > 150
        # The live phantom-<li> detector.
        assert all(ref.title for ref in refs)
        assert not any("#" in ref.url for ref in refs)
        assert refs[0].title.startswith("第")

    def test_first_and_last_chapters_extract(self, live_adapter):
        refs = live_adapter.fetch_chapter_list(self.URL)
        for ref in (refs[0], refs[-1]):
            body = live_adapter.fetch_chapter(ref)
            assert len(body) > 1000
            assert "loadAdv" not in body
            assert "下一章" not in body

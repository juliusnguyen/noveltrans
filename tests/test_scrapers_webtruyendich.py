"""Tests for the webtruyendich adapter's parsing surface and wiring.

No browser here: fetching goes through Playwright, so the parse functions are pure
and take markup. They run against real captured fixtures (a trimmed landing page,
TOC and one chapter body) so the selectors are checked against markup that
actually exists rather than a hand-written approximation.
"""

from __future__ import annotations

import pytest

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.webtruyendich import (
    TRANSLATOR_LABEL,
    TRANSLATOR_MODEL,
    WebtruyendichAdapter,
    landing_url,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    slug,
    toc_url,
)

from conftest import load_fixture

NOVEL_URL = "https://webtruyendich.com/truyen/ta-manh-nhat-doc-si-nu-de-goi-thang-nguoi-gian-ac"
TOC_URL = NOVEL_URL + "/danh-sach-chuong-day-du"
CHAPTER_URL = NOVEL_URL + "/fanqie/chuong-5-nu-de-danh-gia"


class TestUrlDerivation:
    @pytest.mark.parametrize("url", [NOVEL_URL, CHAPTER_URL, TOC_URL])
    def test_slug_from_any_page_of_the_novel(self, url):
        assert slug(url) == "ta-manh-nhat-doc-si-nu-de-goi-thang-nguoi-gian-ac"

    def test_landing_and_toc_urls(self):
        # Whichever page the user pastes, both targets resolve to the same canonical pair.
        assert landing_url(CHAPTER_URL) == NOVEL_URL
        assert toc_url(CHAPTER_URL) == TOC_URL

    def test_slug_raises_on_a_non_novel_url(self):
        with pytest.raises(ScrapeError):
            slug("https://webtruyendich.com/the-loai/tien-hiep")


class TestRegistry:
    @pytest.mark.parametrize("url", [NOVEL_URL, CHAPTER_URL])
    def test_matches_landing_and_chapter_urls(self, url):
        assert WebtruyendichAdapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.69shuba.com/book/59024/",
            "https://webtruyendich.com/the-loai/tien-hiep",
            "https://example.com/truyen/abc",
        ],
    )
    def test_rejects_other_urls(self, url):
        assert not WebtruyendichAdapter.matches(url)

    def test_adapter_for_url_resolves_to_this_adapter(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, WebtruyendichAdapter)

    def test_flags_mark_content_as_pre_translated(self):
        assert WebtruyendichAdapter.content_is_translated is True
        assert WebtruyendichAdapter.translated_lang == "vi"
        assert WebtruyendichAdapter.translator_label == TRANSLATOR_LABEL == "webtruyendich (Gemini Flash 3.5)"


class TestMetadata:
    def test_reads_landing_page(self):
        markup = load_fixture("webtruyendich", "landing.html")
        meta = parse_metadata(markup, NOVEL_URL, "webtruyendich")
        assert meta.title == "Ta, Mạnh Nhất Độc Sĩ, Nữ Đế Gọi Thẳng Người Gian Ác"
        assert meta.author == "Tinh Tinh Tử"
        assert meta.cover_url.startswith("https://")
        assert meta.source_lang == "vi"  # content arrives already-Vietnamese
        assert meta.url == NOVEL_URL  # echoed, not rewritten

    def test_falls_back_to_cleaned_og_title_without_an_h1(self):
        markup = (
            '<html><head><meta property="og:title" content="[Mới nhất] Tên Truyện'
            ' - Đọc Truyện Dịch Chuẩn Online | webtruyendich.com">'
            '<meta property="og:image" content="https://x/c.webp"></head><body></body></html>'
        )
        meta = parse_metadata(markup, NOVEL_URL, "webtruyendich")
        assert meta.title == "Tên Truyện"  # bracket prefix and site suffix stripped

    def test_raises_when_no_title(self):
        with pytest.raises(ScrapeError, match="title not found"):
            parse_metadata("<html><head></head><body></body></html>", NOVEL_URL, "webtruyendich")


class TestChapterList:
    def test_reading_order_ascending_from_newest_first_markup(self):
        refs = parse_chapter_list(load_fixture("webtruyendich", "toc.html"), NOVEL_URL)
        assert refs  # non-empty
        assert all(refs[i].index == i for i in range(len(refs)))  # index is 0-based reading order
        assert "/chuong-1-" in refs[0].url  # chapter 1 sorts first despite newest-first HTML
        numbers = [int(r.url.split("/chuong-")[1].split("-")[0]) for r in refs]
        assert numbers == sorted(numbers)

    def test_urls_are_deduplicated(self):
        refs = parse_chapter_list(load_fixture("webtruyendich", "toc.html"), NOVEL_URL)
        urls = [r.url for r in refs]
        assert len(urls) == len(set(urls))

    def test_a_chapter_number_with_two_slugs_keeps_both(self):
        # 32 chapter numbers on this novel carry two different title-slugs; both are
        # real chapters, so dedup is by URL, not by number. The fixture includes ch.110.
        refs = parse_chapter_list(load_fixture("webtruyendich", "toc.html"), NOVEL_URL)
        n110 = [r for r in refs if "/chuong-110-" in r.url]
        assert len(n110) == 2

    def test_absolute_urls(self):
        refs = parse_chapter_list(load_fixture("webtruyendich", "toc.html"), NOVEL_URL)
        assert all(r.url.startswith("https://webtruyendich.com/") for r in refs)

    def test_raises_when_no_links(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html><body>no links</body></html>", NOVEL_URL)


class TestChapterContent:
    def test_parses_the_real_chapter_body(self):
        text = parse_chapter(load_fixture("webtruyendich", "chapter_body.html"), CHAPTER_URL)
        assert len(text) > 400
        assert "\n\n" in text  # paragraphs separated by blank lines

    def test_strips_the_citation_anchor_hash(self):
        html = (
            '<div id="chapter-content-body">'
            '<p class="fade-in-paragraph has-anchor"><a class="citation-anchor" href="#p-1">#</a>Câu một.</p>'
            '<p class="fade-in-paragraph has-anchor"><a class="citation-anchor" href="#p-2">#</a>Câu hai.</p>'
            "</div>"
        )
        assert parse_chapter(html, CHAPTER_URL) == "Câu một.\n\nCâu hai."

    def test_tolerates_a_stray_cjk_char(self):
        # The AI output is not 100% clean; a stray CJK char must be kept, not fatal.
        html = (
            '<div id="chapter-content-body">'
            '<p class="fade-in-paragraph"><a class="citation-anchor" href="#p-1">#</a>Nói bỏ là bỏ, 绝 không thể.</p>'
            "</div>"
        )
        assert parse_chapter(html, CHAPTER_URL) == "Nói bỏ là bỏ, 绝 không thể."

    def test_raises_when_container_is_empty(self):
        with pytest.raises(ScrapeError, match="empty"):
            parse_chapter('<div id="chapter-content-body"></div>', CHAPTER_URL)

    def test_raises_when_only_anchor_hashes_remain(self):
        html = (
            '<div id="chapter-content-body">'
            '<p class="fade-in-paragraph"><a class="citation-anchor" href="#p-1">#</a></p>'
            "</div>"
        )
        with pytest.raises(ScrapeError, match="empty"):
            parse_chapter(html, CHAPTER_URL)


class _FakeSession:
    """Stands in for WtdBrowserSession: serves fixtures, records what was asked."""

    def __init__(self, pages: dict[str, str], chapter_html: str = ""):
        self.pages = pages
        self.chapter_html = chapter_html
        self.requested: list[str] = []
        self.translated: list[tuple[str, str]] = []  # (url, model)
        self.closed = False

    def get_html(self, url: str) -> str:
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"adapter fetched an unexpected URL: {url}")
        return self.pages[url]

    def read_translated_chapter(self, url, *, translator_value, **_kw) -> str:
        self.translated.append((url, translator_value))
        return self.chapter_html

    def close(self) -> None:
        self.closed = True


def make_adapter() -> tuple[WebtruyendichAdapter, _FakeSession]:
    adapter = WebtruyendichAdapter(HttpClient(delay_seconds=0))
    session = _FakeSession(
        {
            NOVEL_URL: load_fixture("webtruyendich", "landing.html"),
            TOC_URL: load_fixture("webtruyendich", "toc.html"),
        },
        chapter_html=load_fixture("webtruyendich", "chapter_body.html"),
    )
    adapter._session = session  # never launches a browser
    return adapter, session


class TestAdapterWiring:
    def test_fetch_metadata_hits_the_landing_page(self):
        adapter, session = make_adapter()
        meta = adapter.fetch_metadata(CHAPTER_URL)  # even from a chapter URL
        assert session.requested == [NOVEL_URL]
        assert meta.title == "Ta, Mạnh Nhất Độc Sĩ, Nữ Đế Gọi Thẳng Người Gian Ác"
        assert meta.url == CHAPTER_URL  # echoed, not rewritten to the landing page

    def test_fetch_chapter_list_hits_the_toc_page(self):
        adapter, session = make_adapter()
        refs = adapter.fetch_chapter_list(NOVEL_URL)
        assert session.requested == [TOC_URL]
        assert len(refs) > 1

    def test_fetch_chapter_selects_the_gemini_model_and_parses(self):
        adapter, session = make_adapter()
        ref = ChapterRef(index=0, title="Chương 5", url=CHAPTER_URL)
        text = adapter.fetch_chapter(ref)
        assert session.translated == [(CHAPTER_URL, TRANSLATOR_MODEL)]
        assert len(text) > 400 and "\n\n" in text

    def test_one_session_is_reused_across_fetches(self):
        adapter, session = make_adapter()
        adapter.fetch_metadata(NOVEL_URL)
        adapter.fetch_chapter_list(NOVEL_URL)
        assert adapter._session is session

    def test_close_releases_the_session_and_is_idempotent(self):
        adapter, session = make_adapter()
        adapter.close()
        adapter.close()
        assert session.closed and adapter._session is None

    def test_close_is_safe_before_any_fetch(self):
        WebtruyendichAdapter(HttpClient(delay_seconds=0)).close()  # must not raise or launch

    def test_constructing_never_launches_a_browser(self):
        assert WebtruyendichAdapter(HttpClient(delay_seconds=0))._session is None

    def test_politeness_delay_is_taken_from_the_client(self, monkeypatch):
        built = {}
        monkeypatch.setattr(
            "noveltrans.scrapers.webtruyendich.WtdBrowserSession",
            lambda **kw: built.update(kw) or _FakeSession({NOVEL_URL: "<html></html>"}),
        )
        adapter = WebtruyendichAdapter(HttpClient(delay_seconds=2.5))
        with pytest.raises(ScrapeError):  # empty markup fails to parse; we only want the kwargs
            adapter.fetch_metadata(NOVEL_URL)
        assert built["delay_seconds"] == 2.5
        assert built["headless"] is False  # headless is fingerprinted; headed default

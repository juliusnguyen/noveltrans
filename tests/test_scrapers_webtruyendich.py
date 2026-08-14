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
    WebtruyendichAdapter,
    landing_url,
    looks_like_api_error,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    rank_translator_models,
    slug,
    toc_url,
    translator_label_for,
)

from conftest import load_fixture

NOVEL_URL = "https://webtruyendich.com/truyen/ta-manh-nhat-doc-si-nu-de-goi-thang-nguoi-gian-ac"
TOC_URL = NOVEL_URL + "/danh-sach-chuong-day-du"
CHAPTER_URL = NOVEL_URL + "/fanqie/chuong-5-nu-de-danh-gia"

# The #translator dropdown as the live site served it (captured Aug 2026). Kept
# verbatim because the ranking is the whole point: the adapter used to pin
# "AI Gemini FLASH 3.5 - Memory - No Apikey", which this lineup no longer offers.
LIVE_MODEL_OPTIONS = [
    "Vietphrase",
    "AI Gemini FLASH 3.6 - Memory - No Apikey",
    "AI Gemini FLASH 3.7 - Memory - No Apikey",
    "AI Gemini FLASH 3.5 LITE - Memory - No Apikey",
    "AI Gemini FLASH 3.1 LITE - Memory - No Apikey",
    "AI Gemini FLASH 3.6 - Custom Prompt",
    "AI Gemini FLASH 3.6 - Memory",
    "AI Gemini FLASH 3.7 - Memory",
    "AI Gemini FLASH 3.5 LITE - Memory",
    "AI Gemini FLASH 3.1 LITE - Memory",
    "AI Gemini Gemma 4 31B - Memory",
]


class TestTranslatorModelRanking:
    def test_tries_the_newest_full_no_apikey_model_first(self):
        assert rank_translator_models(LIVE_MODEL_OPTIONS)[0] == (
            "AI Gemini FLASH 3.7 - Memory - No Apikey"
        )

    def test_offers_every_usable_model_as_a_fallback_best_first(self):
        # A listed model can still be overloaded, so the ranking is a queue, not a pick.
        assert rank_translator_models(LIVE_MODEL_OPTIONS) == [
            "AI Gemini FLASH 3.7 - Memory - No Apikey",
            "AI Gemini FLASH 3.6 - Memory - No Apikey",
            "AI Gemini FLASH 3.5 LITE - Memory - No Apikey",
            "AI Gemini FLASH 3.1 LITE - Memory - No Apikey",
        ]

    def test_still_leads_with_the_previously_pinned_model_on_the_old_lineup(self):
        # Ranking must not regress the lineup the adapter was originally built for.
        assert rank_translator_models(
            [
                "Vietphrase",
                "AI Gemini FLASH 3.5 - Memory - No Apikey",
                "AI Gemini FLASH 3.1 LITE - Memory - No Apikey",
            ]
        )[0] == "AI Gemini FLASH 3.5 - Memory - No Apikey"

    def test_prefers_a_full_model_over_a_higher_numbered_lite_one(self):
        assert rank_translator_models(
            [
                "AI Gemini FLASH 9.9 LITE - Memory - No Apikey",
                "AI Gemini FLASH 3.1 - Memory - No Apikey",
            ]
        )[0] == "AI Gemini FLASH 3.1 - Memory - No Apikey"

    def test_falls_back_to_lite_when_that_is_all_there_is(self):
        assert rank_translator_models(
            ["Vietphrase", "AI Gemini FLASH 3.5 LITE - Memory - No Apikey"]
        ) == ["AI Gemini FLASH 3.5 LITE - Memory - No Apikey"]

    @pytest.mark.parametrize(
        "options",
        [
            ["Vietphrase"],  # non-AI dictionary default only
            ["AI Gemini FLASH 3.6 - Custom Prompt"],  # no Memory
            ["AI Gemini FLASH 3.6 - Memory"],  # needs the reader's own API key
            [],
        ],
    )
    def test_rejects_models_we_cannot_drive(self, options):
        with pytest.raises(ScrapeError):
            rank_translator_models(options, CHAPTER_URL)

    def test_error_names_what_the_site_offered(self):
        # Without this the next rename looks like a Cloudflare failure again.
        with pytest.raises(ScrapeError) as excinfo:
            rank_translator_models(["Vietphrase", "AI Gemini FLASH 3.6 - Custom Prompt"])
        assert "Vietphrase" in str(excinfo.value)
        assert "Custom Prompt" in str(excinfo.value)

    def test_label_names_the_model_without_its_flags(self):
        assert (
            translator_label_for("AI Gemini FLASH 3.7 - Memory - No Apikey")
            == "webtruyendich (Gemini FLASH 3.7)"
        )


class TestApiErrorDetection:
    # Verbatim shape the live site streamed into the chapter when 3.7 was overloaded.
    OVERLOADED = (
        'Anya ngã gục xuống đất, tuyết lớn nhanh chóng phủ kín thân hình cô{"error": '
        '{"code": 503,"message": "This model is currently experiencing high demand. '
        'Spikes in demand are usually temporary. Please try again later.",'
        '"status": "UNAVAILABLE"}}'
    )

    def test_spots_an_error_body_glued_onto_a_partial_translation(self):
        assert looks_like_api_error(self.OVERLOADED)

    def test_spots_a_quota_error(self):
        assert looks_like_api_error('{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}')

    def test_leaves_ordinary_translated_prose_alone(self):
        prose = load_fixture("webtruyendich", "chapter_body.html")
        assert not looks_like_api_error(parse_chapter(prose, CHAPTER_URL))

    def test_parse_chapter_refuses_to_hand_back_an_error_body(self):
        # The worker stores whatever parse_chapter returns as the finished
        # translation, so this is the last gate before a corrupt chapter lands.
        markup = f'<div><p class="fade-in-paragraph">{self.OVERLOADED}</p></div>'
        with pytest.raises(ScrapeError):
            parse_chapter(markup, CHAPTER_URL)


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
        assert WebtruyendichAdapter.translator_label == TRANSLATOR_LABEL == "webtruyendich (Gemini Flash)"


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

    def test_reads_a_novel_whose_upstream_source_is_not_fanqie(self):
        # The path segment after the slug names the upstream site and varies per
        # novel; pinning it to "fanqie" broke every other source (measured on
        # /truyen/dong-kinh-y-do, source "sudugu").
        markup = (
            '<a href="/truyen/dong-kinh-y-do/sudugu/chuong-454-muon-vao-bo">Chương 454</a>'
            '<a href="/truyen/dong-kinh-y-do/sudugu/chuong-453-bao-quan-part-2">Chương 453</a>'
        )
        refs = parse_chapter_list(markup, "https://webtruyendich.com/truyen/dong-kinh-y-do")
        assert [r.url.rsplit("/", 1)[1] for r in refs] == [
            "chuong-453-bao-quan-part-2",
            "chuong-454-muon-vao-bo",
        ]

    def test_keeps_a_chapter_whose_slug_does_not_lead_with_its_number(self):
        # "chương lậu đơn" fill-ins are named chuong-lau-don-chuong-<N>; they used
        # to be dropped silently. Sorted by the trailing number, next to ch. 1715.
        markup = (
            '<a href="/truyen/dong-kinh-y-do/sudugu/chuong-1716-a">Chương 1716</a>'
            '<a href="/truyen/dong-kinh-y-do/sudugu/chuong-lau-don-chuong-1715">Lậu đơn</a>'
            '<a href="/truyen/dong-kinh-y-do/sudugu/chuong-1715-b">Chương 1715</a>'
        )
        refs = parse_chapter_list(markup, "https://webtruyendich.com/truyen/dong-kinh-y-do")
        # Both 1715s tie on number, so site order decides between them; what matters
        # is the fill-in survives and lands with 1715 rather than after 1716.
        assert [r.title for r in refs] == ["Lậu đơn", "Chương 1715", "Chương 1716"]

    def test_ignores_links_that_are_not_chapters(self):
        markup = (
            '<a href="/truyen/dong-kinh-y-do/danh-sach-chuong-day-du">Danh sách chương</a>'
            '<a href="/tac-gia/ai-do">Tác giả</a>'
        )
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list(markup, NOVEL_URL)

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

    def __init__(
        self,
        pages: dict[str, str],
        chapter_html: str = "",
        failing_models: dict[str, str] | None = None,
    ):
        self.pages = pages
        self.chapter_html = chapter_html
        # model -> the markup it "returns" instead of a translation (an error body)
        self.failing_models = failing_models or {}
        self.requested: list[str] = []
        self.scrolled: list[str | None] = []  # scroll_item_selector per get_html call
        self.prefer_document: list[bool] = []  # prefer_document per get_html call
        self.translated: list[tuple[str, str]] = []  # (url, model)
        self.closed = False

    def get_html(
        self,
        url: str,
        *,
        prefer_document: bool = False,
        scroll_item_selector: str | None = None,
    ) -> str:
        self.requested.append(url)
        self.scrolled.append(scroll_item_selector)
        self.prefer_document.append(prefer_document)
        if url not in self.pages:
            raise AssertionError(f"adapter fetched an unexpected URL: {url}")
        return self.pages[url]

    def read_translated_chapter(
        self, url, *, rank_translators, is_usable_output, **_kw
    ) -> tuple[str, str]:
        # Mirrors the real session: walk the ranked models, skipping any whose output
        # the caller rejects, so the adapter's fallback wiring is actually exercised.
        for model in rank_translators(LIVE_MODEL_OPTIONS):
            markup = self.failing_models.get(model, self.chapter_html)
            self.translated.append((url, model))
            if is_usable_output(markup):
                return markup, model
        raise AssertionError("no model produced usable output")

    def close(self) -> None:
        self.closed = True


def make_adapter(
    failing_models: dict[str, str] | None = None,
) -> tuple[WebtruyendichAdapter, _FakeSession]:
    adapter = WebtruyendichAdapter(HttpClient(delay_seconds=0))
    session = _FakeSession(
        {
            NOVEL_URL: load_fixture("webtruyendich", "landing.html"),
            TOC_URL: load_fixture("webtruyendich", "toc.html"),
        },
        chapter_html=load_fixture("webtruyendich", "chapter_body.html"),
        failing_models=failing_models,
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

    def test_toc_reads_the_raw_document_with_a_scroll_fallback(self):
        # The TOC is fully server-rendered but JS virtualizes it, so it's fetched
        # from the raw document (prefer_document) with scrolling as the fallback;
        # the landing page needs neither.
        adapter, session = make_adapter()
        adapter.fetch_metadata(NOVEL_URL)
        adapter.fetch_chapter_list(NOVEL_URL)
        assert session.prefer_document == [False, True]
        assert session.scrolled == [None, 'a[href*="/chuong-"]']

    def test_fetch_chapter_selects_the_gemini_model_and_parses(self):
        adapter, session = make_adapter()
        ref = ChapterRef(index=0, title="Chương 5", url=CHAPTER_URL)
        text = adapter.fetch_chapter(ref)
        assert session.translated == [(CHAPTER_URL, "AI Gemini FLASH 3.7 - Memory - No Apikey")]
        assert len(text) > 400 and "\n\n" in text

    def test_fetch_chapter_falls_back_when_the_top_model_is_overloaded(self):
        # Measured on the live site: 3.7 answered with a 503 body while 3.6
        # translated the same chapter in full.
        overloaded = (
            '<div><p class="fade-in-paragraph">Anya ngã gục xuống đất{"error": '
            '{"code": 503, "status": "UNAVAILABLE"}}</p></div>'
        )
        adapter, session = make_adapter(
            failing_models={"AI Gemini FLASH 3.7 - Memory - No Apikey": overloaded}
        )
        text = adapter.fetch_chapter(ChapterRef(index=0, title="Chương 5", url=CHAPTER_URL))
        assert [model for _url, model in session.translated] == [
            "AI Gemini FLASH 3.7 - Memory - No Apikey",
            "AI Gemini FLASH 3.6 - Memory - No Apikey",
        ]
        assert "error" not in text and len(text) > 400
        assert adapter.translator_label == "webtruyendich (Gemini FLASH 3.6)"

    def test_fetch_chapter_records_the_model_it_actually_used(self):
        # Chapter.translator must name the model that produced the text, since the
        # site's lineup rotates between runs.
        adapter, _session = make_adapter()
        adapter.fetch_chapter(ChapterRef(index=0, title="Chương 5", url=CHAPTER_URL))
        assert adapter.translator_label == "webtruyendich (Gemini FLASH 3.7)"

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

import os

import pytest
import responses

from noveltrans.errors import (
    AuthRequiredError,
    DailyLimitError,
    RateLimitedError,
    ScrapeError,
)
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.medoctruyen import MedoctruyenAdapter

from conftest import load_fixture

NOVEL_URL = "https://medoctruyen.vn/tu-bao-tien-bon"
CHAPTER_URL = "https://medoctruyen.vn/tu-bao-tien-bon/chuong-1"

# The synthetic TOC fixtures use this slug (2 pages, chapters 1–6).
TOC_URL = "https://medoctruyen.vn/truyen-thu"


def make_adapter() -> MedoctruyenAdapter:
    return MedoctruyenAdapter(HttpClient(delay_seconds=0))


class TestRegistry:
    def test_matches_novel_and_chapter_urls(self):
        assert MedoctruyenAdapter.matches(NOVEL_URL)
        assert MedoctruyenAdapter.matches(CHAPTER_URL)
        assert not MedoctruyenAdapter.matches("https://example.com/tu-bao-tien-bon")

    def test_adapter_for_url(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, MedoctruyenAdapter)


class TestMetadata:
    @responses.activate
    def test_fetch_metadata(self):
        responses.get(NOVEL_URL, body=load_fixture("medoctruyen", "novel.html"))
        meta = make_adapter().fetch_metadata(NOVEL_URL)
        # title comes from schema.org JSON-LD Book — no " - Truyện …" suffix
        assert meta.title == "Tụ Bảo Tiên Bồn"
        assert meta.author == "Hương Quả Vị Nãi Trà"
        assert meta.description.startswith("Đệ tử tạp dịch")
        assert meta.cover_url.startswith("https://cdn.medoctruyen.vn/")
        assert meta.site == "medoctruyen"
        assert meta.source_lang == "vi"

    @responses.activate
    def test_fetch_metadata_normalizes_chapter_url(self):
        # Pasting a chapter URL still scrapes the novel landing page.
        responses.get(NOVEL_URL, body=load_fixture("medoctruyen", "novel.html"))
        meta = make_adapter().fetch_metadata(CHAPTER_URL)
        assert meta.url == NOVEL_URL
        assert meta.title == "Tụ Bảo Tiên Bồn"


class TestChapterList:
    @responses.activate
    def test_fetch_chapter_list(self):
        responses.get(TOC_URL, body=load_fixture("medoctruyen", "toc_p1.html"))
        responses.get(f"{TOC_URL}?ch=2", body=load_fixture("medoctruyen", "toc_p2.html"))
        refs = make_adapter().fetch_chapter_list(TOC_URL)

        assert len(refs) == 6  # chapters 1–6, chrome/duplicate links deduped
        assert [r.index for r in refs] == [0, 1, 2, 3, 4, 5]
        assert refs[0].title == "Chương 1: Khởi đầu"  # not "Đọc từ đầu"
        assert refs[0].url == f"{TOC_URL}/chuong-1"
        assert refs[-1].title == "Chương 6: Chương mới nhất"
        assert refs[-1].url == f"{TOC_URL}/chuong-6"
        # trailing "Miễn phí" free-tag stripped from titles
        assert all("Miễn phí" not in r.title for r in refs)


class TestChapterContentFull:
    @responses.activate
    def test_fetch_chapter(self):
        url = f"{TOC_URL}/chuong-1"
        responses.get(url, body=load_fixture("medoctruyen", "chapter_full.html"))
        ref = ChapterRef(index=0, title="Chương 1: Khởi đầu", url=url)
        text = make_adapter().fetch_chapter(ref)
        assert text.startswith("Đây là câu văn đầu tiên")
        assert text.endswith("đầy hứa hẹn.")
        assert "\n\n" in text  # paragraphs separated by blank lines
        assert text.count("\n\n") == 2  # empty <p data-line="3"> dropped → 3 paragraphs


class TestPreviewGate:
    @responses.activate
    def test_preview_raises_auth_required(self):
        responses.get(CHAPTER_URL, body=load_fixture("medoctruyen", "chapter_preview.html"))
        ref = ChapterRef(index=0, title="Chương 1: Nhặt được chậu rách", url=CHAPTER_URL)
        with pytest.raises(AuthRequiredError) as exc:
            make_adapter().fetch_chapter(ref)
        assert "đăng nhập" in str(exc.value).lower()


class TestRateLimit:
    @responses.activate
    def test_rate_limited_raises_retryable(self):
        url = f"{TOC_URL}/chuong-4"
        responses.get(url, body=load_fixture("medoctruyen", "chapter_ratelimited.html"))
        ref = ChapterRef(index=3, title="Chương 4: Cô nương", url=url)
        with pytest.raises(RateLimitedError):
            make_adapter().fetch_chapter(ref)

    @responses.activate
    def test_daily_limit_raises_with_unlock_instructions(self):
        url = f"{TOC_URL}/chuong-201"
        responses.get(url, body=load_fixture("medoctruyen", "chapter_dailylimit.html"))
        ref = ChapterRef(index=200, title="Chương 201: Tử Lan", url=url)
        with pytest.raises(DailyLimitError) as exc:
            make_adapter().fetch_chapter(ref)
        msg = str(exc.value)
        assert "50 chương/ngày" in msg
        assert "/mochuong URFME4PA" in msg  # site's own unlock code surfaced
        assert "discord.gg/QDP8zjCa38" in msg
        # structured fields let the app run the /mochuong auto-unlock, not just show text
        assert exc.value.code == "URFME4PA"
        assert exc.value.discord_url == "https://discord.gg/QDP8zjCa38"
        # a daily cap is NOT the short "reading too fast" throttle
        assert not isinstance(exc.value, RateLimitedError)

    @responses.activate
    def test_missing_article_without_ratelimit_is_not_retryable(self):
        # A genuinely broken layout (no article, no throttle marker) is a hard error,
        # not a rate-limit — so the downloader won't loop forever waiting.
        url = f"{TOC_URL}/chuong-99"
        responses.get(url, body="<html><body><p>no article here</p></body></html>")
        ref = ChapterRef(index=0, title="Chương 99", url=url)
        with pytest.raises(ScrapeError) as exc:
            make_adapter().fetch_chapter(ref)
        assert not isinstance(exc.value, RateLimitedError)


class TestCookies:
    def test_cookies_attached(self):
        client = HttpClient(delay_seconds=0, cookies="a=b; c=d")
        assert client._session.headers["Cookie"] == "a=b; c=d"

    def test_no_cookie_header_by_default(self):
        client = HttpClient(delay_seconds=0)
        assert "Cookie" not in client._session.headers

    def test_set_cookies_ignores_blank(self):
        client = HttpClient(delay_seconds=0)
        client.set_cookies("   ")
        assert "Cookie" not in client._session.headers


@pytest.mark.live
class TestLive:
    """Hit the real site (run with `make test-live`). Catches layout drift."""

    def test_metadata(self):
        meta = make_adapter().fetch_metadata(NOVEL_URL)
        assert meta.title
        assert meta.author
        assert meta.cover_url.startswith("https://")
        assert meta.source_lang == "vi"

    def test_anonymous_chapter_is_gated(self):
        # Without a session cookie the site only serves a login-gated preview.
        ref = ChapterRef(index=0, title="", url=CHAPTER_URL)
        with pytest.raises(AuthRequiredError):
            make_adapter().fetch_chapter(ref)

    @pytest.mark.skipif(
        not os.environ.get("MEDOCTRUYEN_COOKIES"),
        reason="set MEDOCTRUYEN_COOKIES to a logged-in session cookie to test full fetch",
    )
    def test_authenticated_chapter_full(self):
        client = HttpClient(delay_seconds=0, cookies=os.environ["MEDOCTRUYEN_COOKIES"])
        ref = ChapterRef(index=0, title="", url=CHAPTER_URL)
        text = MedoctruyenAdapter(client).fetch_chapter(ref)
        assert len(text) > 500  # a full chapter, not the ~89-word preview

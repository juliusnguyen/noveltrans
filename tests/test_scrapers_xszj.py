"""Tests for the xszj.org (小说之家) adapter."""

from __future__ import annotations

import pytest
import responses

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.xszj import (
    ORIGIN,
    XszjAdapter,
    book_id,
    page_count,
    page_position,
    parse_chapter,
    parse_metadata,
    parse_toc_page,
    read_url,
    sub_page_url,
    toc_page_numbers,
    toc_url,
)
from conftest import load_fixture

BID = "506549"
BOOK_URL = f"{ORIGIN}/b/{BID}"
TOC_URL_1 = f"{ORIGIN}/b/{BID}/cs/1"
TOC_URL_2 = f"{ORIGIN}/b/{BID}/cs/2"
CH1_URL = f"{ORIGIN}/b/{BID}/c/4772907"
CH2_URL = f"{ORIGIN}/b/{BID}/c/4772917"
CH4_URL = f"{ORIGIN}/b/{BID}/c/4772920"

ALL_FORMS = (
    "https://xszj.org/b/506549",
    "https://xszj.org/b/506549/",
    "https://m.xszj.org/b/506549",
    "https://www.xszj.org/b/506549",
    "https://xszj.org/b/506549/cs/1",
    "https://xszj.org/b/506549/c/4772907",
    "https://xszj.org/b/506549/c/4772907?page=2",
    "https://m.xszj.org/b/506549/c/4772907?page=4",
)


def fx(name: str) -> str:
    return load_fixture("xszj", name)


def make_adapter() -> XszjAdapter:
    return XszjAdapter(HttpClient(delay_seconds=0))


class TestUrlDerivation:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_book_id_from_every_paste_form(self, url):
        assert book_id(url) == BID

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_read_url_folds_every_paste_form_to_canonical(self, url):
        assert read_url(url) == BOOK_URL

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_toc_url_from_every_paste_form(self, url):
        assert toc_url(url) == TOC_URL_1
        assert toc_url(url, 2) == TOC_URL_2

    @pytest.mark.parametrize(
        "url",
        ["", "https://xszj.org/", "https://xszj.org/c/18", "https://example.com/b/506549"],
    )
    def test_book_id_raises_on_non_book_url(self, url):
        with pytest.raises(ScrapeError):
            book_id(url)

    def test_sub_page_url_builds_query_from_page_one(self):
        assert sub_page_url(CH1_URL, 2) == f"{CH1_URL}?page=2"
        assert sub_page_url(CH1_URL, 4) == f"{CH1_URL}?page=4"

    def test_sub_page_url_refuses_an_already_paginated_url(self):
        with pytest.raises(ScrapeError):
            sub_page_url(f"{CH1_URL}?page=2", 3)

    def test_sub_page_url_refuses_non_chapter_url(self):
        with pytest.raises(ScrapeError):
            sub_page_url(BOOK_URL, 2)


class TestRegistry:
    def test_adapter_is_registered(self):
        assert XszjAdapter in ADAPTERS

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_matches_and_resolves_every_paste_form(self, url):
        assert XszjAdapter.matches(url)
        adapter = adapter_for_url(url, HttpClient(delay_seconds=0))
        assert isinstance(adapter, XszjAdapter)

    @pytest.mark.parametrize(
        "url",
        [
            "https://xszj.org/",
            "https://xszj.org/c/18",
            "https://xszj.org/t/month",
            "https://xszj.org/s?keyword=test",
            "https://ixdzs8.com/read/506549/",
            "https://www.novel543.com/506549/",
        ],
    )
    def test_rejects_non_novel_and_other_site_urls(self, url):
        assert not XszjAdapter.matches(url)

    def test_content_is_not_pre_translated(self):
        assert XszjAdapter.content_is_translated is False


class TestMetadata:
    def test_reads_opengraph_and_intro_and_blanks_nocover(self):
        meta = parse_metadata(fx("book.html"), ALL_FORMS[2], "xszj")
        assert meta.url == BOOK_URL
        assert meta.site == "xszj"
        assert meta.title == "星河归客"
        assert meta.author == "无名氏"
        assert meta.description == "【读心+吃瓜】第一段简介文本。\n第二段简介文本。"
        assert meta.cover_url == ""
        assert meta.source_lang == "zh"

    def test_real_cover_is_preserved_and_default_male_cover_is_blanked(self):
        real_cover = "https://img.xszj.org/cover/506549.jpg"
        html = fx("book.html").replace(
            "https://xszj.org/static/default/img/nocover.jpg", real_cover
        )
        assert parse_metadata(html, BOOK_URL).cover_url == real_cover

        male_default = "https://qcdn.zhangzhongyun.com/default_cover_male.jpg?imageView2/0/w/300/q/75"
        html_default = fx("book.html").replace(
            "https://xszj.org/static/default/img/nocover.jpg", male_default
        )
        assert parse_metadata(html_default, BOOK_URL).cover_url == ""

    def test_falls_back_to_visible_dom_when_og_tags_are_absent(self):
        html = """
        <html><body>
        <article class="card">
            <img src="/static/default/img/nocover.jpg" aria-hidden="true">
            <img src="/uploads/cover.jpg" alt="星河归客">
            <h1>星河归客</h1>
            <p>作者：<a href="/z/1">无名氏</a></p>
        </article>
        <section aria-labelledby="sec-intro"><p>可见简介内容</p></section>
        </body></html>
        """
        meta = parse_metadata(html, BOOK_URL)
        assert meta.title == "星河归客"
        assert meta.author == "无名氏"
        assert meta.description == "可见简介内容"
        assert meta.cover_url == f"{ORIGIN}/uploads/cover.jpg"

    def test_missing_title_raises(self):
        with pytest.raises(ScrapeError, match="title"):
            parse_metadata("<html><body></body></html>", BOOK_URL)


class TestChapterList:
    def test_single_toc_page_parses_in_order_and_ignores_other_books(self):
        entries = parse_toc_page(fx("toc_single.html"), BID, TOC_URL_1)
        assert len(entries) == 4
        assert entries[0] == ("第1章 启程", CH1_URL)
        assert entries[-1] == ("第4章 终幕", CH4_URL)
        assert toc_page_numbers(fx("toc_single.html"), BID) == [1]

    def test_toc_page_numbers_ignores_homonym_book_link(self):
        # toc_page1.html carries a "/b/349775/cs/1" link for another book with the same title.
        assert toc_page_numbers(fx("toc_page1.html"), BID) == [1, 2]

    @responses.activate
    def test_fetch_chapter_list_walks_all_toc_pages_and_never_uses_truncated_book_page(self):
        responses.add(responses.GET, TOC_URL_1, body=fx("toc_page1.html"))
        responses.add(responses.GET, TOC_URL_2, body=fx("toc_page2.html"))

        refs = make_adapter().fetch_chapter_list(BOOK_URL)
        assert [r.index for r in refs] == [0, 1, 2, 3]
        assert [r.title for r in refs] == ["第1章 启程", "第2章 星图", "第3章 远航", "第4章 终幕"]
        assert [r.url for r in refs] == [
            CH1_URL,
            CH2_URL,
            f"{ORIGIN}/b/{BID}/c/4772919",
            CH4_URL,
        ]
        assert [c.request.url for c in responses.calls] == [TOC_URL_1, TOC_URL_2]

    @responses.activate
    def test_non_contiguous_toc_pages_raise(self):
        broken = fx("toc_page1.html").replace("/b/506549/cs/2", "/b/506549/cs/3")
        responses.add(responses.GET, TOC_URL_1, body=broken)
        with pytest.raises(ScrapeError, match="không liền mạch"):
            make_adapter().fetch_chapter_list(BOOK_URL)

    def test_empty_toc_raises(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_toc_page("<html><body></body></html>", BID, TOC_URL_1)


class TestChapterContent:
    def test_page_count_and_position_detection(self):
        assert page_count(fx("chapter_p1of2.html"), CH1_URL) == 2
        assert page_position(fx("chapter_p1of2.html")) == (1, 2)
        assert page_position(fx("chapter_p2of2.html")) == (2, 2)
        assert page_count(fx("chapter_single.html"), CH4_URL) == 1
        assert page_position(fx("chapter_single.html")) is None

    def test_title_tag_is_a_backup_signal_when_h1_is_missing(self):
        html = fx("chapter_p1of2.html").replace("<h1>第1章 启程（1/2）</h1>", "")
        assert page_count(html, CH1_URL) == 2
        assert page_position(html) == (1, 2)

    def test_missing_both_h1_and_title_raises(self):
        with pytest.raises(ScrapeError, match="no heading"):
            page_count("<html><body><div id='booktxt'><p>正文</p></div></body></html>", CH1_URL)

    def test_promo_trailer_paragraphs_are_stripped(self):
        body = parse_chapter(fx("chapter_p2of2.html"), "", f"{CH1_URL}?page=2")
        assert body == "第二页第一段正文内容。\n\n第二页第二段正文内容。"
        assert "小说之家" not in body
        assert "粘贴以下网址" not in body
        assert "xszj.org" not in body

    @responses.activate
    def test_paginated_chapter_fetches_all_sub_pages_and_never_follows_next_chapter(self):
        responses.add(responses.GET, CH1_URL, body=fx("chapter_p1of2.html"))
        responses.add(responses.GET, f"{CH1_URL}?page=2", body=fx("chapter_p2of2.html"))

        ref = ChapterRef(index=0, title="第1章 启程", url=CH1_URL)
        body = make_adapter().fetch_chapter(ref)

        assert body == (
            "第一页第一段正文内容。\n\n"
            "第一页第二段正文内容。\n\n"
            "第二页第一段正文内容。\n\n"
            "第二页第二段正文内容。"
        )
        assert [c.request.url for c in responses.calls] == [CH1_URL, f"{CH1_URL}?page=2"]

    @responses.activate
    def test_unpaginated_chapter_fetches_only_one_page(self):
        responses.add(responses.GET, CH4_URL, body=fx("chapter_single.html"))

        ref = ChapterRef(index=3, title="第4章 终幕", url=CH4_URL)
        body = make_adapter().fetch_chapter(ref)

        assert body == "单页章节第一段正文。\n\n单页章节第二段正文。"
        assert [c.request.url for c in responses.calls] == [CH4_URL]

    @responses.activate
    def test_sub_page_returning_page_one_again_raises(self):
        # Out-of-range ?page=N on xszj.org returns page 1 with HTTP 200 and （1/M）.
        responses.add(responses.GET, CH1_URL, body=fx("chapter_p1of2.html"))
        responses.add(responses.GET, f"{CH1_URL}?page=2", body=fx("chapter_p1of2.html"))

        ref = ChapterRef(index=0, title="第1章 启程", url=CH1_URL)
        with pytest.raises(ScrapeError, match="sai trang"):
            make_adapter().fetch_chapter(ref)


@pytest.mark.live
class TestLive:
    URL = "https://xszj.org/b/506549"

    def test_live_metadata_toc_and_chapters(self):
        adapter = XszjAdapter(HttpClient(delay_seconds=0.3))
        meta = adapter.fetch_metadata(self.URL)
        assert meta.title == "上朝第一天，吃瓜自爆我女扮男装"
        assert meta.author == "悠悠哉又哉"
        assert meta.url == self.URL
        assert meta.cover_url == ""
        assert meta.source_lang == "zh"

        refs = adapter.fetch_chapter_list(self.URL)
        assert len(refs) >= 95
        assert refs[0].title.startswith("第1章")

        ch1 = adapter.fetch_chapter(refs[0])
        assert len(ch1) > 1500
        assert "小说之家为广大书友们" not in ch1
        assert "本书网址" not in ch1

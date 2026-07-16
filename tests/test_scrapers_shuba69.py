"""Tests for the 69shuba parsing surface.

No browser here: 69shuba's fetching goes through Playwright, so the parse functions
are pure and take markup. They run against the real captured fixtures — the book info
page, the TOC and one chapter — so the selectors are checked against markup that
actually exists rather than a hand-written approximation.
"""

from __future__ import annotations

import pytest

from noveltrans.errors import ScrapeError
from noveltrans.scrapers.shuba69 import (
    _clean_title,
    book_id,
    info_url,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    toc_url,
)

from conftest import load_fixture

BOOK_URL = "https://www.69shuba.com/book/59024/"
INFO_URL = "https://www.69shuba.com/book/59024.htm"
CHAPTER_URL = "https://www.69shuba.com/txt/59024/38369377"
CHAPTER_TITLE = "第1章 这是什么地狱开局？"


class TestUrlDerivation:
    # One book id yields three different pages — the metadata and the chapter list do
    # NOT live on the same URL, which is the thing most likely to be got wrong.
    @pytest.mark.parametrize("url", [BOOK_URL, INFO_URL, "https://www.69shuba.com/book/59024"])
    def test_book_id_from_either_url_form(self, url):
        assert book_id(url) == "59024"

    def test_info_and_toc_urls_differ(self):
        assert info_url(BOOK_URL) == INFO_URL
        assert toc_url(BOOK_URL) == BOOK_URL
        assert info_url(BOOK_URL) != toc_url(BOOK_URL)

    def test_derivation_is_stable_from_either_form(self):
        # Whichever page the user pastes, both targets resolve the same.
        assert info_url(INFO_URL) == info_url(BOOK_URL)
        assert toc_url(INFO_URL) == toc_url(BOOK_URL)

    def test_origin_is_preserved_so_mirrors_keep_working(self):
        assert info_url("https://www.69shuba.cx/book/59024/").startswith(
            "https://www.69shuba.cx/"
        )

    def test_rejects_a_url_without_a_book_id(self):
        with pytest.raises(ScrapeError):
            book_id("https://www.69shuba.com/novels/hot")


class TestCleanTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.第1章 这是什么地狱开局？", "第1章 这是什么地狱开局？"),  # ch.1 has the prefix
            ("第2章 剧情开始，第一女主出现", "第2章 剧情开始，第一女主出现"),  # ch.2 doesn't
            ("199.第199章 横行万古", "第199章 横行万古"),
            ("12．第12章 全角句点", "第12章 全角句点"),
            ("12、第12章 顿号", "第12章 顿号"),
        ],
    )
    def test_strips_the_index_prefix_when_present(self, raw, expected):
        assert _clean_title(raw) == expected

    def test_leaves_a_chapter_number_that_is_not_a_prefix_alone(self):
        # Must not eat the 第N章 itself.
        assert _clean_title("第1章 这是什么地狱开局？") == "第1章 这是什么地狱开局？"

    def test_is_idempotent(self):
        assert _clean_title(_clean_title("1.第1章 x")) == _clean_title("1.第1章 x")


class TestMetadata:
    def test_reads_the_opengraph_tags(self):
        meta = parse_metadata(load_fixture("69shuba", "novel.html"), BOOK_URL, "69shuba")
        assert meta.title == "穿书反派跟班，开局被女主盯上"
        assert meta.author == "远赴人间惊鸿客"
        assert meta.cover_url.endswith("59024s.jpg")
        assert meta.site == "69shuba"
        assert meta.source_lang == "zh"

    def test_url_is_echoed_unchanged(self):
        # The library keys projects off the URL the user gave; rewriting it here would
        # orphan them.
        meta = parse_metadata(load_fixture("69shuba", "novel.html"), BOOK_URL, "69shuba")
        assert meta.url == BOOK_URL

    def test_description_has_its_literal_br_tags_unescaped(self):
        # og:description arrives as an attribute value carrying literal "<br />" text,
        # so BeautifulSoup never strips it for us.
        meta = parse_metadata(load_fixture("69shuba", "novel.html"), BOOK_URL, "69shuba")
        assert meta.description
        assert "<br" not in meta.description
        assert "\n\n\n" not in meta.description  # blank runs collapsed
        assert meta.description == meta.description.strip()

    def test_raises_when_the_title_is_missing(self):
        with pytest.raises(ScrapeError, match="layout may have changed"):
            parse_metadata("<html><head></head><body></body></html>", BOOK_URL, "69shuba")


class TestChapterList:
    def test_reads_every_chapter_from_the_single_toc_page(self):
        refs = parse_chapter_list(load_fixture("69shuba", "toc.html"), BOOK_URL)
        # The TOC is not paginated: og:novel:latest_chapter_name on the info page says
        # 199 is the last chapter, and the TOC carries exactly 199 links.
        assert len(refs) == 199
        assert refs[0].index == 0 and refs[-1].index == 198

    def test_first_and_last_refs(self):
        refs = parse_chapter_list(load_fixture("69shuba", "toc.html"), BOOK_URL)
        assert refs[0].url == CHAPTER_URL
        assert refs[0].title == CHAPTER_TITLE  # "1." prefix stripped
        assert refs[-1].title == "第199章 横行万古"  # "199." prefix stripped

    def test_titles_are_normalised_uniformly(self):
        # The site prefixes some titles with an index and not others; after parsing,
        # none should carry one.
        refs = parse_chapter_list(load_fixture("69shuba", "toc.html"), BOOK_URL)
        assert all(r.title.startswith("第") for r in refs)

    def test_urls_are_absolute_chapter_links(self):
        refs = parse_chapter_list(load_fixture("69shuba", "toc.html"), BOOK_URL)
        assert all(r.url.startswith("https://www.69shuba.com/txt/59024/") for r in refs)

    def test_raises_when_the_catalog_is_missing(self):
        with pytest.raises(ScrapeError, match="layout may have changed"):
            parse_chapter_list("<html><body><a href='/txt/1/2'>x</a></body></html>", BOOK_URL)


class TestChapterContent:
    def test_extracts_the_body(self):
        text = parse_chapter(load_fixture("69shuba", "chapter.html"), CHAPTER_TITLE, CHAPTER_URL)
        assert len(text) > 1500  # ~2.1k chars; 40.75万字/199 ≈ 2048 average
        assert "\n\n" in text  # paragraphs separated by blank lines
        assert text.endswith("(本章完)")

    def test_strips_the_duplicated_title_line(self):
        text = parse_chapter(load_fixture("69shuba", "chapter.html"), CHAPTER_TITLE, CHAPTER_URL)
        assert not text.startswith(CHAPTER_TITLE)

    def test_strips_the_title_even_when_the_ref_carries_an_index_prefix(self):
        # The real trap: the TOC title is "1.第1章 …" while the body line is "第1章 …",
        # so an exact match would silently fail to strip. Titles stored before the
        # normaliser existed still look like this.
        text = parse_chapter(
            load_fixture("69shuba", "chapter.html"), "1." + CHAPTER_TITLE, CHAPTER_URL
        )
        assert not text.startswith(CHAPTER_TITLE)

    def test_does_not_eat_a_real_first_line(self):
        # Over-stripping is the dangerous direction: it deletes prose silently.
        text = parse_chapter(
            load_fixture("69shuba", "chapter.html"), "第999章 不匹配的标题", CHAPTER_URL
        )
        assert text.startswith(CHAPTER_TITLE)  # nothing stripped when it doesn't match

    def test_removes_site_chrome(self):
        text = parse_chapter(load_fixture("69shuba", "chapter.html"), CHAPTER_TITLE, CHAPTER_URL)
        assert "2024-09-08" not in text  # div.txtinfo date
        assert "作者：" not in text  # div.txtinfo byline
        assert "69书吧" not in text

    def test_raises_when_the_container_is_missing(self):
        with pytest.raises(ScrapeError, match="layout may have changed"):
            parse_chapter("<html><body><p>x</p></body></html>", CHAPTER_TITLE, CHAPTER_URL)

    def test_raises_when_the_body_is_empty(self):
        markup = "<html><body><div class='txtnav'><h1 class='hide720'>t</h1></div></body></html>"
        with pytest.raises(ScrapeError, match="empty"):
            parse_chapter(markup, CHAPTER_TITLE, CHAPTER_URL)

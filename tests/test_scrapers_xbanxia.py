import responses

from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.xbanxia import XbanxiaAdapter

from conftest import load_fixture

NOVEL_URL = "https://www.xbanxia.cc/books/331303.html"
CHAPTER_URL = "https://www.xbanxia.cc/books/331303/60799199.html"


def make_adapter() -> XbanxiaAdapter:
    return XbanxiaAdapter(HttpClient(delay_seconds=0))


class TestRegistry:
    def test_matches_novel_url(self):
        assert XbanxiaAdapter.matches(NOVEL_URL)
        assert XbanxiaAdapter.matches("http://xbanxia.com/books/1.html")
        assert not XbanxiaAdapter.matches("https://example.com/books/1.html")

    def test_adapter_for_url(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, XbanxiaAdapter)


class TestMetadata:
    @responses.activate
    def test_fetch_metadata(self):
        responses.get(NOVEL_URL, body=load_fixture("xbanxia", "novel.html"))
        meta = make_adapter().fetch_metadata(NOVEL_URL)
        assert meta.title == "豪門甜寵：傅先生的團寵小嬌妻"
        assert meta.author == "檸檬味DE貓"
        assert meta.description.startswith("【豪門甜寵雙潔1v1")
        assert meta.cover_url.endswith("331303s.jpg")
        assert meta.site == "xbanxia"


class TestChapterList:
    @responses.activate
    def test_fetch_chapter_list(self):
        responses.get(NOVEL_URL, body=load_fixture("xbanxia", "novel.html"))
        refs = make_adapter().fetch_chapter_list(NOVEL_URL)
        assert len(refs) == 234
        assert refs[0].index == 0
        assert refs[0].title == "第1章 救命的十萬塊沒了"
        assert refs[0].url == CHAPTER_URL
        assert refs[-1].title == "第234章 、最終章：番外、萌寶篇"


class TestChapterContent:
    @responses.activate
    def test_fetch_chapter(self):
        responses.get(CHAPTER_URL, body=load_fixture("xbanxia", "chapter.html"))
        ref = ChapterRef(index=0, title="第1章 救命的十萬塊沒了", url=CHAPTER_URL)
        text = make_adapter().fetch_chapter(ref)
        assert text.startswith("B市，醫院。")
        # title-dup line stripped, site chrome (span slogan) removed
        assert "第1章 救命的十萬塊沒了" not in text
        assert "半夏小說，快樂很多" not in text
        # paragraphs separated by blank lines
        assert "\n\n" in text
        assert len(text) > 1500

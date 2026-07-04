import json

import responses
from responses import matchers

from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.ixdzs import IxdzsAdapter

from conftest import load_fixture

NOVEL_URL = "https://ixdzs8.com/read/620438/"
CLIST_URL = "https://ixdzs8.com/novel/clist/"
CHAPTER_URL = "https://ixdzs8.com/read/620438/p1.html"
CHAPTER_TITLE = "第1章 穿进兽世即将分娩，却掉进雄性洞中"


def make_adapter() -> IxdzsAdapter:
    return IxdzsAdapter(HttpClient(delay_seconds=0))


class TestRegistry:
    def test_matches(self):
        assert IxdzsAdapter.matches(NOVEL_URL)
        assert IxdzsAdapter.matches("https://ixdzs.tw/read/1/")
        assert not IxdzsAdapter.matches("https://ixdzs8.com/hot/")

    def test_adapter_for_url(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, IxdzsAdapter)


class TestMetadata:
    @responses.activate
    def test_fetch_metadata(self):
        responses.get(NOVEL_URL, body=load_fixture("ixdzs", "novel.html"))
        meta = make_adapter().fetch_metadata(NOVEL_URL)
        assert meta.title == "丑雌一胎七崽？兽夫们跪求复合"
        assert meta.author == "金顶顶"
        assert meta.description.startswith("沈月穿成了")
        assert meta.site == "ixdzs"


class TestChapterList:
    @responses.activate
    def test_fetch_chapter_list(self):
        responses.post(
            CLIST_URL,
            json=json.loads(load_fixture("ixdzs", "clist.json")),
            match=[matchers.urlencoded_params_matcher({"bid": "620438"})],
        )
        refs = make_adapter().fetch_chapter_list(NOVEL_URL)
        assert len(refs) >= 400
        assert refs[0].title == CHAPTER_TITLE
        assert refs[0].url == CHAPTER_URL
        # indices are contiguous even if the site interleaves volume headers
        assert [r.index for r in refs] == list(range(len(refs)))


class TestChapterContent:
    @responses.activate
    def test_fetch_chapter_solves_challenge(self):
        # 1st GET -> JS challenge page; 2nd GET (?challenge=token) -> real page
        responses.get(
            CHAPTER_URL,
            body=load_fixture("ixdzs", "challenge.html"),
            match=[matchers.query_param_matcher({})],
        )
        responses.get(
            CHAPTER_URL,
            body=load_fixture("ixdzs", "chapter.html"),
            match=[
                matchers.query_param_matcher(
                    {
                        "challenge": (
                            "MTc4MzE2NzU0NzpmY2EzZGZjY2U3OWExYWMyODg4NjY4"
                            "YjNhODBmYWFlN2UxMGNhMDg1YWNjNDJhYTIzY2Q2OWIzZTc5OWEyMzQ4"
                        )
                    }
                )
            ],
        )
        ref = ChapterRef(index=0, title=CHAPTER_TITLE, url=CHAPTER_URL)
        text = make_adapter().fetch_chapter(ref)
        assert text.startswith("沈月刚穿进兽世大陆")
        assert CHAPTER_TITLE not in text
        assert len(text) > 1000

    @responses.activate
    def test_fetch_chapter_direct(self):
        # cookie already set -> no challenge round-trip
        responses.get(CHAPTER_URL, body=load_fixture("ixdzs", "chapter.html"))
        ref = ChapterRef(index=0, title=CHAPTER_TITLE, url=CHAPTER_URL)
        text = make_adapter().fetch_chapter(ref)
        assert text.startswith("沈月刚穿进兽世大陆")

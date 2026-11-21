import json

import httpx
import pytest

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import Http2Client, HttpClient
from noveltrans.scrapers.ixdzs import IxdzsAdapter

from conftest import load_fixture

NOVEL_URL = "https://ixdzs8.com/read/620438/"
CLIST_URL = "https://ixdzs8.com/novel/clist/"
CHAPTER_URL = "https://ixdzs8.com/read/620438/p1.html"
CHAPTER_TITLE = "第1章 穿进兽世即将分娩，却掉进雄性洞中"
CHALLENGE_TOKEN = (
    "MTc4MzE2NzU0NzpmY2EzZGZjY2U3OWExYWMyODg4NjY4"
    "YjNhODBmYWFlN2UxMGNhMDg1YWNjNDJhYTIzY2Q2OWIzZTc5OWEyMzQ4"
)


def html(name: str) -> httpx.Response:
    return httpx.Response(
        200, content=load_fixture("ixdzs", name).encode("utf-8"),
        headers={"content-type": "text/html; charset=utf-8"},
    )


def make_adapter(handler) -> IxdzsAdapter:
    """An adapter whose HTTP goes to `handler` (httpx MockTransport), never the network."""
    client = Http2Client(delay_seconds=0, transport=httpx.MockTransport(handler))
    return IxdzsAdapter(client)


class TestRegistry:
    def test_matches(self):
        assert IxdzsAdapter.matches(NOVEL_URL)
        assert IxdzsAdapter.matches("https://ixdzs.tw/read/1/")
        assert not IxdzsAdapter.matches("https://ixdzs8.com/hot/")

    def test_adapter_for_url_wraps_the_caller_client_in_http2(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, IxdzsAdapter)
        assert isinstance(adapter.client, Http2Client)


class TestMetadata:
    def test_fetch_metadata(self):
        make = lambda req: html("novel.html")  # noqa: E731
        meta = make_adapter(make).fetch_metadata(NOVEL_URL)
        assert meta.title == "丑雌一胎七崽？兽夫们跪求复合"
        assert meta.author == "金顶顶"
        assert meta.description.startswith("沈月穿成了")
        assert meta.site == "ixdzs"


class TestChapterList:
    def test_fetch_chapter_list(self):
        def handler(request):
            assert request.method == "POST" and str(request.url) == CLIST_URL
            assert request.content == b"bid=620438"
            return httpx.Response(200, json=json.loads(load_fixture("ixdzs", "clist.json")))

        refs = make_adapter(handler).fetch_chapter_list(NOVEL_URL)
        assert len(refs) >= 400
        assert refs[0].title == CHAPTER_TITLE
        assert refs[0].url == CHAPTER_URL
        # indices are contiguous even if the site interleaves volume headers
        assert [r.index for r in refs] == list(range(len(refs)))


class TestChapterContent:
    def test_fetch_chapter_solves_challenge(self):
        # 1st GET -> JS challenge page; 2nd GET (?challenge=token) -> real page
        def handler(request):
            assert str(request.url).startswith(CHAPTER_URL)
            if request.url.params.get("challenge") == CHALLENGE_TOKEN:
                return html("chapter.html")
            assert "challenge" not in request.url.params
            return html("challenge.html")

        ref = ChapterRef(index=0, title=CHAPTER_TITLE, url=CHAPTER_URL)
        text = make_adapter(handler).fetch_chapter(ref)
        assert text.startswith("沈月刚穿进兽世大陆")
        assert CHAPTER_TITLE not in text
        assert len(text) > 1000

    def test_fetch_chapter_direct(self):
        # cookie already set -> no challenge round-trip
        ref = ChapterRef(index=0, title=CHAPTER_TITLE, url=CHAPTER_URL)
        text = make_adapter(lambda req: html("chapter.html")).fetch_chapter(ref)
        assert text.startswith("沈月刚穿进兽世大陆")


class TestHttp2Client:
    def test_sends_the_source_clients_headers_and_cookie(self):
        source = HttpClient(delay_seconds=0, cookies="a=1")
        seen = {}

        def handler(request):
            seen["ua"] = request.headers.get("user-agent")
            seen["cookie"] = request.headers.get("cookie")
            return html("novel.html")

        client = Http2Client.wrap(source)
        client._http = httpx.Client(http2=True, transport=httpx.MockTransport(handler))
        client.get_html(NOVEL_URL)
        assert seen["ua"] == source._session.headers["User-Agent"]
        assert seen["cookie"] == "a=1"

    def test_a_block_becomes_a_scrape_error(self):
        client = Http2Client(
            delay_seconds=0, max_retries=1,
            transport=httpx.MockTransport(lambda req: httpx.Response(403)),
        )
        with pytest.raises(ScrapeError, match="403"):
            client.get_html(NOVEL_URL)

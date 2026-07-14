import pytest
import responses

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.giatocvuongtai import GiatocvuongtaiAdapter

from conftest import load_fixture

SLUG = "dm-edit-chao-mung-den-voi-phong-livestream-ac-mong"
STORY_URL = f"https://giatocvuongtai.com/stories/{SLUG}/"
STORY_API = f"https://giatocvuongtai.com/api/public/story/{SLUG}.json"

# ids in the trimmed story.json fixture: [0]=published, [1]=published, [2]=unpublished
CH1_ID = "f8d51ca8-d1bd-44f1-8811-0027b23d67ce"
CH2_ID = "3390ed1b-d6fd-4c58-ac2c-98bcb3e989c9"
CH1_API = f"https://giatocvuongtai.com/api/public/chapter/{CH1_ID}.json"


def make_adapter() -> GiatocvuongtaiAdapter:
    return GiatocvuongtaiAdapter(HttpClient(delay_seconds=0))


class TestRegistry:
    def test_matches_story_url_with_and_without_trailing_slash(self):
        assert GiatocvuongtaiAdapter.matches(STORY_URL)
        assert GiatocvuongtaiAdapter.matches(STORY_URL.rstrip("/"))

    def test_rejects_foreign_and_reader_urls(self):
        assert not GiatocvuongtaiAdapter.matches(f"https://example.com/stories/{SLUG}")
        # reader URLs carry no slug — routed via the /stories/<slug> page instead
        assert not GiatocvuongtaiAdapter.matches(
            "https://giatocvuongtai.com/reader/?storyId=abc&chapter=def"
        )

    def test_adapter_for_url(self):
        adapter = adapter_for_url(STORY_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, GiatocvuongtaiAdapter)


class TestMetadata:
    @responses.activate
    def test_fetch_metadata(self):
        responses.get(STORY_API, body=load_fixture("giatocvuongtai", "story.json"))
        meta = make_adapter().fetch_metadata(STORY_URL)
        assert meta.title == "[ĐM/EDIT] CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG"
        assert meta.author == "Tang Ốc"
        assert meta.description.startswith("Tác giả: Tang Ốc")
        assert meta.cover_url.startswith("https://")
        assert meta.site == "giatocvuongtai"
        assert meta.source_lang == "vi"

    @responses.activate
    def test_url_normalized_from_slashless_input(self):
        responses.get(STORY_API, body=load_fixture("giatocvuongtai", "story.json"))
        meta = make_adapter().fetch_metadata(STORY_URL.rstrip("/"))
        assert meta.url == STORY_URL  # canonical trailing-slash form


class TestChapterList:
    @responses.activate
    def test_only_published_chapters_sequential_index(self):
        responses.get(STORY_API, body=load_fixture("giatocvuongtai", "story.json"))
        refs = make_adapter().fetch_chapter_list(STORY_URL)
        # fixture has 3 chapters, one unpublished → 2 refs
        assert len(refs) == 2
        assert [r.index for r in refs] == [0, 1]
        assert refs[0].title == "TRƯỜNG TRUNG HỌC ĐỨC TÀI"
        assert refs[0].url == CH1_API
        assert refs[1].url.endswith(f"{CH2_ID}.json")

    @responses.activate
    def test_empty_chapter_list_raises(self):
        responses.get(STORY_API, json={"data": {"chapters": []}, "error": None})
        with pytest.raises(ScrapeError):
            make_adapter().fetch_chapter_list(STORY_URL)


class TestChapterContent:
    @responses.activate
    def test_dict_content(self):
        responses.get(CH1_API, body=load_fixture("giatocvuongtai", "chapter.json"))
        text = make_adapter().fetch_chapter(ChapterRef(index=0, title="c1", url=CH1_API))
        assert text.startswith("“Rầm!”")
        paras = text.split("\n\n")
        assert len(paras) == 5  # fixture trimmed to 5 paragraph blocks
        assert "{" not in text and "blocks" not in text  # no raw JSON leakage

    @responses.activate
    def test_string_content_matches_dict_content(self):
        responses.get(CH1_API, body=load_fixture("giatocvuongtai", "chapter.json"))
        dict_text = make_adapter().fetch_chapter(ChapterRef(index=0, title="c", url=CH1_API))
        responses.replace(
            responses.GET,
            CH1_API,
            body=load_fixture("giatocvuongtai", "chapter_stringcontent.json"),
        )
        str_text = make_adapter().fetch_chapter(ChapterRef(index=0, title="c", url=CH1_API))
        assert str_text == dict_text  # string→json.loads path yields identical text

    @responses.activate
    def test_mixed_blocks_skips_non_text(self):
        responses.get(CH1_API, body=load_fixture("giatocvuongtai", "chapter_mixed_blocks.json"))
        text = make_adapter().fetch_chapter(ChapterRef(index=0, title="c", url=CH1_API))
        # heading + 2 paragraphs kept; divider/image/empty-paragraph dropped
        assert text == "Phần Một\n\nCâu văn thứ nhất.\n\nCâu thứ hai."

    @responses.activate
    def test_missing_content_raises(self):
        responses.get(CH1_API, json={"data": {"id": "x"}, "error": None})
        with pytest.raises(ScrapeError):
            make_adapter().fetch_chapter(ChapterRef(index=0, title="c", url=CH1_API))

    @responses.activate
    def test_empty_after_extraction_raises(self):
        responses.get(
            CH1_API,
            json={"data": {"content": {"blocks": [{"type": "divider"}]}}, "error": None},
        )
        with pytest.raises(ScrapeError):
            make_adapter().fetch_chapter(ChapterRef(index=0, title="c", url=CH1_API))


class TestErrors:
    @responses.activate
    def test_api_error_envelope_raises(self):
        responses.get(STORY_API, json={"data": None, "error": "not found"})
        with pytest.raises(ScrapeError):
            make_adapter().fetch_metadata(STORY_URL)

    @responses.activate
    def test_non_json_body_raises(self):
        responses.get(STORY_API, body="<html>nope</html>")
        with pytest.raises(ScrapeError):
            make_adapter().fetch_metadata(STORY_URL)

    def test_url_without_slug_raises(self):
        with pytest.raises(ScrapeError):
            make_adapter().fetch_metadata("https://giatocvuongtai.com/stories/")


@pytest.mark.live
class TestLive:
    def test_metadata_and_first_chapter(self):
        adapter = make_adapter()
        meta = adapter.fetch_metadata(STORY_URL)
        assert meta.title and meta.author
        assert meta.source_lang == "vi"
        refs = adapter.fetch_chapter_list(STORY_URL)
        assert len(refs) > 100  # ~745 published chapters
        text = adapter.fetch_chapter(refs[0])
        assert len(text) > 200

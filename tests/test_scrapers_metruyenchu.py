import pytest
import responses

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.metruyenchu import MetruyenchuAdapter

from conftest import load_fixture

NOVEL_URL = "https://metruyenchu.com.vn/truyen-mau-thu"
CHAPTER_URL = "https://metruyenchu.com.vn/truyen-mau-thu/chuong-01-hash01"
LIST_URL = "https://metruyenchu.com.vn/get/listchap/83685"
EMPTY_PAGE = '{"data": ""}'


def make_adapter() -> MetruyenchuAdapter:
    return MetruyenchuAdapter(HttpClient(delay_seconds=0))


def add_json(rsps: responses.RequestsMock, page: int, body: str) -> None:
    rsps.add(
        responses.GET,
        f"{LIST_URL}?page={page}",
        body=body,
        content_type="application/json",
    )


def mock_toc(rsps: responses.RequestsMock) -> None:
    """Landing (1-3) + pages 2-4 (4-9, with a duplicate 9 and a non-chapter link) + empty page 5."""
    rsps.add(responses.GET, NOVEL_URL, body=load_fixture("metruyenchu", "novel.html"))
    add_json(rsps, 2, load_fixture("metruyenchu", "listchap_page2.json"))
    add_json(rsps, 3, load_fixture("metruyenchu", "listchap_page3.json"))
    add_json(rsps, 4, load_fixture("metruyenchu", "listchap_page4.json"))
    add_json(rsps, 5, EMPTY_PAGE)


class TestRegistry:
    def test_matches_novel_and_chapter_urls(self):
        assert MetruyenchuAdapter.matches(NOVEL_URL)
        assert MetruyenchuAdapter.matches(CHAPTER_URL)
        assert not MetruyenchuAdapter.matches("https://example.com/truyen-mau-thu")

    def test_adapter_for_url(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, MetruyenchuAdapter)


class TestMetadata:
    @responses.activate
    def test_fields_decode_and_normalize(self):
        responses.add(
            responses.GET, NOVEL_URL, body=load_fixture("metruyenchu", "novel.html")
        )
        meta = make_adapter().fetch_metadata(NOVEL_URL)
        assert meta.title == "Truyện Mẫu Thử"
        assert meta.author == "Tác Giả Mẫu"
        assert meta.description.startswith("Truyện Truyện Mẫu Thử")
        assert meta.cover_url.endswith("fixture.jpeg")
        assert meta.source_lang == "vi"
        assert meta.site == "metruyenchu"

    @responses.activate
    def test_chapter_url_normalizes_to_landing_url(self):
        responses.add(
            responses.GET, NOVEL_URL, body=load_fixture("metruyenchu", "novel.html")
        )
        meta = make_adapter().fetch_metadata(CHAPTER_URL)
        assert meta.url == NOVEL_URL

    @responses.activate
    def test_missing_title_raises(self):
        responses.add(responses.GET, NOVEL_URL, body="<html><body></body></html>")
        with pytest.raises(ScrapeError):
            make_adapter().fetch_metadata(NOVEL_URL)


class TestChapterList:
    @responses.activate
    def test_full_toc_is_sorted_and_contiguous(self):
        mock_toc(responses)
        toc = make_adapter().fetch_chapter_list(NOVEL_URL)
        numbers = [int(r.title.split()[1].rstrip(":")) for r in toc]
        assert numbers == list(range(1, 10))
        assert [r.index for r in toc] == list(range(len(toc)))

    @responses.activate
    def test_sidebar_link_is_excluded(self):
        mock_toc(responses)
        toc = make_adapter().fetch_chapter_list(NOVEL_URL)
        assert not any("Sidebar" in r.title for r in toc)

    @responses.activate
    def test_duplicate_number_keeps_first_entry(self):
        mock_toc(responses)
        toc = make_adapter().fetch_chapter_list(NOVEL_URL)
        nine = [r for r in toc if r.title.startswith("Chương 09:")]
        assert len(nine) == 1
        # The slug without a number appears first on page 4, so it wins.
        assert nine[0].url.endswith("chuong-chuong--hashnonum")

    @responses.activate
    def test_odd_slugs_use_title_number_and_skip_unnumbered_links(self):
        mock_toc(responses)
        toc = make_adapter().fetch_chapter_list(NOVEL_URL)
        by_num = {int(r.title.split()[1].rstrip(":")): r for r in toc}
        assert by_num[8].url == (
            "https://metruyenchu.com.vn/truyen-mau-thu/chuong-chuong-08-hash08"
        )
        assert not any("Trang kế tiếp" in r.title for r in toc)

    @responses.activate
    def test_titles_are_whitespace_normalized_and_urls_absolute(self):
        mock_toc(responses)
        toc = make_adapter().fetch_chapter_list(NOVEL_URL)
        first = toc[0]
        assert first.title == "Chương 01: Mở Đầu Mẫu"
        assert first.url == "https://metruyenchu.com.vn/truyen-mau-thu/chuong-01-hash01"

    @responses.activate
    def test_missing_chapter_list_raises(self):
        responses.add(responses.GET, NOVEL_URL, body="<html><body></body></html>")
        with pytest.raises(ScrapeError):
            make_adapter().fetch_chapter_list(NOVEL_URL)

    @responses.activate
    def test_numbering_gap_raises(self):
        responses.add(
            responses.GET, NOVEL_URL, body=load_fixture("metruyenchu", "novel.html")
        )
        # Landing has 1-3; page 2 jumps to 5-6, so chapter 4 is missing.
        add_json(
            responses,
            2,
            '{"data": "<li><a href=\'/truyen-mau-thu/chuong-05-h5\'>Chương 05: X</a></li>'
            "<li><a href='/truyen-mau-thu/chuong-06-h6'>Chương 06: Y</a></li>\"}",
        )
        add_json(responses, 3, EMPTY_PAGE)
        with pytest.raises(ScrapeError, match="khoảng trống"):
            make_adapter().fetch_chapter_list(NOVEL_URL)


class TestChapter:
    @responses.activate
    def test_paragraphs_join_and_footer_excluded(self):
        responses.add(
            responses.GET, CHAPTER_URL, body=load_fixture("metruyenchu", "chapter.html")
        )
        ref = ChapterRef(index=0, title="Chương 01", url=CHAPTER_URL)
        text = make_adapter().fetch_chapter(ref)
        assert text.split("\n\n") == [
            '"Đây là đoạn mẫu thứ nhất."',
            "Nhân vật chính bước vào phòng sách rộng lớn.",
            "Đoạn mẫu thứ ba có dấu tiếng Việt: Ánh sáng chiếu qua cửa sổ.",
            "Đoạn mẫu thứ tư kết thúc chương.",
        ]
        assert "Tải Ebook" not in text
        assert "sưu tầm" not in text  # footer disclaimer sits outside div.truyen

    @responses.activate
    def test_output_is_nfc_normalized(self):
        decomposed = "Cha" + "̀"  # "Chà" in NFD form
        html = load_fixture("metruyenchu", "chapter.html").replace(
            "Nhân vật chính", decomposed + " nhân vật chính", 1
        )
        responses.add(responses.GET, CHAPTER_URL, body=html)
        ref = ChapterRef(index=0, title="Chương 01", url=CHAPTER_URL)
        text = make_adapter().fetch_chapter(ref)
        assert "Chà nhân vật chính" in text
        assert decomposed not in text

    @responses.activate
    def test_gated_last_chapter_raises_without_calling_verification_service(self):
        gated_url = "https://metruyenchu.com.vn/truyen-mau-thu/chuong-342-hash342"
        responses.add(
            responses.GET,
            gated_url,
            body=load_fixture("metruyenchu", "chapter_gated.html"),
        )
        ref = ChapterRef(index=8, title="Chương 09", url=gated_url)
        with pytest.raises(ScrapeError, match="xác minh thủ công"):
            make_adapter().fetch_chapter(ref)
        # Only the chapter page was requested; the verification service was never hit.
        assert [c.request.url for c in responses.calls] == [gated_url]

    @responses.activate
    def test_cloudflare_challenge_raises(self):
        responses.add(
            responses.GET,
            CHAPTER_URL,
            body="<html><body>Just a moment... cf-chl-bypass</body></html>",
        )
        ref = ChapterRef(index=0, title="Chương 01", url=CHAPTER_URL)
        with pytest.raises(ScrapeError, match="Cloudflare"):
            make_adapter().fetch_chapter(ref)

    @responses.activate
    def test_empty_body_without_gate_raises(self):
        responses.add(
            responses.GET,
            CHAPTER_URL,
            body='<html><body><div class="truyen"><main></main></div></body></html>',
        )
        ref = ChapterRef(index=0, title="Chương 01", url=CHAPTER_URL)
        with pytest.raises(ScrapeError, match="Không tìm thấy nội dung"):
            make_adapter().fetch_chapter(ref)

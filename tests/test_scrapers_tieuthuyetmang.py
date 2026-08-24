"""Feature 046 — tieuthuyetmang.com.

Fixtures are hand-built, not captured (`tests/fixtures/tieuthuyetmang/`): a raw capture
would commit the site's own chapter titles and would contain the recommended-story trap
only by luck, whereas a built one encodes it deliberately. Every string in them is invented.
"""

import pytest
import responses

from noveltrans.errors import AudioUnavailableError, AuthRequiredError, ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.tieuthuyetmang import (
    TieuthuyetmangAdapter,
    audio_entries,
    audio_gate_reason,
    audio_locked_count,
    audio_page_url,
    chapter_entries,
    chapter_number,
    chapter_url,
    find_audio_media,
    find_story,
    flight_payload,
    landing_url,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    slug,
)

from conftest import load_fixture

NOVEL_URL = "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem"
CHAPTER_URL = "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/2"


def landing() -> str:
    return load_fixture("tieuthuyetmang", "landing.html")


def stream() -> str:
    return flight_payload(landing())


def make_adapter() -> TieuthuyetmangAdapter:
    return TieuthuyetmangAdapter(HttpClient(delay_seconds=0))


class TestFlightPayload:
    def test_concatenates_every_push_in_document_order(self):
        """The fixture splits the page's own story across two pushes, so a parser that
        reads pushes one at a time instead of joining them cannot find it at all."""
        assert '"storySlug":"truyen-thu-nghiem"' in stream()

    def test_decodes_escaped_quotes_in_a_chapter_title(self):
        """A regex like `push\\(\\[1,"(.*?)"\\]\\)` truncates at the first escaped quote in a
        Vietnamese title and silently drops the rest of the stream."""
        titles = [entry.get("title") for entry in chapter_entries(stream())]
        assert 'Chương 3: Kẻ lạ nói "xin chào"' in titles

    def test_ignores_pushes_that_are_not_chunk_rows(self):
        markup = (
            '<script>self.__next_f.push([0,"bootstrap"])</script>'
            '<script>self.__next_f.push([1,"payload"])</script>'
        )
        assert flight_payload(markup) == "payload"

    def test_returns_empty_string_when_there_are_no_pushes(self):
        assert flight_payload("<html><body>nothing here</body></html>") == ""


class TestStoryAnchoring:
    """The trap, and the shape of it MEASURED live after getting it wrong.

    The landing page's own story component carries `storySlug` / `title` / `coverUrl` /
    `status` and nothing else. The sidebar recommendations carry MORE — `slug`, `author`,
    `excerpt`, `chapters_count` — so a filter written to the sidebar's shape excludes the
    real story and leaves only recommendations. That is exactly what shipped: the novel
    came back under another novel's title, author and cover.
    """

    def test_the_page_story_is_found_by_its_storyslug(self):
        assert find_story(stream(), "truyen-thu-nghiem")["title"] == "Truyện Thử Nghiệm"

    def test_a_story_without_chapters_count_or_author_is_still_found(self):
        """The regression, stated as a property: requiring the sidebar's richer fields is
        what hid the real story."""
        story = find_story(stream(), "truyen-thu-nghiem")
        assert "chapters_count" not in story and "author" not in story

    def test_a_richer_decoy_before_it_does_not_win(self):
        text = stream()
        assert text.index('"truyen-moi-nhat"') < text.index('"truyen-thu-nghiem"')
        assert find_story(text, "truyen-thu-nghiem")["title"] == "Truyện Thử Nghiệm"

    def test_a_richer_decoy_after_it_does_not_win(self):
        assert find_story(stream(), "truyen-thu-nghiem").get("chapters_count") is None

    def test_an_unknown_slug_raises_rather_than_returning_another_novel(self):
        """A wrong novel that looks right is far worse than a failure that says so — the
        old positional fallback returned a recommendation and nothing looked broken."""
        with pytest.raises(ScrapeError, match="Không tìm thấy dữ liệu truyện"):
            find_story(stream(), "khong-co-truyen-nay", NOVEL_URL)

    def test_a_story_keyed_by_plain_slug_is_also_accepted(self):
        text = stream().replace('"storySlug":"truyen-thu-nghiem"', '"slug":"truyen-thu-nghiem"')
        assert find_story(text, "truyen-thu-nghiem")["title"] == "Truyện Thử Nghiệm"


class TestMetadata:
    """Half the fields are server-rendered markup and never reach the flight stream, so
    metadata reads both halves of the page."""

    def test_reads_title_and_cover_from_the_story_component(self):
        meta = parse_metadata(landing(), NOVEL_URL, "tieuthuyetmang")
        assert meta.title == "Truyện Thử Nghiệm"
        assert meta.cover_url.endswith("truyen-thu-nghiem.jpg")

    def test_reads_the_author_from_the_server_rendered_link(self):
        """The flight data carries no author at all; it is a `/tac-gia/` link."""
        assert parse_metadata(landing(), NOVEL_URL, "tieuthuyetmang").author == "Người Viết Thử"

    def test_reads_the_whole_gioi_thieu_block_verbatim(self):
        """Including the "Giới thiệu truyện :" line it opens with: that reads like a UI
        label but sits inside the block and was written by whoever posted the novel."""
        meta = parse_metadata(landing(), NOVEL_URL, "tieuthuyetmang")
        assert meta.description.startswith("Giới thiệu truyện :")
        assert meta.description.rstrip().endswith("không có gì khác xảy ra.")

    def test_source_lang_is_vietnamese(self):
        assert parse_metadata(landing(), NOVEL_URL, "tieuthuyetmang").source_lang == "vi"

    def test_url_is_echoed_not_rewritten(self):
        """The library keys projects off the URL the user gave; rewriting it to the
        landing page orphans a project opened from a chapter link."""
        meta = parse_metadata(landing(), CHAPTER_URL, "tieuthuyetmang")
        assert meta.url == CHAPTER_URL

    def test_the_heading_carries_metadata_when_the_flight_data_is_unrecognisable(self):
        """A safe fallback uses only this page's own DOM — never another novel's object."""
        markup = landing().replace('"storySlug"', '"renamedSlug"')
        meta = parse_metadata(markup, NOVEL_URL, "tieuthuyetmang")
        assert meta.title == "Truyện Thử Nghiệm"
        assert meta.author == "Người Viết Thử"
        assert meta.cover_url.endswith("truyen-thu-nghiem.jpg")

    def test_raises_when_there_is_no_title_anywhere(self):
        with pytest.raises(ScrapeError, match="title not found"):
            parse_metadata("<html><body></body></html>", NOVEL_URL, "tieuthuyetmang")


class TestChapterList:
    def test_reading_order_is_by_chapter_number_not_document_order(self):
        refs = parse_chapter_list(landing(), NOVEL_URL)
        assert [chapter_number(ref.url) for ref in refs] == [1, 2, 3, 4, 5, 6]

    def test_index_is_dense_zero_based_not_the_site_chapter_number(self):
        refs = parse_chapter_list(landing(), NOVEL_URL)
        assert [ref.index for ref in refs] == list(range(len(refs)))

    def test_a_repeated_chapter_id_is_listed_once(self):
        refs = parse_chapter_list(landing(), NOVEL_URL)
        assert len(refs) == len({ref.url for ref in refs}) == 6

    def test_locked_chapters_are_listed_not_filtered(self):
        """`ChapterRef.index` is the project DB key. A list that shrank or grew with the
        cookie's state would silently re-map existing chapters onto different content on
        the next scan — corruption, not inconvenience."""
        assert len(parse_chapter_list(landing(), NOVEL_URL)) == 6
        assert sum(1 for e in chapter_entries(stream()) if e.get("isLocked")) == 4

    def test_a_sidebar_latest_chapter_stub_is_excluded(self):
        """Recommended stories carry a `latestChapter` with a number and a title but no
        per-chapter flags; those must not become chapters of this novel."""
        numbers = [entry["chapterNumber"] for entry in chapter_entries(stream())]
        assert 97 not in numbers and 480 not in numbers

    def test_titles_carry_no_lock_or_badge_decoration(self):
        """The title is persisted, exported and read aloud by TTS, and lock state belongs
        to the account rather than to the chapter."""
        for ref in parse_chapter_list(landing(), NOVEL_URL):
            assert "🔒" not in ref.title and "VIP" not in ref.title

    def test_a_chapter_with_no_title_falls_back_to_its_number(self):
        refs = parse_chapter_list(landing(), NOVEL_URL)
        assert refs[-1].title == "Chương 6"

    def test_raises_when_no_chapters_are_found(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html></html>", NOVEL_URL)


class TestChapterContent:
    def test_reads_a_body_from_an_article(self):
        text = parse_chapter(
            load_fixture("tieuthuyetmang", "chapter_free.html"),
            "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/1",
            number=1,
            title="Chương 1: Mở đầu",
        )
        assert text.startswith("Trời đổ mưa")
        assert "\n\n" in text  # paragraphs, blank-line separated
        assert "Chương 1: Mở đầu" not in text  # the reader's title line dropped
        assert "Trang chủ" not in text  # nav chrome excluded

    def test_reads_a_body_from_a_prose_wrapper_with_no_article(self):
        """MEASURED: this site renders long prose in a Tailwind `prose` /
        `whitespace-pre-wrap` wrapper — that is how the landing page's synopsis is built,
        and the first extractor only knew about `<article>`, so a page that plainly
        contained the chapter came back empty."""
        text = parse_chapter(
            load_fixture("tieuthuyetmang", "chapter_prose.html"),
            CHAPTER_URL,
            number=2,
            title="Chương 2: Người khách",
        )
        assert text.startswith("Trời đổ mưa")
        assert text.rstrip().endswith("không quan trọng.")

    def test_reads_a_body_from_an_unnamed_div(self):
        """The general fallback: the longest block of text on the page, which needs no
        class name at all — a chapter dwarfs every label and footer around it."""
        text = parse_chapter(
            load_fixture("tieuthuyetmang", "chapter_plaindiv.html"),
            "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/3",
            number=3,
        )
        assert text.startswith("Trời đổ mưa")
        assert "Nền tảng đọc truyện" not in text  # footer excluded

    def test_reads_a_body_carried_only_in_the_flight_stream(self):
        """And it may be a row of its own rather than a field of the chapter object —
        looking only at that object is what made the first version fail."""
        text = parse_chapter(
            load_fixture("tieuthuyetmang", "chapter_flight.html"),
            "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/4",
            number=4,
            title="Chương 4: Đêm mưa",
        )
        assert text.startswith("Trời đổ mưa")
        assert text.count("\n\n") == 5

    def test_a_locked_chapter_raises_auth_required(self):
        with pytest.raises(AuthRequiredError, match="trả phí"):
            parse_chapter(
                load_fixture("tieuthuyetmang", "chapter_locked.html"),
                "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/4",
                number=4,
            )

    def test_a_preview_raises_auth_required_naming_the_cookie(self):
        with pytest.raises(AuthRequiredError, match="dán lại cookie"):
            parse_chapter(
                load_fixture("tieuthuyetmang", "chapter_preview.html"),
                "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/5",
                number=5,
            )

    def test_the_lock_gate_runs_before_extraction(self):
        """A teaser must never come back as though it were the chapter — the preview
        fixture HAS an <article> with readable text, and it still refuses."""
        markup = load_fixture("tieuthuyetmang", "chapter_preview.html")
        assert "<article>" in markup and "Trời đổ mưa" in markup
        with pytest.raises(AuthRequiredError):
            parse_chapter(markup, CHAPTER_URL, number=5)

    def test_locked_is_not_a_rate_limit(self):
        """`_fetch_with_backoff` retries `RateLimitedError` eight times at a minute apiece.
        A 119-locked-chapter novel must not spend two hours re-asking for chapters it
        cannot have."""
        from noveltrans.errors import RateLimitedError

        assert not issubclass(AuthRequiredError, RateLimitedError)

    def test_a_page_with_no_chapter_data_blames_the_login_not_the_parser(self):
        """The two ways this fails need different fixes, so they say different things:
        no chapter object at all usually means the page was not served as a logged-in
        reader page."""
        with pytest.raises(ScrapeError, match="chưa đăng nhập"):
            parse_chapter(
                load_fixture("tieuthuyetmang", "chapter_empty.html"),
                CHAPTER_URL,
                number=2,
            )

    def test_a_chapter_object_without_text_says_the_text_loads_separately(self):
        with pytest.raises(ScrapeError, match="tải riêng bằng JavaScript"):
            parse_chapter(
                load_fixture("tieuthuyetmang", "chapter_notext.html"),
                CHAPTER_URL,
                number=2,
            )

    def test_the_failure_prints_a_command_that_actually_runs(self):
        """A printed command that then dies on a missing argument is worse than none —
        the first live failure printed exactly that. `--chapter` alone must suffice."""
        from pathlib import Path

        script = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_tieuthuyetmang.py"
        assert 'nargs="?"' in script.read_text(encoding="utf-8")
        with pytest.raises(ScrapeError, match=r"--chapter https://"):
            parse_chapter(
                load_fixture("tieuthuyetmang", "chapter_empty.html"),
                CHAPTER_URL,
                number=2,
            )


class TestUrlDerivation:
    def test_slug_from_landing_and_chapter_urls(self):
        assert slug(NOVEL_URL) == "truyen-thu-nghiem"
        assert slug(CHAPTER_URL) == "truyen-thu-nghiem"

    def test_landing_url_normalises_any_page_of_the_novel(self):
        assert landing_url(CHAPTER_URL) == NOVEL_URL
        assert landing_url(NOVEL_URL) == NOVEL_URL

    def test_slug_raises_on_a_url_that_is_not_a_novel(self):
        with pytest.raises(ScrapeError, match="slug"):
            slug("https://tieuthuyetmang.com/the-loai/do-thi")

    def test_the_chapter_url_is_slug_then_doc_then_number(self):
        """MEASURED from the site's own route chunk, which builds it three separate ways
        and always as `/truyen/<slug>/doc/<chapterNumber>`. Seven other shapes were
        guessed before it was measured, and all seven 404'd."""
        assert chapter_url("truyen-thu-nghiem", 7) == (
            "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/doc/7"
        )

    def test_chapter_number_reads_both_reader_and_audio_urls(self):
        assert chapter_number(CHAPTER_URL) == 2
        assert chapter_number(f"{NOVEL_URL}/nghe/9") == 9

    def test_chapter_number_raises_on_a_landing_url(self):
        with pytest.raises(ScrapeError, match="chapter number"):
            chapter_number(NOVEL_URL)


class TestRegistry:
    def test_matches_landing_and_chapter_urls(self):
        assert TieuthuyetmangAdapter.matches(NOVEL_URL)
        assert TieuthuyetmangAdapter.matches(CHAPTER_URL)

    def test_rejects_other_sites_and_non_novel_paths(self):
        assert not TieuthuyetmangAdapter.matches("https://medoctruyen.vn/truyen-thu-nghiem")
        assert not TieuthuyetmangAdapter.matches("https://tieuthuyetmang.com/the-loai/do-thi")
        assert not TieuthuyetmangAdapter.matches("https://tieuthuyetmang.com/hoi-vien")

    def test_adapter_for_url_resolves_to_this_adapter(self):
        adapter = adapter_for_url(NOVEL_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, TieuthuyetmangAdapter)

    def test_the_adapter_is_in_the_default_registry(self):
        """A forgotten line in `_import_adapters()` would ship silently — every URL would
        just report "Chưa hỗ trợ trang web này"."""
        assert TieuthuyetmangAdapter in ADAPTERS

    def test_content_is_not_flagged_as_pre_translated(self):
        """This adapter does not PRODUCE a translation the way webtruyendich does; the
        text is simply already Vietnamese, which `source_lang` covers."""
        assert TieuthuyetmangAdapter.content_is_translated is False


class TestAdapterWiring:
    @responses.activate
    def test_metadata_and_chapter_list_share_one_request(self):
        """The politeness guarantee, pinned: the landing page carries the whole TOC, so a
        scan makes ONE round trip where medoctruyen walks up to 500 TOC pages."""
        responses.get(NOVEL_URL, body=landing())
        adapter = make_adapter()
        meta = adapter.fetch_metadata(NOVEL_URL)
        refs = adapter.fetch_chapter_list(NOVEL_URL)
        assert meta.title == "Truyện Thử Nghiệm"
        assert len(refs) == 6
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_chapter_url_is_normalised_to_the_landing_page_before_fetching(self):
        responses.get(NOVEL_URL, body=landing())
        assert make_adapter().fetch_metadata(CHAPTER_URL).title == "Truyện Thử Nghiệm"

    @responses.activate
    def test_the_locked_count_is_reported_as_a_status_line(self):
        responses.get(NOVEL_URL, body=landing())
        adapter = make_adapter()
        messages: list[str] = []
        adapter.on_status = messages.append
        adapter.fetch_chapter_list(NOVEL_URL)
        assert any("4/6" in message and "🔒" in message for message in messages)

    @responses.activate
    def test_a_page_without_flight_data_fails_with_a_readable_message(self):
        responses.get(NOVEL_URL, body="<html><body>maintenance</body></html>")
        with pytest.raises(ScrapeError, match="Next.js"):
            make_adapter().fetch_metadata(NOVEL_URL)

    @responses.activate
    def test_fetch_chapter_reads_the_reader_page(self):
        responses.get(
            CHAPTER_URL, body=load_fixture("tieuthuyetmang", "chapter_flight.html")
        )
        ref = ChapterRef(index=1, title="Chương 2: Người khách", url=CHAPTER_URL)
        assert make_adapter().fetch_chapter(ref).startswith("Trời đổ mưa")


AUDIO_URL_1 = "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/nghe/1"


def audio_page(name: str) -> str:
    return load_fixture("tieuthuyetmang", name)


class TestAudioDiscovery:
    """Feature 059. Every state below was measured against the live site before the
    fixtures were built; see `changes/059-TIEUTHUYETMANG-AUDIO-DOWNLOAD/059.01-HISTORY.md`."""

    def test_audio_page_url_uses_the_nghe_route_and_the_chapter_number(self):
        assert audio_page_url("truyen-thu-nghiem", 11) == (
            "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem/nghe/11"
        )

    def test_audio_entries_keeps_only_chapters_flagged_has_audio(self):
        entries = audio_entries(stream(), "truyen-thu-nghiem")
        assert entries, "the landing fixture must carry at least one hasAudio chapter"
        assert all(entry["hasAudio"] for entry in entries)

    def test_audio_entries_are_a_subset_of_the_novels_own_chapters(self):
        """Anchored through `story_chapters`, so a recommended novel's audio can never
        leak in — the same trap `find_story` exists for."""
        numbers = {e["chapterNumber"] for e in audio_entries(stream(), "truyen-thu-nghiem")}
        assert numbers <= {e["chapterNumber"] for e in chapter_entries(stream())}

    def test_finds_an_mp3_url_in_the_flight_stream(self):
        assert find_audio_media(audio_page("audio_ready.html")).endswith(".mp3")

    def test_finds_an_aac_url_too(self):
        """The regression that matters most. This site publishes some volumes as AAC and
        others as MP3 (8 and 13 of 21 on the reference novel); a search that only knows
        ".mp3" reports the AAC volumes as having no audio at all."""
        assert find_audio_media(audio_page("audio_aac.html")).endswith(".aac")

    def test_prefers_a_real_audio_element_when_the_page_has_one(self):
        assert find_audio_media(audio_page("audio_element.html")).endswith(".mp3")

    def test_returns_empty_when_the_prop_is_present_but_holds_no_url(self):
        assert find_audio_media(audio_page("audio_pending.html")) == ""

    def test_returns_empty_for_a_page_with_no_player_at_all(self):
        assert find_audio_media(audio_page("audio_vip.html")) == ""

    def test_the_url_is_decoded_not_regexed_out_of_the_raw_markup(self):
        """A regex over the raw page picks up the flight stream's own escaping and
        returns a URL with a trailing backslash, which then 404s."""
        assert "\\" not in find_audio_media(audio_page("audio_ready.html"))

    def test_gate_reason_is_empty_when_the_player_props_are_present(self):
        assert audio_gate_reason(audio_page("audio_ready.html")) == ""
        assert audio_gate_reason(audio_page("audio_pending.html")) == ""

    def test_gate_reason_is_vip_when_the_player_is_replaced_by_an_upsell(self):
        assert audio_gate_reason(audio_page("audio_vip.html")) == "vip"

    def test_locked_count_counts_the_flag_without_filtering_on_it(self):
        entries = [{"audioLocked": True}, {"audioLocked": False}, {}]
        assert audio_locked_count(entries) == 1


class TestAudioAdapter:
    @responses.activate
    def test_manifest_reuses_the_cached_landing_page(self):
        responses.get(NOVEL_URL, body=landing())
        adapter = make_adapter()
        adapter.fetch_chapter_list(NOVEL_URL)
        entries = adapter.fetch_audio_manifest(NOVEL_URL)
        assert entries
        assert len(responses.calls) == 1, "the manifest must not cost a second request"

    @responses.activate
    def test_fetch_audio_url_returns_the_media_url(self):
        responses.get(AUDIO_URL_1, body=audio_page("audio_ready.html"))
        ref = ChapterRef(index=0, title="Tập 1", url=CHAPTER_URL.replace("/doc/2", "/doc/1"))
        assert make_adapter().fetch_audio_url(ref).endswith(".mp3")

    @responses.activate
    def test_fetch_audio_url_reads_the_nghe_page_not_the_doc_page(self):
        """The `/doc/` reader carries no media URL with or without a session cookie."""
        responses.get(AUDIO_URL_1, body=audio_page("audio_ready.html"))
        ref = ChapterRef(index=0, title="Tập 1", url=CHAPTER_URL.replace("/doc/2", "/doc/1"))
        make_adapter().fetch_audio_url(ref)
        assert responses.calls[0].request.url == AUDIO_URL_1

    @responses.activate
    def test_an_unentitled_page_raises_auth_required(self):
        responses.get(AUDIO_URL_1, body=audio_page("audio_vip.html"))
        ref = ChapterRef(index=0, title="Tập 1", url=CHAPTER_URL.replace("/doc/2", "/doc/1"))
        with pytest.raises(AuthRequiredError):
            make_adapter().fetch_audio_url(ref)

    @responses.activate
    def test_an_entitled_page_with_no_file_yet_raises_audio_unavailable(self):
        """Distinct from AuthRequiredError on purpose: retrying or re-authenticating
        cannot help, so the batch reports this one and moves on."""
        responses.get(AUDIO_URL_1, body=audio_page("audio_pending.html"))
        ref = ChapterRef(index=0, title="Tập 1", url=CHAPTER_URL.replace("/doc/2", "/doc/1"))
        with pytest.raises(AudioUnavailableError):
            make_adapter().fetch_audio_url(ref)

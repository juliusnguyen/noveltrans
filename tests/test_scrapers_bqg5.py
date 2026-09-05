"""Feature 079 — the bqg5.com adapter.

Three tests carry this module, and each pins a failure that looks like nothing:

`test_the_duplicated_latest_block_is_not_in_the_list` — the TOC's first `<dt>` section is
最新章节 and its `<dd>`s are copies of the NEWEST chapters, in the same flat `<dl>` as the
real ones. Taking every `<dd>` files chapter 20's body under chapter 1, and `replace_toc`
preserves content across re-scans, so a later corrected title would sit on top of the
wrong body forever. The live count of that block is 9 where an earlier fetch of the same
page showed 5 — which is why no rule here may count entries.

`test_a_toc_with_only_the_decoy_section_is_refused` — the same trap one step further on: a
page whose ONLY `<dt>` is 最新章节. A plain "fall back to the last `<dt>`" rule reads that
as the chapter list and returns the newest chapters as chapters 1-9. The adapter must
refuse rather than fall back.

`test_every_request_carries_the_mobile_user_agent` — the site answers our default desktop
User-Agent with HTTP 404 on *both* its hosts. That is the entire reason this feature
exists (the user could open the site on a phone and not on a desktop), and it is invisible
in every other test, because a lost header fails as a 404 that says nothing about UAs.

No test here touches the network — the live class is `@pytest.mark.live` and deselected by
default. Fixtures are hand-built to the structure measured on the live site (the two-`<dt>`
`<dl>`, the `#info` 最新章节 anchor that is a 449th chapter-shaped link, `#listtj`
recommending other books, the `&nbsp;` label padding and indent runs, the literal `\\n` and
`\\"` escapes in the blurb, the `data-evt` nocover attribute, the `<h1>`'s leading space,
and the site's own inconsistent `第 4章` / `第5 章` spacing) with invented filler text — so
the traps are encoded deliberately rather than captured by luck, and no novel text lives in
this repo.
"""

from __future__ import annotations

import pytest
import responses

from noveltrans.errors import ScrapeError
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import USER_AGENT, HttpClient
from noveltrans.scrapers.bqg5 import (
    MOBILE_UA,
    ORIGIN,
    Bqg5Adapter,
    book_id,
    chapter_url,
    latest_chapter_url,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    read_url,
)
from tests.conftest import load_fixture

BID = "4_4217"
LANDING = f"{ORIGIN}/{BID}/"
CHAPTER_URL = f"{ORIGIN}/{BID}/1000001.html"
LATEST_URL = f"{ORIGIN}/{BID}/1000020.html"
TITLE = "星河归客"
AUTHOR = "无名氏"
CH_TITLE = "第1章 荒村来客"
COVER = f"{ORIGIN}/files/article/image/4/4217/4217s.jpg"

# Every form the user can paste. The `m.` one is what the site hands out on a phone, and
# therefore the one this feature was reported with.
ALL_FORMS = [
    "https://m.bqg5.com/4_4217/",
    "https://www.bqg5.com/4_4217/",
    "https://bqg5.com/4_4217/",
    "https://m.bqg5.com/4_4217/index_3.html",
    "https://www.bqg5.com/4_4217/2029247.html",
    "https://m.bqg5.com/4_4217/2029247_2.html",
]


def fx(name: str) -> str:
    return load_fixture("bqg5", name)


def make_adapter() -> Bqg5Adapter:
    return Bqg5Adapter(HttpClient(delay_seconds=0))


class TestUrlDerivation:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_read_url_folds_every_paste_form_to_the_www_canonical(self, url):
        """`Library.find_by_url` is exact string equality — echoing the pasted string
        would make one novel up to three projects with separate progress."""
        assert read_url(url) == LANDING

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_the_book_id_is_the_category_book_pair(self, url):
        assert book_id(url) == BID

    def test_a_url_with_no_book_id_is_refused(self):
        with pytest.raises(ScrapeError):
            book_id("https://www.bqg5.com/xuanhuanxiaoshuo/")

    def test_chapter_url_is_built_on_the_www_origin(self):
        assert chapter_url(BID, "1000001") == CHAPTER_URL


class TestMetadata:
    def test_it_reads_the_opengraph_block(self):
        meta = parse_metadata(fx("index.html"), ALL_FORMS[0], "bqg5")

        assert (meta.title, meta.author, meta.site) == (TITLE, AUTHOR, "bqg5")
        assert meta.cover_url == COVER
        assert meta.source_lang == "zh"

    def test_the_stored_url_is_the_canonical_one_not_the_pasted_one(self):
        meta = parse_metadata(fx("index.html"), "https://m.bqg5.com/4_4217/", "bqg5")
        assert meta.url == LANDING

    def test_it_falls_back_to_the_visible_book_box(self):
        meta = parse_metadata(fx("index_no_og.html"), ALL_FORMS[0], "bqg5")

        assert (meta.title, meta.author) == (TITLE, AUTHOR)
        assert meta.description

    def test_the_author_label_is_split_on_its_colon_not_matched_as_a_prefix(self):
        """The site pads the label with NBSPs — `作\\xa0\\xa0者：` — so a
        `startswith("作者")` test never fires and the author silently comes back empty."""
        assert "\xa0" in fx("index_no_og.html").replace("&nbsp;", "\xa0")
        assert parse_metadata(fx("index_no_og.html"), ALL_FORMS[0], "bqg5").author == AUTHOR

    def test_a_relative_cover_is_absolutised(self):
        """`#fmimg` ships a relative src; only `og:image` is absolute."""
        assert parse_metadata(fx("index_no_og.html"), ALL_FORMS[0], "bqg5").cover_url == COVER

    def test_the_nocover_placeholder_is_blanked(self):
        """Stored, it renders into the EPUB and thumbnail as a *broken* cover."""
        assert parse_metadata(fx("index_nocover.html"), ALL_FORMS[0], "bqg5").cover_url == ""

    def test_a_page_with_no_title_anywhere_raises(self):
        with pytest.raises(ScrapeError, match="Novel title not found"):
            parse_metadata(fx("index_no_title.html"), ALL_FORMS[0], "bqg5")


class TestDescription:
    @pytest.fixture
    def description(self) -> str:
        return parse_metadata(fx("index.html"), ALL_FORMS[0], "bqg5").description

    def test_literal_backslash_n_becomes_a_real_newline(self, description):
        """The blurb carries two-character escapes, not markup — BeautifulSoup can't help."""
        assert "\\n" not in description
        assert "\n" in description

    def test_escaped_quotes_are_unescaped(self, description):
        assert '\\"' not in description
        assert '"我会回来的"' in description

    def test_the_nbsp_indent_runs_do_not_survive(self, description):
        assert "\xa0" not in description

    def test_blank_line_runs_are_collapsed_and_the_edges_stripped(self, description):
        assert "\n\n\n" not in description
        assert description == description.strip()

    def test_the_intro_is_preferred_over_the_truncated_meta_tag(self, description):
        """`og:description` is the same blurb with a cosmetic trailing ellipsis."""
        assert not description.endswith("...")


class TestChapterList:
    @pytest.fixture
    def refs(self):
        return parse_chapter_list(fx("index.html"), LANDING)

    def test_the_duplicated_latest_block_is_not_in_the_list(self, refs):
        """**The load-bearing test.** The 最新章节 `<dd>`s are copies of the NEWEST
        chapters sitting in the same flat `<dl>`. Taking every `<dd>` returns 29 refs with
        chapter 20 at index 0 — and `replace_toc` would keep that body there forever."""
        assert len(refs) == 20
        assert refs[0].title == CH_TITLE
        assert refs[0].url == CHAPTER_URL
        assert len({r.url for r in refs}) == 20

    def test_a_toc_with_only_the_decoy_section_is_refused(self):
        """**The load-bearing test.** A "last `<dt>`" fallback reads the decoy as the
        chapter list and hands back the newest chapters as chapters 1-9."""
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list(fx("index_no_body_dt.html"), LANDING)

    def test_a_novel_with_no_latest_block_still_parses(self):
        """One `<dt>`, and it is the real one — the fallback must still find it."""
        assert len(parse_chapter_list(fx("index_one_dt.html"), LANDING)) == 4

    def test_the_info_latest_link_is_not_an_extra_chapter(self, refs):
        """`#info` holds a chapter-shaped anchor — the 449th on the live page."""
        assert len(refs) == 20
        assert sum(1 for r in refs if r.url == LATEST_URL) == 1

    def test_sidebar_recommendations_are_not_chapters(self, refs):
        """`#listtj` recommends OTHER books, and their URLs are chapter-shaped too."""
        assert not [r for r in refs if "9_9999" in r.url or "8_8888" in r.url]

    def test_the_index_is_positional_not_the_title_number(self, refs):
        """20 entries whose highest title number is 第19章 — mirroring the live 439
        entries ending at 第438章. Any index parsed out of a title misfiles chapters, and
        any total derived from one refuses every scan on the site."""
        assert [r.index for r in refs] == list(range(20))
        assert refs[-1].title.startswith("第19章")

    def test_titles_keep_the_sites_own_inconsistent_spacing(self, refs):
        """Titles are persisted, exported and read aloud — a tidy-up normaliser here
        would silently rewrite them on the next re-scan."""
        assert refs[3].title == "第 4章 旧图新解"
        assert refs[4].title == "第5 章 落雨长街"

    def test_the_chapter_order_is_the_reading_order(self, refs):
        assert refs[-1].url == LATEST_URL

    def test_the_urls_are_absolutised_onto_the_www_origin(self, refs):
        assert all(r.url.startswith(f"{ORIGIN}/{BID}/") for r in refs)

    def test_a_page_with_no_list_raises(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list("<html><body>nothing here</body></html>", LANDING)


class TestLatestChapterUrl:
    def test_it_reads_the_info_anchor(self):
        assert latest_chapter_url(fx("index.html")) == LATEST_URL

    def test_it_agrees_with_the_end_of_the_chapter_list(self):
        """That agreement is what makes it usable as a reversal check."""
        refs = parse_chapter_list(fx("index.html"), LANDING)
        assert latest_chapter_url(fx("index.html")) == refs[-1].url

    def test_a_page_that_does_not_say_returns_empty(self):
        assert latest_chapter_url("<html><body></body></html>") == ""


class TestChapterContent:
    @pytest.fixture
    def body(self) -> str:
        return parse_chapter(fx("chapter.html"), CH_TITLE, CHAPTER_URL)

    def test_it_returns_the_paragraphs_blank_line_separated(self, body):
        assert len(body.split("\n\n")) == 8

    def test_the_nav_block_is_removed_and_its_neighbours_stay_adjacent(self, body):
        """A `div.bottem1` is planted mid-prose. Decomposing it must not leave a hole
        between the paragraphs it separated."""
        paragraphs = body.split("\n\n")
        assert paragraphs[3].startswith("门是虚掩的")
        assert paragraphs[4].startswith("「你终于来了。」")

    @pytest.mark.parametrize("junk", ["下一章", "上一章", "章节目录", "newmessage"])
    def test_no_site_chrome_survives(self, body, junk):
        assert junk not in body

    def test_the_nbsp_indents_are_stripped(self, body):
        assert "\xa0" not in body
        assert not any(line.startswith(" ") for line in body.split("\n"))

    def test_there_are_no_blank_line_runs(self, body):
        assert "\n\n\n" not in body

    def test_the_leading_space_h1_echo_is_dropped_despite_the_spacing(self):
        """The site's own `<h1>` carries a leading space and an ideographic space the TOC
        title does not — so the comparison has to normalise both sides."""
        body = parse_chapter(fx("chapter_title_echo.html"), CH_TITLE, CHAPTER_URL)

        assert len(body.split("\n\n")) == 8
        assert not body.startswith("第1章")

    def test_a_non_matching_title_never_strips_the_first_line(self):
        body = parse_chapter(fx("chapter_title_echo.html"), "第7章 别的标题", CHAPTER_URL)

        assert len(body.split("\n\n")) == 9
        assert body.startswith("第1章")

    def test_a_page_with_no_content_container_raises(self):
        with pytest.raises(ScrapeError, match="Chapter content not found"):
            parse_chapter("<html><body>nope</body></html>", CH_TITLE, CHAPTER_URL)

    def test_an_empty_container_raises(self):
        with pytest.raises(ScrapeError, match="Chapter content is empty"):
            parse_chapter(fx("chapter_empty.html"), CH_TITLE, CHAPTER_URL)


class TestTransport:
    @responses.activate
    def test_every_request_carries_the_mobile_user_agent(self):
        """**The load-bearing test.** The site answers our default desktop UA with a 404
        on BOTH hosts — that is the whole reason this adapter exists. A lost header breaks
        metadata, TOC and chapters at once, with an error that says nothing about UAs."""
        responses.add(responses.GET, LANDING, body=fx("index.html"))
        responses.add(responses.GET, CHAPTER_URL, body=fx("chapter.html"))

        adapter = make_adapter()
        adapter.fetch_metadata(ALL_FORMS[0])
        adapter.fetch_chapter(parse_chapter_list(fx("index.html"), LANDING)[0])

        assert len(responses.calls) == 2
        for call in responses.calls:
            assert call.request.headers["User-Agent"] == MOBILE_UA
            assert call.request.headers["User-Agent"] != USER_AGENT

    @responses.activate
    def test_the_shared_session_user_agent_is_not_mutated(self):
        """The header is per-request on purpose: `HttpClient` documents "one client, one
        site" as an *assumption*, and this adapter must not be what breaks it."""
        responses.add(responses.GET, LANDING, body=fx("index.html"))

        client = HttpClient(delay_seconds=0)
        Bqg5Adapter(client).fetch_metadata(ALL_FORMS[0])

        assert client._session.headers["User-Agent"] == USER_AGENT

    @responses.activate
    def test_gbk_bytes_with_no_charset_header_decode_correctly(self):
        """The site declares `charset=gbk` only in a `<meta>`, so requests defaults to
        ISO-8859-1 and `HttpClient.get_html`'s `apparent_encoding` fixup is what actually
        carries this adapter."""
        responses.add(
            responses.GET,
            LANDING,
            body=fx("index.html").encode("gb18030"),
            content_type="text/html",
        )

        meta = make_adapter().fetch_metadata(ALL_FORMS[0])

        assert meta.title == TITLE
        assert meta.author == AUTHOR
        assert "�" not in meta.title + meta.author + meta.description

    @responses.activate
    def test_a_scan_costs_exactly_one_request(self):
        """Metadata and the chapter list come off the same page, and it is cached."""
        responses.add(responses.GET, LANDING, body=fx("index.html"))

        adapter = make_adapter()
        adapter.fetch_metadata(ALL_FORMS[0])
        adapter.fetch_chapter_list(ALL_FORMS[0])
        adapter.fetch_metadata(ALL_FORMS[0])

        assert len(responses.calls) == 1
        assert responses.calls[0].request.url == LANDING

    @responses.activate
    def test_the_landing_page_is_fetched_on_the_www_host_whatever_was_pasted(self):
        responses.add(responses.GET, LANDING, body=fx("index.html"))

        make_adapter().fetch_chapter_list("https://m.bqg5.com/4_4217/2029247_2.html")

        assert responses.calls[0].request.url == LANDING

    @responses.activate
    def test_a_404_becomes_a_scrape_error_naming_the_url(self):
        """What the site returns to a desktop User-Agent."""
        responses.add(responses.GET, LANDING, status=404)

        with pytest.raises(ScrapeError) as excinfo:
            make_adapter().fetch_metadata(ALL_FORMS[0])
        assert LANDING in str(excinfo.value)

    @responses.activate
    def test_a_reversed_chapter_list_is_refused_rather_than_saved(self):
        """If the decoy block ever reaches index 0, the newest chapter's body would be
        filed under chapter 1 and `replace_toc` would keep it there. The page states its
        own newest chapter in `#info`, so this costs nothing and needs no total."""
        # Reverse the 正文 section's `<dd>`s and nothing else, so the page stays valid and
        # the ONLY thing wrong with it is the order.
        head, sep, tail = fx("index.html").partition("正文</dt>\n")
        entries, rest = tail.split("\n</dl>", 1)
        chapters = "\n".join(reversed(entries.split("\n")))
        responses.add(responses.GET, LANDING, body=f"{head}{sep}{chapters}\n</dl>{rest}")

        with pytest.raises(ScrapeError, match="đảo ngược"):
            make_adapter().fetch_chapter_list(ALL_FORMS[0])


class TestRegistry:
    def test_the_adapter_is_registered(self):
        """Forgetting the `_import_adapters` line ships silently — every bqg5 URL would
        report "chưa hỗ trợ trang web này"."""
        assert Bqg5Adapter in ADAPTERS

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_adapter_for_url_resolves_to_this_adapter(self, url):
        assert isinstance(adapter_for_url(url, HttpClient(delay_seconds=0)), Bqg5Adapter)

    @pytest.mark.parametrize(
        "url",
        [
            "https://m.bqg5.com/",
            "https://www.bqg5.com/xuanhuanxiaoshuo/",
            "https://www.bqg5.com/map/",
            "https://www.bqg5.com/paihangbang/",
            "https://www.bqg5.com/mybook.php",
        ],
    )
    def test_non_novel_urls_are_ignored(self, url):
        assert not Bqg5Adapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://sto9.com/book/13908/index.html",
            "https://twkan.com/book/114283.html",
            "https://www.69shuba.com/book/59024/",
            "https://www.timotxt.com/2608569069/1.html",
        ],
    )
    def test_it_does_not_steal_other_sites_urls(self, url):
        assert not Bqg5Adapter.matches(url)

    def test_no_other_adapter_claims_a_bqg5_url(self):
        """Every registered pattern is host-anchored; this proves it stays that way."""
        assert [a for a in ADAPTERS if a.matches(CHAPTER_URL)] == [Bqg5Adapter]

    @pytest.mark.parametrize("href", ["/4_4217/index_2.html", "/4_4217/2029247_2.html"])
    def test_the_mobile_layouts_urls_are_not_mistaken_for_chapters(self, href):
        """A mobile TOC page and a mobile chapter sub-page are neither of them chapters.
        Both would otherwise land in a chapter list as phantom entries."""
        refs = parse_chapter_list(
            f'<div id="list"><dl><dt>正文</dt>'
            f'<dd><a href="{href}">看起来像章节</a></dd>'
            f'<dd><a href="/{BID}/1000001.html">{CH_TITLE}</a></dd></dl></div>',
            LANDING,
        )
        assert len(refs) == 1
        assert refs[0].url == CHAPTER_URL

    def test_the_content_is_not_a_translation(self):
        """Flipping this would land Chinese in `translated` and mark chapters done."""
        assert Bqg5Adapter.content_is_translated is False


@pytest.mark.live
class TestLive:
    """Deselected by default (`addopts = "-m 'not live'"`). Run with `-m live`."""

    URL = "https://m.bqg5.com/4_4217/"  # deliberately the form the user pastes

    def test_the_desktop_user_agent_is_still_refused(self):
        """The canary for the premise. If the gate is ever lifted this fails, rather than
        the UA override quietly becoming dead code nobody can explain."""
        import requests

        for host in ("https://www.bqg5.com", "https://m.bqg5.com"):
            response = requests.get(
                f"{host}/4_4217/", headers={"User-Agent": USER_AGENT}, timeout=20
            )
            assert response.status_code == 404

    def test_it_scans_the_novel(self):
        adapter = make_adapter()
        meta = adapter.fetch_metadata(self.URL)

        assert meta.url == LANDING
        assert meta.title and "�" not in meta.title
        assert meta.source_lang == "zh"
        assert "\\n" not in meta.description
        assert "�" not in meta.description

    def test_the_chapter_list_is_complete_and_in_order(self):
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)

        assert len(refs) > 400
        assert len({r.url for r in refs}) == len(refs)
        assert refs[0].title.startswith("第")
        assert refs[-1].url == latest_chapter_url(adapter._landing_page(self.URL))

    @pytest.mark.parametrize("position", [0, -1])
    def test_a_chapter_comes_back_whole(self, position):
        adapter = make_adapter()
        refs = adapter.fetch_chapter_list(self.URL)
        body = adapter.fetch_chapter(refs[position])

        assert len(body) > 1000
        assert "�" not in body  # the gbk-vs-gb18030 canary
        assert "第(1/" not in body  # a mobile-template sub-page marker
        assert "加入书签" not in body
        assert "下一章" not in body

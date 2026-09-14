"""Feature 087 — the ttks.tw (天天看小說) adapter.

Three tests carry this module, one per trap the site sets:

`test_the_duplicate_latest_block_never_reaches_the_list` — the index page carries TWO
`div.chapters_frame` blocks with identical classes, and the first is a decoy holding the
newest handful of chapters **backwards**. A flat anchor scrape returns more links than the
novel has chapters, last-chapter-first. Saving that would file the last chapter's body under
index 0, and `replace_toc` preserves content across re-scans, so a later correct scan would
rewrite the title over the wrong body.

`test_the_navigation_div_is_never_the_body` — a chapter page carries TWO `div.content`, the
second being the prev/next navigation. The body comes first today, so `select_one` is right by
luck; this test fails the moment anyone relies on that.

`test_the_promo_is_its_own_line_and_the_prose_sentence_survives_whole` — every chapter has a
site advert spliced into the middle of a real paragraph, behind `<br/><br/>`, with the domain
written in a Unicode mathematical alphabet that changes chapter to chapter. It is removable
only because `get_text("\n")` puts it on a line of its own; swap in `get_text(strip=True)` —
novel543's shape — and the advert glues onto the prose sentence, which is where over-deletion
starts.

The browser is faked throughout — no test here launches Chrome or touches the network.

Fixtures are hand-built to the structure measured on the live site (the twin frames and their
backwards decoy, the twin `div.content`, the `&emsp;` indents, the mid-prose ad slots, the
site's own 章-number drift, and the `<br/><br/>` advert in three obfuscation alphabets) with
invented filler text — so the traps are encoded deliberately rather than captured by luck, and
no novel text lives in this repo.
"""

from __future__ import annotations

import unicodedata

import pytest

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSessionError
from noveltrans.errors import ObfuscatedContentError, ScrapeError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import ADAPTERS, adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.ttks import (
    ORIGIN,
    TtksAdapter,
    _is_promo_line,
    chapter_positions,
    chapter_url,
    latest_position,
    parse_chapter,
    parse_chapter_list,
    parse_metadata,
    read_url,
    slug,
)
from tests.conftest import load_fixture

SLUG = "changshengdianyuanlu"
N = 20  # chapters in the fixture novel
LATEST_BLOCK = 7  # entries in the decoy — deliberately not hardcoded in any assertion

READ_URL = f"{ORIGIN}/novel/chapters/{SLUG}/index.html"
BARE_URL = f"{ORIGIN}/novel/chapters/{SLUG}/"
NO_SCHEME_HOST = f"http://www.ttks.tw/novel/chapters/{SLUG}/index.html"
CHAPTER_PASTE = f"{ORIGIN}/novel/chapters/{SLUG}/12.html"

# Every form a user might realistically paste. All must fold to ONE identity string.
ALL_FORMS = (READ_URL, BARE_URL, NO_SCHEME_HOST, CHAPTER_PASTE)

TITLE = "長生點元錄"
AUTHOR = "假名"
COVER = "https://ttks.tw/files/article/image/100/100999/100999s.jpg"
# The marker that gives the SEO boilerplate away. It must never reach NovelMeta.description.
SEO_MARKER = "全本章節列表"

# The site advertises this as its newest chapter. Nothing may ever fetch it.
ADVERTISED_LATEST = f"{ORIGIN}/novel/chapters/{SLUG}/{N}.html"


def ch_number(position: int) -> int:
    """The fixture's 章 number for a position — deliberately drifting past 10."""
    return position if position <= 10 else position - 1


def ch_title(position: int) -> str:
    return f"第{ch_number(position)}章 假標題{position}"


def ch_url(position: int) -> str:
    return f"{ORIGIN}/novel/chapters/{SLUG}/{position}.html"


def fx(name: str) -> str:
    return load_fixture("ttks", name)


class _FakeSession:
    """Stands in for BrowserSession: serves fixtures by URL, records what was asked."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []
        self.closed = False

    def get_html(self, url: str) -> str:
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"adapter fetched an unexpected URL: {url}")
        return self.pages[url]

    def close(self) -> None:
        self.closed = True


def make_adapter(**overrides: str) -> tuple[TtksAdapter, _FakeSession]:
    pages = {
        READ_URL: fx("index.html"),
        ch_url(1): fx("chapter.html"),
        # Present but must NEVER be requested while fetching chapter 1 — that page's
        # 下一章 points straight at it.
        ch_url(2): fx("chapter_promo_bare.html"),
        ch_url(3): fx("chapter_nav_first.html"),
    }
    pages.update(overrides)
    adapter = TtksAdapter(HttpClient(delay_seconds=0))
    session = _FakeSession(pages)
    adapter._session = session  # never launches a browser
    return adapter, session


def capture_status(adapter: TtksAdapter) -> list[str]:
    messages: list[str] = []
    adapter.on_status = messages.append
    return messages


def body_of(fixture: str, title: str = "第1章 假標題1") -> str:
    return parse_chapter(fx(fixture), title, ch_url(1))


class TestUrlDerivation:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_slug_from_every_paste_form(self, url):
        assert slug(url) == SLUG

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_every_paste_form_folds_to_one_identity(self, url):
        """`Library.find_by_url` is string equality — one novel must be one project."""
        assert read_url(url) == READ_URL

    @pytest.mark.parametrize(
        "url", ["", "https://example.com/novel/chapters/x", "https://ttks.tw/", "not a url"]
    )
    def test_slug_raises_on_a_non_novel_url(self, url):
        with pytest.raises(ScrapeError):
            slug(url)

    def test_chapter_url_is_built_from_the_position(self):
        assert chapter_url(CHAPTER_PASTE, 7) == ch_url(7)


class TestMetadata:
    def test_reads_the_opengraph_block(self):
        meta = parse_metadata(fx("index.html"), READ_URL, "ttks")
        assert meta.title == TITLE
        assert meta.author == AUTHOR
        assert meta.cover_url == COVER
        assert meta.site == "ttks"
        assert meta.source_lang == "zh"

    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_url_is_always_canonical(self, url):
        assert parse_metadata(fx("index.html"), url, "ttks").url == READ_URL

    def test_the_description_is_the_visible_blurb_not_the_seo_boilerplate(self):
        """This site's og:description names the title and author and says the novel has
        chapters. Anyone "harmonising" this with twkan's OG-first rule turns this red."""
        description = parse_metadata(fx("index.html"), READ_URL, "ttks").description
        assert SEO_MARKER not in description
        assert "假的簡介文字" in description

    def test_a_site_truncated_blurb_is_stored_as_given(self):
        """The site truncates its own blurb; no cleaning recovers a tail it never sent."""
        assert parse_metadata(fx("index.html"), READ_URL, "ttks").description.endswith("...")

    def test_the_blurb_carries_no_ad_markup(self):
        description = parse_metadata(fx("index.html"), READ_URL, "ttks").description
        assert "pubadx" not in description

    def test_falls_back_to_the_visible_info_box(self):
        meta = parse_metadata(fx("index_no_og.html"), READ_URL, "ttks")
        assert meta.title == TITLE
        assert meta.author == AUTHOR

    def test_raises_when_no_title_anywhere(self):
        with pytest.raises(ScrapeError, match="title"):
            parse_metadata(fx("index_no_title.html"), READ_URL, "ttks")

    def test_cover_url_is_stored_unmodified(self):
        """No placeholder-blanking rule exists; adding one must bring its own fixture."""
        assert parse_metadata(fx("index.html"), READ_URL, "ttks").cover_url == COVER

    def test_the_opengraph_block_is_read_from_the_name_attribute(self):
        """The live site is a Nuxt/AMP page: it writes `name="og:…"` with NO `property`
        attribute. A `meta[property=…]` selector matches nothing there, and the visible-box
        fallbacks hide it — the title and author still come out right while `cover_url` goes
        quietly empty. Measured against the live page; this is why `index.html` is spelled the
        site's way."""
        assert 'property="og:' not in fx("index.html")
        assert parse_metadata(fx("index.html"), READ_URL, "ttks").cover_url == COVER

    def test_the_property_spelling_still_works(self):
        """Every sibling site writes `property=`. A CMS change back to it must not break."""
        meta = parse_metadata(fx("index_og_property.html"), READ_URL, "ttks")
        assert meta.title == TITLE
        assert meta.cover_url == COVER


class TestChapterList:
    def test_the_duplicate_latest_block_never_reaches_the_list(self):
        """TRAP 1. The decoy's entries carry the same positions, so the position key drops
        them; a flat scrape would have returned N + LATEST_BLOCK + noise."""
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert len(refs) == N
        assert len({r.url for r in refs}) == N

    def test_the_list_comes_out_ascending_despite_the_descending_block(self):
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert refs[0].url == ch_url(1)
        assert refs[-1].url == ch_url(N)
        assert chapter_positions(refs) == list(range(1, N + 1))

    def test_index_is_dense_and_positional(self):
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert [r.index for r in refs] == list(range(N))

    def test_positions_come_from_the_url_not_the_title(self):
        """The fixture's 章 numbers drift from position past chapter 10, so a reader that
        parsed the title would order the tail wrongly."""
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert refs[-1].title == ch_title(N)
        assert ch_number(N) != N  # the drift is real, not a no-op

    def test_the_index_self_link_is_not_a_chapter(self):
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert all(not r.url.endswith("index.html") for r in refs)

    def test_a_foreign_novels_anchor_is_not_a_chapter(self):
        """The recommendation widget points at another novel in real chapter-URL shape."""
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert all(SLUG in r.url for r in refs)

    def test_a_novel_with_no_latest_block_still_parses(self):
        """A genuinely short novel has no decoy at all."""
        refs = parse_chapter_list(fx("index_short.html"), SLUG, READ_URL)
        assert chapter_positions(refs) == [1, 2, 3, 4]

    def test_the_flat_fallback_comes_out_ascending(self):
        """With the frame wrapper gone, the href filter and the position key must still
        produce a clean ascending list."""
        refs = parse_chapter_list(fx("index_no_frames.html"), SLUG, READ_URL)
        assert chapter_positions(refs) == list(range(1, N + 1))

    def test_raises_when_there_is_no_list(self):
        with pytest.raises(ScrapeError, match="Chapter list not found"):
            parse_chapter_list(fx("index_no_chapters.html"), SLUG, READ_URL)

    def test_every_ref_is_titled(self):
        refs = parse_chapter_list(fx("index.html"), SLUG, READ_URL)
        assert all(r.title for r in refs)


class TestLatestPosition:
    def test_read_off_the_url_not_the_name(self):
        """og:novel:latest_chapter_name is a TITLE and its 章 number drifts; the URL's is a
        position. Reading the name would report the wrong count."""
        assert latest_position(fx("index.html"), SLUG) == N
        assert ch_number(N) != N

    def test_a_foreign_slug_is_not_this_novels_length(self):
        assert latest_position(fx("index.html"), "someothernovel") is None

    def test_none_when_the_page_does_not_say(self):
        assert latest_position(fx("index_no_og.html"), SLUG) is None

    def test_read_from_either_attribute_spelling(self):
        assert latest_position(fx("index_og_property.html"), SLUG) == N


class TestContiguityGuard:
    def test_a_gap_in_the_positions_raises(self):
        """The fixture's gap is at position 5, outside the decoy's range — a gap inside it
        would be filled by the decoy and the fixture would test nothing."""
        adapter, _ = make_adapter(**{READ_URL: fx("index_gap.html")})
        with pytest.raises(ScrapeError, match="không liền mạch") as exc:
            adapter.fetch_chapter_list(READ_URL)
        assert "19" in str(exc.value) and "20" in str(exc.value)

    def test_a_list_that_is_only_the_latest_block_raises(self):
        """The decoy alone starts at 14, not 1 — filing those under index 0 would be
        permanent, because replace_toc keeps content across re-scans."""
        adapter, _ = make_adapter(**{READ_URL: fx("index_only_recent.html")})
        with pytest.raises(ScrapeError, match="không liền mạch"):
            adapter.fetch_chapter_list(READ_URL)

    def test_a_complete_list_is_not_reported_as_short(self):
        adapter, _ = make_adapter()
        messages = capture_status(adapter)
        assert len(adapter.fetch_chapter_list(READ_URL)) == N
        assert not [m for m in messages if "⚠️" in m]

    def test_a_short_but_contiguous_list_is_kept_and_reported(self):
        """Contiguity proves it is a dense prefix, so it is safe — but never silently."""
        adapter, _ = make_adapter(**{READ_URL: fx("index_short.html")})
        messages = capture_status(adapter)
        refs = adapter.fetch_chapter_list(READ_URL)
        assert len(refs) == 4
        assert any("4/20" in m for m in messages)

    def test_the_advertised_latest_chapter_url_is_never_fetched(self):
        """It is absent from the fake's page map, so requesting it would raise."""
        adapter, session = make_adapter()
        adapter.fetch_chapter_list(READ_URL)
        assert ADVERTISED_LATEST not in session.requested


class TestChapterContainer:
    def test_the_navigation_div_is_never_the_body(self):
        """TRAP 2. This is the test that fails if anyone writes select_one("div.content")."""
        text = parse_chapter(fx("chapter_nav_first.html"), "第3章 假標題3", ch_url(3))
        assert "假文字第1段" in text
        for nav in ("上一章", "下一章", "返回目錄"):
            assert nav not in text

    def test_the_body_is_found_when_it_comes_first(self):
        text = body_of("chapter.html")
        assert "假文字第1段" in text
        assert "上一章" not in text

    def test_prose_survives_the_mid_prose_chrome(self):
        """Eight ad slots sit INSIDE the body on the live site. The paragraphs either side of
        one must both survive, and stay adjacent."""
        text = body_of("chapter.html")
        paragraphs = text.split("\n\n")
        assert "假文字第3段" in paragraphs[2]
        assert "假文字第4段" in paragraphs[3]
        for junk in ("pubadx", "loadAdv", "添加書籤", "章節報錯", "分享給朋友"):
            assert junk not in text

    def test_the_bare_text_fallback_reads_a_p_less_body(self):
        text = parse_chapter(fx("chapter_no_p.html"), "第4章 假標題4", ch_url(4))
        assert "假文字第1段" in text
        assert "假文字第4段" in text

    def test_raises_when_no_content_div_qualifies(self):
        with pytest.raises(ScrapeError, match="Chapter content not found"):
            parse_chapter("<html><body><div class='other'>x</div></body></html>", "t", "u")


class TestPromoStripping:
    def test_nfkc_folds_every_observed_domain_variant_to_one_token(self):
        """The measured fact the whole predicate rests on: the site rotates the alphabet, and
        normalisation collapses every spelling to the same ASCII domain."""
        for first_a in (0x1D5EE, 0x1D552, 0x1D41A):  # sans-bold, double-struck, bold
            spelled = "".join(chr(first_a + ord(c) - 97) if c.isalpha() else c for c in "ttk.tw")
            assert spelled != "ttk.tw"  # it really is obfuscated
            assert unicodedata.normalize("NFKC", spelled) == "ttk.tw"

    def test_the_promo_is_its_own_line_and_the_prose_sentence_survives_whole(self):
        """TRAP 3, and the test that pins the `get_text("\\n")` separator: with
        `strip=True` the advert glues onto the prose sentence and this goes red."""
        text = body_of("chapter.html")
        assert "假文字第五段，這一段是真的正文。" in text
        assert "假提示文字" not in text
        assert "ttk" not in unicodedata.normalize("NFKC", text)

    def test_the_bare_unparenthesised_variant_is_dropped(self):
        text = parse_chapter(fx("chapter_promo_bare.html"), "第2章 假標題2", ch_url(2))
        assert "假標語文字" not in text
        assert "假文字第二段，這一段是真的正文。" in text

    def test_a_prose_line_that_mentions_a_website_survives(self):
        """The anti-regression case: prose that legitimately names a site must be untouched."""
        text = parse_chapter(fx("chapter_promo_bare.html"), "第2章 假標題2", ch_url(2))
        assert "他打開瀏覽器，輸入 example.com，然後等著頁面慢慢地載入出來。" in text

    def test_a_long_paragraph_naming_the_sites_own_domain_survives(self):
        """Length alone saves a real paragraph, whatever domain it happens to contain."""
        text = parse_chapter(fx("chapter_promo_bare.html"), "第2章 假標題2", ch_url(2))
        assert "他想起那個叫做 ttk.tw 的網站" in text

    def test_removing_a_promo_line_does_not_split_its_paragraph(self):
        """The advert shares a <p> with real prose. Dropping it must leave ONE paragraph —
        downstream TTS chunking keys off the blank line."""
        text = body_of("chapter.html")
        assert len(text.split("\n\n")) == 10  # ten <p>, not eleven

    def test_stripping_never_empties_a_chapter(self):
        """A body that is nothing but adverts returns them rather than "". A surviving advert
        is a nuisance; a blanked chapter is data loss noticed chapters later."""
        text = parse_chapter(fx("chapter_all_promo.html"), "第6章 假標題6", ch_url(6))
        assert text.strip()

    @pytest.mark.parametrize(
        "line",
        [
            "他打開瀏覽器，輸入 example.com，然後等著頁面慢慢地載入出來。",
            "她在紙上寫下 ttk 兩個字母，然後把紙揉成一團丟進了火盆裡面。",
            "Truy cập vào hệ thống, hắn thấy một dòng chữ đỏ.",
            "",
            "   ",
        ],
    )
    def test_the_predicate_leaves_ordinary_lines_alone(self, line):
        assert not _is_promo_line(line)

    def test_the_predicate_needs_a_domain(self):
        """The load-bearing conjunct: no domain, no deletion, however promotional it reads."""
        assert not _is_promo_line("請直接訪問我們的網站，看最新章節，體驗超讚")


class TestChapterContent:
    def test_paragraphs_are_separated_by_blank_lines(self):
        text = body_of("chapter.html")
        assert "\n\n" in text
        assert "\n\n\n" not in text

    def test_no_leading_or_trailing_blank(self):
        text = body_of("chapter.html")
        assert text == text.strip()

    def test_the_emsp_indents_are_stripped(self):
        assert not any(line.startswith(" ") for line in body_of("chapter.html").split("\n"))

    def test_a_title_echo_is_dropped(self):
        text = parse_chapter(fx("chapter_title_echo.html"), "第2章 假標題2", ch_url(2))
        assert not text.startswith("第2章")
        assert "假文字第2段" in text

    def test_a_non_matching_title_never_strips_the_first_line(self):
        text = parse_chapter(fx("chapter.html"), "完全不同的標題", ch_url(1))
        assert text.startswith("假文字第1段")

    def test_raises_when_the_body_is_empty(self):
        with pytest.raises(ScrapeError, match="empty|not found"):
            parse_chapter(fx("chapter_empty.html"), "第5章 假標題5", ch_url(5))

    def test_empty_content_is_not_reported_as_obfuscated(self):
        """ObfuscatedContentError has GUI meaning: it offers the user a repair that cannot
        help here, because nothing on this site is encoded."""
        with pytest.raises(ScrapeError) as exc:
            parse_chapter(fx("chapter_empty.html"), "第5章 假標題5", ch_url(5))
        assert not isinstance(exc.value, ObfuscatedContentError)


class TestRegistry:
    @pytest.mark.parametrize("url", ALL_FORMS)
    def test_matches_every_paste_form(self, url):
        assert TtksAdapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://ttks.tw/",
            "https://ttks.tw/novel/",
            "https://ttks.tw/files/article/image/100/100999/100999s.jpg",
            # Same CMS family, a different host. Claiming it unmeasured is how a scraper
            # silently half-works — widening is a deliberate edit.
            f"https://www.ttkan.co/novel/chapters/{SLUG}/index.html",
        ],
    )
    def test_rejects_urls_it_has_not_measured(self, url):
        assert not TtksAdapter.matches(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://twkan.com/book/114283.html",
            "https://twkan.com/txt/114283/9527",
            "https://www.novel543.com/0802691255",
            "https://www.timotxt.com/2608569069/",
            "https://sto9.com/book/13908/index.html",
            "https://www.69shuba.com/book/59024/",
            "https://m.bqg5.com/4_4217/",
        ],
    )
    def test_does_not_steal_a_neighbours_urls(self, url):
        """ADAPTERS is first-match-wins by import order, so a loose pattern steals silently."""
        assert not TtksAdapter.matches(url)

    def test_adapter_for_url_routes_ttks_to_this_adapter(self):
        adapter = adapter_for_url(READ_URL, HttpClient(delay_seconds=0))
        assert isinstance(adapter, TtksAdapter)

    def test_the_adapter_is_registered(self):
        """Catches a forgotten `_import_adapters()` edit — without it the module never
        registers and the adapter silently does not exist."""
        assert TtksAdapter in ADAPTERS

    def test_content_is_not_pre_translated(self):
        """Flipping this would land Chinese in `translated` and mark chapters done."""
        assert TtksAdapter.content_is_translated is False


class TestAdapterWiring:
    def test_a_full_scan_costs_exactly_one_navigation(self):
        """Metadata and the TOC live on one page, and every navigation into a Cloudflare host
        is another chance at an interstitial."""
        adapter, session = make_adapter()
        adapter.fetch_metadata(READ_URL)
        adapter.fetch_chapter_list(READ_URL)
        assert session.requested == [READ_URL]

    def test_metadata_from_a_chapter_url_hits_the_index_page(self):
        adapter, session = make_adapter()
        assert adapter.fetch_metadata(CHAPTER_PASTE).title == TITLE
        assert session.requested == [READ_URL]

    def test_a_chapter_fetches_exactly_one_page(self):
        adapter, session = make_adapter()
        adapter.fetch_chapter(ChapterRef(index=0, title=ch_title(1), url=ch_url(1)))
        assert session.requested == [ch_url(1)]

    def test_the_next_chapter_link_is_never_followed(self):
        """Chapter 2 IS in the fake's map, so following the link would succeed silently."""
        adapter, session = make_adapter()
        adapter.fetch_chapter(ChapterRef(index=0, title=ch_title(1), url=ch_url(1)))
        assert ch_url(2) not in session.requested


class TestBrowserPath:
    def test_constructing_never_launches_a_browser(self):
        adapter = TtksAdapter(HttpClient(delay_seconds=0))
        assert adapter._session is None

    def test_close_is_safe_before_any_fetch(self):
        TtksAdapter(HttpClient(delay_seconds=0)).close()

    def test_close_releases_the_session_and_is_idempotent(self):
        adapter, session = make_adapter()
        adapter.close()
        adapter.close()
        assert session.closed
        assert adapter._session is None

    def test_one_session_is_reused_across_chapters(self):
        adapter, session = make_adapter()
        adapter.fetch_chapter(ChapterRef(index=0, title=ch_title(1), url=ch_url(1)))
        adapter.fetch_chapter(ChapterRef(index=2, title=ch_title(3), url=ch_url(3)))
        assert adapter._session is session

    def test_the_user_is_warned_before_chrome_appears(self, monkeypatch):
        adapter = TtksAdapter(HttpClient(delay_seconds=0))
        messages = capture_status(adapter)
        monkeypatch.setattr(
            "noveltrans.scrapers.ttks.BrowserSession",
            lambda **kw: _FakeSession({READ_URL: fx("index.html")}),
        )
        adapter._get_html(READ_URL)
        assert any("Cloudflare" in m for m in messages)

    def test_the_configured_delay_is_passed_through(self, monkeypatch):
        """HttpClient's own throttle is bypassed on this path, so this is the only one left."""
        seen: dict = {}

        def record(**kwargs):
            seen.update(kwargs)
            return _FakeSession({READ_URL: fx("index.html")})

        monkeypatch.setattr("noveltrans.scrapers.ttks.BrowserSession", record)
        TtksAdapter(HttpClient(delay_seconds=2.5))._get_html(READ_URL)
        assert seen["delay_seconds"] == 2.5
        assert seen["headless"] is False  # headless is fingerprinted and never clears the gate

    def test_a_missing_browser_says_how_to_install_one(self, monkeypatch):
        adapter, session = make_adapter()

        def boom(_url):
            raise BrowserUnavailableError("no playwright")

        monkeypatch.setattr(session, "get_html", boom)
        with pytest.raises(ScrapeError) as exc:
            adapter.fetch_metadata(READ_URL)
        assert "Chrome" in str(exc.value) and "playwright install" in str(exc.value)

    def test_a_dead_session_says_to_retry(self, monkeypatch):
        adapter, session = make_adapter()

        def boom(_url):
            raise BrowserSessionError("window closed")

        monkeypatch.setattr(session, "get_html", boom)
        with pytest.raises(ScrapeError, match="Thử tải lại"):
            adapter.fetch_metadata(READ_URL)


# The live class is deselected by default (`addopts = "-m 'not live'"`). It exists as a drift
# detector: run it when the site looks like it has changed, with
# `pytest -m live tests/test_scrapers_ttks.py`. It launches a real Chrome.
LIVE_URL = "https://ttks.tw/novel/chapters/daqiandingliutaohunfumaye/index.html"


@pytest.mark.live
class TestLive:
    @pytest.fixture(scope="class")
    def adapter(self):
        adapter = TtksAdapter(HttpClient(delay_seconds=1.5))
        try:
            yield adapter
        finally:
            adapter.close()

    @pytest.fixture(scope="class")
    def refs(self, adapter):
        return adapter.fetch_chapter_list(LIVE_URL)

    def test_metadata(self, adapter):
        meta = adapter.fetch_metadata(LIVE_URL)
        assert meta.title and meta.author
        assert meta.source_lang == "zh"
        assert meta.url == LIVE_URL
        assert SEO_MARKER not in meta.description  # the boilerplate must not have crept back

    def test_the_chapter_list_is_complete_and_contiguous(self, refs):
        assert len(refs) > 900
        assert chapter_positions(refs) == list(range(1, len(refs) + 1))
        assert len({r.url for r in refs}) == len(refs)
        assert all(r.title for r in refs)

    def test_the_advertised_latest_position_matches_the_list_length(self, adapter, refs):
        """The one assumption the offline fixtures cannot check: that
        `og:novel:latest_chapter_url` really is positional. If this goes red, drop the
        report-only cross-check rather than gating on it."""
        markup = adapter._index_page(LIVE_URL)
        assert latest_position(markup, slug(LIVE_URL)) == len(refs)

    def test_a_chapter_comes_back_whole_and_promo_free(self, adapter, refs):
        text = adapter.fetch_chapter(refs[0])
        assert len(text) > 1000
        assert "ttk" not in unicodedata.normalize("NFKC", text)
        for junk in ("下一章", "返回目錄", "添加書籤", "分享給朋友"):
            assert junk not in text

    def test_a_short_consecutive_walk_survives_the_default_delay(self, adapter, refs):
        """The measurement that decides whether a minimum-delay floor is needed. novel543
        pins one at 3 s on the strength of a walk like this; ttks has none because this
        passed at the default."""
        for ref in refs[:10]:
            assert len(adapter.fetch_chapter(ref)) > 500

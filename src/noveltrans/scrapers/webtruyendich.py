"""Adapter for webtruyendich.com — Chinese web novels served as Vietnamese MTL.

Landing page: https://webtruyendich.com/truyen/<slug>              — metadata (open)
Chapter list: https://webtruyendich.com/truyen/<slug>/danh-sach-chuong-day-du
Chapter page: https://webtruyendich.com/truyen/<slug>/fanqie/chuong-<N>-<title>

Two things make this adapter unlike the plain `requests` scrapers:

  * The whole site sits behind a Cloudflare challenge, so every page comes through
    a real browser (`webtruyendich_browser.WtdBrowserSession`), like 69shuba.
  * The chapter text is produced on-page by an AI translation the reader selects
    from a dropdown. This adapter drives that: it picks the
    "AI Gemini FLASH 3.5 - Memory - No Apikey" model, clicks "Dịch lại", and reads
    the rendered Vietnamese. Because that text is already the translation, the
    adapter is flagged `content_is_translated` — the download worker stores it as
    the chapter's `translated` text and skips NovelTrans's own translators.

As with 69shuba, fetching is split from parsing: everything above the adapter
class is pure and takes markup, so the whole parsing surface is tested against
saved fixtures without launching a browser.

**Do not parallelise downloads.** One Chrome, and this site is Cloudflare- and
AI-quota-gated — hammering it risks a block that would kill the only path it has.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.browser import BrowserUnavailableError
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter
from noveltrans.webtruyendich_browser import WtdBrowserSession, WtdBrowserSessionError

_ORIGIN = "https://webtruyendich.com"

# The on-page AI model to select for every chapter. The <option> value equals its
# visible label, so this literal string is what select_option() receives.
TRANSLATOR_MODEL = "AI Gemini FLASH 3.5 - Memory - No Apikey"
# Recorded in Chapter.translator so the user can see how a chapter was produced.
TRANSLATOR_LABEL = "webtruyendich (Gemini Flash 3.5)"

SEL_TRANSLATOR = "#translator"
SEL_CONTENT = "#chapter-content-body"
SEL_PARAGRAPH = "p.fade-in-paragraph"
RETRANSLATE_BUTTON = "Dịch lại"

SEL_TOC_LINKS = 'a[href*="/fanqie/chuong-"]'
_CH_NUM_RE = re.compile(r"/chuong-(\d+)(?:[-/]|$)")
# Chapter anchor: href + inner title. Parsed by regex, not an HTML parser — the
# raw TOC document is malformed enough that lxml/html.parser drop the title text
# of all but the first ~135 chapters (see parse_chapter_list).
_CH_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?\bhref="([^"]*/fanqie/chuong-\d+[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"webtruyendich\.com/truyen/([a-z0-9-]+)")
# The <h1> reads "Truyện <title>"; drop that leading label.
_TITLE_PREFIX_RE = re.compile(r"^\s*Truyện\s+")
# og:title is decorated: "[Mới nhất] <title> - Đọc Truyện Dịch Chuẩn Online | webtruyendich.com".
_OG_TITLE_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_OG_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*Đọc Truyện.*$")


def slug(url: str) -> str:
    match = _SLUG_RE.search(url)
    if not match:
        raise ScrapeError("Could not extract novel slug from URL", url)
    return match.group(1)


def landing_url(url: str) -> str:
    return f"{_ORIGIN}/truyen/{slug(url)}"


def toc_url(url: str) -> str:
    return f"{landing_url(url)}/danh-sach-chuong-day-du"


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the landing page. Prefers the clean <h1> title over the decorated
    og:title; author comes from the /tac-gia/ link (there's no og:novel:author).

    `url` is echoed into NovelMeta.url unchanged — the library keys projects off
    the URL the user gave, so rewriting it here would orphan them.
    """
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        el = soup.select_one(f"meta[property='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    h1 = soup.select_one("h1")
    title = _TITLE_PREFIX_RE.sub("", h1.get_text(strip=True)).strip() if h1 else ""
    if not title:
        og_title = og("og:title")
        title = _OG_TITLE_SUFFIX_RE.sub("", _OG_TITLE_PREFIX_RE.sub("", og_title)).strip()
    if not title:
        el = soup.select_one("title")
        title = el.get_text(strip=True) if el else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author_el = soup.select_one("a[href^='/tac-gia/']")
    author = author_el.get_text(strip=True) if author_el else ""

    return NovelMeta(
        url=url,
        site=site,
        title=title,
        author=author,
        description=og("og:description"),
        cover_url=og("og:image"),
        source_lang="vi",  # content arrives already-Vietnamese
    )


def parse_chapter_list(markup: str, base_url: str) -> list[ChapterRef]:
    """Read the full TOC. Links are newest-first and a chapter number can
    occasionally carry two different title-slugs, so dedup by URL and order by
    chapter number (falling back to first-seen order for equal numbers).

    Uses a regex over the raw markup rather than an HTML parser on purpose: the
    server-rendered TOC document (read whole to avoid scrolling the JS-virtualized
    list) is malformed enough that lxml and html.parser both drop the title text of
    all but the first ~135 chapters. The regex recovers every title, and works
    equally on the well-formed browser DOM used as the fallback."""
    seen: set[str] = set()
    entries: list[tuple[int, int, str, str]] = []  # (num, order, title, url)
    for match in _CH_ANCHOR_RE.finditer(markup):
        href = match.group(1)
        num = _CH_NUM_RE.search(href)
        if not num:
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        title = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", match.group(2)))).strip()
        entries.append((int(num.group(1)), len(entries), title, absolute))

    if not entries:
        raise ScrapeError("Chapter list not found — page layout may have changed", base_url)

    entries.sort(key=lambda e: (e[0], e[1]))
    return [ChapterRef(index=i, title=title, url=url) for i, (_n, _o, title, url) in enumerate(entries)]


def parse_chapter(container_html: str, url: str) -> str:
    """Return the chapter body as paragraphs separated by blank lines.

    Each AI paragraph is `<p class="fade-in-paragraph has-anchor">` and opens with
    an `<a class="citation-anchor">#</a>` link; drop that anchor so its "#" doesn't
    leak into the text. A stray CJK char occasionally survives the AI output — it's
    left as-is (tolerated, not an error)."""
    soup = BeautifulSoup(container_html, "lxml")
    paragraphs = soup.select(SEL_PARAGRAPH) or soup.find_all("p")
    lines: list[str] = []
    for p in paragraphs:
        for anchor in p.select("a.citation-anchor"):
            anchor.decompose()
        text = p.get_text(" ", strip=True).strip()
        if text:
            lines.append(text)
    if not lines:
        raise ScrapeError("Chapter content is empty", url)
    return "\n\n".join(lines)


@register
class WebtruyendichAdapter(SiteAdapter):
    """webtruyendich.com. Fetches through a browser and drives the on-page AI
    translation; everything above this class does the parsing."""

    name = "webtruyendich"
    display_name = "Web Truyện Dịch (webtruyendich.com)"
    url_patterns = [r"webtruyendich\.com/truyen/[a-z0-9-]+"]

    content_is_translated = True
    translated_lang = "vi"
    translator_label = TRANSLATOR_LABEL

    def __init__(self, client, *, headless: bool = False):
        super().__init__(client)
        # Headless is fingerprinted by Cloudflare and does not clear the challenge
        # (measured — see cf_browser). The flag exists in case that ever changes.
        self._headless = headless
        self._session: WtdBrowserSession | None = None

    # -- fetching: the only part that touches a browser ---------------------------

    def _ensure_session(self) -> WtdBrowserSession:
        if self._session is None:
            # A Chrome window is about to appear on the user's screen — say why.
            self._status(
                "🌐 Đang mở trình duyệt để đọc webtruyendich (vượt Cloudflare + dịch AI) "
                "— giữ cửa sổ mở…"
            )
            self._session = WtdBrowserSession(
                headless=self._headless,
                delay_seconds=self.client.delay_seconds,
            )
        return self._session

    def _get_html(
        self, url: str, *, prefer_document: bool = False, scroll_item_selector: str | None = None
    ) -> str:
        try:
            return self._ensure_session().get_html(
                url, prefer_document=prefer_document, scroll_item_selector=scroll_item_selector
            )
        except BrowserUnavailableError as exc:
            raise self._browser_needed_error(url) from exc
        except WtdBrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc được trang webtruyendich — trình duyệt bị đóng hoặc không "
                f"vượt được kiểm tra Cloudflare. Thử lại. ({exc})",
                url,
            ) from exc

    def _browser_needed_error(self, url: str) -> ScrapeError:
        return ScrapeError(
            "Cần trình duyệt để đọc webtruyendich (trang có kiểm tra Cloudflare và dịch "
            "AI trên trang). Cài Google Chrome, hoặc chạy:  pip install "
            "'noveltrans[browser]' && playwright install chromium",
            url,
        )

    def close(self) -> None:
        """Release the browser. Idempotent; never raises."""
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- SiteAdapter ---------------------------------------------------------------

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._get_html(landing_url(url)), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        # The TOC is fully server-rendered but JS virtualizes the live DOM down to
        # ~135 rows. Read the raw document (all chapters, instant); if that response
        # is a Cloudflare challenge, fall back to scrolling the rebuilt DOM.
        markup = self._get_html(
            toc_url(url), prefer_document=True, scroll_item_selector=SEL_TOC_LINKS
        )
        return parse_chapter_list(markup, url)

    def fetch_chapter(self, ref: ChapterRef) -> str:
        try:
            container_html = self._ensure_session().read_translated_chapter(
                ref.url,
                translator_select=SEL_TRANSLATOR,
                translator_value=TRANSLATOR_MODEL,
                retranslate_button=RETRANSLATE_BUTTON,
                content_selector=SEL_CONTENT,
                paragraph_selector=SEL_PARAGRAPH,
            )
        except BrowserUnavailableError as exc:
            raise self._browser_needed_error(ref.url) from exc
        except WtdBrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc/dịch được chương webtruyendich — có thể bị Cloudflare "
                f"Turnstile chặn hoặc hết hạn mức AI. Thử lại. ({exc})",
                ref.url,
            ) from exc
        return parse_chapter(container_html, ref.url)

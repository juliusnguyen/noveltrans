"""Adapter for 69书吧 (69shuba.com).

Info page:    https://www.69shuba.com/book/<id>.htm    — metadata, as OpenGraph tags
Chapter list: https://www.69shuba.com/book/<id>/       — the full TOC, one page
Chapter page: https://www.69shuba.com/txt/<id>/<cid>

Unlike the other adapters, this one does NOT fetch over `HttpClient`: 69shuba sits
behind a Cloudflare challenge that only a real browser passes, so every page comes
through `cf_browser.BrowserSession`. See that module for the measured details, and
changes/015-69SHUBA-SOURCE for how that was established.

**Do not parallelise downloads from this site.** Each concurrent session is another
Chrome, and hammering a CF-protected site risks an IP block — which would kill the
browser path too, and it is the only path this site has.

The module splits fetching from parsing on purpose: everything below the `parse_*`
line is pure and takes markup, so the whole parsing surface is tested against saved
fixtures without launching a browser.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSession, BrowserSessionError
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

SEL_TOC_LINKS = "#catalog ul li a[href]"
SEL_CONTENT = ".txtnav"
# Chrome living *inside* .txtnav. Targeted by name rather than xbanxia's blanket
# "span, div, script, ins": .txtnav holds bare <span>s, and decomposing every <div>
# would silently eat content the day the site wraps a paragraph in one.
SEL_CHROME = (
    "h1.hide720, div.txtinfo.hide720, div#txtright, div.contentadv, div.bottom-ad, "
    "script, style, ins"
)

# The site is inconsistent about prefixing chapter titles with their index: ch.1 is
# "1.第1章 …" but ch.2 is plain "第2章 …". The prefix is pure duplication of the 第N章
# already in the title, and it would otherwise leak into the EPUB TOC and be read
# aloud by TTS, so it's normalised away at parse time.
_INDEX_PREFIX_RE = re.compile(r"^\s*\d+\s*[.．、]\s*")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _clean_title(text: str) -> str:
    """Drop a leading "N." index prefix so titles compare and display uniformly."""
    return _INDEX_PREFIX_RE.sub("", text).strip()


def book_id(url: str) -> str:
    """Extract the book id from either URL form (/book/59024.htm or /book/59024/)."""
    match = re.search(r"/book/(\d+)", url)
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def _origin(url: str) -> str:
    """Keep the scheme+host the user gave us, so mirrors keep working."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def info_url(url: str) -> str:
    """The metadata page — a *different* page from the chapter list."""
    return f"{_origin(url)}/book/{book_id(url)}.htm"


def toc_url(url: str) -> str:
    """The chapter list page."""
    return f"{_origin(url)}/book/{book_id(url)}/"


def _clean_description(text: str) -> str:
    """og:description carries literal "<br />" text, not markup — unescape to newlines.

    It arrives as a meta *attribute value*, so BeautifulSoup never sees those tags and
    can't strip them for us.
    """
    text = _BR_RE.sub("\n", text)
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the info page's OpenGraph tags.

    `url` is echoed into NovelMeta.url unchanged — the library keys projects off the
    URL the user gave, so rewriting it here would orphan them.
    """
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        el = soup.select_one(f"meta[property='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    title = og("og:novel:book_name") or og("og:title")
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)
    return NovelMeta(
        url=url,
        site=site,
        title=title,
        author=og("og:novel:author"),
        description=_clean_description(og("og:description")),
        cover_url=og("og:image"),
    )


def parse_chapter_list(markup: str, base_url: str) -> list[ChapterRef]:
    """Read the TOC. It is not paginated — every chapter is on this one page."""
    soup = BeautifulSoup(markup, "lxml")
    links = soup.select(SEL_TOC_LINKS)
    if not links:
        raise ScrapeError("Chapter list not found — page layout may have changed", base_url)
    return [
        ChapterRef(
            index=i,
            title=_clean_title(a.get_text(strip=True)),
            url=urljoin(base_url, a["href"]),
        )
        for i, a in enumerate(links)
    ]


def parse_chapter(markup: str, title: str, url: str) -> str:
    """Return the chapter body as paragraphs separated by blank lines."""
    soup = BeautifulSoup(markup, "lxml")
    container = soup.select_one(SEL_CONTENT)
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)

    for el in container.select(SEL_CHROME):
        el.decompose()

    # What's left is text nodes separated by <br>.
    lines = [line.strip() for line in container.get_text("\n").split("\n") if line.strip()]

    # The body repeats the chapter title as its first line — but not identically: the
    # title may carry an "N." index prefix the body omits. Compare both sides
    # normalised, and drop at most one line. Under-stripping leaves a duplicated
    # heading (cosmetic); over-stripping silently eats a real opening line (data loss
    # the reader won't notice until later), so bias hard to under-strip.
    if lines and _clean_title(lines[0]) == _clean_title(title):
        lines = lines[1:]

    if not lines:
        raise ScrapeError("Chapter content is empty", url)
    return "\n\n".join(lines)


@register
class Shuba69Adapter(SiteAdapter):
    """69书吧. Fetches through a browser; everything above this line does the parsing.

    Module is `shuba69`, not `69shuba` — the latter isn't a valid Python identifier and
    couldn't be imported. `name` is the plain string, so it's unaffected.
    """

    name = "69shuba"
    display_name = "69书吧 (69shuba.com)"
    # Matches both /book/59024.htm and /book/59024/ — `matches()` uses re.search.
    # Only www.69shuba.com has actually been read past the challenge; .cx is the same
    # operator's known mirror (worst case there is a clean ScrapeError).
    url_patterns = [r"69shuba\.(?:com|cx)/book/\d+"]

    def __init__(self, client, *, headless: bool = False):
        super().__init__(client)
        # Headless is fingerprinted by Cloudflare and does not clear the challenge
        # (measured — see cf_browser). The flag exists in case that ever changes.
        self._headless = headless
        self._session: BrowserSession | None = None

    # -- fetching: the only part that touches a browser ---------------------------

    def _get_html(self, url: str) -> str:
        """The single seam between this adapter and the browser."""
        if self._session is None:
            # A Chrome window is about to appear on the user's screen — say why before
            # it does, or it reads as the app misbehaving.
            self._status(
                "🌐 Đang mở trình duyệt để vượt kiểm tra Cloudflare của 69shuba — "
                "giữ cửa sổ mở…"
            )
            self._session = BrowserSession(
                headless=self._headless,
                # Honour the app's configured politeness delay: HttpClient's own
                # throttle is bypassed on this path, so this is the only one left.
                delay_seconds=self.client.delay_seconds,
            )
        try:
            return self._session.get_html(url)
        except BrowserUnavailableError as exc:
            raise ScrapeError(
                "Cần trình duyệt để đọc 69shuba (trang này có kiểm tra Cloudflare). "
                "Cài Google Chrome, hoặc chạy:  pip install 'noveltrans[browser]' "
                "&& playwright install chromium",
                url,
            ) from exc
        except BrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc được trang 69shuba — trình duyệt bị đóng hoặc không vượt "
                f"được kiểm tra Cloudflare. Thử tải lại. ({exc})",
                url,
            ) from exc

    def close(self) -> None:
        """Release the browser. Idempotent; never raises."""
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- SiteAdapter ---------------------------------------------------------------

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._get_html(info_url(url)), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        return parse_chapter_list(self._get_html(toc_url(url)), url)

    def fetch_chapter(self, ref: ChapterRef) -> str:
        return parse_chapter(self._get_html(ref.url), ref.title, ref.url)

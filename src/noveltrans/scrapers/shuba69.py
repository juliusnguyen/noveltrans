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

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta

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

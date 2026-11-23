"""Adapter for 小说之家 (xszj.org).

Detail page:  https://xszj.org/b/<bid>                  — metadata (OpenGraph + visible DOM)
Full TOC:     https://xszj.org/b/<bid>/cs/<page>        — 200 chapters per page, <ol> list
Chapter page: https://xszj.org/b/<bid>/c/<cid>          — sub-page 1
Sub-page:     https://xszj.org/b/<bid>/c/<cid>?page=<k> — k = 2..M

Plain HTTP (`HttpClient`): no Cloudflare challenge, no User-Agent gate, and no cookies are
required. `xszj.org` serves Simplified Chinese (`zh-Hans`) and `m.xszj.org` serves
Traditional Chinese (`zh-Hant`) at the same book/chapter IDs; every URL form normalises to
`https://xszj.org` so one novel never splits into multiple projects in the library.

Three site-specific traps are handled here:

* **The detail page's chapter lists are both incomplete.** `#sec-latest` lists the 9 newest
  chapters in reverse order, and `#sec-all` lists only the first 60 chapters even on a
  95-chapter novel. `fetch_chapter_list` always reads `/b/<bid>/cs/1` (and `/cs/2..N` when
  the `<select>` pagination widget is present on novels with >200 chapters).
* **Chapters are paginated across `?page=2..M`, and out-of-range `?page=` requests silently
  return page 1 with HTTP 200.** The `<h1>` (backed up by `<title>`) carries a `（1/M）` /
  `(1/M)` suffix when and only when a chapter is split across multiple sub-pages (e.g.
  chapter 1 is `（1/4）`, chapter 2 is `（1/2）`, chapter 96 has no suffix and is 1 page).
  Sub-pages are built from the chapter URL stem and verified with `page_position(markup) ==
  (k, pages)` so a silent fallback to page 1 can never duplicate the chapter opening. Never
  follow the `rel="next"` link: on the last sub-page it points to the next *chapter*.
* **Promo trailer paragraphs sit inside `#booktxt` on the final sub-page of some chapters.**
  The last sub-page of chapters 1 and 3 appends three `<p>` elements inside `#booktxt`:
  `小说之家为广大书友们提供好看的网络小说…`, `如果您觉得《…》小说很精彩的话，请粘贴以下网址…`, and
  `（ 本书网址：https://xszj.org/b/<bid> ）`. Because only the third paragraph carries a URL,
  downstream `drop_site_ads` would only catch the third line after translation and leave the
  first two promo paragraphs in the story. `_is_promo_paragraph` strips all three at scrape
  time so the stored source text is already clean.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

ORIGIN = "https://xszj.org"

_BID_RE = re.compile(r"(?:^|//)(?:(?:www|m)\.)?xszj\.org/b/(\d+)")
_TOC_HREF_RE = re.compile(r"^/b/(\d+)/cs/(\d+)$")
_CHAPTER_HREF_RE = re.compile(r"^/b/(\d+)/c/(\d+)$")
_CHAPTER_URL_RE = re.compile(r"(?:^|//)(?:(?:www|m)\.)?xszj\.org/b/(\d+)/c/(\d+)(?:\?page=(\d+))?$")

# Matches the trailing "(1/4)" or "（1/4）" on a paginated chapter's <h1> or <title>.
# On <title>, "-小说之家" follows the "(1/4)" suffix, so we allow optional "-小说之家" at the end.
_PAGE_SUFFIX_RE = re.compile(r"[(（]\s*(\d+)\s*/\s*(\d+)\s*[)）](?:\s*-\s*小说之家)?\s*$")

_MAX_SUB_PAGES = 30
_MAX_TOC_PAGES = 100

_COLONS = ":："
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")

_PLACEHOLDER_COVERS = (
    "/static/default/img/nocover.jpg",
    "/default_cover_male.jpg",
    "/default_cover_female.jpg",
)

SEL_TOC_SECTION = "section[aria-labelledby='toc-title']"
SEL_TOC_LINKS = "section[aria-labelledby='toc-title'] ol li a[href]"
SEL_TOC_OPTIONS = "section[aria-labelledby='toc-title'] select option[value]"
SEL_INTRO = "section[aria-labelledby='sec-intro'] p"
SEL_HEADING = "article.reader h1"
SEL_CONTENT = "#booktxt"
SEL_CHROME = "script, style, ins, iframe, div"

_PROMO_MARKERS = (
    "小说之家为广大书友们提供好看的网络小说",
    "小说很精彩的话，请粘贴以下网址分享给您的好友",
    "本书网址：",
)


def _norm(text: str) -> str:
    """Collapse horizontal whitespace for title comparison and single-line fields."""
    return _WS_RE.sub(" ", text or "").strip()


# --------------------------------------------------------------------------- urls


def book_id(url: str) -> str:
    """Extract the numeric book ID from any xszj.org book, TOC, or chapter URL."""
    match = _BID_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def read_url(url: str) -> str:
    """Canonical novel URL (`https://xszj.org/b/<bid>`), regardless of which form was pasted."""
    return f"{ORIGIN}/b/{book_id(url)}"


def toc_url(url: str, page: int = 1) -> str:
    """The paginated table-of-contents URL (`/b/<bid>/cs/<page>`)."""
    return f"{ORIGIN}/b/{book_id(url)}/cs/{page}"


def sub_page_url(page_one_url: str, page: int) -> str:
    """Build `?page=<page>` from a chapter's page-1 URL, refusing an existing `?page=`."""
    match = _CHAPTER_URL_RE.search(page_one_url or "")
    if match is None or match.group(3):
        raise ScrapeError("Not a chapter page-1 URL", page_one_url)
    return f"{ORIGIN}/b/{match.group(1)}/c/{match.group(2)}?page={page}"


# ------------------------------------------------------------------------ parsing


def _is_placeholder_cover(url: str) -> bool:
    path = (url or "").split("?", 1)[0]
    return any(path.endswith(suffix) for suffix in _PLACEHOLDER_COVERS)


def parse_metadata(markup: str, url: str, site: str = "xszj") -> NovelMeta:
    """Read the book page's OpenGraph metadata, falling back to visible elements."""
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        el = soup.select_one(f"meta[property='{prop}']") or soup.select_one(
            f"meta[name='{prop}']"
        )
        return (el.get("content") or "").strip() if el else ""

    article = soup.select_one("article.card")

    title = og("og:novel:book_name")
    if not title:
        heading = article.select_one("h1") if article else soup.select_one("h1")
        title = _norm(heading.get_text()) if heading else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = og("og:novel:author")
    if not author and article:
        for p in article.select("p"):
            text = _norm(p.get_text(" "))
            if text.startswith("作者"):
                author = re.split(f"[{_COLONS}]", text, maxsplit=1)[-1].strip()
                break

    intro = soup.select_one(SEL_INTRO)
    description = intro.get_text("\n", strip=True) if intro else og("og:description")

    cover = og("og:image")
    if not cover and article:
        for img in article.select("img[src]"):
            if img.get("aria-hidden") == "true":
                continue
            cover = urljoin(ORIGIN, (img.get("src") or "").strip())
            break
    if _is_placeholder_cover(cover):
        cover = ""

    return NovelMeta(
        url=read_url(url),
        site=site,
        title=title,
        author=author,
        description=description,
        cover_url=cover,
        source_lang="zh",
    )


def toc_page_numbers(markup: str, bid: str) -> list[int]:
    """Return all TOC page numbers `[1..N]` listed in `/cs/1`'s `<select>` pagination."""
    soup = BeautifulSoup(markup, "lxml")
    pages = {1}
    for option in soup.select(SEL_TOC_OPTIONS):
        match = _TOC_HREF_RE.match((option.get("value") or "").strip())
        if match and match.group(1) == bid:
            page = int(match.group(2))
            if 1 <= page <= _MAX_TOC_PAGES:
                pages.add(page)
    return sorted(pages)


def parse_toc_page(markup: str, bid: str, url: str) -> list[tuple[str, str]]:
    """Parse one `/b/<bid>/cs/<page>` document into `(title, chapter_url)` pairs."""
    soup = BeautifulSoup(markup, "lxml")
    anchors = soup.select(SEL_TOC_LINKS)
    entries: list[tuple[str, str]] = []
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        match = _CHAPTER_HREF_RE.match(href)
        if not match or match.group(1) != bid:
            continue
        title = _norm(anchor.get_text())
        if not title:
            continue
        entries.append((title, f"{ORIGIN}/b/{bid}/c/{match.group(2)}"))

    if not entries:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)
    return entries


def page_position(markup: str) -> tuple[int, int] | None:
    """Return `(current_page, total_pages)` from the chapter `<h1>` or `<title>`, or None."""
    soup = BeautifulSoup(markup, "lxml")
    for element in (soup.select_one(SEL_HEADING), soup.title):
        if element is None:
            continue
        match = _PAGE_SUFFIX_RE.search(_norm(element.get_text()))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def page_count(markup: str, url: str) -> int:
    """How many sub-pages a chapter has, per its `<h1>` `(1/M)` suffix (1 if unpaginated)."""
    soup = BeautifulSoup(markup, "lxml")
    heading = soup.select_one(SEL_HEADING)
    if heading is None and soup.title is None:
        raise ScrapeError("Chapter page layout may have changed — no heading found", url)

    stated = page_position(markup)
    if stated is None:
        return 1
    pages = stated[1]
    if pages < 1 or pages > _MAX_SUB_PAGES:
        raise ScrapeError(f"Implausible chapter page count ({pages})", url)
    return pages


def _is_promo_paragraph(line: str) -> bool:
    """True when `line` is one of the site's trailing self-promotion paragraphs."""
    if len(line) > 200:
        return False
    if any(marker in line for marker in _PROMO_MARKERS):
        return True
    return "xszj.org" in line.casefold() and ("小说之家" in line or "本书网址" in line)


def parse_chapter(markup: str, title: str, url: str) -> str:
    """Extract one sub-page's prose paragraphs separated by blank lines."""
    soup = BeautifulSoup(markup, "lxml")
    container = soup.select_one(SEL_CONTENT)
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)

    for junk in container.select(SEL_CHROME):
        junk.decompose()

    lines = [
        text
        for p in container.find_all("p", recursive=False)
        if (text := p.get_text(strip=True)) and not _is_promo_paragraph(text)
    ]
    if not lines:
        lines = [
            text
            for p in container.select("p")
            if (text := p.get_text(strip=True)) and not _is_promo_paragraph(text)
        ]

    if lines and title and _norm(lines[0]) == _norm(title):
        lines = lines[1:]

    body = "\n\n".join(lines).strip()
    if not body:
        raise ScrapeError("Chapter content is empty", url)
    return body


# ------------------------------------------------------------------------ adapter


@register
class XszjAdapter(SiteAdapter):
    name = "xszj"
    display_name = "小说之家 (xszj.org)"
    url_patterns = [r"xszj\.org/b/\d+"]
    content_is_translated = False

    def __init__(self, client):
        super().__init__(client)
        self._detail: tuple[str, str] | None = None

    def _detail_page(self, url: str) -> str:
        bid = book_id(url)
        if self._detail is not None and self._detail[0] == bid:
            return self._detail[1]
        markup = self.client.get_html(read_url(url))
        self._detail = (bid, markup)
        return markup

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._detail_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        bid = book_id(url)
        first_url = toc_url(url, 1)
        first_markup = self.client.get_html(first_url)
        entries = list(parse_toc_page(first_markup, bid, first_url))

        pages = toc_page_numbers(first_markup, bid)
        if pages != list(range(1, len(pages) + 1)):
            raise ScrapeError(
                f"Danh sách trang mục lục của xszj không liền mạch ({pages}). Thử quét lại sau.",
                first_url,
            )

        for page in pages[1:]:
            page_url = toc_url(url, page)
            page_markup = self.client.get_html(page_url)
            entries.extend(parse_toc_page(page_markup, bid, page_url))

        seen: set[str] = set()
        refs: list[ChapterRef] = []
        for title, chapter_href in entries:
            if chapter_href in seen:
                continue
            seen.add(chapter_href)
            refs.append(ChapterRef(index=len(refs), title=title, url=chapter_href))

        if not refs:
            raise ScrapeError("Chapter list is empty", first_url)
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        first = self.client.get_html(ref.url)
        pages = page_count(first, ref.url)
        bodies = [parse_chapter(first, ref.title, ref.url)]

        for k in range(2, pages + 1):
            url = sub_page_url(ref.url, k)
            markup = self.client.get_html(url)
            if page_position(markup) != (k, pages):
                raise ScrapeError(
                    f"xszj trả về sai trang {k}/{pages} của chương — thử tải lại chương này.",
                    url,
                )
            bodies.append(parse_chapter(markup, "", url))

        return "\n\n".join(bodies)

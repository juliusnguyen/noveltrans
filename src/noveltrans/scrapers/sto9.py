"""Adapter for 思兔閱讀 (sto9.com).

Detail page:  https://sto9.com/book/<id>.html          — metadata, as OpenGraph tags
Chapter list: https://sto9.com/book/<id>/index.html    — the canonical "read" URL
Chapter page: https://sto9.com/txt/<id>/<cid>.html
Full TOC:     https://sto9.com/ajax_novels/chapterlist/<id>.html

**The chapter-list page is truncated and does not say so in its markup.** It ships the
first ~15 and last ~20 chapters with a `LoadMore()` button between them, so a 187-chapter
novel renders as 36 entries that look perfectly normal. Worse, those 36 are not a prefix:
the `data-num` sequence jumps 15 -> 167. Since `ChapterRef.index` is dense and positional,
saving that list would file chapter 167's text under chapter 16's row, and `replace_toc`
preserves content across re-scans — so a later successful scan would rewrite the title and
URL while leaving the wrong body in place. Hence `fetch_chapter_list` takes the complete
list from the ajax fragment `LoadMore()` itself requests, and **raises rather than falling
back** to a list the page has already told us is incomplete. See changes/062-STO9-SCRAPER.

Same PHP novel CMS as 69shuba — `div.txtnav`, `<br>`-separated body, OpenGraph metadata —
so `shuba69.py` is the reference for the parsing. Unlike 69shuba this site issues no
Cloudflare challenge: plain `HttpClient` gets 200s everywhere and no cookie is needed.
Every page does carry an invisible `/cdn-cgi/content?id=...` bait link as its first
<body> child; this adapter never follows links, and it must stay that way.

Both the detail page and the TOC page also carry an inline `var bookinfo = {...}`. It is
single-quoted JS rather than JSON and holds nothing the OpenGraph tags don't, so it is
deliberately unused — it is not an oversight to "fix".

Content is Traditional Chinese served as UTF-8 with a correct charset header (the site's
s/t toggle is client-side only), so `source_lang` is "zh" and no encoding fixup applies.

The module splits fetching from parsing on purpose: everything below the `parse_*` line is
pure and takes markup, so the whole parsing surface is tested against saved fixtures.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

# Fixed, not taken from the pasted URL the way shuba69 does for its mirror. No sto9 mirror
# is known, and pinning the origin is what guarantees that http://, https://www. and the
# bare host all canonicalise to ONE string — which is the whole point of `read_url`. If a
# mirror ever appears, widening this is a deliberate edit, not an accident.
ORIGIN = "https://sto9.com"

_ID_RE = re.compile(r"/(?:book|txt)/(\d+)")
# "點擊展開全部187章節目錄" -> 187. The button states the true total even though the page
# below it does not contain that many entries.
_TOTAL_RE = re.compile(r"(\d+)\s*章")
_COLONS = ":："
# A coverless novel gets a grey "no cover" JPEG here. Stored, it renders into the EPUB and
# the video thumbnail as a *broken* cover rather than an absent one, and everything
# downstream already handles "". Matched by path suffix so a CDN host or a cache-buster
# query doesn't defeat it.
_PLACEHOLDER_COVER = "/images/nocover.jpg"

# Both the TOC page and the ajax fragment use this same shape, which is why one parser
# serves both. `li[data-num]` rather than a bare `a[href]` keeps the breadcrumb, the
# bookmark widget and any wrapper the CMS adds out of the list.
SEL_TOC_LINKS = "li[data-num] a[href]"
SEL_LOADMORE = "#loadmore"
SEL_CONTENT = "div.txtnav"
# Chrome living *inside* .txtnav, named rather than blanket-decomposing every <div> the
# way xbanxia does: the day the CMS wraps a paragraph in one, a blanket rule eats it
# silently. `div.txtcenter` is the notable one — it is an ad slot and it appears several
# times MID-PROSE, not only at the end.
SEL_CHROME = "h1, div.txtright, div.txtad, div.txtcenter, script, style, ins"

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Whitespace-normalised form, used only for comparing titles — never for storing."""
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------- urls


def book_id(url: str) -> str:
    """The numeric book id, from any of the three URL forms."""
    match = _ID_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def read_url(url: str) -> str:
    """The canonical URL for a novel — the site's own `og:novel:read_url`.

    `NovelMeta.url` is set from this rather than echoing what the user pasted, because
    `Library.find_by_url` is exact string equality: echoing would let the detail page, the
    index page and a chapter URL become three projects for one novel, each with its own
    translation progress and video settings.
    """
    return f"{ORIGIN}/book/{book_id(url)}/index.html"


def detail_url(url: str) -> str:
    """The metadata page — a *different* page from the chapter list, and the only one
    carrying OpenGraph tags."""
    return f"{ORIGIN}/book/{book_id(url)}.html"


def chapterlist_url(url: str) -> str:
    """The fragment `LoadMore()` fetches: the complete TOC, unpaginated."""
    return f"{ORIGIN}/ajax_novels/chapterlist/{book_id(url)}.html"


# ------------------------------------------------------------------------ parsing


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the detail page's OpenGraph tags, falling back to the visible book box."""
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        el = soup.select_one(f"meta[property='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    title = og("og:novel:book_name") or og("og:title")
    if not title:
        heading = soup.select_one("div.booknav2 h1")
        title = heading.get_text(strip=True) if heading else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = og("og:novel:author")
    if not author:
        for row in soup.select("div.booknav2 p"):
            text = row.get_text(" ", strip=True)
            if text.startswith("作者"):
                author = re.split(f"[{_COLONS}]", text, maxsplit=1)[-1].strip()
                break

    cover = og("og:image")
    if _PLACEHOLDER_COVER in cover:
        cover = ""

    return NovelMeta(
        url=read_url(url),
        site=site,
        title=title,
        author=author,
        description=og("og:description"),
        cover_url=cover,
        # Written explicitly rather than inherited: this is a Chinese source, and a future
        # change to the dataclass default must not silently reclassify it.
        source_lang="zh",
    )


def stated_total(markup: str) -> int | None:
    """How many chapters the novel really has, per the page's own claims.

    Two independent signals, because either alone is a single point of failure: the
    `#loadmore` button's text, and the highest `data-num` on the page (the truncated page
    ships the *last* chapters as well as the first, so this reaches the true total too).
    Takes the larger — over-estimating errs toward refusing an incomplete list, which is
    the safe direction. Returns None when the markup claims nothing.
    """
    soup = BeautifulSoup(markup, "lxml")
    signals: list[int] = []

    button = soup.select_one(SEL_LOADMORE)
    if button is not None:
        match = _TOTAL_RE.search(button.get_text(" ", strip=True))
        if match:
            signals.append(int(match.group(1)))

    numbers = [
        int(li["data-num"])
        for li in soup.select("li[data-num]")
        if str(li.get("data-num", "")).isdigit()
    ]
    if numbers:
        signals.append(max(numbers))

    return max(signals) if signals else None


def parse_chapter_list(markup: str, base_url: str) -> list[ChapterRef]:
    """Read a TOC, from either the ajax fragment or the page. Document order is reading
    order in both, and neither is paginated.

    The fragment never closes its `<li>` elements; lxml recovers that correctly into a
    flat list, which is why this is a real parse and not a regex.
    """
    soup = BeautifulSoup(markup, "lxml")
    links = soup.select(SEL_TOC_LINKS)
    if not links:
        raise ScrapeError("Chapter list not found — page layout may have changed", base_url)
    # `index` comes from enumerate, never from `data-num`: that is the site's display
    # number, it is not contiguous on the truncated page, and `index` is a dense
    # positional key that replace_toc and every exporter depend on.
    return [
        ChapterRef(
            index=i,
            title=a.get_text(strip=True),
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

    # What's left is text nodes separated by <br>. The &emsp;&emsp; indents need no
    # special handling: U+2003 is whitespace, so strip() removes them — and a blanket
    # replace would also eat the em-spaces the CMS uses *inside* a line.
    lines = [line.strip() for line in container.get_text("\n").split("\n") if line.strip()]

    # The <h1> is decomposed above, so this only fires if the CMS ever drops it and
    # repeats the title as plain prose. Drop at most one line: a duplicated heading is
    # cosmetic, while an eaten opening line is data loss the reader won't notice.
    if lines and _norm(lines[0]) == _norm(title):
        lines = lines[1:]

    if not lines:
        raise ScrapeError("Chapter content is empty", url)
    return "\n\n".join(lines)


# ------------------------------------------------------------------------ adapter


@register
class Sto9Adapter(SiteAdapter):
    name = "sto9"
    display_name = "思兔閱讀 (sto9.com)"
    # Host-anchored on purpose: a bare `/txt/\d+/\d+` would also match 69shuba's chapter
    # URLs, and whichever adapter imports first would silently steal them. The first
    # pattern covers both book forms — `matches()` uses re.search, and `/book/13908.html`
    # and `/book/13908/index.html` both contain `/book/13908`.
    url_patterns = [r"sto9\.com/book/\d+", r"sto9\.com/txt/\d+/\d+"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping
    # this would land Chinese in `translated` and mark chapters already-translated, so the
    # user's Vietnamese output would be Chinese.
    content_is_translated = False

    def __init__(self, client):
        super().__init__(client)
        # Per-instance, never module-level: the app is long-lived and the user switches
        # novels, so a shared cache would answer one novel's questions with another's page.
        self._detail: tuple[str, str] | None = None  # (bid, markup)

    def _detail_page(self, url: str) -> str:
        """The detail page, fetched at most once per adapter."""
        bid = book_id(url)
        if self._detail is not None and self._detail[0] == bid:
            return self._detail[1]
        markup = self.client.get_html(detail_url(url))
        self._detail = (bid, markup)
        return markup

    def fetch_metadata(self, url: str) -> NovelMeta:
        # Always the detail page, whichever form was pasted: the TOC page carries no
        # OpenGraph tags at all, and its <meta description> is SEO boilerplate about the
        # directory rather than the blurb.
        return parse_metadata(self._detail_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        page = self.client.get_html(read_url(url))
        total = stated_total(page)
        try:
            page_refs = parse_chapter_list(page, ORIGIN)
        except ScrapeError:
            page_refs = []
        # The page telling us it holds fewer entries than the novel has is the signal that
        # its list is the truncated one, and therefore unusable.
        truncated = total is not None and total > len(page_refs)

        try:
            ajax_refs = parse_chapter_list(self.client.get_html(chapterlist_url(url)), ORIGIN)
        except ScrapeError:
            ajax_refs = []

        if ajax_refs:
            if total is not None and len(ajax_refs) < total:
                # Still usable — it is contiguous from chapter 1, so a later re-scan just
                # extends it — but never silently.
                self._status(
                    f"⚠️ sto9 chỉ trả về {len(ajax_refs)}/{total} chương. "
                    "Quét lại sau để lấy đủ."
                )
            return ajax_refs

        if truncated:
            raise ScrapeError(
                f"Không lấy được danh sách chương đầy đủ của sto9 "
                f"(trang mục lục chỉ hiện {len(page_refs)}/{total} chương). Thử quét lại sau.",
                chapterlist_url(url),
            )

        if page_refs:
            # A genuinely short novel has no LoadMore button, so its page list really is
            # complete. This is the only branch where falling back to the page is safe.
            self._status(f"Lấy danh sách chương từ trang mục lục ({len(page_refs)} chương).")
            return page_refs

        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    def fetch_chapter(self, ref: ChapterRef) -> str:
        return parse_chapter(self.client.get_html(ref.url), ref.title, ref.url)

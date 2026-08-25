"""Adapter for 台灣小說網 (twkan.com).

Detail page:  https://twkan.com/book/<id>.html          — metadata, as OpenGraph tags
Chapter list: https://twkan.com/book/<id>/index.html    — the canonical "read" URL
Chapter page: https://twkan.com/txt/<id>/<cid>          — **no .html suffix**
Full TOC:     https://twkan.com/ajax_novels/chapterlist/<id>.html

**Every page comes through a browser.** twkan sits behind a Cloudflare challenge decided
on TLS/browser *fingerprint*: a plain `curl` with our own User-Agent gets 403
(`cf-mitigated: challenge`), and so does a full hand-forged real-Chrome header set
(accept-language, sec-ch-ua, sec-fetch-*, the lot) — measured, both. There is no header
trick and no token to replay, so `HttpClient` cannot read this site at all and
`cf_browser.BrowserSession` is the only path. See that module for the measured details
and changes/015-69SHUBA-SOURCE for how the same conclusion was reached for 69shuba.
**Do not parallelise twkan.** N concurrent sessions is N Chromes, and hammering a
CF-protected host is the fastest route to an IP block — which would kill the browser
path too, and it is the only path this site has.

**A challenge mid-download is normal here and is NOT a failure.** At the default 1.5 s
delay twkan answers roughly every 11th chapter with an HTTP 429 plus an interstitial, and
it clears itself in ~3 s if the page is simply left alone. `BrowserSession` waits that out
(`cf_browser._settle_challenge`) and only raises if it is still there at the deadline —
measured: 30/30 consecutive chapters succeed, with two ~3 s pauses. Before that existed
this adapter aborted the whole download at chapter 11. Do not "fix" the pauses by raising
the delay: they are self-healing and cost ~50 s across a 188-chapter novel, which is less
than slowing every request down would.

**The chapter-list page is truncated and does not say so in its markup.** It ships the
first ~15 and last ~20 chapters with a `LoadMore()` button between them, so a 188-chapter
novel renders as 36 entries that look perfectly normal — and those 36 are not a prefix:
the `data-num` sequence jumps 15 -> 168. Since `ChapterRef.index` is dense and positional,
saving that list would file chapter 168's text under chapter 16's row, and `replace_toc`
preserves content across re-scans, so a later successful scan would rewrite the title and
URL while leaving the wrong body in place. Hence `fetch_chapter_list` takes the complete
list from the ajax fragment `LoadMore()` itself requests, and **raises rather than falling
back** to a list the page has already told us is incomplete. Identical trap to sto9 — see
changes/062-STO9-SCRAPER.

**A phantom chapter that sto9 does not have.** The TOC page wraps its hidden bookmark
widget in an `li[data-num]`, inside the SAME `<ul>` as the chapters:

    <li data-num="7"><a href="#" id="bookcase" style="display:none"></a></li>

sto9's `li[data-num] a[href]` selector matches it, and it breaks two things at once:
  * the chapter list — a titleless ref at index 0, shifting every real chapter by one;
  * `stated_total` — `max(data-num)` reads 7, so a *complete* 4-chapter novel looks
    truncated and gets refused, quoting a total that appears nowhere on the site.
Both are fixed by one rule, `_CHAPTER_HREF_RE`: an `<li>` counts only if it holds a real
`/txt/<id>/<cid>` anchor. That regex's trailing `\\d+` also rejects `/txt/<id>/end.html`,
the last chapter's 下一章 sentinel.

**The 字數 numbers are NOT a paywall — do not build a completeness guard on them.** The
site's declared 字數 is ~1.6x the prose a chapter actually renders (ch.1: declared 3370,
prose 2014), the rendered lengths cluster near ~2000 regardless of the declared figure,
and chapter pages do load `crypto-js` and `/js/newread.js`. Every one of those is a
classic truncation signature and all of them are red herrings here. Measured: the **raw
pre-JS server response and the post-JS rendered DOM give a byte-identical `#txtcontent0`**
(2014 chars / 87 lines), so nothing is lazy-loaded or decrypted; and the `inner_html` of
`#txtcontent0` is **3352** chars against a declared **3370** — 字數 counts the stored
*markup*, `<br/>` tags and `&emsp;` indents included, not the prose. Chapters are served
complete. A length heuristic here would reject every chapter on the site; feature 061
shipped exactly that false positive on bookqq and had to be reverted (commit 1f9eb6f).

Same PHP novel CMS as sto9 and 69shuba — `div.txtnav`, `<br>`-separated body, OpenGraph
metadata — so `sto9.py` and `shuba69.py` are both reference reading: this module takes its
*shape* from the first and its *transport* from the second.

Both the detail page and the TOC page carry an inline `var bookinfo = {...}`. It is
single-quoted JS rather than JSON and holds nothing the OpenGraph tags don't, so it is
deliberately unused — it is not an oversight to "fix".

**No placeholder-cover check, deliberately.** `sto9.py` blanks a known "no cover" JPEG so
it cannot render into the EPUB and the video thumbnail as a *broken* cover. No such form
has been observed on twkan (no `nocover`/`noimage` string appears in any captured page),
and inventing a filename would be either dead code or a rule that eats a real cover. If a
coverless twkan novel ever turns up, the fix is one suffix constant — `sto9.py`'s shape.

Content is Traditional Chinese (the site's 繁/簡 toggle is client-side only), so
`source_lang` is "zh". Encoding needs no handling on this path: `page.content()` returns
an already-decoded `str`, so the site's `charset=utf8` declaration never matters.

The module splits fetching from parsing on purpose: `_get_html` is the only method that
touches a browser, and everything below the `parse_*` line is pure and takes markup — so
the whole parsing surface, both traps included, is tested against saved fixtures with no
Chrome anywhere in CI.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSession, BrowserSessionError
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

# Fixed, not taken from the pasted URL the way shuba69 does for its mirror. No twkan
# mirror is known, and pinning the origin is what guarantees that http://, https://www.
# and the bare host all canonicalise to ONE string — which is the whole point of
# `read_url`. If a mirror ever appears, widening this is a deliberate edit, not an
# accident (and it would mean revisiting `read_url` too).
ORIGIN = "https://twkan.com"

# Anchors on /book/ or /txt/ and takes the FIRST \d+ after it, so it works unchanged on
# twkan's suffix-less chapter URLs. Do NOT "tighten" this to /txt/(\d+)/\d+\.html — that
# is the sto9 shape and it matches nothing here.
_ID_RE = re.compile(r"/(?:book|txt)/(\d+)")
# "點擊展開全部188章節目錄" -> 188. The button states the true total even though the page
# below it does not contain that many entries.
_TOTAL_RE = re.compile(r"(\d+)\s*章")
# What makes an <li> a chapter. The trailing \d+ is load-bearing twice: it rejects the
# `href="#"` bookmark widget, and it rejects /txt/<id>/end.html, the last chapter's
# 下一章 sentinel. A substring test (`href*="/txt/"`) would admit both.
_CHAPTER_HREF_RE = re.compile(r"/txt/\d+/\d+")
_COLONS = ":："
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# `li[data-num]` rather than a bare `a[href]` keeps the breadcrumb and any wrapper the CMS
# adds out; the `_CHAPTER_HREF_RE` filter in `_chapter_anchors` keeps the bookmark widget
# out. They guard different things — neither alone is enough.
SEL_TOC_LINKS = "li[data-num] a[href]"
SEL_LOADMORE = "#loadmore"
# div.txtnav, not #txtcontent0: both give byte-identical output today (measured), but
# `div.txtnav` is what shuba69 and sto9 already use for this same CMS, and the numeric
# suffix on #txtcontent0 is CMS-speak for "there may one day be a txtcontent1" — which
# div.txtnav would pick up for free and #txtcontent0 would silently drop.
SEL_CONTENT = "div.txtnav"
# Chrome living *inside* .txtnav, named rather than blanket-decomposing every <div> the
# way xbanxia does: the day the CMS wraps a paragraph in one, a blanket rule eats it
# silently. div.txtad and div.txtcenter are the notable pair — measured, one of each sits
# INSIDE #txtcontent0, mid-prose, not merely around it.
SEL_CHROME = "h1, div.txtinfo, div.txtad, div.txtcenter, script, style, ins"

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Whitespace-normalised form, used only for comparing titles — never for storing.

    Collapsing matters here: the site spaces its own titles inconsistently ("第1 章",
    "第4章", "第 188 章"), so an exact comparison would miss a real duplicated heading
    whenever the <h1> and the TOC anchor happened to disagree.
    """
    return _WS_RE.sub(" ", text).strip()


def _clean_description(text: str) -> str:
    """og:description carries literal "<br />" text, not markup — unescape to newlines.

    It arrives as a meta *attribute value*, so BeautifulSoup never sees those tags and
    can't strip them for us. sto9 needs no such helper; twkan does (measured).
    """
    text = _BR_RE.sub("\n", text)
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


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
    carrying OpenGraph tags (the TOC page has none at all)."""
    return f"{ORIGIN}/book/{book_id(url)}.html"


def chapterlist_url(url: str) -> str:
    """The fragment `LoadMore()` fetches: the complete TOC, unpaginated."""
    return f"{ORIGIN}/ajax_novels/chapterlist/{book_id(url)}.html"


# ------------------------------------------------------------------------ parsing


def _chapter_anchors(soup: BeautifulSoup) -> list:
    """The `<a>`s that are really chapters, in document order.

    Not a bare `li[data-num] a[href]` (sto9.py's selector): twkan's TOC page wraps a
    hidden bookmark widget — `<a href="#" id="bookcase">` — in an `li[data-num]`, inside
    the SAME <ul> as the chapters. A bare href test picks it up as a phantom chapter at
    index 0 and shifts every real chapter by one.
    """
    return [
        a
        for a in soup.select(SEL_TOC_LINKS)
        if _CHAPTER_HREF_RE.search(urljoin(ORIGIN, a["href"]))
    ]


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

    return NovelMeta(
        url=read_url(url),
        site=site,
        title=title,
        author=author,
        description=_clean_description(og("og:description")),
        # Stored as-is: no placeholder form has been observed on this site, and inventing
        # a filename to blank would be worse than the documented gap. See the docstring.
        cover_url=og("og:image"),
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

    The `data-num` signal walks the SAME chapter filter `_chapter_anchors` uses. Without
    it the hidden bookmark widget's `data-num="7"` becomes the maximum on any novel with
    fewer than 7 chapters, and a complete short novel would be refused as truncated.
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
        and any(_CHAPTER_HREF_RE.search(urljoin(ORIGIN, a["href"])) for a in li.select("a[href]"))
    ]
    if numbers:
        signals.append(max(numbers))

    return max(signals) if signals else None


def parse_chapter_list(markup: str, base_url: str) -> list[ChapterRef]:
    """Read a TOC, from either the ajax fragment or the page. Document order is reading
    order in both, and neither is paginated."""
    soup = BeautifulSoup(markup, "lxml")
    links = _chapter_anchors(soup)
    if not links:
        raise ScrapeError("Chapter list not found — page layout may have changed", base_url)
    # `index` comes from enumerate, never from `data-num`: that is the site's display
    # number, it is not contiguous on the truncated page, and `index` is a dense
    # positional key that replace_toc and every exporter depend on.
    #
    # The title is the anchor's TEXT, never its `title=` attribute: the attribute splices
    # the chapter name together with "字數：NNNN 更新時間：…", and 字數 is precisely the
    # number this module's docstring exists to stop anyone trusting.
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
class TwkanAdapter(SiteAdapter):
    """台灣小說網. Fetches through a browser; everything above this line does the parsing."""

    name = "twkan"
    display_name = "台灣小說網 (twkan.com)"
    # Host-anchored on purpose: a bare `/txt/\d+/\d+` would also match sto9's and
    # 69shuba's chapter URLs, and `ADAPTERS` is ordered by import — first match wins, so
    # the theft would be silent and order-dependent. The first pattern covers both book
    # forms (`matches()` uses re.search, and `/book/114283.html` and
    # `/book/114283/index.html` both contain `/book/114283`).
    url_patterns = [r"twkan\.com/book/\d+", r"twkan\.com/txt/\d+/\d+"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping
    # this would land Chinese in `translated` and mark chapters already-translated, so the
    # user's Vietnamese output would be Chinese.
    content_is_translated = False

    def __init__(self, client, *, headless: bool = False):
        super().__init__(client)
        # Headless is fingerprinted by Cloudflare and does not clear the challenge
        # (measured — see cf_browser). The flag exists in case that ever changes.
        self._headless = headless
        self._session: BrowserSession | None = None
        # Per-instance, never module-level: the app is long-lived and the user switches
        # novels, so a shared cache would answer one novel's questions with another's page.
        self._detail: tuple[str, str] | None = None  # (bid, markup)

    # -- fetching: the only part that touches a browser ---------------------------

    def _get_html(self, url: str) -> str:
        """The single seam between this adapter and the browser."""
        if self._session is None:
            # A Chrome window is about to appear on the user's screen — say why before
            # it does, or it reads as the app misbehaving.
            self._status(
                "🌐 Đang mở trình duyệt để vượt kiểm tra Cloudflare của twkan — "
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
                "Cần trình duyệt để đọc twkan (trang này có kiểm tra Cloudflare). "
                "Cài Google Chrome, hoặc chạy:  pip install 'noveltrans[browser]' "
                "&& playwright install chromium",
                url,
            ) from exc
        except BrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc được trang twkan — trình duyệt bị đóng hoặc không vượt "
                f"được kiểm tra Cloudflare. Thử tải lại. ({exc})",
                url,
            ) from exc

    def close(self) -> None:
        """Release the browser. Idempotent; never raises."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def _detail_page(self, url: str) -> str:
        """The detail page, fetched at most once per adapter."""
        bid = book_id(url)
        if self._detail is not None and self._detail[0] == bid:
            return self._detail[1]
        markup = self._get_html(detail_url(url))
        self._detail = (bid, markup)
        return markup

    # -- SiteAdapter ---------------------------------------------------------------

    def fetch_metadata(self, url: str) -> NovelMeta:
        # Always the detail page, whichever form was pasted: the TOC page carries no
        # OpenGraph tags at all, and neither do chapter pages.
        return parse_metadata(self._detail_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        page = self._get_html(read_url(url))
        total = stated_total(page)
        try:
            page_refs = parse_chapter_list(page, ORIGIN)
        except ScrapeError:
            page_refs = []
        # The page telling us it holds fewer entries than the novel has is the signal that
        # its list is the truncated one, and therefore unusable.
        truncated = total is not None and total > len(page_refs)

        try:
            ajax_refs = parse_chapter_list(self._get_html(chapterlist_url(url)), ORIGIN)
        except ScrapeError:
            ajax_refs = []

        if ajax_refs:
            if total is not None and len(ajax_refs) < total:
                # Still usable — it is contiguous from chapter 1, so a later re-scan just
                # extends it — but never silently.
                self._status(
                    f"⚠️ twkan chỉ trả về {len(ajax_refs)}/{total} chương. "
                    "Quét lại sau để lấy đủ."
                )
            return ajax_refs

        if truncated:
            raise ScrapeError(
                f"Không lấy được danh sách chương đầy đủ của twkan "
                f"(trang mục lục chỉ hiện {len(page_refs)}/{total} chương). Thử quét lại sau.",
                chapterlist_url(url),
            )

        if page_refs:
            # A genuinely short novel has no LoadMore button, so its page list really is
            # complete. This is the only branch where falling back to the page is safe —
            # and it is only trustworthy because `stated_total` filters the phantom <li>.
            self._status(f"Lấy danh sách chương từ trang mục lục ({len(page_refs)} chương).")
            return page_refs

        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    def fetch_chapter(self, ref: ChapterRef) -> str:
        return parse_chapter(self._get_html(ref.url), ref.title, ref.url)

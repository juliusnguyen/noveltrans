"""Adapter for 笔趣阁 (bqg5.com).

Landing page: https://www.bqg5.com/<cat>_<book>/          — metadata AND the full TOC
Chapter page: https://www.bqg5.com/<cat>_<book>/<cid>.html

**The site is not mobile-only — it runs a User-Agent allowlist.** This is the whole
reason the feature exists: the user could open it on a phone and not on a desktop.
Measured, both hosts, same minute:

    UA=base.USER_AGENT (Chrome/126 on Mac) -> HTTP 404, 548 bytes   (www AND m)
    UA=iPhone Safari                       -> HTTP 200              (www AND m)

So there is no Cloudflare, no JS challenge and no token to replay — `HttpClient` reads
this site perfectly once `MOBILE_UA` is on the request. Do NOT reach for `cf_browser`
here the way twkan has to; a Chrome per download would be pure cost. The desktop 404 is
also why `_get_html` is the single network seam: every path needs the header, and losing
it breaks metadata, TOC and chapters at once with a 404 that says nothing about UAs.

**Only the `www` layout is read, whichever host the user pasted.** With a mobile UA the
`www` host serves the classic desktop template: the entire chapter list in ONE request
(`#list dl`) and each chapter whole in one page (`#content`). A scan therefore costs
exactly one request, since metadata and the TOC come off the same page — no other adapter
in this repo manages that. The `m.` layout serves the same prose at the same chapter ids
and is strictly worse in four ways:

  * its TOC is paginated 20-per-page (`index_N.html`), so 22 requests for a 439-chapter
    novel instead of one;
  * its chapters are split across `<cid>_N.html` sub-pages chained by `a#pt_next`, whose
    link on the LAST sub-page points at the next *chapter* — a chain that silently runs
    off the end of a chapter is exactly the misfiling `replace_toc` makes permanent;
  * every sub-page repeats a `第N章 …第(1/2)页` header and a `…,点击下一页继续阅读。`
    footer, plus a `『加入书签，方便阅读』` bookmark anchor and a `p.synopsisAd` slot;
  * none of it would ever be exercised, so it would be untested code on the data path.
`read_url` folds every paste form to `www` for that reason. See its docstring for why
`NovelMeta.url` must not echo the pasted string.

**gbk with no charset header.** The response carries `content-type: text/html` with no
charset and declares `charset=gbk` only in a `<meta>`, so requests defaults to
ISO-8859-1 and `HttpClient.get_html`'s `apparent_encoding` fixup is what actually decodes
this site — measured: it picks `gbk` and the result is byte-identical to a manual
`gb18030` decode, 0 replacement characters across the TOC and two chapters. That is why
there is no encoding code here. Do not "fix" it by assigning `response.encoding`; that
would need a new `HttpClient` hook for one site, and the existing path already works.

**The TOC carries a duplicate block whose length varies.** `#list dl` is FLAT — two `<dt>`
section headings and 448 `<dd>` as siblings. The first section is 最新章节 and its `<dd>`s
are copies of the NEWEST chapters: **9** of them on the reference novel, where an earlier
probe of the same page saw 5. So the real 正文 block must be located structurally and a
"drop the first N" rule is wrong by construction. `_chapter_dds` explains what taking
every `<dd>` instead would cost.

**There is deliberately no `stated_total` guard.** sto9 and twkan both refuse a chapter
list that falls short of a total the page states, and transplanting that here would refuse
every scan on the site: the real block holds **439** `<dd>` whose last title is
**第438章**, and `og:novel:latest_chapter_name` agrees with the title, not the count. The
numbering is not authoritative for another reason too — 37 of those 439 titles do not even
match `第\\d+章`, because the site spaces them inconsistently (`第 269章`, `第270 章`). The
`<dd>` order IS the reading order; `index` comes from position and nothing else. The
cross-check that IS worth having is `latest_chapter_url` — see `fetch_chapter_list`.

`#info` contributes a 449th chapter-shaped anchor and `#listtj` recommends other books, so
a flat `a[href]` scrape overcounts this page three separate ways.

**The description is escaped text, not markup.** Both `#intro` and `og:description` carry
literal two-character `\\n` and `\\"` sequences — 23 backslash-n and zero real newlines,
measured — plus `\\xa0` runs used as mid-line indents. BeautifulSoup cannot help with any
of it; `_clean_description` unescapes them.

`translators/ads.py` (feature 069) is the net if this site ever watermarks a chapter with
a promo line: that predicate is shape-based and generically domain-anchored, so it needs
no bqg5 entry, and adding one would be the literal blocklist that module exists to avoid.
`SEL_CHROME` here is structural insurance only — measured, `#content` holds `<br>` and
nothing else: 0 `<script>`, 0 `<div>`, 0 `<a>`.

Same PHP novel CMS family as sto9, and the placeholder cover is *evidenced* rather than
guessed for once: the real `<img>` ships `data-evt="src='/images/nocover.jpg'"`.

Everything below the `parsing` line is pure: markup in, values out, no network.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

# Pinned, never taken from the pasted URL — see `read_url`.
ORIGIN = "https://www.bqg5.com"

# The site 404s our default desktop UA on BOTH hosts (measured). A UA allowlist, not a
# mobile site: no viewport, cookie, token or browser is involved.
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
_HEADERS = {"User-Agent": MOBILE_UA}

# Ids are <category>_<book>, e.g. "4_4217" — never a bare number. Host-anchored so this
# cannot claim a neighbour's URLs; `www.`, `m.` and the bare host all contain the same
# "bqg5.com/4_4217", which is what makes one pattern enough.
_ID_RE = re.compile(r"bqg5\.com/(\d+_\d+)")
# What makes an anchor a chapter. The trailing `\d+\.html` is load-bearing twice over: it
# rejects the mobile TOC pages (`/4_4217/index_2.html`) and the mobile sub-pages
# (`/4_4217/2029247_2.html`), neither of which is a chapter of its own.
_CHAPTER_HREF_RE = re.compile(r"/(\d+_\d+)/(\d+)\.html")

_COLONS = ":："
# \xa0 is this site's indent character and its label padding ("作\xa0\xa0者：") — a plain
# `\s`-class rule would be fine, but naming the three explicitly is what documents that
# the NBSP is deliberate rather than stray.
_WS_RE = re.compile("[ \\t\\u00a0\\u3000]+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
# Literal, two-character escapes sitting in a text node. See the module docstring.
_LITERAL_NL_RE = re.compile(r"\\+n")
_LITERAL_QUOTE_RE = re.compile(r'\\+"')

SEL_LIST = "#list dl"
SEL_INFO = "#info"
SEL_INTRO = "#intro"
SEL_COVER = "#fmimg img[src]"
SEL_CONTENT = "#content"
# Named per element, never a blanket `div` decompose: the day the CMS wraps a paragraph in
# one, a blanket rule eats it silently (twkan.py records the same reasoning). Measured,
# none of these currently appear inside #content — this is insurance, not a live rule.
SEL_CHROME = "script, style, ins, iframe, div.bottem1, div.bottem2, div.bookname, h1, a"

# A coverless novel gets a grey "no cover" JPEG. Stored, it renders into the EPUB and the
# video thumbnail as a *broken* cover rather than an absent one, and everything downstream
# already handles "". Matched by path suffix so a CDN host or a cache-buster query cannot
# defeat it. Same string as sto9 — same CMS family.
_PLACEHOLDER_COVER = "/images/nocover.jpg"

# The heading of the real chapter section, and the heading of the decoy one whose
# `<dd>`s duplicate the newest chapters. Both are needed: `_BODY_SECTION` finds the list,
# and `_LATEST_SECTION` is what stops the fallback below from ever returning the decoy.
_BODY_SECTION = "正文"
_LATEST_SECTION = "最新章节"


def _norm(text: str) -> str:
    """Whitespace-normalised form, used only for comparing titles — never for storing."""
    return _WS_RE.sub(" ", text or "").strip()


# --------------------------------------------------------------------------- urls


def book_id(url: str) -> str:
    """The `<category>_<book>` novel id, from any paste form."""
    match = _ID_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def read_url(url: str) -> str:
    """The canonical URL for a novel — the site's own `og:novel:read_url` shape.

    Always the `www` host, whichever the user pasted, and never the pasted string itself.
    Two reasons, both load-bearing:

    * `Library.find_by_url` is exact string equality, so echoing would let
      `m.bqg5.com/4_4217/`, `www.bqg5.com/4_4217/` and a chapter URL become three projects
      for one novel, each with its own translation progress and video settings. `ScanWorker`
      looks a project up by BOTH the pasted URL and `meta.url`, so a re-scan pasted in the
      `m.` form still finds the project this creates.
    * `NovelMeta.source_host` strips only a `www.` prefix, so storing the `m.` host would
      print "m.bqg5.com" in the novel tab bar and the picker.
    """
    return f"{ORIGIN}/{book_id(url)}/"


def chapter_url(bid: str, cid: str) -> str:
    return f"{ORIGIN}/{bid}/{cid}.html"


# ------------------------------------------------------------------------ parsing


def _clean_description(text: str) -> str:
    """Unescape the site's literal `\\n` / `\\"` sequences and drop its NBSP indents.

    Order matters: the escapes become real characters first, so the whitespace collapse
    below can then flatten the `\\xa0` indent runs that sit *inside* a line — `strip()`
    alone only reaches the ends.
    """
    text = _LITERAL_QUOTE_RE.sub('"', text or "")
    text = _LITERAL_NL_RE.sub("\n", text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the landing page's OpenGraph block, falling back to the visible book box."""
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        # `property=` is the standard and what this site uses; `name=` is accepted too so
        # that a CMS update switching to it cannot break us (timotxt ships the other way).
        el = soup.select_one(f"meta[property='{prop}']") or soup.select_one(f"meta[name='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    info = soup.select_one(SEL_INFO)

    title = og("og:novel:book_name") or og("og:title")
    if not title:
        heading = info.select_one("h1") if info else None
        title = _norm(heading.get_text()) if heading else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = og("og:novel:author")
    if not author and info:
        for row in info.select("p"):
            # Split on the colon rather than `startswith("作者")`: the label is padded
            # with NBSPs ("作\xa0\xa0者："), so the naive prefix test never fires.
            text = _norm(row.get_text(" "))
            if text.startswith("作") and len(text) < 40:
                author = re.split(f"[{_COLONS}]", text, maxsplit=1)[-1].strip()
                break

    # `#intro` over `og:description`: they are the same blurb, but the meta tag carries a
    # cosmetic trailing ellipsis. Both need the same unescaping either way.
    intro = soup.select_one(SEL_INTRO)
    description = _clean_description(intro.get_text() if intro else og("og:description"))

    cover = og("og:image")
    if not cover:
        img = soup.select_one(SEL_COVER)
        # Relative on this page (`/files/article/image/...`), absolute in og:image.
        cover = urljoin(ORIGIN, (img.get("src") or "").strip()) if img else ""
    if cover.split("?")[0].endswith(_PLACEHOLDER_COVER):
        cover = ""

    return NovelMeta(
        url=read_url(url),
        site=site,
        title=title,
        author=author,
        description=description,
        cover_url=cover,
        # Simplified Chinese. Written explicitly: fetch_chapter returns the original and
        # tab 2 translates it.
        source_lang="zh",
    )


def latest_chapter_url(markup: str) -> str:
    """The newest chapter's URL per the page's own `#info` 最新章节 link. "" if absent.

    A cross-check only, never a source of chapters — that single anchor is one of the
    three ways a flat `a[href]` scrape overcounts this page.
    """
    soup = BeautifulSoup(markup, "lxml")
    info = soup.select_one(SEL_INFO)
    for anchor in info.select("a[href]") if info else []:
        href = anchor.get("href") or ""
        if _CHAPTER_HREF_RE.search(href):
            return urljoin(ORIGIN, href)
    return ""


def _chapter_dds(dl) -> list:
    """The `<dd>`s of the 正文 section, in document order — never the 最新章节 block.

    `#list dl` is flat: `<dt>` headings and `<dd>` entries as siblings, no nesting. The
    first section is 最新章节 and its `<dd>`s duplicate the NEWEST chapters (9 of them on
    the reference novel, 5 on an earlier fetch of the same page — the count varies, so it
    can never be hardcoded).

    Preferred rule: the `<dt>` whose text contains 正文. Fallback: the last `<dt>` that is
    NOT the 最新章节 heading — which covers a novel whose only section is the real one
    (one `<dt>`, every `<dd>` real) and a CMS that renames 正文 without renaming the decoy.

    Excluding the decoy explicitly is the whole point, and a plain "last `<dt>`" rule is
    not good enough: a page whose ONLY heading is 最新章节 would hand back the newest
    chapters as if they were chapters 1-9.

    There is deliberately NO "if that fails, take every `<dd>`" branch either, for the
    same reason. `ChapterRef.index` is dense and positional while `replace_toc` preserves
    content across re-scans, so a decoy at index 0 would file chapter 431's body under
    chapter 1 forever, behind a title a later scan quietly corrected. Same trap as
    timotxt's backwards recent block and sto9's truncated page. Returning [] makes the
    caller raise, which is the safe direction.
    """
    children = [t for t in dl.children if getattr(t, "name", None)]
    headings = [i for i, t in enumerate(children) if t.name == "dt"]
    real = [i for i in headings if _LATEST_SECTION not in children[i].get_text()]
    start = next(
        (i for i in headings if _BODY_SECTION in children[i].get_text()),
        real[-1] if real else None,
    )
    if start is None:
        return []
    return [t for t in children[start + 1 :] if t.name == "dd"]


def parse_chapter_list(markup: str, url: str) -> list[ChapterRef]:
    """Every chapter of the 正文 section, in reading order."""
    soup = BeautifulSoup(markup, "lxml")
    dl = soup.select_one(SEL_LIST)
    anchors = (
        [
            anchor
            for dd in _chapter_dds(dl)
            for anchor in dd.select("a[href]")
            if _CHAPTER_HREF_RE.search(anchor.get("href") or "")
        ]
        if dl is not None
        else []
    )
    if not anchors:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    # `index` from enumerate, never from a number parsed out of the title: the reference
    # novel has 439 entries whose highest title number is 第438章, and 37 of them do not
    # match `第\d+章` at all because the site spaces them inconsistently. Titles are stored
    # verbatim, that spacing included — they are persisted, exported and read aloud, so a
    # tidy-up normaliser here would silently rewrite them on the next re-scan.
    return [
        ChapterRef(
            index=i,
            title=anchor.get_text(strip=True),
            url=urljoin(ORIGIN, anchor["href"]),
        )
        for i, anchor in enumerate(anchors)
    ]


def parse_chapter(markup: str, title: str, url: str) -> str:
    """The chapter body as plain text, paragraphs separated by blank lines."""
    soup = BeautifulSoup(markup, "lxml")
    container = soup.select_one(SEL_CONTENT)
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)

    for junk in container.select(SEL_CHROME):
        junk.decompose()

    # `<br>`-separated text nodes, not `<p>`s. `\xa0` is `str.isspace()` in Python 3, so
    # the strip below removes the four-NBSP indents on its own — do not add a blanket
    # `replace("\xa0", "")`, which would also close up the NBSPs used mid-line.
    lines = [line.strip() for line in container.get_text("\n").split("\n")]
    lines = [line for line in lines if line]

    # At most one, and only when it really is the title: the `<h1>` lives outside
    # `#content` so this normally never fires, but the site's own h1 carries a leading
    # space the TOC title does not — hence `_norm` on both sides rather than `==`.
    if lines and title and _norm(lines[0]) == _norm(title):
        lines = lines[1:]

    body = "\n\n".join(lines).strip()
    if not body:
        raise ScrapeError("Chapter content is empty", url)
    return body


@register
class Bqg5Adapter(SiteAdapter):
    name = "bqg5"
    display_name = "笔趣阁 (bqg5.com)"
    # One pattern covers www., m. and the bare host. The `\d+_\d+` id shape is what keeps
    # the category pages (/xuanhuanxiaoshuo/), /map/ and /paihangbang/ out, and the host
    # anchor is what stops it stealing a neighbour's chapter URLs — ADAPTERS is
    # first-match-wins by import order, so a bare pattern would be a silent theft.
    url_patterns = [r"bqg5\.com/\d+_\d+"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping
    # this would land Chinese in `translated` and mark chapters already-translated.
    content_is_translated = False

    def __init__(self, client):
        super().__init__(client)
        # Per-instance, never module-level: the app is long-lived and the user switches
        # novels, so a shared cache would answer one novel's questions with another's page.
        self._landing: tuple[str, str] | None = None  # (bid, markup)

    def _get_html(self, url: str) -> str:
        """The single seam between this adapter and the network.

        The mobile UA is a PER-REQUEST header, never a mutation of the shared session.
        Today one `HttpClient` is built per worker run and bound to one adapter, so
        mutating it would happen to be safe — but `HttpClient.set_cookies` already
        documents "scope one client to one site" as an *assumption*, and requests merges a
        per-request `headers=` over the session's for the same key. So this costs nothing,
        cannot leak into another site, and leaves `base.py` untouched — which is the whole
        blast radius of this adapter.
        """
        return self.client.get_html(url, headers=_HEADERS)

    def _landing_page(self, url: str) -> str:
        """The landing page, fetched once per novel — it holds the metadata AND the TOC."""
        bid = book_id(url)
        if self._landing is not None and self._landing[0] == bid:
            return self._landing[1]
        markup = self._get_html(read_url(url))
        self._landing = (bid, markup)
        return markup

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._landing_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        markup = self._landing_page(url)
        refs = parse_chapter_list(markup, read_url(url))

        # The page states its own newest chapter in `#info`, and that chapter belongs at
        # the END of the list. Seeing it at index 0 means the 最新章节 duplicates reached
        # the front — chapter 438's body would be filed under chapter 1 and `replace_toc`
        # would keep it there through every later scan. Cheap, and it needs no total.
        latest = latest_chapter_url(markup)
        if latest and len(refs) > 1 and refs[0].url == latest:
            raise ScrapeError(
                "Danh sách chương của bqg5 bị đảo ngược (chương mới nhất nằm ở đầu "
                "danh sách) — app không lưu danh sách sai. Thử quét lại sau.",
                read_url(url),
            )
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        return parse_chapter(self._get_html(ref.url), ref.title, ref.url)

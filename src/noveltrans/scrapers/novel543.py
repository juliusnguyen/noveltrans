"""Adapter for 小說543 / 稷下書院 (novel543.com).

Landing page:  https://www.novel543.com/<id>/                    — metadata only
Full index:    https://www.novel543.com/<id>/dir                 — the complete, ordered TOC
Chapter page:  https://www.novel543.com/<id>/<inner>_<n>.html    — n is the reading position
Sub-page:      https://www.novel543.com/<id>/<inner>_<n>_<k>.html — k = 2..M

**This is timotxt's CMS behind a Cloudflare challenge.** `div.chaplist ul.all`,
`#chapterWarp div.content`, `div.intro.is-hidden-mobile`, `h1.title`, `div.gadBlock`, the
unclassed trailing 溫馨提示 notice and the `i<N>.<host>/thumb/120x160/<date>/<id>.jpg` cover
CDN are all identical, so `timotxt.py` is the parsing reference and most of the shapes below
are ports rather than new designs. Three things differ, and each is why this module exists:
Cloudflare, an internal id in chapter URLs, and per-chapter pagination.

`twkan.py` is the transport reference — same `BrowserSession` lifetime, same two exception
translations, same wording — so the three Cloudflare adapters read alike.

**Every page comes through a browser.** Measured, same minute, every form: our own desktop
UA, an iPhone UA and a Googlebot UA all get HTTP 403 with `cf-mitigated: challenge` on
`www.novel543.com` AND on the bare host, and there is no `m.` host at all (NXDOMAIN). So the
bqg5 trick — a UA allowlist that `HttpClient` can satisfy with one header — does not apply
here, and there is no token to replay. `cf_browser.BrowserSession` cleared the gate on the
first navigation and served 9 distinct URLs across 3 sessions with no interstitial.
**Do not parallelise novel543.** N concurrent sessions is N Chromes, and hammering a
CF-protected host is the fastest route to an IP block — which would kill the browser path
too, and it is the only path this site has. That matters more here than on twkan because
this site costs roughly twice the navigations per chapter; see `fetch_chapter`.

**And it needs a slower delay than the app's default.** Unlike twkan, whose periodic 429 is
self-healing at 1.5 s, novel543 answers a sustained walk at that speed with challenges that
do NOT clear inside `cf_browser`'s settle deadline — measured, 14 consecutive chapter pages:
2 hard failures at 1.5 s versus 0 at 3.0 s. `_MIN_DELAY_SECONDS` is therefore a floor under
whatever the user configured, and the numbers behind it are recorded there. The lever for a
site that gets stricter still is that constant, **not** a shorter `_CHALLENGE_WAIT_SECONDS`.

**`og:novel:latest_chapter_url` is a 404 — never read it.** The detail page advertises
`/<id>/439.html` while the real address of that chapter is `/<id>/8096_439.html`; fetching
the advertised one returns the site's 404 page (`<title>/(ㄒoㄒ)/~~</title>`). This is a live
trap rather than a hypothetical, because **`timotxt.stated_total` reads exactly that tag**
and `bqg5.fetch_chapter_list` fetches it as a list-orientation cross-check. Transplanting
either — the obvious "harmonise the siblings" edit — breaks this site. `og:novel:latest_
chapter_name` is no better: it is a chapter TITLE (第438章) against 439 real entries, because
`<n>` is positional and the 章 numbering drifts, so any equality guard built on it refuses
every scan on the site. The one number here that can be trusted is `章節： N` in
`section.info` on `/dir`, which matched the anchor count exactly (439/439).

**`/dir` carries a duplicate 最新章節 block, first in document order and backwards.** 451
chapter-shaped anchors for 439 chapters: 12 copies of the newest chapters ahead of the real
ascending `ul.all`. Its length varies — bqg5's equivalent block was 9 on one probe and 5 on
an earlier one — so a "drop the first N" rule is wrong by construction, and fixing the count
without fixing the order would not save it either. The blocks are distinguished only by the
`all` class, which is why the container is selected rather than the anchors de-duplicated,
and why the fallback path sorts by position rather than trusting document order.

**Chapters are paginated, and only SOME of them are.** The chapter `<h1>` carries a `(N/M)`
suffix when and only when the chapter is split — measured: chapters 1, 3, 5, 50, 399 and 438
are `(1/2)`, chapters 120 and 301 carry no suffix and are whole. An adapter that assumes one
page loses half of most chapters *silently*; one that assumes two 404s or duplicates on the
rest. So `page_count` reads M off the page and `fetch_chapter` builds `_2 … _M` from the
stem the TOC gave us. **The 下一章 link is never followed**: on the last sub-page it points at
the next *chapter*, and on an unpaginated chapter it can point at the page itself (measured
on `8096_301.html`). A chain that silently runs off the end of a chapter is exactly the
misfiling `replace_toc` makes permanent — the same trap `bqg5.py` avoids by switching hosts,
which is an escape this site does not offer.

**The chapter URL's `<inner>` is a per-book internal id** (`8096` on the reference novel)
that appears only in TOC and chapter hrefs and is NOT derivable from the book id. timotxt's
`chapter_url(bid, n)` therefore has no counterpart here and must not be invented: the TOC is
the only source of chapter addresses. The detail page's 開始閱讀 button does reveal the id, so
one *could* synthesise `<inner>_1 … <inner>_N` from `章節： N` without reading `/dir` at all —
rejected, because it invents URLs the site never offered, carries no titles, and would
produce a plausible-looking list of 404s the moment the numbering is not dense.

**`meta[name=description]` is truncated — use `div.intro`.** The meta tag is SEO copy
(`…無彈窗最新章節由<author>所著，《<title>》…在線閱讀: `) followed by the blurb **cut off with an
ellipsis**. It is not the blurb with padding, it is a *truncated* blurb with padding, so no
prefix-stripping recovers the tail. `div.intro` carries the full text, clean.

There is nothing to un-escape and nothing to un-indent on this site: measured across five
chapter pages, zero `<br>`, zero `　`, zero `\xa0`, zero `&emsp;`, and no literal `\n`
sequences in the description. Do not copy sto9's `&emsp;` note or bqg5's `_clean_description`
— a comment about something this site does not do is a trap for the next reader. Nothing is
obfuscated either, which matters because timotxt's Hangul machinery will be sitting one file
away: an empty body here raises a plain `ScrapeError`, never `ObfuscatedContentError`, whose
GUI meaning is "offer the user the residual-character repair" and which cannot help.

No placeholder-cover check, deliberately, on twkan's argument: no `nocover`/`noimage` string
appears in any captured page, and inventing a filename would be either dead code or a rule
that eats a real cover. If a coverless novel turns up, the fix is one suffix constant.

Content is Traditional Chinese, so `source_lang` is "zh". Encoding needs no handling on this
path: `page.content()` returns an already-decoded `str`.

Everything above the `adapter` line is pure: markup in, values out, no network. `_get_html`
is the only method that touches a browser, which is what lets all three traps above — the
pagination logic included — be tested against saved fixtures with no Chrome anywhere in CI.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSession, BrowserSessionError
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

# Pinned rather than taken from the pasted URL: the site answers on both the bare host and
# www, `og:url` uses www, and echoing whichever the user happened to paste would make one
# novel several projects. Same reasoning as timotxt.py:97 and twkan.py:102.
ORIGIN = "https://www.novel543.com"

_ID_RE = re.compile(r"novel543\.com/(\d+)")

# A chapter href: /<bid>/<inner>_<position>[_<subpage>].html
#   group 1 bid       — filters out anchors pointing at ANOTHER book
#   group 2 inner     — the per-book internal id (8096); NOT derivable from bid
#   group 3 position  — 1..N, the reading position (NOT the 章 number)
#   group 4 subpage   — present only on a sub-page URL, which is never a TOC entry
_CHAPTER_HREF_RE = re.compile(r"/(\d+)/(\d+)_(\d+)(?:_(\d+))?\.html")

# "章節： 439" in section.info. The ONE stated total on this site that can be trusted — see
# the module docstring for why neither og:novel:latest_chapter_* tag can be.
_STATED_TOTAL_RE = re.compile(r"章節\s*[：:]\s*(\d+)")

# The "(1/2)" suffix on a paginated chapter's <h1>. Anchored at the END so a bracketed
# fraction inside a chapter title cannot be mistaken for it. Full-width parens are accepted
# because this is a Traditional-Chinese site and the CMS is inconsistent about them.
_PAGE_SUFFIX_RE = re.compile(r"[(（]\s*(\d+)\s*/\s*(\d+)\s*[)）]\s*$")

# A floor under the app's configured delay, because the default (1.5 s) is measurably too
# fast for THIS site. Walking 14 consecutive chapter pages, twice, same session shape:
#
#     delay=1.5s -> 12/14 ok, 2 hard failures, 3 challenge stalls (one of 28.7 s, past
#                   cf_browser's 20 s settle deadline — which is what turns it into a failure)
#     delay=3.0s -> 14/14 ok, 0 failures, 1 stall of 6.5 s
#
# twkan self-heals at 1.5 s; novel543 does not, and it also costs ~2 navigations per chapter,
# so a 439-chapter novel at the default would strand a double-digit percentage of chapters as
# errors. `max()`, never assignment: a user who has deliberately set a SLOWER delay keeps it.
_MIN_DELAY_SECONDS = 3.0

# A refusal ceiling, not a guess at the real maximum. M is 2 on every paginated chapter
# measured; if a parse ever yields something absurd, walking thousands of sub-pages through
# a Cloudflare-protected host is the worst possible failure. Raise instead.
_MAX_SUB_PAGES = 20

_COLONS = ":："
_WS_RE = re.compile(r"[ \t　]+")

# The real index. NOT the full `.flex.one.two-700.three-900.all` chain — that is a CSS grid
# framework's breakpoint vocabulary, incidental and exactly what a theme refresh rewrites.
# `chaplist` and `all` are the only semantic tokens. Identical to timotxt.py:112.
SEL_ALL_CHAPTERS = "div.chaplist ul.all a[href]"
SEL_DETAIL = "section#detail"
SEL_INFO = "section.info"
SEL_INTRO = "div.intro"
SEL_COVER = "div.cover img[src]"
# Anchored under #chapterWarp because `div.content` alone is far too generic to trust.
SEL_CONTENT = "#chapterWarp div.content"
SEL_HEADING = "#chapterWarp h1"
# Structural insurance only — the `> p` rule in `parse_chapter` already excludes all of
# this. Named per element, never a blanket <div> decompose (twkan.py:135 records why that
# eats prose).
SEL_CHROME = "div.adBlock, div.gadBlock, ins, script, style, iframe"


def _norm(text: str) -> str:
    """Collapse the site's whitespace without touching its characters."""
    return _WS_RE.sub(" ", (text or "").replace("\xa0", " ")).strip()


def book_id(url: str) -> str:
    """The numeric novel id, from any paste form."""
    match = _ID_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def read_url(url: str) -> str:
    """The canonical URL for a novel — what `NovelMeta.url` is set to.

    Never the pasted string. `Library.find_by_url` is exact string equality and `ScanWorker`
    looks a project up by both the pasted URL and `meta.url`, so echoing would let the bare
    landing page, the trailing-slash form, the bare host, `/dir` and a chapter URL become
    FIVE projects for one novel — each with its own translation progress, tags, thumbnail
    prompt and video settings. Re-scanning from a chapter address is the normal case (the
    user reads, then re-scans from where they are), and it would fall through to
    `create_project`, which overwrites `meta.json` wholesale.

    The trailing slash is the site's own `og:url` shape.
    """
    return f"{ORIGIN}/{book_id(url)}/"


def dir_url(url: str) -> str:
    """The complete chapter index — the only page this adapter reads chapters from."""
    return f"{ORIGIN}/{book_id(url)}/dir"


def sub_page_url(page_one_url: str, page: int) -> str:
    """Page `page` of a chapter whose first page is `page_one_url`.

    Built from the stem the TOC gave us, never from a 下一章 link — see the module docstring
    for what that link points at. Rebuilding from the matched groups rather than doing string
    surgery on `.html` is what makes "page 3 of page 2" impossible: a URL that already
    carries a sub-page component is refused rather than yielding `<inner>_1_2_3.html`.
    """
    match = _CHAPTER_HREF_RE.search(page_one_url or "")
    if match is None or match.group(4):
        raise ScrapeError("Not a chapter page-1 URL", page_one_url)
    return f"{ORIGIN}/{match.group(1)}/{match.group(2)}_{match.group(3)}_{page}.html"


# ------------------------------------------------------------------------ parsing


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the detail page's OpenGraph tags, falling back to the visible book box."""
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        # timotxt — the same CMS — declares its OpenGraph block with `name=` rather than the
        # standard `property=`. Accepting both costs one `or` and means neither spelling can
        # break us if this site is "fixed" either way.
        el = soup.select_one(f"meta[property='{prop}']") or soup.select_one(f"meta[name='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    detail = soup.select_one(SEL_DETAIL)

    title = og("og:novel:book_name") or og("og:title")
    if not title:
        heading = detail.select_one("h1.title") if detail else None
        title = _norm(heading.get_text()) if heading else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = og("og:novel:author")
    if not author:
        span = detail.select_one("span.author") if detail else None
        author = _norm(span.get_text()) if span else ""
    if not author:
        for row in soup.select("p.meta, p, div"):
            text = _norm(row.get_text(" "))
            if text.startswith("作者") and len(text) < 40:
                author = re.split(f"[{_COLONS}]", text, maxsplit=1)[-1].strip()
                break

    # The visible blurb, NOT meta[name=description] / og:description — see the module
    # docstring. This is the one field where the rendered markup beats the metadata.
    intro = soup.select_one(SEL_INTRO)
    description = _norm(intro.get_text(" ")) if intro else _norm(og("og:description"))

    cover = og("og:image")
    if not cover:
        img = soup.select_one(SEL_COVER)
        cover = (img.get("src") or "").strip() if img else ""

    return NovelMeta(
        url=read_url(url),
        site=site,
        title=title,
        author=author,
        description=description,
        cover_url=cover,
        source_lang="zh",
    )


def stated_total(markup: str) -> int | None:
    """How many chapters the `/dir` page says the novel has. None when it does not say.

    ONE signal, `章節： N` in `section.info`, and deliberately not backed up by the OpenGraph
    block the way timotxt and twkan back theirs up — both `og:novel:latest_chapter_*` tags
    are unusable here, for two different reasons. See the module docstring.
    """
    soup = BeautifulSoup(markup, "lxml")
    info = soup.select_one(SEL_INFO)
    match = _STATED_TOTAL_RE.search(_norm(info.get_text(" "))) if info else None
    return int(match.group(1)) if match else None


def parse_chapter_list(markup: str, bid: str, url: str) -> list[ChapterRef]:
    """Every chapter on the `/dir` page, in reading order.

    Prefers the real index container. The fallback de-duplicates a flat anchor scrape by
    reading position and **sorts by that position rather than document order** — the 最新章節
    block sits first on the page and runs backwards, so trusting document order would put the
    last 12 chapters at the front. Both paths are then checked for contiguity by the caller,
    which is what makes the fallback safe rather than a hole in the no-fallback policy.

    Anchors are filtered on the book id (a recommendation widget must not contribute another
    novel's chapters) and on the absence of a sub-page component (a sub-page is part of a
    chapter, never a chapter of its own).
    """
    soup = BeautifulSoup(markup, "lxml")
    anchors = soup.select(SEL_ALL_CHAPTERS)
    if not anchors:
        anchors = soup.select("a[href]")

    by_position: dict[int, ChapterRef] = {}
    for anchor in anchors:
        href = anchor.get("href") or ""
        match = _CHAPTER_HREF_RE.search(href)
        if not match or match.group(1) != bid or match.group(4):
            continue
        title = _norm(anchor.get_text())
        if not title:
            continue
        by_position.setdefault(
            int(match.group(3)),
            ChapterRef(
                index=0,
                title=title,
                url=f"{ORIGIN}/{match.group(1)}/{match.group(2)}_{match.group(3)}.html",
            ),
        )
    if not by_position:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    return [
        ChapterRef(index=i, title=ref.title, url=ref.url)
        for i, (_n, ref) in enumerate(sorted(by_position.items()))
    ]


def chapter_positions(refs: list[ChapterRef]) -> list[int]:
    """The reading position each ref points at, read back off its URL."""
    positions = []
    for ref in refs:
        match = _CHAPTER_HREF_RE.search(ref.url)
        if match:
            positions.append(int(match.group(3)))
    return positions


def page_position(markup: str) -> tuple[int, int] | None:
    """The `(N, M)` a chapter page states about itself, or None if it states nothing.

    Reads the `<h1>` and falls back to `<title>`; the two carry the same suffix, and having
    a second signal is what keeps a heading-only markup tweak from silently halving every
    chapter on the site.
    """
    soup = BeautifulSoup(markup, "lxml")
    for element in (soup.select_one(SEL_HEADING), soup.title):
        if element is None:
            continue
        match = _PAGE_SUFFIX_RE.search(_norm(element.get_text()))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def page_count(markup: str, url: str) -> int:
    """How many sub-pages this chapter has, per the page's own `<h1>` "(N/M)" suffix.

    A chapter page with NO heading at all raises rather than defaulting to 1. An `<h1>` that
    exists and carries no suffix genuinely means one page (measured: chapters 120 and 301);
    an `<h1>` that has VANISHED means the layout changed, and quietly reading that as "one
    page" would drop the second half of every paginated chapter on the site with no error
    anywhere. That distinction is the whole point of this function.
    """
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


def parse_chapter(markup: str, title: str, url: str) -> str:
    """One sub-page's prose as plain text, paragraphs separated by blank lines."""
    soup = BeautifulSoup(markup, "lxml")
    container = soup.select_one(SEL_CONTENT)
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)

    for junk in container.select(SEL_CHROME):
        junk.decompose()

    # Direct children only. On this markup "a direct-child <p> of div.content" IS the
    # definition of a prose paragraph, and it is the only rule that excludes the site's
    # unclassed 溫馨提示 notice <div>. A literal string blocklist would be wrong: the notice's
    # wording ROTATES per request (加入書架 / 跨設備保存書架 / 「站內信」功能已優化 …), and its
    # wrapper carries no class to name. `> p` drops it whatever it says today.
    lines = [
        t for t in (p.get_text(strip=True) for p in container.find_all("p", recursive=False)) if t
    ]
    if not lines:
        # The CMS nested the prose one level deeper. Falling back costs at most one junk
        # notice line — which `translators/ads.py` is the downstream net for — while not
        # falling back costs the whole chapter.
        lines = [t for t in (p.get_text(strip=True) for p in container.select("p")) if t]

    # At most one, and only when a title was passed. `fetch_chapter` passes "" for every
    # sub-page: the <h1> lives outside div.content so this normally never fires, and a
    # sub-page whose first paragraph happened to match the title would otherwise lose a real
    # line of prose. Over-stripping is silent data loss; a duplicated heading is cosmetic.
    if lines and title and _norm(lines[0]) == _norm(title):
        lines = lines[1:]

    body = "\n\n".join(lines).strip()
    if not body:
        # Plain ScrapeError, never ObfuscatedContentError: nothing on this site is
        # obfuscated (that is timotxt's problem, on the same CMS), and that exception has
        # GUI meaning — it offers the user a repair that cannot help here.
        raise ScrapeError("Chapter content is empty", url)
    return body


# ------------------------------------------------------------------------ adapter


@register
class Novel543Adapter(SiteAdapter):
    """小說543. Fetches through a browser; everything above this line does the parsing."""

    name = "novel543"
    display_name = "小說543 (novel543.com)"
    # Host-anchored on purpose. A bare id pattern would collide with timotxt's URLs, which
    # have the identical `/<10-digit id>/` shape on a different host, and `ADAPTERS` is
    # ordered by import — first match wins, so the theft would be silent and order-dependent.
    # The trailing guard keeps the image host (i2.novel543.com/thumb/...) out. One pattern
    # covers every accepted form: the landing page, `/dir` and a chapter URL all contain
    # "novel543.com/<digits>" followed by "/" or end-of-string.
    url_patterns = [r"novel543\.com/\d+(?:/|$)"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping this
    # would land Chinese in `translated` and mark chapters already-translated, so the user's
    # Vietnamese output would be Chinese.
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
                "🌐 Đang mở trình duyệt để vượt kiểm tra Cloudflare của novel543 — giữ cửa sổ mở…"
            )
            self._session = BrowserSession(
                headless=self._headless,
                # The app's configured delay, but never below this site's floor. HttpClient's
                # own throttle is bypassed on this path, so this is the only one left.
                delay_seconds=max(self.client.delay_seconds, _MIN_DELAY_SECONDS),
            )
        try:
            return self._session.get_html(url)
        except BrowserUnavailableError as exc:
            raise ScrapeError(
                "Cần trình duyệt để đọc novel543 (trang này có kiểm tra Cloudflare). "
                "Cài Google Chrome, hoặc chạy:  pip install 'noveltrans[browser]' "
                "&& playwright install chromium",
                url,
            ) from exc
        except BrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc được trang novel543 — trình duyệt bị đóng hoặc không vượt "
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
        markup = self._get_html(read_url(url))
        self._detail = (bid, markup)
        return markup

    # -- SiteAdapter ---------------------------------------------------------------

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._detail_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        """The full TOC. One navigation, and it never touches the detail page.

        The contiguity check below is what carries this module. Because chapter URLs embed
        the reading position, `positions == [1 … N]` is a *proof* that the list is a dense
        ascending prefix starting at chapter 1 — exactly the property `ChapterRef.index`
        needs. Every realistic degradation breaks it: the duplicate block leaking in is not
        ascending, losing `ul.all` leaves 428…439 rather than starting at 1, and a dropped
        middle leaves a gap. `replace_toc` preserves content across re-scans while updating
        title and URL, so a list that is wrong in ORDER is not a transient error — it files
        chapter 439's body under index 0 and a later, correct scan quietly rewrites the title
        over it. Refusing is the only safe direction.
        """
        markup = self._get_html(dir_url(url))
        refs = parse_chapter_list(markup, book_id(url), dir_url(url))

        positions = chapter_positions(refs)
        if positions != list(range(1, len(positions) + 1)):
            raise ScrapeError(
                f"Danh sách chương của novel543 không liền mạch từ chương 1 "
                f"({len(positions)} mục, cao nhất {max(positions) if positions else 0}). "
                "Thử quét lại sau.",
                dir_url(url),
            )

        # A report, not a gate. The contiguity above already proves the list is a dense
        # prefix, so a short one is safe — a later re-scan simply extends it and nothing can
        # be misfiled. Never silently, though. And a list LONGER than the stated total must
        # equally not fail: the bqg5 lesson is that a stated number never gets to refuse a
        # provably-good list.
        total = stated_total(markup)
        if total is not None and len(refs) != total:
            self._status(f"⚠️ novel543 trả về {len(refs)}/{total} chương. Quét lại sau để lấy đủ.")
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        """The whole chapter, re-joined from however many sub-pages the site split it into.

        This is the only adapter in the repo that fetches more than one page per chapter: the
        reference novel is 439 chapters of which most are two pages, so a full download is
        ~800 browser navigations. That is the price of the site, and it is why the politeness
        delay must be honoured rather than tuned down.

        Any sub-page problem fails the whole chapter, on purpose. Saving what we have would
        write a half chapter that looks complete, and `DownloadWorker` would never revisit it
        because `pending_download` sees content; raising instead marks the chapter as an
        error, which is visible and retryable.
        """
        first = self._get_html(ref.url)
        pages = page_count(first, ref.url)
        bodies = [parse_chapter(first, ref.title, ref.url)]

        for k in range(2, pages + 1):
            url = sub_page_url(ref.url, k)
            markup = self._get_html(url)
            # The sub-page states its own position, and this check is load-bearing rather
            # than defensive: **an out-of-range sub-page serves page 1 again, with a 200
            # and no error of any kind.** Measured, both shapes —
            #   /8096_120_2.html (chapter 120 is unpaginated) -> page 1 of ch.120, no suffix
            #   /8096_1_3.html   (chapter 1 has only 2 pages) -> page 1 of ch.1, "(1/2)"
            # Both parse perfectly as prose. So without this, any over-estimate of `pages`
            # duplicates the chapter's opening into its own middle, silently and
            # permanently. The suffix is the only thing that distinguishes them.
            if page_position(markup) != (k, pages):
                raise ScrapeError(
                    f"novel543 trả về sai trang {k}/{pages} của chương — thử tải lại chương này.",
                    url,
                )
            bodies.append(parse_chapter(markup, "", url))

        return "\n\n".join(bodies)

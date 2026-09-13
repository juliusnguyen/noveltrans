"""Adapter for 天天看小說 (ttks.tw).

Index page:   https://ttks.tw/novel/chapters/<slug>/index.html  — metadata AND the full TOC
Chapter page: https://ttks.tw/novel/chapters/<slug>/<n>.html    — n is the reading position

**One page is both the detail page and the complete table of contents**, so a full scan
costs exactly ONE browser navigation. That matters more here than it would on a plain HTTP
site: every navigation into a Cloudflare host is another chance at an interstitial, so
`fetch_metadata` and `fetch_chapter_list` share one cached fetch rather than taking a page
each. `twkan.py` caches a *detail* page for the same reason; this one caches the index.

**Every page comes through a browser.** Measured: a plain `curl` carrying our own desktop
User-Agent gets HTTP 403 with `cf-mitigated: challenge` on every URL form, so `HttpClient`
cannot read this site at all and `cf_browser.BrowserSession` is the only path.
`BrowserSession` cleared the gate on the first navigation and served the index plus three
chapter pages with no interstitial. There is no header trick and no token to replay.
**Do not parallelise ttks.** N concurrent sessions is N Chromes, and hammering a
CF-protected host is the fastest route to an IP block — which would kill the browser path
too, and it is the only path this site has.

`twkan.py` is the transport reference (same `BrowserSession` lifetime, same two exception
translations, same wording) and `novel543.py` is the TOC reference, so the three Cloudflare
adapters read alike. Three things here are this site's own, and each is a trap:

**1. The TOC page carries TWO chapter blocks and the first one is a decoy.** Both are
`div.chapters_frame` with *identical* classes; they are distinguished only by the text of a
preceding `h3.chapters_title` — 最新章節 for the newest handful, 全部章節 for the real list.
The decoy runs **backwards**. Measured on a 925-chapter novel: a flat `a[href]` scrape
returns 942 links — 925 real, 16 duplicates from the decoy, and one `index.html` self-link —
with the last chapters first.

*Anchoring on the heading text was considered and rejected.* novel543 can anchor its primary
path on `ul.all` because that is a **class**, a semantic token in the markup
(`novel543.py:156-158`); here the only discriminator is site *copy*, and a rule keyed on a
literal string of prose is exactly what `bqg5.py` refuses for its rotating notice and
`novel543.py:392-396` refuses for the 溫馨提示 line. The decoy's length is no better an
anchor: bqg5 measured that same block at 9 entries once and 5 the next time.

So this module ports novel543's answer instead — **key every anchor by the reading position
parsed out of its own URL**. Dedupe falls out of the key (a duplicate carries the same `<n>`),
ascending order falls out of sorting by it, the `index.html` self-link falls out because it
has no `<n>`, and a recommendation widget pointing at another novel falls out because the
slug is part of the match. None of that depends on which frame is which.

**The contiguity proof is what makes the above safe rather than merely convenient.** Because
chapter URLs embed the position, `positions == [1 … N]` *proves* the list is a dense ascending
prefix starting at chapter 1 — exactly the property `ChapterRef.index` needs. Every realistic
degradation breaks it: the decoy leaking in is not ascending, losing the real frame leaves
14…20 rather than starting at 1, and a dropped middle leaves a gap. `replace_toc` preserves
content across re-scans while updating title and URL, so a list that is wrong in ORDER is not
a transient error — it files chapter 925's body under index 0, and a later, correct scan
quietly rewrites the title over it. Refusing is the only safe direction.

**2. A chapter page carries TWO `div.content`, and one of them is the prev/next navigation.**
The body happens to come first today, so `select_one("div.content")` is right by luck. The
rule used here is conjunctive and non-positional instead: the body is the `div.content` that
holds at least one `<p>` and holds no `div.prev_page`/`div.next_page`. Identifying the body by
its *previous sibling* (`div.title`) was rejected — sibling position is precisely what a theme
refresh rewrites, and it identifies the body by something outside the body.

**3. Every chapter has a site advert spliced into the middle of a real paragraph.** The shape
is `<p>` = prose sentence, `<br/><br/>`, advert — and the advert writes the site's domain in
Unicode mathematical-alphanumeric letters (`𝘁𝘁𝗸.𝘁𝘄`, `𝐭𝐭𝐤.𝐭𝐰`, `𝕥𝕥𝕜.𝕥𝕨`), a different
alphabet from chapter to chapter, specifically so that a literal match misses it. Measured:
`unicodedata.normalize("NFKC", …)` folds all three observed spellings to plain `ttk.tw`.

The `<br/><br/>` is what makes this tractable. `container.get_text("\n")` emits nothing for a
`<br>`, so the two text nodes are separated by the `\n` separator and **the advert lands on
its own line** — which means it can be removed a whole line at a time and can never take prose
with it. Measured across three chapters: the advert isolates as exactly one line every time
(50, 52 and 18 chars), and no legitimate line carries the site name without also carrying the
domain.

    **Do NOT port `novel543.py:398`'s `p.get_text(strip=True)` here.** It glues the advert
    onto the end of the prose sentence, and the filter would then have to cut a clause out of
    the middle of a line — which is where over-deletion lives (`translators/ads.py:157`
    records that as a known limitation). The separator argument to `get_text` is load-bearing
    and `test_the_promo_is_its_own_line_and_the_prose_sentence_survives_whole` pins it.

**Why the advert is stripped here and not left to `translators/ads.py`.** That module is the
established filter for exactly this kind of line, but it runs only on fresh translator output
and deliberately never touches the stored original `content` (`ads.py:28-30`). This advert is
in the Chinese source, so stripping at scrape time (a) keeps the stored original clean, which
is what the user reads, edits, exports and re-translates from; (b) means the translator never
sees the line and so cannot paraphrase it into a shape no filter recognises; and (c) saves
tokens on every chapter. Measured, and the reason (b) is not hypothetical: `ads._is_ad_line`
returns **True** for all three real Chinese adverts — so `ads.py` remains a genuine
second net if this filter ever misses a new form — but **False** for a plausible Vietnamese
translation of the same line, because `ads.py:189` normalises with NFKC *before* testing
whether the domain survived de-obfuscation, so NFKC-style obfuscation is invisible to its
obfuscation conjunct (it was built for an emoji standing in for a character, which folding
*deletes* rather than *resolves*). Closing that gap belongs to `ads.py` and its own regression
suite, not here; nothing in this module changes it.

**No completeness guard, deliberately.** Chapter bodies render complete in the DOM with
nothing lazy-loaded, and the repo has been burned by length heuristics before — see
`twkan.py:52-62` and feature 061, which shipped exactly that false positive on bookqq and had
to be reverted (commit 1f9eb6f). `og:novel:latest_chapter_name` is likewise NOT usable as a
count: it is a chapter *title*, and 章 numbering drifts from reading position (novel543's
position 439 is 第438章). `og:novel:latest_chapter_url` IS positional and is used — but as a
report only, never a gate: contiguity already proves the list is a dense prefix, so a short
list is safe (a re-scan extends it) and a list LONGER than advertised must never be refused.
The advertised URL itself is never fetched; novel543's equivalent is a 404.

**`og:description` is SEO boilerplate on this site, not a synopsis** — it reads
"<title>小說是知名作家<author>作品…最新免費vip章節和全本章節列表。". The real blurb is the visible
`div.description`, which the site truncates itself with a trailing "...". That truncation is
the site's and no prefix-stripping recovers a tail it never sent, so the blurb is stored as
given; feature 065 caps the length downstream. Anyone "harmonising" this with twkan's
OG-first rule will turn `test_the_description_is_the_visible_blurb_not_the_seo_boilerplate`
red, which is the point.

**No minimum-delay floor — measured, not assumed.** novel543 pins one at 3 s on the strength
of a measured 14-chapter walk (`novel543.py:135-145`). The equivalent walk was run here: the
index plus **ten consecutive chapters at the default 1.5 s delay, in ~18 s, with no
interstitial and no 429**. So the floor novel543 needs, this site does not, and inventing one
would slow every user on a guess. If a sustained download ever does show unclearable
challenges, `delay_seconds` in `_get_html` is the one lever to reach for;
`TestLive.test_a_short_consecutive_walk_survives_the_default_delay` is the probe that decided
this and is the one to re-run.

Content is Traditional Chinese, so `source_lang` is "zh". Encoding needs no handling on this
path: `page.content()` returns an already-decoded `str`.

The module splits fetching from parsing on purpose: `_get_html` is the only method that
touches a browser, and everything below the `parsing` line is pure and takes markup — so the
whole parsing surface, all three traps included, is tested against hand-built fixtures with no
Chrome anywhere in CI.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup

from noveltrans.browser import BrowserUnavailableError
from noveltrans.cf_browser import BrowserSession, BrowserSessionError
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

# Fixed, not taken from the pasted URL. Pinning the origin is what guarantees that http://,
# https://www. and the bare host all canonicalise to ONE string — the whole point of
# `read_url`. It is also the explicit decision NOT to claim the wider ttkan family
# (ttkan.co, wa01.ttkan.co, …): those hosts are unmeasured, and widening must be a deliberate
# edit here AND in `url_patterns`, not an accident.
ORIGIN = "https://ttks.tw"

# The novel's slug, from any paste form. Anchored on the HOST as well as the path, because
# `read_url` builds a ttks.tw identity out of whatever this returns: an unanchored pattern
# would happily read a slug out of someone else's /novel/chapters/ URL and mint a ttks
# project for it. `_CHAPTER_HREF_RE` below is deliberately NOT host-anchored — it reads
# hrefs, and the site writes some of those relative.
_SLUG_RE = re.compile(r"ttks\.tw/novel/chapters/([A-Za-z0-9][A-Za-z0-9_-]*)")
# What makes an anchor a chapter. Group 1 is load-bearing twice over: it is how a
# recommendation widget pointing at ANOTHER novel is excluded, and it is how `index.html`
# excludes itself. Group 2 is the reading position, which is the key the whole TOC turns on.
_CHAPTER_HREF_RE = re.compile(r"/novel/chapters/([A-Za-z0-9][A-Za-z0-9_-]*)/(\d+)\.html")

_COLONS = ":："
_WS_RE = re.compile(r"\s+")


def _og(soup: BeautifulSoup, prop: str) -> str:
    """An OpenGraph value, accepting BOTH attribute spellings.

    **This site writes `name="og:…"`, not `property="og:…"`.** It is a Nuxt/AMP page and its
    head is emitted as `<meta data-hid="og:x" name="og:x" content="…">` — no `property`
    attribute anywhere. A `meta[property=…]` selector (every sibling adapter's shape, because
    every other site uses it) silently matches nothing here, and because `parse_metadata` and
    `parse_description` both have visible-markup fallbacks, the failure does not look like a
    failure: the title and author still come out right off the info box, while `cover_url`
    goes quietly empty and `latest_position` returns None. Measured on the live page — the
    offline fixtures cannot catch it unless they spell the tags the way the site does, which
    is why `index.html` uses `name=` and `index_og_property.html` exists to keep the other
    spelling working.
    """
    el = soup.select_one(f"meta[property='{prop}']") or soup.select_one(f"meta[name='{prop}']")
    return (el.get("content") or "").strip() if el else ""


# Anchoring the TOC scrape here keeps site navigation out of the flat fallback. Belt and
# braces only: `_CHAPTER_HREF_RE` plus the contiguity proof would carry it alone, which is
# exactly why falling back to a bare `a[href]` scrape is safe.
SEL_TOC_LINKS = "div.chapters_frame a[href]"
SEL_CONTENT = "div.content"
SEL_NAV = "div.prev_page, div.next_page"
# Named per element, never a blanket <div> decompose — a blanket rule eats a paragraph the
# day the CMS wraps one (`twkan.py:134-138` records the same reasoning). The risk is live
# rather than theoretical here: measured, eight `div.txtad` sit INSIDE the body, mid-prose.
# Runs BEFORE paragraphs are collected, so a <p> inside an ad slot never reaches the body.
SEL_CHROME = (
    "a.anchor_bookmark, div.txtad, div.div_feedback, div.social_share_frame, "
    "amp-social-share, amp-img, script, style, ins, iframe"
)

# The site's own domain, after NFKC folding. The extension point if it ever rotates hosts.
_PROMO_DOMAINS = ("ttks.tw", "ttk.tw")
# An advert is short by nature; a real paragraph is not. A paragraph that genuinely discusses
# the site survives on length alone, whatever else it contains.
MAX_PROMO_LINE_CHARS = 120
# Below this many ASCII alphanumerics either side of the domain, the line is the watermark and
# nothing else. Mirrors `ads._BARE_LEFTOVER_MAX`.
_BARE_LEFTOVER_MAX = 4


def _norm(text: str) -> str:
    """Whitespace-normalised form, used only for comparing titles — never for storing."""
    return _WS_RE.sub(" ", text).strip()


def _is_promo_line(line: str) -> bool:
    """True when this whole line exists only to advertise ttks.

    **Conjunctive and domain-anchored**, deliberately copying the philosophy of
    `translators/ads.py` rather than the code: a line is dropped only if the site's own domain
    survives de-obfuscation *on that line*. No domain, no deletion — and that single conjunct
    is what makes "the filter ate my story" structurally impossible for prose that is not
    about this exact website.

    The risk is deliberately **asymmetric**. A miss costs one visible junk line the user can
    find-and-replace; an over-deletion is silent data loss noticed chapters later. Every
    choice below biases to under-delete.

    Note what is NOT here: a list of the site's slogans. The adverts vary their wording every
    chapter, and a literal blocklist is precisely the failure mode `ads.py` exists to avoid.

    Residual false-positive surface, stated honestly: a Chinese prose line under
    `MAX_PROMO_LINE_CHARS` that names the site's domain plainly *and* carries almost no other
    text. Such a line is a watermark by construction, so the trade is accepted.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_PROMO_LINE_CHARS:
        return False

    folded = unicodedata.normalize("NFKC", stripped).casefold()
    domain = next((d for d in _PROMO_DOMAINS if d in folded), None)
    if domain is None:
        return False  # the load-bearing guarantee: no domain, no deletion

    # 1. Obfuscation — the domain exists only AFTER folding, i.e. it was written in a
    #    decorative alphabet. This is the primary signal and it is what the site actually
    #    does. `ads.py` cannot express this conjunct: it folds before comparing (`ads.py:189`).
    if domain not in stripped.casefold():
        return True

    # 2. Bare — the domain is written plainly, but there is essentially nothing else on the
    #    line. Covers a future plain-ASCII advert. CJK is not counted: a Chinese sentence
    #    carries no ASCII alphanumerics at all, so counting only ASCII would make every
    #    Chinese line look "bare". Length is what protects those, above.
    leftover = folded.replace(domain, "")
    return sum(c.isalnum() for c in leftover) < _BARE_LEFTOVER_MAX


# --------------------------------------------------------------------------- urls


def slug(url: str) -> str:
    """The novel's slug, from any of the paste forms."""
    match = _SLUG_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract novel slug from URL", url)
    return match.group(1)


def read_url(url: str) -> str:
    """The canonical URL for a novel — the site's own `og:novel:read_url`.

    `NovelMeta.url` is set from this rather than echoing what the user pasted, because
    `Library.find_by_url` is exact string equality: echoing would let the index page and a
    chapter URL become two projects for one novel, each with its own translation progress,
    tags and video settings. Re-scanning from a chapter URL is the normal case.
    """
    return f"{ORIGIN}/novel/chapters/{slug(url)}/index.html"


def chapter_url(url: str, position: int) -> str:
    """Chapter `position`'s URL. Used to NORMALISE an href the TOC already gave us.

    Never used to synthesise a list: the site's chapter URLs are constructible, but a
    synthesised list would invent URLs the site never offered and would carry no titles.
    """
    return f"{ORIGIN}/novel/chapters/{slug(url)}/{position}.html"


# ------------------------------------------------------------------------ parsing


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the index page's OpenGraph tags, falling back to the visible book box."""
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        return _og(soup, prop)

    title = og("og:novel:book_name")
    if not title:
        heading = soup.select_one("div.novel_info h1")
        title = heading.get_text(strip=True) if heading else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = og("og:novel:author")
    if not author:
        for row in soup.select("div.novel_info li"):
            text = row.get_text(" ", strip=True)
            if text.startswith("作者"):
                author = re.split(f"[{_COLONS}]", text, maxsplit=1)[-1].strip()
                break

    return NovelMeta(
        url=read_url(url),
        site=site,
        title=title,
        author=author,
        description=parse_description(soup),
        # Stored as-is: no placeholder form has been observed on this site, and inventing a
        # filename to blank would be worse than the documented gap (`twkan.py:72-76`).
        cover_url=og("og:image"),
        # Written explicitly rather than inherited: this is a Chinese source, and a future
        # change to the dataclass default must not silently reclassify it.
        source_lang="zh",
    )


def parse_description(soup: BeautifulSoup) -> str:
    """The visible blurb, NOT `og:description`.

    Backwards from every sibling adapter, and deliberately so: this site's `og:description` is
    SEO boilerplate that names the title and author and says the novel has chapters. The
    visible `div.description` is the real synopsis. The site truncates it itself with a
    trailing "..."; that is stored as given, because no amount of cleaning recovers a tail the
    server never sent.
    """
    block = soup.select_one("div.description")
    if block is not None:
        for junk in block.select("div.ad_300x250, ins, script, style"):
            junk.decompose()
        text = _norm(block.get_text(" "))
        if text:
            return text

    return _og(soup, "og:description")


def latest_position(markup: str, novel_slug: str) -> int | None:
    """The position the page advertises as its newest chapter, or None.

    Read off `og:novel:latest_chapter_url`, whose `<n>` is a reading position and therefore a
    count. **Never** off `og:novel:latest_chapter_name`: that is a chapter title, and 章
    numbering drifts from position.

    The slug check is not decoration — a stale or cross-linked tag pointing at a different
    novel would otherwise report that novel's length as this one's.
    """
    soup = BeautifulSoup(markup, "lxml")
    match = _CHAPTER_HREF_RE.search(_og(soup, "og:novel:latest_chapter_url"))
    if match is None or match.group(1) != novel_slug:
        return None
    return int(match.group(2))


def parse_chapter_list(markup: str, novel_slug: str, url: str) -> list[ChapterRef]:
    """Every chapter on the index page, in reading order.

    Keyed by the reading position parsed out of each href, which is what neutralises the
    decoy 最新章節 block: its entries carry the same positions as real ones, so `setdefault`
    drops them, and `sorted` undoes its backwards order. Falls back to a flat anchor scrape
    when the frame wrapper is missing — safe because the href filter and the caller's
    contiguity proof, not the selector, are what make the result trustworthy.
    """
    soup = BeautifulSoup(markup, "lxml")
    anchors = soup.select(SEL_TOC_LINKS) or soup.select("a[href]")

    by_position: dict[int, ChapterRef] = {}
    for anchor in anchors:
        match = _CHAPTER_HREF_RE.search(anchor.get("href") or "")
        if not match or match.group(1) != novel_slug:
            continue
        title = _norm(anchor.get_text())
        if not title:
            continue
        position = int(match.group(2))
        by_position.setdefault(
            position,
            ChapterRef(index=0, title=title, url=chapter_url(url, position)),
        )
    if not by_position:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    # `index` comes from enumerate, never from the position: `index` is a dense positional key
    # that replace_toc and every exporter depend on, and the position is only proven dense by
    # the caller's contiguity check.
    return [
        ChapterRef(index=i, title=ref.title, url=ref.url)
        for i, (_position, ref) in enumerate(sorted(by_position.items()))
    ]


def chapter_positions(refs: list[ChapterRef]) -> list[int]:
    """The reading position each ref points at, read back off its URL."""
    positions = []
    for ref in refs:
        match = _CHAPTER_HREF_RE.search(ref.url)
        if match:
            positions.append(int(match.group(2)))
    return positions


def _body_container(soup: BeautifulSoup):
    """The `div.content` that holds the chapter, or None.

    Conjunctive and non-positional: holds at least one `<p>`, and holds no prev/next
    navigation. The negative conjunct rejects the nav `div.content` by the very thing that
    makes it the nav; the positive one rejects an empty or chrome-only container.
    """
    for candidate in soup.select(SEL_CONTENT):
        if candidate.select_one(SEL_NAV) is None and candidate.find("p") is not None:
            return candidate
    return None


def parse_chapter(markup: str, title: str, url: str) -> str:
    """Return the chapter body as paragraphs separated by blank lines, adverts removed."""
    soup = BeautifulSoup(markup, "lxml")
    container = _body_container(soup)
    if container is None:
        # No <p> anywhere: the CMS may have switched to bare text nodes and <br>. Fall back to
        # the first div.content that is not the navigation, rather than losing the chapter.
        container = next(
            (c for c in soup.select(SEL_CONTENT) if c.select_one(SEL_NAV) is None), None
        )
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)

    for junk in container.select(SEL_CHROME):
        junk.decompose()

    # `get_text("\n")`, NEVER `get_text(strip=True)` — see the module docstring. The <br/><br/>
    # before an advert produces no text node, so the separator is the only thing putting the
    # advert on a line of its own, and a line of its own is what makes it removable at all.
    # The &emsp; indents need no handling: U+2003 is whitespace, so strip() removes them.
    def clean(block) -> list[str]:
        lines = [line.strip() for line in block.get_text("\n").split("\n") if line.strip()]
        return [line for line in lines if not _is_promo_line(line)]

    paragraphs = container.find_all("p")
    if paragraphs:
        # Joined with "\n", not "\n\n": removing an advert from inside a paragraph must leave
        # ONE paragraph. Downstream TTS chunking keys off "\n\n".
        blocks = [joined for p in paragraphs if (joined := "\n".join(clean(p)))]
    else:
        blocks = clean(container)

    if not blocks:
        # Everything was an advert, or the filter misjudged. Keep the text rather than raising:
        # a surviving advert is a nuisance, a blanked chapter is data loss the user will not
        # notice for chapters. Mirrors `ads.drop_site_ads`'s own safety valve.
        if paragraphs:
            blocks = [text for p in paragraphs if (text := _norm(p.get_text("\n")))]
        else:
            blocks = [line.strip() for line in container.get_text("\n").split("\n") if line.strip()]

    # The <h1> lives outside the container, so this only fires if the CMS ever repeats the
    # title as prose. Drop at most one line: a duplicated heading is cosmetic, while an eaten
    # opening line is data loss the reader won't notice.
    if blocks and _norm(blocks[0]) == _norm(title):
        blocks = blocks[1:]

    if not blocks:
        raise ScrapeError("Chapter content is empty", url)
    return "\n\n".join(blocks)


# ------------------------------------------------------------------------ adapter


@register
class TtksAdapter(SiteAdapter):
    """天天看小說. Fetches through a browser; everything above this line does the parsing."""

    name = "ttks"
    display_name = "天天看小說 (ttks.tw)"
    # Host-anchored AND path-anchored on purpose. `ADAPTERS` is ordered by import and first
    # match wins, so a loose pattern steals another site's URLs silently and
    # order-dependently. The escaped dot matters too: an unescaped `ttks.tw` also matches
    # `ttksXtw`. One pattern covers every paste form — `<slug>/index.html` and `<slug>/<n>.html`
    # both contain `ttks.tw/novel/chapters/<slug>`.
    url_patterns = [r"ttks\.tw/novel/chapters/[A-Za-z0-9][A-Za-z0-9_-]*"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping this
    # would land Chinese in `translated` and mark chapters already-translated, so the user's
    # Vietnamese output would be Chinese.
    content_is_translated = False

    def __init__(self, client, *, headless: bool = False):
        super().__init__(client)
        # Headless is fingerprinted by Cloudflare and does not clear the challenge (measured —
        # see cf_browser). The flag exists in case that ever changes.
        self._headless = headless
        self._session: BrowserSession | None = None
        # Per-instance, never module-level: the app is long-lived and the user switches
        # novels, so a shared cache would answer one novel's questions with another's page.
        self._index: tuple[str, str] | None = None  # (slug, markup)

    # -- fetching: the only part that touches a browser ---------------------------

    def _get_html(self, url: str) -> str:
        """The single seam between this adapter and the browser."""
        if self._session is None:
            # A Chrome window is about to appear on the user's screen — say why before it
            # does, or it reads as the app misbehaving.
            self._status(
                "🌐 Đang mở trình duyệt để vượt kiểm tra Cloudflare của ttks — giữ cửa sổ mở…"
            )
            self._session = BrowserSession(
                headless=self._headless,
                # Honour the app's configured politeness delay: HttpClient's own throttle is
                # bypassed on this path, so this is the only one left. No floor is imposed —
                # see the module docstring.
                delay_seconds=self.client.delay_seconds,
            )
        try:
            return self._session.get_html(url)
        except BrowserUnavailableError as exc:
            raise ScrapeError(
                "Cần trình duyệt để đọc ttks (trang này có kiểm tra Cloudflare). "
                "Cài Google Chrome, hoặc chạy:  pip install 'noveltrans[browser]' "
                "&& playwright install chromium",
                url,
            ) from exc
        except BrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc được trang ttks — trình duyệt bị đóng hoặc không vượt "
                f"được kiểm tra Cloudflare. Thử tải lại. ({exc})",
                url,
            ) from exc

    def close(self) -> None:
        """Release the browser. Idempotent; never raises."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def _index_page(self, url: str) -> str:
        """The index page, fetched at most once per adapter.

        This cache is why a full scan costs one navigation rather than two: metadata and the
        chapter list live on the same page, and every extra navigation into a Cloudflare host
        is another chance at an interstitial.
        """
        novel_slug = slug(url)
        if self._index is not None and self._index[0] == novel_slug:
            return self._index[1]
        markup = self._get_html(read_url(url))
        self._index = (novel_slug, markup)
        return markup

    # -- SiteAdapter ---------------------------------------------------------------

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._index_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        """The full TOC. One navigation, shared with `fetch_metadata`.

        The contiguity check is what carries this module — see the module docstring for why
        refusing beats saving a list that is wrong in order.
        """
        markup = self._index_page(url)
        novel_slug = slug(url)
        refs = parse_chapter_list(markup, novel_slug, url)

        positions = chapter_positions(refs)
        if positions != list(range(1, len(positions) + 1)):
            raise ScrapeError(
                f"Danh sách chương của ttks không liền mạch từ chương 1 "
                f"({len(positions)} mục, cao nhất {max(positions) if positions else 0}). "
                "Thử quét lại sau.",
                read_url(url),
            )

        # A report, not a gate. Contiguity already proves the list is a dense prefix, so a
        # short one is safe — a later re-scan simply extends it and nothing can be misfiled.
        # Never silently, though. And a list LONGER than the advertised latest must equally not
        # fail: the bqg5 lesson is that a stated number never gets to refuse a provably-good
        # list.
        latest = latest_position(markup, novel_slug)
        if latest is not None and len(refs) < latest:
            self._status(f"⚠️ ttks trả về {len(refs)}/{latest} chương. Quét lại sau để lấy đủ.")
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        """One page, one chapter. The site does not paginate chapters.

        The prev/next links are never followed: chapter 1's 上一章 points back at the index,
        and a chain that runs off the end of a chapter is a trap novel543 and bqg5 both record.
        """
        return parse_chapter(self._get_html(ref.url), ref.title, ref.url)

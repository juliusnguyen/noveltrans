"""QQ阅读 (book.qq.com) — Chinese source, partly behind Tencent's paywall.

A **Chinese-source** adapter, like `ixdzs` / `xbanxia` / `shuba69`: `fetch_chapter`
returns the original Chinese and tab 2 translates it with the app's own engines. It is
deliberately NOT a `content_is_translated` adapter — see the note on the class.

URL shapes (both accepted, both normalised to the detail page):

    https://book.qq.com/book-detail/<bid>
    https://book.qq.com/book-read/<bid>/<n>        <n> is the 1-based POSITION, not the cid

**Nuxt 2, not Nuxt 3.** The page embeds `window.__NUXT__=(function(a,b,…){…}(0,1,…))` —
a minified IIFE whose values are single-letter identifiers resolved positionally from the
invocation's argument list. It is not JSON and can never be `json.loads`-ed. This is a
different shape from `tieuthuyetmang`'s `__NUXT_DATA__` JSON island; do not confuse them.

Extraction is deliberately **hybrid**:

* the chapter list comes from the payload (`bookChapters`), because the `free` /
  `purchased` flags are data and appear nowhere in the rendered DOM;
* chapter **bodies** come from the server-rendered `<p>` elements, because they are plain
  and unobfuscated there. `fetch_chapter` never touches the payload, so a payload format
  change cannot corrupt a chapter body.

**Most of a QQ novel is paid.** On the reference novel (58625737) 77 of 226 chapters are
free — the first 65 contiguously, then occasional single free chapters among the paid
ones. A paid chapter still returns **HTTP 200**, with a short teaser and a subscribe
button; nothing about the status code says the fetch failed. So the paywall is gated
twice, before any extraction, and a refusal raises `AuthRequiredError`. This adapter does
not and must not attempt to read chapters the user has not bought.

⚠️ Chapter URLs are **positional**. If Tencent ever inserts a chapter, every later `<n>`
points at different content, and `replace_toc` would keep the old text under the new
title. `cid` is the stable key and is deliberately not persisted (`ChapterRef` has room
for exactly index/title/url); it is one detail-page fetch away if this ever bites.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from noveltrans.errors import AuthRequiredError, ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

ORIGIN = "https://book.qq.com"

# The content container, identical on free and paid pages — MEASURED: a paid chapter
# serves the same `div#article.chapter-content.isTxt` holding a short teaser, which is
# exactly why the gate cannot be "did we extract anything".
SEL_CONTENT = "#article"

# Short call-to-action elements that mean "this chapter is not yours to read".
#
# 登录 is deliberately ABSENT: it sits in the header of ordinary pages too, so using it
# as a trigger would refuse free chapters. The real CTA reads 登录订阅本章 and is caught
# by 订阅. `medoctruyen.py:211-213` records the same trap with `previewLoginHref`.
_PAYWALL_MARKERS = ("订阅", "购买", "付费章节", "阅读全文")

# A CTA is a button, not prose. Length is used ONLY to tell those apart — never as a
# reason to call a chapter paywalled, which would refuse genuinely short chapters.
_CTA_MAX_CHARS = 200

_BID_RE = re.compile(r"/book-(?:detail|read)/(\d+)")
_CHAPTER_N_RE = re.compile(r"/book-read/\d+/(\d+)")
_HREF_RE = re.compile(r"^/book-read/(\d+)/(\d+)")


# --------------------------------------------------------------------------- urls


def book_id(url: str) -> str:
    """The numeric book id, from either URL form."""
    match = _BID_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def detail_url(url: str) -> str:
    """Canonical detail page for either URL form.

    `NovelMeta.url` is set from this rather than echoing what the user pasted, because
    `Library.find_by_url` is exact string equality — echoing would let the detail URL and
    a chapter URL become two projects for one novel.
    """
    return f"{ORIGIN}/book-detail/{book_id(url)}"


def chapter_url(bid: str, number: int) -> str:
    return f"{ORIGIN}/book-read/{bid}/{number}"


def chapter_number(url: str) -> int:
    """The 1-based position out of a /book-read/ URL."""
    match = _CHAPTER_N_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract chapter number from URL", url)
    return int(match.group(1))


# ----------------------------------------------------------------- nuxt 2 payload


class _Reader:
    """A tolerant reader for the JS literals inside a minified Nuxt 2 payload.

    Not a JS evaluator and not a regex: `chapterName` values are Chinese strings that may
    contain escaped quotes, and a `chapterName:"(.*?)"` shape truncates at the first
    escape and silently drops the rest of the chapter list.
    """

    def __init__(self, text: str, pos: int = 0, args: dict | None = None):
        self.text = text
        self.pos = pos
        self.args = args or {}

    def _skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def value(self):
        self._skip()
        if self.pos >= len(self.text):
            return None
        char = self.text[self.pos]
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char in "\"'":
            return self.string()
        return self.atom()

    def string(self):
        quote = self.text[self.pos]
        self.pos += 1
        out = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                nxt = self.text[self.pos + 1 : self.pos + 2]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.pos += 2
                continue
            if char == quote:
                self.pos += 1
                break
            out.append(char)
            self.pos += 1
        return "".join(out)

    def atom(self):
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ",}]:":
            self.pos += 1
        token = self.text[start : self.pos].strip()
        if token in ("!0", "true"):
            return True
        if token in ("!1", "false"):
            return False
        if token in ("null", "void 0", "undefined"):
            return None
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        if re.fullmatch(r"-?\d*\.\d+", token):
            return float(token)
        # a bare identifier: resolve through the argument table, else give up on this
        # value rather than on the whole payload
        return self.args.get(token)

    def array(self):
        self.pos += 1  # [
        out = []
        while self.pos < len(self.text):
            self._skip()
            if self.text[self.pos] == "]":
                self.pos += 1
                break
            out.append(self.value())
            self._skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
        return out

    def object(self):
        self.pos += 1  # {
        out = {}
        while self.pos < len(self.text):
            self._skip()
            if self.pos >= len(self.text):
                break
            if self.text[self.pos] == "}":
                self.pos += 1
                break
            key = self.string() if self.text[self.pos] in "\"'" else self._bare_key()
            self._skip()
            if self.pos < len(self.text) and self.text[self.pos] == ":":
                self.pos += 1
            out[key] = self.value()
            self._skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
        return out

    def _bare_key(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ":},":
            self.pos += 1
        return self.text[start : self.pos].strip()


def _payload_source(markup: str) -> str:
    """The `window.__NUXT__=…` assignment, bounded to its own <script>."""
    start = (markup or "").find("window.__NUXT__")
    if start < 0:
        return ""
    end = markup.find("</script>", start)
    return markup[start : end if end > 0 else len(markup)]


def _arg_table(source: str) -> dict:
    """`{param_name: value}` for the IIFE's positional arguments."""
    header = re.search(r"\(function\(([^)]*)\)", source)
    if not header:
        return {}
    names = [n.strip() for n in header.group(1).split(",") if n.strip()]

    call = source.rfind("}(")
    if call < 0:
        return {}
    return dict(zip(names, _read_arg_list(source, call + 1)))


def _read_arg_list(source: str, at: int) -> list:
    """Read `(v1, v2, …)` starting at the opening paren.

    Separate from `_Reader.array` because the argument list is paren-delimited while
    every array inside the payload is bracket-delimited.
    """
    reader = _Reader(source, at + 1)
    out: list = []
    while reader.pos < len(source):
        reader._skip()
        if reader.pos >= len(source) or source[reader.pos] == ")":
            break
        out.append(reader.value())
        reader._skip()
        if reader.pos < len(source) and source[reader.pos] == ",":
            reader.pos += 1
        else:
            break
    return out


def chapter_entries(markup: str) -> list[dict]:
    """The `bookChapters` array from the Nuxt payload, or `[]` if it cannot be read.

    Never raises: an unreadable payload degrades to the rendered-HTML TOC
    (`parse_chapter_list`), which costs the `free`/`purchased` flags and nothing else.
    """
    source = _payload_source(markup)
    if not source:
        return []
    try:
        args = _arg_table(source)
        at = source.find("bookChapters")
        if at < 0:
            return []
        at = source.find("[", at)
        if at < 0:
            return []
        entries = _Reader(source, at, args).value()
    except Exception:  # noqa: BLE001 — a payload we cannot read is not a scan failure
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


# ------------------------------------------------------------------------ parsing


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def parse_metadata(markup: str, url: str, site: str = "bookqq") -> NovelMeta:
    """Title/author/description/cover from the detail page's og: tags, then the DOM."""
    soup = BeautifulSoup(markup, "lxml")
    title = _meta_content(soup, "og:novel:book_name", "og:title")
    if not title:
        heading = soup.select_one("h1")
        title = heading.get_text(strip=True) if heading else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)
    return NovelMeta(
        url=detail_url(url),
        site=site,
        title=title,
        author=_meta_content(soup, "og:novel:author"),
        description=_meta_content(soup, "og:description", "description"),
        cover_url=_meta_content(soup, "og:image"),
        # Explicit even though it is the dataclass default: this is a Chinese-source
        # adapter and a future default change must not silently reclassify it.
        source_lang="zh",
    )


def parse_chapter_list(markup: str, url: str) -> tuple[list[ChapterRef], list[dict]]:
    """`(refs, payload entries)`. Entries are `[]` when only the HTML TOC was readable.

    Paid chapters are listed, never filtered, and titles carry no 🔒 decoration: a TOC
    whose length varied with entitlement would re-map existing `idx` values onto different
    content on the next `replace_toc`, and titles are persisted, exported and read aloud.
    """
    bid = book_id(url)
    entries = chapter_entries(markup)
    refs = [
        ChapterRef(
            index=position,
            title=str(entry.get("chapterName") or "").strip() or f"第{position + 1}章",
            # position, not `cid` and not a number parsed out of the title — web-novel
            # numbering skips and repeats, and the route means position
            url=chapter_url(bid, position + 1),
        )
        for position, entry in enumerate(entries)
    ]
    if refs:
        return refs, entries

    # Payload unreadable — fall back to the rendered TOC. Loses the free/purchased flags;
    # survivable because the paywall gate does not depend on them.
    soup = BeautifulSoup(markup, "lxml")
    seen: dict[int, str] = {}
    for anchor in soup.select("a[href]"):
        match = _HREF_RE.match(anchor["href"])
        if match and match.group(1) == bid:
            seen.setdefault(int(match.group(2)), anchor.get_text(strip=True))
    refs = [
        ChapterRef(index=position, title=seen[n] or f"第{n}章", url=chapter_url(bid, n))
        for position, n in enumerate(sorted(seen))
    ]
    if not refs:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)
    return refs, []


def is_paywalled(markup: str) -> bool:
    """True when the page carries a subscribe call-to-action.

    Scanned over the page chrome with the **content container removed** — MEASURED, and
    the hard-won part:

    * the CTA (`div.btn-login`, "登录订阅本章") sits *outside* `#article`, so scoping the
      search **to** the container would miss it and let a teaser through;
    * the story itself is *inside* `#article`, and ordinary prose contains these words all
      the time — 购买 ("buy") is an everyday verb in a wuxia novel (buying pills, herbs,
      treasures). Scanning the whole page therefore refused free chapters 26, 27 and 30 on
      sentences about villagers buying medicine.

    Removing the container is what separates the two. Length is a secondary guard only: a
    Chinese paragraph is routinely under 200 characters, so length alone cannot tell prose
    from a button and must never be the deciding signal.

    `<script>` goes too, because the Nuxt payload embeds every chapter name and the site's
    own UI strings; a bare `"订阅" in html` matches free pages as well.
    """
    soup = BeautifulSoup(markup or "", "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for container in soup.select(SEL_CONTENT):
        container.decompose()  # the story lives here; CTAs never do
    for element in soup.find_all(True):
        text = element.get_text(strip=True)
        if 0 < len(text) <= _CTA_MAX_CHARS and any(m in text for m in _PAYWALL_MARKERS):
            return True
    return False


def _paywalled(url: str) -> AuthRequiredError:
    return AuthRequiredError(
        "Chương trả phí của QQ 阅读 — tài khoản QQ của bạn chưa mua chương này. "
        "NovelTrans không vượt tường phí.",
        url,
    )


def parse_chapter(markup: str, title: str, url: str, *, entry: dict | None = None) -> str:
    """Chapter text, or `AuthRequiredError` if it is paid.

    Order matters: both gates run **before** extraction, so a teaser can never be returned
    as though it were the chapter. Saving one would be permanent and silent — a stored
    teaser satisfies `pending_download`'s `content = ''` test forever after and flows on
    into translation, TTS, video and the EPUB with nothing looking wrong.
    """
    if entry is not None and not entry.get("free") and not entry.get("purchased"):
        raise _paywalled(url)
    if is_paywalled(markup):
        raise _paywalled(url)

    soup = BeautifulSoup(markup, "lxml")
    container = soup.select_one(SEL_CONTENT)
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)
    lines = [p.get_text(strip=True) for p in container.select("p")]
    lines = [line for line in lines if line]
    # Drop at most one leading heading, and only on an exact match: a duplicated title is
    # cosmetic, an eaten opening line is invisible data loss.
    if lines and lines[0] == (title or "").strip():
        lines = lines[1:]
    if not lines:
        raise ScrapeError("Chapter content is empty", url)
    return "\n\n".join(lines)


# ------------------------------------------------------------------------ adapter


@register
class BookqqAdapter(SiteAdapter):
    name = "bookqq"
    display_name = "QQ阅读 (book.qq.com)"
    url_patterns = [r"book\.qq\.com/book-(?:detail|read)/\d+"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping
    # this would land Chinese in `translated` and mark chapters already-translated, so the
    # user's Vietnamese output would be Chinese.
    content_is_translated = False

    def __init__(self, client):
        super().__init__(client)
        # Per-instance, never module-level: the app is long-lived and the user switches
        # novels, so a shared cache would answer one novel's paywall question with
        # another's flags.
        self._detail: tuple[str, str] | None = None  # (bid, markup)
        self._entries: dict[int, dict] = {}  # 1-based position -> TOC entry

    def _detail_page(self, url: str) -> str:
        """The detail page, fetched at most once per adapter — a scan is one request."""
        bid = book_id(url)
        if self._detail is not None and self._detail[0] == bid:
            return self._detail[1]
        markup = self.client.get_html(detail_url(url))
        self._detail = (bid, markup)
        return markup

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._detail_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        refs, entries = parse_chapter_list(self._detail_page(url), url)
        self._entries = {i + 1: entry for i, entry in enumerate(entries)}
        paid = sum(1 for e in entries if not e.get("free") and not e.get("purchased"))
        if paid:
            # "tải được", not "miễn phí": a chapter the user has bought is downloadable
            # without being free, and `paid` counts only what is neither.
            self._status(
                f"🔒 {paid}/{len(refs)} chương là chương trả phí trên QQ — "
                f"chỉ tải được {len(refs) - paid} chương."
            )
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        number = chapter_number(ref.url)
        entry = self._entries.get(number)
        if entry is None:
            # Best-effort: a detail page we cannot fetch must never fail a chapter the
            # page-level gate would have let through.
            try:
                _, entries = parse_chapter_list(self._detail_page(ref.url), ref.url)
                self._entries = {i + 1: e for i, e in enumerate(entries)}
                entry = self._entries.get(number)
            except ScrapeError:
                entry = None
        if entry is not None and not entry.get("free") and not entry.get("purchased"):
            raise _paywalled(ref.url)  # refuse without spending a request
        return parse_chapter(self.client.get_html(ref.url), ref.title, ref.url, entry=entry)

"""Adapter for 提莫小說 (timotxt.com).

Landing page:  https://www.timotxt.com/<id>/          — metadata; only 12 chapter links
Full index:    https://www.timotxt.com/<id>/dir       — the complete, ordered TOC
Chapter page:  https://www.timotxt.com/<id>/<n>.html  — n is the reading position

Plain HTTP throughout: no Cloudflare challenge, no JS rendering, no cookie, and a correct
`charset=UTF-8` header, so `HttpClient` alone is enough and `get_html`'s apparent-encoding
fixup never fires. Content is Traditional Chinese (`lang="zh-cmn-Hant"`), so `source_lang`
stays "zh" — the translators handle both scripts.

**The landing page's chapter list is truncated, and worse than sto9's.** It carries a
`開始閱讀` link plus the ~12 newest chapters, **newest first**, and says nothing about being
partial — a 376-chapter novel renders as 13 perfectly normal-looking links. `ChapterRef.index`
is dense and positional and `replace_toc` preserves content across re-scans, so accepting it
would file chapter 376 under index 1 and chapter 1 under index 0, then a later good scan would
rewrite the titles while leaving the wrong bodies in place. Unlike sto9 (which keeps one safe
page-fallback branch for short novels with no LoadMore button) there is **no safe case here**:
`/dir` is a static path that exists for every novel, and the landing block is always the
12-newest excerpt whatever the length. So this adapter never parses the landing page for
chapters at all — not even as a fallback. See changes/062-STO9-SCRAPER for the original
lesson and changes/070-TIMOTXT-SCRAPER for this one.

What makes that safe without a second source to check against: chapter URLs embed the reading
position, so `chapter_numbers(refs) == [1 … N]` is a *proof* that the list is a dense prefix
starting at chapter 1 — exactly the property the positional index needs. Every realistic
degradation breaks it (losing `ul.all` leaves 365…376; a dropped middle leaves a gap), so the
landing page's stated total is an upgrade from "prefix" to "complete", never a safety
requirement — a dead landing page must not fail an otherwise perfect scan.

**`/dir` carries the recent block too, and it comes FIRST in document order.** A flat anchor
scrape returns 388 links for 376 chapters whose first 12 entries are the *last* 12 chapters
reversed. Fixing the count without fixing the order would not save it, which is why the
container is selected rather than the list de-duplicated — and why the fallback path sorts by
chapter number rather than trusting document order.

**Chapter bodies are character-obfuscated.** Roughly 2-5% of the characters in every body are
replaced with visually-similar Hangul syllables (U+AC00-U+D7A3), server-side, with a different
random subset on every request. This is not a webfont trick — there is no `@font-face`
anywhere on the page, so the corruption is in the delivered text and no browser renders it
correctly either. The *table* is fixed; only its application is randomised, which is what
makes it recoverable: two fetches of one URL return the same paragraphs at the same lengths
with different subsets substituted, so aligning them positionally reveals each mapping.
`scripts/build_timotxt_table.py` does that and is how `_SUBSTITUTIONS` below was built.

Only chapter bodies are affected — chapter titles and the description come back clean
(measured), so they must never be routed through the decoder.

The decoder can only ever under-correct: every key is a Hangul syllable, so no Chinese, Latin
or punctuation character can be touched, and an unmapped glyph survives verbatim. Its worst
case is therefore exactly the do-nothing baseline.

**But "under-corrects harmlessly" was wrong, and feature 071 is the correction.** A single
undecoded glyph is enough to ruin a chapter: the translator meets a character it cannot read
and emits something unpredictable — in the reported case a plausible-looking Han character
that appears nowhere in the source, which no later scan of the translation could catch. So
`fetch_chapter` no longer merely warns about residue. It re-fetches, up to
`_RESIDUE_REFETCH_MAX` extra draws, and merges **positionally**: because the scrambled subset
is re-randomised per response, a character garbled in one draw is very likely readable in the
next. The merge takes the best character at each position rather than the better of two
bodies, since neither draw is "the good one". A character garbled in every draw still survives
verbatim, and a chapter edited between draws is not merged at all.

That retry is cheap only because the table is good: a clean body costs exactly one request, so
the extra traffic is proportional to how incomplete `_SUBSTITUTIONS` is. Extending the table
is therefore no longer about correctness — it is the cost lever on this retry.

`_OBFUSCATION_ALARM_RATIO` fires inside `parse_chapter`, on every draw, and the first parse in
`fetch_chapter` is deliberately outside the retry's `try`. **The scenario to watch is the site
rotating to a different table** — then previously-clean characters start arriving as unmapped
Hangul. Wrapping the whole retry in one `except ScrapeError` would convert that, the loudest
failure mode there is, into a silent one. See `changes/071-TIMOTXT-RESIDUAL-CHARS`.

`translators/ads.py` (feature 069) is a separate net and neither substitutes for the other:
it runs on fresh *translator output*, targeting a promotional line whose wording is model
output, while the stripping here is structural, on DOM nodes, before anything is stored. If
timotxt ever watermarks chapters with a promo line inside `div.content` as a real <p>, this
adapter would correctly keep it as prose and `ads.py` would catch it after translation.

No `&emsp;` indents here — do not copy sto9's note about them; a comment about something this
site does not do is a trap for the next reader.

Everything below the `parsing` line is pure: markup in, values out, no network.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from noveltrans.errors import ObfuscatedContentError, ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

# Pinned rather than taken from the pasted URL: the site answers on both the bare host and
# www, and echoing whichever the user happened to paste would make one novel two projects.
ORIGIN = "https://www.timotxt.com"

_ID_RE = re.compile(r"timotxt\.com/(\d+)")
_CHAPTER_HREF_RE = re.compile(r"/(\d+)/(\d+)\.html")
_LATEST_N_RE = re.compile(r"第(\d+)章")
_BOOK_TITLE_RE = re.compile(r"《(.+?)》")
_AUTHOR_IN_TITLE_RE = re.compile(r"》\s*\((.+?)\)")
_COLONS = ":："
_WS_RE = re.compile(r"[ \t　]+")

# The real index. NOT the full `.flex.one.two-700.three-900.all` chain — that is a CSS grid
# framework's breakpoint vocabulary (one column, two at 700px, three at 900px), incidental and
# exactly what a theme refresh rewrites. `chaplist` and `all` are the only semantic tokens.
SEL_ALL_CHAPTERS = "div.chaplist ul.all a[href]"
# Anchored under #chapterWarp because `div.content` alone is far too generic to trust.
SEL_CONTENT = "#chapterWarp div.content"
SEL_CHROME = "div.gadBlock, div.adUnit, ins, script, style, iframe"
SEL_INTRO = "div.intro"

# Recovered by diffing repeat fetches — see the module docstring and
# scripts/build_timotxt_table.py. Rebuilt 2026-08-29 for feature 071 from 60 chapters
# of novel 2608569069 plus 15 each of 2302601602 and 2201601601, 2 fetches apiece:
# 140 mappings, **zero conflicts**, and all three novel pairs agree on every shared
# key (100/100, 107/107, 86/86) — so the table is site-wide, not per-novel.
#
# The 070 build missed `꿫`->`仍`, and that one gap was enough to corrupt a chapter's
# translation (see changes/071). It is here now, but the lesson is that no sample can
# prove this complete — which is why `fetch_chapter` re-fetches rather than trusting it.
_SUBSTITUTIONS = {
    "괗": "二", "굛": "十", "굜": "丁", "궝": "七", "귷": "八", "그": "人", "극": "入", "깇": "九",
    "깊": "了", "꺅": "刀", "꺆": "力", "꺗": "又", "꺘": "三", "꺛": "干", "꺱": "土", "꺲": "工",
    "꺳": "才", "꺴": "寸", "꺵": "丈", "꺶": "大", "껗": "上", "께": "小", "껙": "口", "껚": "山",
    "껛": "巾", "껜": "千", "껡": "亡", "껣": "之", "껥": "已", "껦": "弓", "껧": "己", "껩": "也",
    "꼇": "川", "꼊": "么", "꼋": "久", "꼎": "凡", "꼐": "及", "꽗": "叉", "꽬": "夫", "꽭": "天",
    "꽮": "元", "꽱": "扎", "꾉": "五", "꾊": "支", "꾦": "犬", "꾨": "尤", "꾫": "巨", "꾬": "牙",
    "꾮": "互", "꾿": "切", "꿀": "止", "꿁": "少", "꿂": "日", "꿗": "中", "꿛": "手", "꿢": "午",
    "꿤": "升", "꿦": "仁", "꿧": "片", "꿨": "化", "꿩": "仇", "꿫": "仍", "꿭": "斤", "꿮": "爪",
    "꿯": "反", "꿰": "介", "꿵": "父", "꿷": "今", "꿸": "凶", "꿹": "乏", "꿻": "氏", "뀑": "丹",
    "뀔": "勾", "뀖": "六", "뀗": "文", "뀘": "方", "뀙": "火", "뀞": "心", "뀟": "尺", "뀧": "巴",
    "뀪": "以", "뀫": "允", "뀬": "予", "냪": "幻", "냫": "玉", "냬": "末", "냭": "未", "녈": "打",
    "녉": "巧", "녊": "正", "녌": "功", "녠": "甘", "녡": "世", "녢": "古", "녤": "本", "녦": "可",
    "녨": "左", "녪": "石", "녿": "右", "놀": "布", "놂": "平", "놅": "的", "놆": "是", "놇": "在",
    "놊": "不", "놋": "有", "놌": "和", "놖": "我", "놘": "由", "놙": "只", "놚": "要", "놛": "他",
    "뇽": "叫", "뇾": "用", "눁": "四", "눂": "失", "눃": "生", "누": "到", "눑": "代", "눒": "作",
    "눓": "地", "눕": "出", "늀": "就", "늁": "分", "늂": "乎", "늄": "令", "늅": "成", "늉": "句",
    "늌": "外", "늵": "包", "덿": "主", "뎀": "市", "뎃": "年", "돗": "它", "땡": "百", "땢": "同",
    "땣": "能", "땤": "而", "떘": "下", "떚": "子",
}

_DEOBFUSCATE = str.maketrans(_SUBSTITUTIONS)
_HANGUL_RE = re.compile(r"[가-힣]")
# Measured residue is 1.8-4.8% of a body, so this is ~4x the worst observation. Below it the
# table is merely incomplete and the chapter is still worth saving; at or above, the scheme
# itself has changed and the text cannot be trusted.
_OBFUSCATION_ALARM_RATIO = 0.20
# Extra draws allowed when a body comes back with characters the table did not know. Draw 2
# clears the large majority of residue and draw 3 all but the rest; a fourth buys nothing
# measurable and would turn a pathological chapter (or one legitimately containing Korean)
# into a 4x request cost. See `TimotxtAdapter.fetch_chapter`.
_RESIDUE_REFETCH_MAX = 2


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


# --------------------------------------------------------------------------- urls


def book_id(url: str) -> str:
    """The numeric novel id, from any of the three URL forms."""
    match = _ID_RE.search(url or "")
    if not match:
        raise ScrapeError("Could not extract book id from URL", url)
    return match.group(1)


def read_url(url: str) -> str:
    """The canonical URL for a novel — what `NovelMeta.url` is set to.

    Never the pasted string: `Library.find_by_url` is exact string equality, so echoing
    would let the landing page, `/dir` and a chapter URL become three projects for one
    novel, each with its own translation progress and video settings.
    """
    return f"{ORIGIN}/{book_id(url)}/"


def dir_url(url: str) -> str:
    """The complete chapter index — the only page this adapter reads chapters from."""
    return f"{ORIGIN}/{book_id(url)}/dir"


def chapter_url(bid: str, number: int) -> str:
    return f"{ORIGIN}/{bid}/{number}.html"


# ------------------------------------------------------------------------ parsing


def deobfuscate(text: str) -> str:
    """Undo the site's Hangul-for-Han substitution. Pure, total, idempotent.

    Only Hangul syllables are keys, so this cannot alter a Chinese, Latin, digit or
    punctuation character no matter how incomplete the table is — it under-corrects or it
    does nothing. Substitution is strictly 1:1, so length and paragraph structure survive,
    which downstream TTS chunking and the exporters both depend on.
    """
    return text.translate(_DEOBFUSCATE)


def residual_hangul(text: str) -> int:
    """How many substituted characters the table did not know. 0 on clean text."""
    return len(_HANGUL_RE.findall(text))


def needs_refetch(text: str) -> bool:
    """True when stored text still carries characters the table could not decode.

    One is enough: feature 071 was reported because a single undecoded glyph reached the
    translator, which emitted a plausible-looking Han character that appears nowhere in the
    source. There is no "acceptably small" amount of residue.
    """
    return residual_hangul(text) > 0


def bodies_align(a: str, b: str) -> bool:
    """True when two draws of one chapter are the same text with different scrambling.

    The substitution is strictly 1:1, so equal paragraph counts AND equal per-paragraph
    lengths is what "same text" means here. Anything else means the chapter was edited
    between the two requests, and any alignment would be fiction.
    """
    pa, pb = a.split("\n\n"), b.split("\n\n")
    return len(pa) == len(pb) and all(len(x) == len(y) for x, y in zip(pa, pb))


def merge_bodies(a: str, b: str) -> str:
    """Take the readable character at EACH position — not the better of two bodies.

    The two draws scramble different random subsets, so neither is "the good one": `a` may
    be clean where `b` is garbled and vice versa, which is exactly why this is positional.

    Only ever replaces a residual Hangul syllable in `a` with a non-Hangul character from
    `b` — the same under-correct-only asymmetry `deobfuscate` guarantees, so a character
    already readable in `a` can never be overwritten however wrong `b` is.

    The replacement is unambiguous rather than a guess: the table is fixed, so a given
    source character always becomes the SAME Hangul key. After `deobfuscate`, `b[i]` at a
    position where `a[i]` is residual Hangul is therefore either that same unmapped Hangul
    or the true character — there is no third case.

    Returns `a` unchanged when the two do not align. Alignment by paragraph implies equal
    total length, so the character-wise zip below is safe and the blank lines line up.
    """
    if not bodies_align(a, b):
        return a
    return "".join(
        y if _HANGUL_RE.match(x) and not _HANGUL_RE.match(y) else x for x, y in zip(a, b)
    )


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the landing page's OpenGraph tags, falling back to the visible book box."""
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        # This site declares its OpenGraph tags with `name=` rather than the standard
        # `property=`. Both are accepted so that fixing their markup cannot break us.
        el = soup.select_one(f"meta[name='{prop}']") or soup.select_one(f"meta[property='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    page_title = soup.title.get_text(strip=True) if soup.title else ""

    title = og("og:novel:book_name") or og("og:title")
    if not title:
        heading = soup.select_one("h1.title")
        title = _norm(heading.get_text()) if heading else ""
    if not title:
        match = _BOOK_TITLE_RE.search(page_title)
        title = match.group(1).strip() if match else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = og("og:novel:author")
    if not author:
        heading = soup.select_one("h2.title")
        if heading:
            author = _norm(heading.get_text()).split("/")[-1].strip()
    if not author:
        for row in soup.select("p, div"):
            text = _norm(row.get_text(" "))
            if text.startswith("作者") and len(text) < 40:
                author = re.split(f"[{_COLONS}/]", text, maxsplit=1)[-1].strip()
                break
    if not author:
        match = _AUTHOR_IN_TITLE_RE.search(page_title)
        author = match.group(1).strip() if match else ""

    # The visible blurb, NOT og:description / meta[name=description] — those are SEO copy
    # truncated at ~100 characters with a trailing ellipsis, so the meta tag is strictly
    # worse here. This is the one field where the rendered markup beats the metadata.
    intro = soup.select_one(SEL_INTRO)
    description = _norm(intro.get_text(" ")) if intro else og("og:description")

    cover = og("og:image")
    if not cover:
        img = soup.select_one("div.bookimg img[src]")
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
    """How many chapters the landing page claims, or None if it does not say.

    Two independent signals, because either tag alone is one markup tweak from vanishing.
    """
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        el = soup.select_one(f"meta[name='{prop}']") or soup.select_one(f"meta[property='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    match = _CHAPTER_HREF_RE.search(og("og:novel:latest_chapter_url"))
    if match:
        return int(match.group(2))
    match = _LATEST_N_RE.search(og("og:novel:latest_chapter_name"))
    return int(match.group(1)) if match else None


def parse_chapter_list(markup: str, url: str) -> list[ChapterRef]:
    """Every chapter on the `/dir` page, in reading order.

    Prefers the real index container. The fallback de-duplicates a flat anchor scrape by
    chapter number and **sorts by that number rather than document order** — the recent block
    sits first on the page and runs backwards, so trusting document order would put the last
    12 chapters at the front. Both paths are then checked for contiguity by the caller, which
    is what makes the fallback safe rather than a hole in the no-fallback policy.
    """
    soup = BeautifulSoup(markup, "lxml")
    anchors = soup.select(SEL_ALL_CHAPTERS)
    if not anchors:
        anchors = soup.select("a[href]")

    by_number: dict[int, ChapterRef] = {}
    for anchor in anchors:
        match = _CHAPTER_HREF_RE.search(anchor.get("href") or "")
        if not match:
            continue
        number = int(match.group(2))
        title = _norm(anchor.get_text())
        if not title:
            continue
        by_number.setdefault(
            number,
            ChapterRef(index=0, title=title, url=chapter_url(match.group(1), number)),
        )
    if not by_number:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    return [
        ChapterRef(index=i, title=ref.title, url=ref.url)
        for i, (_n, ref) in enumerate(sorted(by_number.items()))
    ]


def chapter_numbers(refs: list[ChapterRef]) -> list[int]:
    """The reading position each ref points at, read back off its URL."""
    numbers = []
    for ref in refs:
        match = _CHAPTER_HREF_RE.search(ref.url)
        if match:
            numbers.append(int(match.group(2)))
    return numbers


def parse_chapter(markup: str, title: str, url: str) -> str:
    """The chapter body as plain text, decoded, paragraphs separated by blank lines."""
    soup = BeautifulSoup(markup, "lxml")
    container = soup.select_one(SEL_CONTENT)
    if container is None:
        raise ScrapeError("Chapter content not found — page layout may have changed", url)

    for junk in container.select(SEL_CHROME):
        junk.decompose()

    # Direct children only. On this markup "a direct-child <p> of div.content" IS the
    # definition of a prose paragraph (117/117 measured), and it is the only rule that
    # excludes the site's unclassed 溫馨提示 notice <div> — no class-based rule can name it.
    lines = [
        t for t in (p.get_text(strip=True) for p in container.find_all("p", recursive=False)) if t
    ]
    if not lines:
        # The CMS nested the prose one level deeper. Falling back costs at most one junk
        # notice line; not falling back costs the whole chapter.
        lines = [t for t in (p.get_text(strip=True) for p in container.select("p")) if t]

    # At most one: a body that legitimately opens by repeating its own title should keep the
    # second copy. The h1 lives outside div.content so this normally never fires.
    if lines and title and _norm(lines[0]) == _norm(title):
        lines = lines[1:]

    body = deobfuscate("\n\n".join(lines).strip())
    if not body:
        raise ScrapeError("Chapter content is empty", url)

    residue = residual_hangul(body)
    if residue and residue / len(body) >= _OBFUSCATION_ALARM_RATIO:
        raise ObfuscatedContentError(
            "timotxt trả về nội dung bị mã hoá mà ứng dụng chưa giải được — "
            "trang có thể đã đổi bảng thay ký tự.",
            url,
        )
    return body


@register
class TimotxtAdapter(SiteAdapter):
    name = "timotxt"
    display_name = "提莫小說 (timotxt.com)"
    # Host-anchored on purpose. A bare chapter pattern would collide with several other
    # adapters' chapter URLs and whichever imported first would silently steal them — the
    # same hazard sto9.py and twkan.py both document. The trailing guard keeps the image
    # host (i1.timotxt.com/thumb/...) out.
    url_patterns = [r"timotxt\.com/\d+(?:/|$)"]
    # Chinese source: fetch_chapter returns the original, tab 2 translates it. Flipping this
    # would land Chinese in `translated` and mark chapters already-translated.
    content_is_translated = False

    def __init__(self, client):
        super().__init__(client)
        # Per-instance, never module-level: the app is long-lived and the user switches
        # novels, so a shared cache would answer one novel's questions with another's page.
        self._landing: tuple[str, str] | None = None  # (bid, markup)

    def _landing_page(self, url: str) -> str:
        bid = book_id(url)
        if self._landing is not None and self._landing[0] == bid:
            return self._landing[1]
        markup = self.client.get_html(read_url(url))
        self._landing = (bid, markup)
        return markup

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._landing_page(url), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        refs = parse_chapter_list(self.client.get_html(dir_url(url)), dir_url(url))
        numbers = chapter_numbers(refs)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ScrapeError(
                f"Danh sách chương của timotxt không liền mạch từ chương 1 "
                f"({len(numbers)} mục, cao nhất {max(numbers) if numbers else 0}). "
                "Thử quét lại sau.",
                dir_url(url),
            )

        # A cross-check, not a safety requirement — the contiguity above already proves the
        # list is a dense prefix. So a dead landing page must not fail a good /dir scan.
        try:
            total = stated_total(self._landing_page(url))
        except ScrapeError:
            total = None
        if total is not None and len(refs) < total:
            # Still usable: a contiguous run from chapter 1 is a prefix, so a later re-scan
            # simply extends it and nothing can be misfiled. But never silently.
            self._status(
                f"⚠️ timotxt chỉ trả về {len(refs)}/{total} chương. Quét lại sau để lấy đủ."
            )
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        """The chapter body, re-fetching to recover anything the table could not decode.

        The first parse is deliberately OUTSIDE the try below: a wholesale table rotation
        raises `ObfuscatedContentError` from `parse_chapter` on draw 1 and must propagate,
        not be swallowed by the retry. Do not wrap this in one big `except ScrapeError` —
        that would turn the loudest failure mode into a silent one.
        """
        body = parse_chapter(self.client.get_html(ref.url), ref.title, ref.url)
        residue = residual_hangul(body)
        if not residue:
            return body  # the normal path: exactly one request

        for _attempt in range(_RESIDUE_REFETCH_MAX):
            self._status(
                f"↻ timotxt: {residue} ký tự chưa giải mã trong “{ref.title}” — tải lại…"
            )
            try:
                other = parse_chapter(self.client.get_html(ref.url), ref.title, ref.url)
            except ScrapeError:
                break  # a failed retry must never lose the good draw we already hold
            if not bodies_align(body, other):
                # Edited between requests. Don't guess-align, and don't spend a third
                # request: the accumulator is anchored on draw 1, so every later draw
                # misaligns against it too.
                break
            body = merge_bodies(body, other)
            residue = residual_hangul(body)
            if not residue:
                return body

        self._status(
            f"⚠️ timotxt: {residue} ký tự chưa giải mã được trong “{ref.title}”."
        )
        return body

"""Adapter for webtruyendich.com — Chinese web novels served as Vietnamese MTL.

Landing page: https://webtruyendich.com/truyen/<slug>              — metadata (open)
Chapter list: https://webtruyendich.com/truyen/<slug>/danh-sach-chuong-day-du
Chapter page: https://webtruyendich.com/truyen/<slug>/<source>/chuong-<N>-<title>
              (<source> is the upstream site — "fanqie", "sudugu", … — per novel)

Two things make this adapter unlike the plain `requests` scrapers:

  * The whole site sits behind a Cloudflare challenge, so every page comes through
    a real browser (`webtruyendich_browser.WtdBrowserSession`), like 69shuba.
  * The chapter text is produced on-page by an AI translation the reader selects
    from a dropdown. This adapter drives that: it ranks the models the dropdown
    actually offers (rank_translator_models), clicks "Dịch lại", and reads the
    rendered Vietnamese, falling to the next model when one answers with an error
    instead of a translation. Because that text is already the translation, the
    adapter is flagged `content_is_translated` — the download worker stores it as
    the chapter's `translated` text and skips NovelTrans's own translators.

    The model is never pinned by name: the site rotates its lineup (it retired
    "AI Gemini FLASH 3.5 - Memory - No Apikey" for 3.6/3.7), and a pinned name
    fails as a select_option timeout blamed on Cloudflare.

As with 69shuba, fetching is split from parsing: everything above the adapter
class is pure and takes markup, so the whole parsing surface is tested against
saved fixtures without launching a browser.

**Do not parallelise downloads.** One Chrome, and this site is Cloudflare- and
AI-quota-gated — hammering it risks a block that would kill the only path it has.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.browser import BrowserUnavailableError
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter
from noveltrans.webtruyendich_browser import WtdBrowserSession, WtdBrowserSessionError

_ORIGIN = "https://webtruyendich.com"

# The on-page AI model is *chosen from the live dropdown*, never pinned to a name.
# The site rotates its lineup — it shipped "AI Gemini FLASH 3.5 - Memory - No
# Apikey", then dropped it for 3.6/3.7 — and a pinned name turns into a 60s
# select_option timeout with a misleading "Cloudflare/quota" message the moment it
# is retired. choose_translator_model() ranks whatever is actually on offer.
#
# A model qualifies only if its label carries both markers:
#   * "No Apikey" — the reader supplies no Gemini key, so keyed models never run.
#   * "Memory"    — carries context across the chapter, which keeps names stable.
# That also excludes "Vietphrase", the site's non-AI dictionary default.
MODEL_REQUIRED_MARKERS = ("no apikey", "memory")
# Recorded in Chapter.translator until a chapter picks a concrete model.
TRANSLATOR_LABEL = "webtruyendich (Gemini Flash)"

_MODEL_VERSION_RE = re.compile(r"(\d+(?:\.\d+)?)")
_LITE_RE = re.compile(r"\blite\b", re.IGNORECASE)
# "AI Gemini FLASH 3.7 - Memory - No Apikey" -> "Gemini FLASH 3.7" for the label.
_MODEL_AI_PREFIX_RE = re.compile(r"^\s*AI\s+", re.IGNORECASE)
_MODEL_FLAGS_RE = re.compile(r"\s*-\s*(?:Memory|No\s*Apikey|Custom\s*Prompt)\s*", re.IGNORECASE)
# A provider error body streamed into the chapter instead of a translation. Both
# halves are JSON shapes that Vietnamese prose never contains, so a false positive
# would take a chapter literally quoting an API response.
_API_ERROR_RE = re.compile(r'"error"\s*:\s*\{|"status"\s*:\s*"[A-Z][A-Z_]+"')

SEL_TRANSLATOR = "#translator"
SEL_CONTENT = "#chapter-content-body"
SEL_PARAGRAPH = "p.fade-in-paragraph"
RETRANSLATE_BUTTON = "Dịch lại"

# Chapter URLs are /truyen/<slug>/<source>/chuong-<N>-<title-slug>, where <source>
# names the upstream site the novel was ripped from — "fanqie" for some, "sudugu"
# for others. It is NOT fixed: pinning it to "fanqie" made every non-fanqie novel
# fail as "Chapter list not found" (measured on /truyen/dong-kinh-y-do, source
# "sudugu"), so match any single segment there.
SEL_TOC_LINKS = 'a[href*="/chuong-"]'
_CH_HREF_RE = r'[^"]*/truyen/[^"/]+/[^"/]+/chuong-[^"]*'
# Leading chapter number, the normal shape: "chuong-454-muon-vao-bo".
_CH_LEAD_NUM_RE = re.compile(r"(\d+)(?:[-/]|$)")
_ANY_NUM_RE = re.compile(r"\d+")
# Chapter anchor: href + inner title. Parsed by regex, not an HTML parser — the
# raw TOC document is malformed enough that lxml/html.parser drop the title text
# of all but the first ~135 chapters (see parse_chapter_list).
_CH_ANCHOR_RE = re.compile(
    rf'<a\b[^>]*?\bhref="({_CH_HREF_RE})"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"webtruyendich\.com/truyen/([a-z0-9-]+)")
# The <h1> reads "Truyện <title>"; drop that leading label.
_TITLE_PREFIX_RE = re.compile(r"^\s*Truyện\s+")
# og:title is decorated: "[Mới nhất] <title> - Đọc Truyện Dịch Chuẩn Online | webtruyendich.com".
_OG_TITLE_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_OG_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*Đọc Truyện.*$")


def slug(url: str) -> str:
    match = _SLUG_RE.search(url)
    if not match:
        raise ScrapeError("Could not extract novel slug from URL", url)
    return match.group(1)


def landing_url(url: str) -> str:
    return f"{_ORIGIN}/truyen/{slug(url)}"


def toc_url(url: str) -> str:
    return f"{landing_url(url)}/danh-sach-chuong-day-du"


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Read the landing page. Prefers the clean <h1> title over the decorated
    og:title; author comes from the /tac-gia/ link (there's no og:novel:author).

    `url` is echoed into NovelMeta.url unchanged — the library keys projects off
    the URL the user gave, so rewriting it here would orphan them.
    """
    soup = BeautifulSoup(markup, "lxml")

    def og(prop: str) -> str:
        el = soup.select_one(f"meta[property='{prop}']")
        return (el.get("content") or "").strip() if el else ""

    h1 = soup.select_one("h1")
    title = _TITLE_PREFIX_RE.sub("", h1.get_text(strip=True)).strip() if h1 else ""
    if not title:
        og_title = og("og:title")
        title = _OG_TITLE_SUFFIX_RE.sub("", _OG_TITLE_PREFIX_RE.sub("", og_title)).strip()
    if not title:
        el = soup.select_one("title")
        title = el.get_text(strip=True) if el else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author_el = soup.select_one("a[href^='/tac-gia/']")
    author = author_el.get_text(strip=True) if author_el else ""

    return NovelMeta(
        url=url,
        site=site,
        title=title,
        author=author,
        description=og("og:description"),
        cover_url=og("og:image"),
        source_lang="vi",  # content arrives already-Vietnamese
    )


def rank_translator_models(options: Sequence[str], url: str = "") -> list[str]:
    """Rank the chapter page's live <select> into models to try, best first.

    Qualifying options (see MODEL_REQUIRED_MARKERS) are ordered: full models before
    LITE ones, then the highest version number, ties broken by the site's own
    order. So a lineup of 3.6/3.7/3.5-LITE tries 3.7 first, and the ranking keeps
    working when the site renumbers again.

    A *list* rather than one pick because a model can be up but overloaded: the
    site hands back Gemini's 503 "high demand" body mid-stream (see
    looks_like_api_error), and the next model down usually answers fine — measured,
    3.7 was refusing while 3.6 translated the same chapter in full.

    Raises ScrapeError naming what *was* on offer if nothing qualifies — the
    alternative is select_option blocking for its full timeout and reporting a
    Cloudflare problem that isn't happening.
    """
    usable = [
        option
        for option in options
        if all(marker in option.lower() for marker in MODEL_REQUIRED_MARKERS)
    ]
    if not usable:
        offered = ", ".join(options) or "(none)"
        raise ScrapeError(
            "Trang webtruyendich không còn mô hình AI nào dùng được (cần loại "
            f"'Memory' + 'No Apikey'). Các lựa chọn hiện có: {offered}",
            url,
        )
    # Stable sort on the negated rank keeps equal-ranked models in the site's order.
    return sorted(usable, key=lambda option: tuple(-part for part in _model_rank(option)))


def looks_like_api_error(text: str) -> bool:
    """True if the rendered "translation" is really a provider error body.

    When the upstream model is overloaded or out of quota, the site streams the raw
    JSON straight into the chapter — e.g. a paragraph that breaks off mid-sentence
    and continues `{"error": {"code": 503, ... "status": "UNAVAILABLE"}}`. It looks
    like ordinary content to a length check, so without this guard a truncated
    chapter with an error blob glued to it gets saved as the finished translation.
    """
    return bool(_API_ERROR_RE.search(text))


def _model_rank(option: str) -> tuple[int, float]:
    version = _MODEL_VERSION_RE.search(option)
    return (0 if _LITE_RE.search(option) else 1, float(version.group(1)) if version else 0.0)


def translator_label_for(model: str) -> str:
    """Chapter.translator text for a chosen model, e.g. 'webtruyendich (Gemini FLASH 3.7)'."""
    name = _WS_RE.sub(" ", _MODEL_FLAGS_RE.sub(" ", _MODEL_AI_PREFIX_RE.sub("", model))).strip()
    return f"webtruyendich ({name})" if name else TRANSLATOR_LABEL


def chapter_number(href: str) -> int | None:
    """Chapter number out of a chapter href, or None if it carries no digits.

    Normally the number leads the slug ("chuong-454-muon-vao-bo"), but a few are
    named without one — "chuong-lau-don-chuong-1715" (a chương lậu đơn, i.e. a
    fill-in for a missed chapter) — so fall back to the last number in the slug.
    Sorting such a chapter next to its namesake beats dropping it from the list."""
    _, _, chapter_slug = href.rpartition("/chuong-")
    if not chapter_slug:
        return None
    lead = _CH_LEAD_NUM_RE.match(chapter_slug)
    if lead:
        return int(lead.group(1))
    numbers = _ANY_NUM_RE.findall(chapter_slug)
    return int(numbers[-1]) if numbers else None


def parse_chapter_list(markup: str, base_url: str) -> list[ChapterRef]:
    """Read the full TOC. Links are newest-first and a chapter number can
    occasionally carry two different title-slugs, so dedup by URL and order by
    chapter number (falling back to first-seen order for equal numbers).

    Uses a regex over the raw markup rather than an HTML parser on purpose: the
    server-rendered TOC document (read whole to avoid scrolling the JS-virtualized
    list) is malformed enough that lxml and html.parser both drop the title text of
    all but the first ~135 chapters. The regex recovers every title, and works
    equally on the well-formed browser DOM used as the fallback."""
    seen: set[str] = set()
    entries: list[tuple[int, int, str, str]] = []  # (num, order, title, url)
    for match in _CH_ANCHOR_RE.finditer(markup):
        href = match.group(1)
        num = chapter_number(href)
        if num is None:
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        title = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", match.group(2)))).strip()
        entries.append((num, len(entries), title, absolute))

    if not entries:
        raise ScrapeError("Chapter list not found — page layout may have changed", base_url)

    entries.sort(key=lambda e: (e[0], e[1]))
    return [ChapterRef(index=i, title=title, url=url) for i, (_n, _o, title, url) in enumerate(entries)]


def parse_chapter(container_html: str, url: str) -> str:
    """Return the chapter body as paragraphs separated by blank lines.

    Each AI paragraph is `<p class="fade-in-paragraph has-anchor">` and opens with
    an `<a class="citation-anchor">#</a>` link; drop that anchor so its "#" doesn't
    leak into the text. A stray CJK char occasionally survives the AI output — it's
    left as-is (tolerated, not an error)."""
    soup = BeautifulSoup(container_html, "lxml")
    paragraphs = soup.select(SEL_PARAGRAPH) or soup.find_all("p")
    lines: list[str] = []
    for p in paragraphs:
        for anchor in p.select("a.citation-anchor"):
            anchor.decompose()
        text = p.get_text(" ", strip=True).strip()
        if text:
            lines.append(text)
    if not lines:
        raise ScrapeError("Chapter content is empty", url)
    text = "\n\n".join(lines)
    # Last gate before the worker stores this as the finished translation.
    if looks_like_api_error(text):
        raise ScrapeError(
            "Mô hình AI của webtruyendich trả về lỗi thay vì bản dịch (quá tải hoặc "
            "hết hạn mức) — chương chưa được dịch xong. Thử lại sau.",
            url,
        )
    return text


@register
class WebtruyendichAdapter(SiteAdapter):
    """webtruyendich.com. Fetches through a browser and drives the on-page AI
    translation; everything above this class does the parsing."""

    name = "webtruyendich"
    display_name = "Web Truyện Dịch (webtruyendich.com)"
    url_patterns = [r"webtruyendich\.com/truyen/[a-z0-9-]+"]

    content_is_translated = True
    translated_lang = "vi"
    translator_label = TRANSLATOR_LABEL

    def __init__(self, client, *, headless: bool = False):
        super().__init__(client)
        # Headless is fingerprinted by Cloudflare and does not clear the challenge
        # (measured — see cf_browser). The flag exists in case that ever changes.
        self._headless = headless
        self._session: WtdBrowserSession | None = None

    # -- fetching: the only part that touches a browser ---------------------------

    def _ensure_session(self) -> WtdBrowserSession:
        if self._session is None:
            # A Chrome window is about to appear on the user's screen — say why.
            self._status(
                "🌐 Đang mở trình duyệt để đọc webtruyendich (vượt Cloudflare + dịch AI) "
                "— giữ cửa sổ mở…"
            )
            self._session = WtdBrowserSession(
                headless=self._headless,
                delay_seconds=self.client.delay_seconds,
            )
        return self._session

    def _get_html(
        self, url: str, *, prefer_document: bool = False, scroll_item_selector: str | None = None
    ) -> str:
        try:
            return self._ensure_session().get_html(
                url, prefer_document=prefer_document, scroll_item_selector=scroll_item_selector
            )
        except BrowserUnavailableError as exc:
            raise self._browser_needed_error(url) from exc
        except WtdBrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc được trang webtruyendich — trình duyệt bị đóng hoặc không "
                f"vượt được kiểm tra Cloudflare. Thử lại. ({exc})",
                url,
            ) from exc

    def _browser_needed_error(self, url: str) -> ScrapeError:
        return ScrapeError(
            "Cần trình duyệt để đọc webtruyendich (trang có kiểm tra Cloudflare và dịch "
            "AI trên trang). Cài Google Chrome, hoặc chạy:  pip install "
            "'noveltrans[browser]' && playwright install chromium",
            url,
        )

    def close(self) -> None:
        """Release the browser. Idempotent; never raises."""
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- SiteAdapter ---------------------------------------------------------------

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._get_html(landing_url(url)), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        # The TOC is fully server-rendered but JS virtualizes the live DOM down to
        # ~135 rows. Read the raw document (all chapters, instant); if that response
        # is a Cloudflare challenge, fall back to scrolling the rebuilt DOM.
        markup = self._get_html(
            toc_url(url), prefer_document=True, scroll_item_selector=SEL_TOC_LINKS
        )
        return parse_chapter_list(markup, url)

    def fetch_chapter(self, ref: ChapterRef) -> str:
        try:
            container_html, model = self._ensure_session().read_translated_chapter(
                ref.url,
                translator_select=SEL_TRANSLATOR,
                rank_translators=lambda options: rank_translator_models(options, ref.url),
                is_usable_output=lambda text: not looks_like_api_error(text),
                retranslate_button=RETRANSLATE_BUTTON,
                content_selector=SEL_CONTENT,
                paragraph_selector=SEL_PARAGRAPH,
            )
            # Shadow the class default so Chapter.translator records the model that
            # actually produced this text, not whichever one the site offered first.
            self.translator_label = translator_label_for(model)
        except BrowserUnavailableError as exc:
            raise self._browser_needed_error(ref.url) from exc
        except WtdBrowserSessionError as exc:
            raise ScrapeError(
                "Không đọc/dịch được chương webtruyendich — có thể bị Cloudflare "
                f"Turnstile chặn hoặc hết hạn mức AI. Thử lại. ({exc})",
                ref.url,
            ) from exc
        return parse_chapter(container_html, ref.url)

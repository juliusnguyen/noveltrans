"""Adapter for MeTruyenChu (metruyenchu.com.vn) — Vietnamese novel reader.

Novel page:   https://metruyenchu.com.vn/<slug>
Chapter page: https://metruyenchu.com.vn/<slug>/chuong-<nn>-<hash>
TOC pages:    GET https://metruyenchu.com.vn/get/listchap/<rid>?page=<n>  -> {"data": "<li>…</li>"}

The landing page carries the first 100 TOC entries inline inside `#chapter-list`,
plus a numeric `rid` in an inline script. Later TOC pages come from the JSON endpoint
above. Chapter slugs are not reliable (some are `chuong-chuong-301-…`, others
`chuong-tiep-…`), so chapter numbers are read from the link text (`Chương NNN:`).

Chapter bodies are `<p>` elements inside `div.truyen`. The last chapter is behind a
manual verification gate (a third-party keyword/code step). The adapter detects it and
raises a ScrapeError asking the user to finish the step in a browser. It never calls the
verification service or forges its cookie, so the batch marks that one chapter failed
and continues.

Content is already Vietnamese, so novels carry source_lang="vi" and are not translated
by the site.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.cf_browser import looks_like_challenge
from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

_ORIGIN = "https://metruyenchu.com.vn"
_SLUG_RE = re.compile(r"metruyenchu\.com\.vn/([a-z0-9-]+)")
_RID_RE = re.compile(r"var\s+rid\s*=\s*'(\d+)'")
_CH_NUM_RE = re.compile(r"^\s*Chương\s+(\d+)\s*:?")
# Markers of the final-chapter verification gate. Only trusted when the chapter
# body is also empty, so a normal chapter that happens to mention them is fine.
_GATE_MARKERS = ('id="formcode"', 'id="form-code"', "control-last")
_MAX_TOC_PAGES = 500  # sanity cap in case the JSON endpoint never runs dry
_GATE_MESSAGE = (
    "Chương này yêu cầu xác minh thủ công trên trang web (bước nhập mã sau khi tìm từ "
    "khóa). Mở chương trên trình duyệt, hoàn tất bước xác minh, rồi tải lại."
)


def _clean(text: str) -> str:
    """Collapse whitespace and NFC-normalize Vietnamese text."""
    return unicodedata.normalize("NFC", " ".join(text.split()))


@register
class MetruyenchuAdapter(SiteAdapter):
    name = "metruyenchu"
    display_name = "MeTruyenChu (metruyenchu.com.vn)"
    url_patterns = [r"metruyenchu\.com\.vn/[a-z0-9-]+"]

    content_is_translated = False

    def _novel_url(self, url: str) -> str:
        m = _SLUG_RE.search(url)
        if not m:
            raise ScrapeError(f"Not a metruyenchu.com.vn novel URL: {url}", url)
        return f"{_ORIGIN}/{m.group(1)}"

    def fetch_metadata(self, url: str) -> NovelMeta:
        landing = self._novel_url(url)
        soup = BeautifulSoup(self.client.get_html(landing), "lxml")

        def meta(attr: str, value: str) -> str:
            tag = soup.find("meta", attrs={attr: value})
            return _clean(tag.get("content", "")) if tag else ""

        h1 = soup.select_one("h1[itemprop=name]")
        title = _clean(h1.get_text(" ", strip=True)) if h1 else ""
        if not title:
            raise ScrapeError("Không tìm thấy tên truyện", landing)
        author_tag = soup.select_one("a[itemprop=author]")
        author = _clean(author_tag.get_text(" ", strip=True)) if author_tag else ""

        return NovelMeta(
            url=landing,
            site=self.name,
            title=title,
            author=author,
            description=meta("name", "description"),
            cover_url=meta("property", "og:image"),
            source_lang="vi",
        )

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        landing = self._novel_url(url)
        landing_html = self.client.get_html(landing)
        soup = BeautifulSoup(landing_html, "lxml")
        if soup.select_one("#chapter-list") is None:
            raise ScrapeError("Không tìm thấy danh sách chương", landing)

        found: dict[int, ChapterRef] = {}
        self._collect(soup, found)

        rid_match = _RID_RE.search(landing_html)
        if rid_match:
            rid = rid_match.group(1)
            for page in range(2, _MAX_TOC_PAGES + 1):
                fragment = self._fetch_toc_page(rid, page, landing)
                if self._collect(BeautifulSoup(fragment, "lxml"), found) == 0:
                    break  # first empty page: the TOC has ended

        if not found:
            raise ScrapeError("Danh sách chương trống", landing)

        numbers = sorted(found)
        if numbers != list(range(1, len(numbers) + 1)):
            raise ScrapeError(
                f"Danh sách chương có khoảng trống (có {len(numbers)} chương, "
                f"số lớn nhất {numbers[-1]}); không thể tải đầy đủ",
                landing,
            )
        return [
            ChapterRef(index=i, title=found[n].title, url=found[n].url)
            for i, n in enumerate(numbers)
        ]

    def _fetch_toc_page(self, rid: str, page: int, referer: str) -> str:
        response = self.client.get(
            f"{_ORIGIN}/get/listchap/{rid}?page={page}",
            headers={"Referer": referer},
        )
        try:
            data = response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ScrapeError(f"Phản hồi danh sách chương không hợp lệ: {exc}", referer) from exc
        return data or ""

    @staticmethod
    def _collect(soup: BeautifulSoup, found: dict[int, ChapterRef]) -> int:
        """Add numbered chapter links from `soup` to `found`; return how many were new.

        Links live in `#chapter-list` on the landing page and as bare `<li>` fragments
        in the JSON pages, so the selector accepts both. The first link seen for a
        chapter number wins, which drops duplicates.
        """
        added = 0
        scope = soup.select_one("#chapter-list") or soup
        for a in scope.select('a[href*="/chuong-"]'):
            text = _clean(a.get_text(" ", strip=True))
            m = _CH_NUM_RE.match(text)
            if not m:
                continue
            number = int(m.group(1))
            if number in found:
                continue
            found[number] = ChapterRef(
                index=0,  # reassigned after sorting in fetch_chapter_list
                title=text,
                url=urljoin(_ORIGIN, a["href"]),
            )
            added += 1
        return added

    def fetch_chapter(self, ref: ChapterRef) -> str:
        html = self.client.get_html(ref.url)
        if looks_like_challenge(html):
            raise ScrapeError(
                "Trang đang bị chặn bởi Cloudflare; thử lại sau", ref.url
            )
        soup = BeautifulSoup(html, "lxml")
        lines = [
            _clean(p.get_text(" ", strip=True).replace("\xa0", " "))
            for p in soup.select("div.truyen p")
        ]
        lines = [line for line in lines if line]
        if not lines:
            if any(marker in html for marker in _GATE_MARKERS):
                raise ScrapeError(_GATE_MESSAGE, ref.url)
            raise ScrapeError("Không tìm thấy nội dung chương", ref.url)
        return "\n\n".join(lines)

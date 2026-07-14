"""Adapter for Mê Đọc Truyện (medoctruyen.vn) — Vietnamese novel aggregator.

Novel page:   https://medoctruyen.vn/<slug>
TOC pages:    https://medoctruyen.vn/<slug>?ch=<n>   (~24 chapters each)
Chapter page: https://medoctruyen.vn/<slug>/chuong-<n>

Next.js App Router site. Metadata (OpenGraph + schema.org JSON-LD) and the
chapter list are readable anonymously, but full chapter bodies are gated behind
login: an anonymous chapter fetch returns only a short preview plus a
`data-preview-gate` overlay. The scraper attaches the user's session cookie
(set via HttpClient.set_cookies) to read full bodies, and raises
AuthRequiredError when it only gets a preview.

Content is already Vietnamese (Hán-Việt), so novels carry source_lang="vi".
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from noveltrans.errors import (
    AuthRequiredError,
    DailyLimitError,
    RateLimitedError,
    ScrapeError,
)
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

_ORIGIN = "https://medoctruyen.vn"
_SLUG_RE = re.compile(r"medoctruyen\.vn/([a-z0-9-]+)")
_CH_NUM_RE = re.compile(r"/chuong-(\d+)")
_CH_PAGE_RE = re.compile(r"[?&]ch=(\d+)")
_FREE_TAG = "Miễn phí"  # trailing tag appended to free-chapter link text
_MAX_TOC_PAGES = 500  # sanity cap in case pager markup drifts
_RATE_LIMIT_NOTICE = "Bạn đọc quá nhanh"  # shown when the site throttles reads
_DAILY_LIMIT_NOTICE = "GIỚI HẠN ĐỌC TRONG NGÀY"  # hard 50-chapters/day cap page


@register
class MedoctruyenAdapter(SiteAdapter):
    name = "medoctruyen"
    display_name = "Mê Đọc Truyện (medoctruyen.vn)"
    url_patterns = [r"medoctruyen\.vn/[a-z0-9-]+"]

    SEL_ARTICLE = "article"
    SEL_PREVIEW_GATE = "[data-preview-gate]"
    SEL_TOC_LINKS = 'a[href*="/chuong-"]'

    def _soup(self, url: str, **kwargs) -> BeautifulSoup:
        return BeautifulSoup(self.client.get_html(url, **kwargs), "lxml")

    @staticmethod
    def _slug(url: str) -> str:
        match = _SLUG_RE.search(url)
        if not match:
            raise ScrapeError("Could not extract novel slug from URL", url)
        return match.group(1)

    def _novel_url(self, url: str) -> str:
        """Normalize any URL (landing or /chuong-N) to the novel landing page."""
        return f"{_ORIGIN}/{self._slug(url)}"

    @staticmethod
    def _book_jsonld(soup: BeautifulSoup) -> dict:
        """The schema.org Book block, if present (clean title + author)."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and item.get("@type") == "Book":
                    return item
        return {}

    def fetch_metadata(self, url: str) -> NovelMeta:
        soup = self._soup(self._novel_url(url))

        def og(prop: str) -> str:
            el = soup.select_one(f"meta[property='{prop}']")
            return (el.get("content") or "").strip() if el else ""

        book = self._book_jsonld(soup)
        title = (book.get("name") or "").strip()
        if not title:
            # og:title carries a " - Truyện <genre>" category suffix; drop it.
            title = re.split(r"\s+-\s+Truyện\b", og("og:title"))[0].strip()
        if not title:
            raise ScrapeError("Novel title not found — page layout may have changed", url)

        author = ""
        author_obj = book.get("author")
        if isinstance(author_obj, dict):
            author = (author_obj.get("name") or "").strip()
        elif isinstance(author_obj, str):
            author = author_obj.strip()

        return NovelMeta(
            url=self._novel_url(url),
            site=self.name,
            title=title,
            author=author,
            description=og("og:description"),
            cover_url=og("og:image"),
            source_lang="vi",
        )

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        base = self._novel_url(url)
        first = self.client.get_html(base)
        last_page = max(
            (int(n) for n in _CH_PAGE_RE.findall(first)), default=1
        )
        if last_page > _MAX_TOC_PAGES:
            raise ScrapeError(
                f"Table of contents reports {last_page} pages — page layout may "
                "have changed",
                url,
            )

        by_number: dict[int, str] = {}  # chapter number -> title (first seen wins)
        for page in range(1, last_page + 1):
            html = first if page == 1 else self.client.get_html(f"{base}?ch={page}")
            soup = BeautifulSoup(html, "lxml")
            found = 0
            for a in soup.select(self.SEL_TOC_LINKS):
                href = a.get("href") or ""
                num_match = _CH_NUM_RE.search(href)
                text = a.get_text(" ", strip=True)
                # Skip chrome links like "Đọc từ đầu" that point at a chapter but
                # don't carry its real "Chương N: …" title.
                if not num_match or not text.startswith("Chương"):
                    continue
                number = int(num_match.group(1))
                found += 1
                if number not in by_number:
                    title = text.removesuffix(_FREE_TAG).strip()
                    by_number[number] = title
            # A page that yields no chapter links means we've run past the end.
            if found == 0 and page > 1:
                break

        if not by_number:
            raise ScrapeError("Chapter list not found — page layout may have changed", url)

        return [
            ChapterRef(index=i, title=by_number[num], url=f"{base}/chuong-{num}")
            for i, num in enumerate(sorted(by_number))
        ]

    @staticmethod
    def _daily_limit_details(soup: BeautifulSoup, html: str) -> tuple[str, str, str]:
        """Parse the daily-limit page into (message, code, discord_url).

        The message is actionable instructions for the user; the code and Discord
        invite are also returned raw so the app can run the `/mochuong <code>`
        unlock itself (auto-unlock) instead of only showing the text.
        """
        text = soup.get_text(" ", strip=True)
        code_match = re.search(r"/mochuong\s+([A-Z0-9]{5,16})", text)
        discord_match = re.search(r"https://discord\.gg/[^\s\"'<]+", html)
        reset = re.search(r"đặt lại vào\s*([\d:]+)\s*[·.]?\s*([\d/]+)", text)
        code = code_match.group(1) if code_match else ""
        discord_url = discord_match.group(0) if discord_match else ""

        parts = ["Đã đạt giới hạn 50 chương/ngày của medoctruyen.vn."]
        if code and discord_url:
            parts.append(
                f"Mở khoá 50 chương tiếp: vào Discord {discord_url} , kênh "
                f"#mở-khoá, gõ lệnh:  /mochuong {code}  rồi bấm “Tải các "
                "chương” lại."
            )
        if reset:
            parts.append(f"Hoặc chờ giới hạn tự đặt lại lúc {reset.group(1)} ngày {reset.group(2)}.")
        return " ".join(parts), code, discord_url

    def fetch_chapter(self, ref: ChapterRef) -> str:
        html = self.client.get_html(ref.url)
        soup = BeautifulSoup(html, "lxml")

        # `previewLoginHref` is emitted on every page (even authenticated, full
        # ones), so it can't signal gating. The rendered gate element and the
        # explicit isPreview flag only appear when the body is actually truncated.
        gated = (
            soup.select_one(self.SEL_PREVIEW_GATE) is not None
            or '"isPreview":true' in html
        )
        if gated:
            raise AuthRequiredError(
                "Chương này cần đăng nhập. Cookie phiên đăng nhập bị thiếu hoặc đã "
                "hết hạn — vào Cài đặt và dán lại cookie medoctruyen.vn",
                ref.url,
            )
        if '"isLocked":true' in html:
            raise AuthRequiredError(
                "Chương bị khoá (trả phí) — tài khoản của bạn chưa mở khoá chương này",
                ref.url,
            )

        container = soup.select_one(self.SEL_ARTICLE)
        lines: list[str] = []
        if container is not None:
            lines = [p.get_text(strip=True) for p in container.select("p")]
            lines = [line for line in lines if line]
            if lines and lines[0] == ref.title:
                lines = lines[1:]

        if not lines:
            # Hard per-day quota (50 chapters/day): waiting won't help — the user
            # must run the site's own Discord unlock or wait for the daily reset.
            if _DAILY_LIMIT_NOTICE in html:
                message, code, discord_url = self._daily_limit_details(soup, html)
                raise DailyLimitError(
                    message, ref.url, code=code, discord_url=discord_url
                )
            # "reading too fast" throttle: the page renders without the body.
            # Retryable after a short wait — the download worker backs off.
            if '"isRateLimited":true' in html or _RATE_LIMIT_NOTICE in html:
                raise RateLimitedError(
                    "medoctruyen.vn tạm giới hạn tốc độ đọc (đọc quá nhanh)", ref.url
                )
            if container is None:
                raise ScrapeError(
                    "Chapter content not found — page layout may have changed", ref.url
                )
            raise ScrapeError("Chapter content is empty", ref.url)
        return "\n\n".join(lines)

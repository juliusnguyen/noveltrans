"""Adapter for 爱下电子书 (ixdzs8.com).

Novel page:   https://ixdzs8.com/read/<bid>/
Chapter page: https://ixdzs8.com/read/<bid>/p<n>.html
Full TOC:     POST /novel/clist/ {bid} -> JSON

Chapter pages sit behind a trivial JS challenge: the first response embeds a
token and reloads itself with ?challenge=<token>, which sets a session cookie.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

_CHALLENGE_RE = re.compile(r'token = "([^"]+)"')


@register
class IxdzsAdapter(SiteAdapter):
    name = "ixdzs"
    display_name = "爱下电子书 (ixdzs8.com)"
    url_patterns = [r"ixdzs\d*\.(?:com|tw)/read/\d+"]

    SEL_CONTENT = "article"

    def _get_html(self, url: str) -> str:
        """GET a page, transparently solving the ?challenge= redirect."""
        html = self.client.get_html(url)
        match = _CHALLENGE_RE.search(html)
        if match and "正在验证" in html:
            html = self.client.get_html(url, params={"challenge": match.group(1)})
            if _CHALLENGE_RE.search(html) and "正在验证" in html:
                raise ScrapeError("Could not pass the site's browser verification", url)
        return html

    @staticmethod
    def _bid(url: str) -> str:
        match = re.search(r"/read/(\d+)", url)
        if not match:
            raise ScrapeError("Could not extract book id from URL", url)
        return match.group(1)

    def fetch_metadata(self, url: str) -> NovelMeta:
        soup = BeautifulSoup(self._get_html(url), "lxml")

        def og(prop: str) -> str:
            el = soup.select_one(f"meta[property='{prop}']")
            return (el.get("content") or "").strip() if el else ""

        title = og("og:novel:book_name") or og("og:title")
        if not title:
            raise ScrapeError("Novel title not found — page layout may have changed", url)
        return NovelMeta(
            url=url,
            site=self.name,
            title=title,
            author=og("og:novel:author"),
            description=og("og:description"),
            cover_url=og("og:image"),
        )

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        bid = self._bid(url)
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        response = self.client.get_json_post(
            f"{origin}/novel/clist/",
            data={"bid": bid},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if response.get("rs") != 200:
            raise ScrapeError(f"Chapter list API returned rs={response.get('rs')}", url)

        refs: list[ChapterRef] = []
        for item in response.get("data", []):
            if str(item.get("ctype")) == "1":  # volume/section header, not a chapter
                continue
            refs.append(
                ChapterRef(
                    index=len(refs),
                    title=item["title"],
                    url=f"{origin}/read/{bid}/p{item['ordernum']}.html",
                )
            )
        if not refs:
            raise ScrapeError("Chapter list is empty", url)
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        soup = BeautifulSoup(self._get_html(ref.url), "lxml")
        container = soup.select_one(self.SEL_CONTENT)
        if container is None:
            raise ScrapeError("Chapter content not found — page layout may have changed", ref.url)

        paragraphs = [p.get_text(strip=True) for p in container.select("p")]
        lines = [p for p in paragraphs if p]
        if lines and lines[0] == ref.title:
            lines = lines[1:]
        if not lines:
            raise ScrapeError("Chapter content is empty", ref.url)
        return "\n\n".join(lines)

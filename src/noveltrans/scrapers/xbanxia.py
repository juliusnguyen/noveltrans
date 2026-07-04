"""Adapter for 半夏小說 (xbanxia.cc) — plain server-rendered HTML.

Novel page:   https://www.xbanxia.cc/books/<book_id>.html
Chapter page: https://www.xbanxia.cc/books/<book_id>/<chapter_id>.html
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

_COLONS = ":：︰"  # ASCII, fullwidth and presentation-form colons seen on the site


@register
class XbanxiaAdapter(SiteAdapter):
    name = "xbanxia"
    display_name = "半夏小說 (xbanxia.cc)"
    url_patterns = [r"xbanxia\.(?:cc|com)/books/\d+"]

    SEL_TITLE = ".book-describe h1"
    SEL_INFO_ROWS = ".book-describe p"
    SEL_DESCRIPTION = ".describe-html"
    SEL_TOC_LINKS = ".book-list a[href]"
    SEL_CONTENT = "#nr1"

    def _soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.client.get_html(url), "lxml")

    def fetch_metadata(self, url: str) -> NovelMeta:
        soup = self._soup(url)
        title_el = soup.select_one(self.SEL_TITLE)
        if title_el is None:
            raise ScrapeError("Novel title not found — page layout may have changed", url)

        author = ""
        for row in soup.select(self.SEL_INFO_ROWS):
            text = row.get_text(" ", strip=True)
            if text.startswith("作者"):
                author = re.split(f"[{_COLONS}]", text, maxsplit=1)[-1].strip()
                break

        desc_el = soup.select_one(self.SEL_DESCRIPTION)
        cover_el = soup.select_one("meta[property='og:image']")

        return NovelMeta(
            url=url,
            site=self.name,
            title=title_el.get_text(strip=True),
            author=author,
            description=desc_el.get_text("\n", strip=True) if desc_el else "",
            cover_url=(cover_el.get("content") or "") if cover_el else "",
        )

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        soup = self._soup(url)
        links = soup.select(self.SEL_TOC_LINKS)
        if not links:
            raise ScrapeError("Chapter list not found — page layout may have changed", url)
        return [
            ChapterRef(index=i, title=a.get_text(strip=True), url=urljoin(url, a["href"]))
            for i, a in enumerate(links)
        ]

    def fetch_chapter(self, ref: ChapterRef) -> str:
        soup = self._soup(ref.url)
        container = soup.select_one(self.SEL_CONTENT)
        if container is None:
            raise ScrapeError("Chapter content not found — page layout may have changed", ref.url)

        # #nr1 holds text nodes separated by <br>; span/div children are site chrome.
        for el in container.select("span, div, script, ins"):
            el.decompose()

        lines = [line.strip() for line in container.get_text("\n").split("\n") if line.strip()]
        # The first line usually repeats the chapter title.
        if lines and lines[0] == ref.title:
            lines = lines[1:]
        if not lines:
            raise ScrapeError("Chapter content is empty", ref.url)
        return "\n\n".join(lines)

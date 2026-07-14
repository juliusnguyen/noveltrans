"""giatocvuongtai.com — an Astro SPA backed by a clean public JSON API.

The story page renders its table of contents client-side, so there's nothing useful
in the static HTML. Instead we talk to the same JSON API the page uses:

  * story + full TOC:  GET /api/public/story/<slug>.json
  * one chapter body:  GET /api/public/chapter/<chapterId>.json

Both are anonymous (no login/cookies), unobfuscated, and un-throttled beyond our own
polite delay. Chapter bodies come as a block document — {"blocks": [{"type",
"inline": [{"text"}]}]} — which we flatten to plain paragraphs.

Content is already Vietnamese (an "edit"), so novels carry source_lang="vi".
"""

from __future__ import annotations

import json
import re

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

_ORIGIN = "https://giatocvuongtai.com"
_STORY_API = _ORIGIN + "/api/public/story/{slug}.json"
_CHAPTER_API = _ORIGIN + "/api/public/chapter/{cid}.json"
_SLUG_RE = re.compile(r"giatocvuongtai\.com/stories/([a-z0-9-]+)")

# Block types that carry readable text; everything else (divider, image, …) is skipped
# rather than assumed, so an unknown block never leaks raw JSON into the chapter body.
_TEXT_BLOCK_TYPES = {"paragraph", "heading"}


@register
class GiatocvuongtaiAdapter(SiteAdapter):
    name = "giatocvuongtai"
    display_name = "Gia Tộc Vương Tài (giatocvuongtai.com)"
    # Match the story landing URL (with/without trailing slash or query). Reader URLs
    # (/reader/?storyId=…) carry no slug, so they're intentionally not matched.
    url_patterns = [r"giatocvuongtai\.com/stories/[a-z0-9-]+"]

    @staticmethod
    def _slug(url: str) -> str:
        """Pull the story slug out of a /stories/<slug>[/] URL."""
        match = _SLUG_RE.search(url)
        if not match:
            raise ScrapeError("Không tách được slug truyện từ URL", url)
        return match.group(1)

    def _get_json(self, url: str) -> dict:
        """GET a public API URL and return its unwrapped `data`.

        The API wraps payloads as {"data": …, "error": …}; a non-null error or a
        missing data envelope is surfaced as a ScrapeError.
        """
        raw = self.client.get_html(url)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ScrapeError("API không trả về JSON hợp lệ", url) from exc
        if payload.get("error"):
            raise ScrapeError(f"API báo lỗi: {payload['error']}", url)
        data = payload.get("data")
        if data is None:
            raise ScrapeError("Phản hồi API thiếu trường 'data'", url)
        return data

    def fetch_metadata(self, url: str) -> NovelMeta:
        slug = self._slug(url)
        data = self._get_json(_STORY_API.format(slug=slug))
        title = (data.get("title") or "").strip()
        if not title:
            raise ScrapeError("Không tìm thấy tên truyện", url)
        author = (data.get("author") or {}).get("name", "").strip()
        return NovelMeta(
            # canonical form so re-scans dedupe regardless of the pasted trailing slash
            url=f"{_ORIGIN}/stories/{slug}/",
            site=self.name,
            title=title,
            author=author,
            description=(data.get("summary") or "").strip(),
            cover_url=data.get("cover_url") or "",
            source_lang="vi",
        )

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        slug = self._slug(url)
        data = self._get_json(_STORY_API.format(slug=slug))
        chapters = data.get("chapters") or []
        if not chapters:
            raise ScrapeError("Không tìm thấy danh sách chương", url)

        # The API returns the full list already ordered (no pagination — do NOT add a
        # pager), but sort by order_number defensively in case that ever drifts.
        ordered = sorted(chapters, key=lambda c: c.get("order_number", 0))
        refs: list[ChapterRef] = []
        for chapter in ordered:
            # Unpublished chapters have no returnable body; skipping them keeps the
            # index contiguous and avoids 404s mid-download.
            if not chapter.get("is_published", True):
                continue
            cid = chapter.get("id")
            if not cid:  # can't build a fetch URL without an id
                continue
            title = (chapter.get("title") or f"Chương {chapter.get('order_number', '')}").strip()
            refs.append(
                ChapterRef(
                    index=len(refs),  # sequential 0-based, decoupled from order_number
                    title=title,
                    url=_CHAPTER_API.format(cid=cid),
                )
            )
        if not refs:
            raise ScrapeError("Không có chương nào đã xuất bản để tải", url)
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        data = self._get_json(ref.url)  # ref.url is already the chapter-content API URL
        content = data.get("content")
        if content is None:
            raise ScrapeError("Chương không có nội dung", ref.url)
        # `content` normally arrives as a nested object, but may be a JSON string.
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError as exc:
                raise ScrapeError("Nội dung chương không phải JSON hợp lệ", ref.url) from exc

        text = _blocks_to_text(content)
        if not text.strip():
            raise ScrapeError("Nội dung chương rỗng", ref.url)
        return text


def _blocks_to_text(content: dict) -> str:
    """Flatten a block document to plain text (paragraphs separated by blank lines)."""
    paragraphs: list[str] = []
    for block in content.get("blocks") or []:
        if block.get("type") not in _TEXT_BLOCK_TYPES:
            continue  # divider / image / unknown — no readable text
        text = "".join(seg.get("text", "") for seg in (block.get("inline") or [])).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)

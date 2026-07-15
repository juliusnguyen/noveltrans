"""Exporter ABC."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from noveltrans.models import Chapter, NovelMeta


@dataclass
class ExportText:
    """What actually gets written for one chapter, after choosing the language."""

    title: str
    body: str


def meta_text(meta: NovelMeta, use_translation: bool) -> tuple[str, str]:
    """(title, description) for the front matter, translated when available."""
    if use_translation and meta.translated_title:
        return meta.translated_title, meta.translated_description or meta.description
    return meta.title, meta.description


_NUMBER_PREFIX = "Chương {n}: "  # prepended to the title when number_chapters is on

# A title that already opens with its own chapter marker — "Chương 3", "Chapter 3",
# "第三章", "第3章" — shouldn't get a second number prepended (avoids "Chương 1: Chương 1").
_ALREADY_NUMBERED_RE = re.compile(
    r"^\s*(?:ch[uư][oơ]ng|chapter)\s*\d"  # Chương 3 / Chuong 3 / Chapter 3
    r"|^\s*第\s*[\d〇零一二三四五六七八九十百千两]+\s*章",  # 第三章 / 第3章
    re.IGNORECASE,
)


def chapter_text(
    chapter: Chapter, use_translation: bool, number_chapters: bool = False
) -> ExportText | None:
    """Pick translated or original text; None if that text isn't available.

    `number_chapters` prepends "Chương {n}: " to the heading (n = chapter.index + 1,
    matching the app's "#" column and gap-preserving), keeping the parsed title after
    the number — unless the title already starts with its own chapter marker, in which
    case it's left as-is. The body text is untouched.
    """
    if use_translation:
        if not chapter.translated:
            return None
        title, body = chapter.translated_title or chapter.title, chapter.translated
    else:
        if not chapter.content:
            return None
        title, body = chapter.title, chapter.content
    if number_chapters and not _ALREADY_NUMBERED_RE.match(title):
        title = _NUMBER_PREFIX.format(n=chapter.index + 1) + title
    return ExportText(title, body)


class Exporter(ABC):
    name: str = ""  # "docx" | "markdown" | "epub"
    display_name: str = ""
    extension: str = ""  # ".docx" …

    @abstractmethod
    def export(
        self,
        meta: NovelMeta,
        chapters: list[Chapter],
        out_path: Path,
        use_translation: bool = True,
        number_chapters: bool = False,
    ) -> Path:
        """Write the book to out_path and return it.

        Chapters without the requested text are skipped; implementations list
        the skipped chapters in the front matter. When `number_chapters` is set, each
        included chapter is titled by its number instead of its parsed title.
        """

    @staticmethod
    def split_available(
        chapters: list[Chapter], use_translation: bool, number_chapters: bool = False
    ) -> tuple[list[tuple[Chapter, ExportText]], list[Chapter]]:
        included: list[tuple[Chapter, ExportText]] = []
        skipped: list[Chapter] = []
        for chapter in chapters:
            text = chapter_text(chapter, use_translation, number_chapters)
            if text is None:
                skipped.append(chapter)
            else:
                included.append((chapter, text))
        return included, skipped

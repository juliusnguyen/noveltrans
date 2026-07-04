"""Exporter ABC."""

from __future__ import annotations

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


def chapter_text(chapter: Chapter, use_translation: bool) -> ExportText | None:
    """Pick translated or original text; None if that text isn't available."""
    if use_translation:
        if not chapter.translated:
            return None
        return ExportText(chapter.translated_title or chapter.title, chapter.translated)
    if not chapter.content:
        return None
    return ExportText(chapter.title, chapter.content)


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
    ) -> Path:
        """Write the book to out_path and return it.

        Chapters without the requested text are skipped; implementations list
        the skipped chapters in the front matter.
        """

    @staticmethod
    def split_available(
        chapters: list[Chapter], use_translation: bool
    ) -> tuple[list[tuple[Chapter, ExportText]], list[Chapter]]:
        included: list[tuple[Chapter, ExportText]] = []
        skipped: list[Chapter] = []
        for chapter in chapters:
            text = chapter_text(chapter, use_translation)
            if text is None:
                skipped.append(chapter)
            else:
                included.append((chapter, text))
        return included, skipped

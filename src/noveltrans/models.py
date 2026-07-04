"""Core dataclasses shared by scrapers, storage, translators and exporters."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class NovelMeta:
    """Metadata for a novel, as scraped from its landing page."""

    url: str
    site: str  # adapter name, e.g. "ixdzs"
    title: str
    author: str = ""
    description: str = ""
    cover_url: str = ""
    source_lang: str = "zh"
    # filled in by the first translation run
    translated_title: str = ""
    translated_description: str = ""
    translated_lang: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NovelMeta":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ChapterRef:
    """One entry of a novel's table of contents."""

    index: int  # 0-based order
    title: str
    url: str


# Chapter lifecycle: pending -> downloaded -> translated (or error at any step)
STATUS_PENDING = "pending"
STATUS_DOWNLOADED = "downloaded"
STATUS_TRANSLATED = "translated"
STATUS_ERROR = "error"


@dataclass
class Chapter:
    """A chapter row as stored in a project's chapters.db."""

    index: int
    title: str
    url: str
    content: str = ""  # original Chinese text ("" = not downloaded)
    translated: str = ""  # "" = not translated
    translated_title: str = ""
    target_lang: str = ""  # language of `translated`
    translator: str = ""  # engine that produced `translated`, e.g. "CLI (agy)"
    translate_seconds: float = 0.0  # wall-clock time of the last translation
    status: str = STATUS_PENDING
    error: str = ""
    updated_at: str = ""

    @property
    def is_downloaded(self) -> bool:
        return bool(self.content)

    @property
    def is_translated(self) -> bool:
        return bool(self.translated)

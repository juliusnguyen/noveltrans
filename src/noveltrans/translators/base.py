"""Translator ABC with paragraph-safe chunking and retry."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from noveltrans.errors import TranslateError

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def cjk_count(text: str) -> int:
    """Number of CJK ideographs in `text` (leftovers in a vi/en translation)."""
    return len(_CJK_RE.findall(text))


def split_paragraph_chunks(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of <= max_chars without breaking paragraphs.

    A single paragraph longer than max_chars becomes its own (oversized)
    chunk — engines tolerate slight overflow better than a mid-sentence cut.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        added = len(paragraph) + (2 if current else 0)
        if current and current_len + added > max_chars:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += added
    if current:
        chunks.append("\n\n".join(current))
    return chunks


class Translator(ABC):
    name: str = ""
    display_name: str = ""
    max_chunk_chars: int = 4000  # engines override; chapters are chunked to this
    max_retries: int = 3
    retry_delay: float = 2.0

    @abstractmethod
    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        """Translate one chunk of plain text. Raise TranslateError on failure."""

    def _translate_with_retry(self, text: str, source: str, target: str) -> str:
        last_error: Exception | None = None
        best: str | None = None  # cleanest dirty attempt (fewest leftover CJK chars)
        best_leftover = 0
        for attempt in range(self.max_retries):
            try:
                result = self.translate(text, source=source, target=target)
            except TranslateError:
                if best is not None:
                    return best
                raise
            except Exception as exc:  # engine/library-specific errors
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                continue
            # models occasionally leave source characters untranslated
            # (e.g. "Phó Thanh Từ皺眉") — retry for a clean output
            leftover = 0 if target.startswith("zh") else cjk_count(result)
            if leftover == 0:
                return result
            if best is None or leftover < best_leftover:
                best, best_leftover = result, leftover
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        if best is not None:
            return best  # a few stray chars beat failing the whole chapter
        raise TranslateError(f"Translation failed after {self.max_retries} tries: {last_error}")

    def translate_chapter(
        self, title: str, content: str, source: str = "zh", target: str = "vi"
    ) -> tuple[str, str]:
        """Translate a chapter title + content. Returns (title, content)."""
        translated_title = self._translate_with_retry(title, source, target) if title else ""
        chunks = split_paragraph_chunks(content, self.max_chunk_chars)
        translated_chunks = [self._translate_with_retry(c, source, target) for c in chunks]
        return translated_title.strip(), "\n\n".join(translated_chunks).strip()

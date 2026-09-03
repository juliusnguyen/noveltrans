"""Translator ABC with paragraph-safe chunking and retry."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from noveltrans.chapter_titles import (
    is_implausible_title,
    looks_like_refusal,
    numeric_title,
    repaired_title,
)
from noveltrans.translators.ads import drop_site_ads
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
    # LLM engines (CLI agent, Claude, LM Studio) set this True and implement `complete`, so
    # features like tag / image-prompt generation can prompt them freely. Google can only
    # translate.
    supports_completion: bool = False

    @abstractmethod
    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        """Translate one chunk of plain text. Raise TranslateError on failure."""

    def complete(self, prompt: str) -> str:
        """Run a free-form prompt and return the model's text. Raise TranslateError on
        failure. Only LLM engines implement this; the default rejects the call."""
        raise NotImplementedError(
            f"{self.display_name or self.name} không hỗ trợ tạo nội dung tự do."
        )

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
            # A refusal ("bạn chưa cung cấp nội dung…") is not a translation, and unlike a
            # bad chunk it would be SAVED and exported — so it never becomes `best`, it
            # only earns another attempt. Scored before the CJK check because a refusal is
            # pure Vietnamese: `cjk_count` returns 0 and would accept it outright, which is
            # exactly how feature 076's ten damaged titles got written.
            if looks_like_refusal(result):
                last_error = TranslateError("engine asked for the text instead of translating it")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
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

    def _safe_title(self, title: str, source: str, target: str) -> str:
        """Translate a chapter TITLE, never letting the model's own words become one.

        A title is not a small chapter: it can be as little as `第127章`, a number with no
        prose at all. Handed that through a prompt written for a chapter body, the model
        reads it as the heading of a chapter whose content was omitted and replies asking
        for the content — which is then saved and exported. Ten titles in the reporting
        library were damaged that way.

        Three steps, cheapest first:

        1. A title carrying only a number needs no model. This alone prevents every case
           observed, and saves one engine call per chapter on novels that number this way.
        2. Otherwise translate, then check the result could plausibly BE the translation
           (`is_implausible_title`). One retry, because the failure is non-deterministic —
           only 8 of 139 bare titles in the library actually tripped it.
        3. Still bad, or the engine errored: fall back rather than raise. A title is not
           worth losing the body over, and the body is translated straight after this by a
           call that will surface the real error itself if the engine is genuinely down.
           Falling back here therefore hides nothing: it only stops a title problem from
           masquerading as a chapter failure.
        """
        local = numeric_title(title, target)
        if local:
            return local
        best = ""
        for _ in range(2):
            try:
                result = self._translate_with_retry(title, source, target)
            except TranslateError:
                break  # the body call right after this reports the real problem
            if not is_implausible_title(title, result):
                return result
            best = best or result
        # `repaired_title` is shared with the storage repair so translate-time and
        # migration-time can never disagree about what a damaged title should become.
        return repaired_title(title, best, target) or title

    def translate_chapter(
        self, title: str, content: str, source: str = "zh", target: str = "vi"
    ) -> tuple[str, str]:
        """Translate a chapter title + content. Returns (title, content)."""
        translated_title = self._safe_title(title, source, target) if title else ""
        chunks = split_paragraph_chunks(content, self.max_chunk_chars)
        translated_chunks = [self._translate_with_retry(c, source, target) for c in chunks]
        # Source-site watermarks are stripped HERE, not inside `_translate_with_retry`:
        # that loop scores each attempt by `cjk_count(result)` to pick the cleanest one,
        # and filtering before the count would silently change which attempt wins — a
        # behaviour change to an unrelated heuristic. Here it is also once per chapter on
        # the joined body, so a paragraph break at a chunk seam normalises correctly.
        # `complete()` is deliberately NOT filtered: tags and image prompts go through it.
        return (
            drop_site_ads(translated_title.strip()),
            drop_site_ads("\n\n".join(translated_chunks).strip()),
        )

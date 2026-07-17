"""Pure find-and-replace over chapter text — no Qt, no sqlite.

Powers the Dịch tab's "Tìm & thay thế" dialog. Kept GUI-free and I/O-free so the
count/replace logic is fully unit-tested on plain `Chapter` objects; the dialog does
the previewing and the storage layer does the writing.

Literal (non-regex) substring replace with an optional case-sensitivity flag. Two
passes share one code path: `scan` previews per-chapter match counts, and the dialog
applies the pre-computed new values so what the user saw is exactly what gets written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from noveltrans.models import Chapter

# Field ids — these are BOTH Chapter attribute names AND chapters-table column names
# (they coincide for these four), so the same string works for reading here and for
# the storage UPDATE.
FIELD_TITLE = "title"  # original source title — reverts on a TOC re-scan
FIELD_CONTENT = "content"  # original body
FIELD_TRANSLATED = "translated"  # translated body
FIELD_TRANSLATED_TITLE = "translated_title"

ALL_FIELDS = (FIELD_TITLE, FIELD_CONTENT, FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE)


@dataclass(frozen=True)
class FieldChange:
    """One field of one chapter, before and after — only built when count > 0."""

    field: str
    old: str
    new: str
    count: int


@dataclass(frozen=True)
class ChapterMatch:
    """A chapter with at least one match across the selected fields."""

    index: int
    label: str  # for the breakdown list, e.g. "Chương 12 — <title>"
    count: int  # total across all changed fields
    changes: list[FieldChange]


def apply_to_text(
    text: str, search: str, replacement: str, case_sensitive: bool
) -> tuple[str, int]:
    """Replace every literal occurrence of `search`. Returns (new_text, count).

    An empty `search` is a no-op — there is no such thing as replacing "nothing".
    """
    if not search:
        return text, 0
    pattern = re.compile(re.escape(search), 0 if case_sensitive else re.IGNORECASE)
    # A function replacer inserts `replacement` verbatim: a bare string would let
    # backslash escapes / \g<...> in the replacement be read as group references.
    return pattern.subn(lambda _m: replacement, text)


def scan(
    chapters: Iterable[Chapter],
    search: str,
    replacement: str,
    fields: Sequence[str],
    *,
    case_sensitive: bool,
) -> list[ChapterMatch]:
    """Preview pass: per-chapter matches across `fields`, in index order.

    Returns only chapters with at least one match. Each `FieldChange` already carries
    the computed `new` value, so the caller applies without re-searching.
    """
    if not search or not fields:
        return []

    matches: list[ChapterMatch] = []
    for chapter in chapters:
        changes: list[FieldChange] = []
        for field in fields:
            old = getattr(chapter, field, "") or ""
            new, count = apply_to_text(old, search, replacement, case_sensitive)
            if count:
                changes.append(FieldChange(field=field, old=old, new=new, count=count))
        if changes:
            total = sum(c.count for c in changes)
            label = f"Chương {chapter.index + 1} — {chapter.title}".rstrip(" —")
            matches.append(
                ChapterMatch(index=chapter.index, label=label, count=total, changes=changes)
            )
    return matches


def total_matches(matches: list[ChapterMatch]) -> int:
    return sum(m.count for m in matches)


def chapter_count(matches: list[ChapterMatch]) -> int:
    return len(matches)

"""The per-novel character-name list: what each Chinese name is written as in Vietnamese.

Stored as `names.json` at the project root, beside `meta.json`, and read back into the
translate run so the same character is spelled the same way in every chapter.

**Why a sidecar and not `meta.json`.** A novel has hundreds of names, while `meta.json` is
read whole on every `NovelProject.open` and rewritten whole by nine different `save_*`
methods — a glossary in there would be re-serialised every time the user saves a YouTube tag
list. It is also read from a worker thread and written from the GUI thread, which is exactly
the split `video_windows.py` already handles this way. And being a plain file at the project
root it is backed up by `onedrive_upload` for free (its `_is_excluded` already drops `*.tmp`,
so the atomic write below never pushes a partial file).

**The merge rule, which is the whole point of the file:**

    A detection never changes `reading` on an entry the user edited, and never removes
    an entry.

Without that, re-scanning a novel would silently undo the user's own corrections — destroying
the one thing this file exists to provide. `auto` records what the detector last produced so
an *unedited* entry can still follow a future improvement to the Hán-Việt table, while an
edited one is pinned. A hand-typed entry survives a detection that cannot see it at all,
which is the escape hatch for names the detector misses (it needs 5+ occurrences, a surname
from a fixed list, and a reading for every character).

Neither `meta.json` re-scans (`refresh_meta`) nor TOC re-scans (`replace_toc`) touch this
file: re-scraping a landing page says nothing about character names. The stored `count` does
go stale as new chapters arrive, so it is presented as "as of the last detection".

Pure: no Qt, no sqlite, no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

_FILE_NAME = "names.json"
_VERSION = 1

ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"

# Anything in these blocks is a Han character. Used to reject a substitution that would put
# Chinese back into the text, or take an empty replacement that would DELETE the name.
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF))


def _has_cjk(text: str) -> bool:
    return any(any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES) for c in text)


@dataclass(frozen=True)
class NameEntry:
    """One character name and how this novel writes it."""

    source: str  # the Chinese string — the key
    reading: str  # what actually gets substituted into the text
    auto: str = ""  # what the detector last produced; "" when it could not convert
    edited: bool = False  # the user changed `reading` away from `auto`
    enabled: bool = True  # off = a false positive, kept so a re-detect cannot revive it
    count: int = 0  # occurrences as of the last detection
    origin: str = ORIGIN_AUTO  # "manual" entries survive a detection that misses them

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "reading": self.reading,
            "auto": self.auto,
            "edited": self.edited,
            "enabled": self.enabled,
            "count": self.count,
            "origin": self.origin,
        }


def names_path(project_path: Path) -> Path:
    """`names.json`, at the project root."""
    return Path(project_path) / _FILE_NAME


def read_names(project_path: Path) -> list[NameEntry]:
    """Every stored name, or `[]` when the file is missing, empty or unreadable.

    Tolerant by design, like `read_manual_windows`: a hand-edited or truncated file must
    degrade to "no names yet" rather than failing a translate run.
    """
    try:
        raw = names_path(project_path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("names")
    if not isinstance(rows, list):
        return []

    entries: list[NameEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        entries.append(
            NameEntry(
                source=source,
                reading=str(row.get("reading") or "").strip(),
                auto=str(row.get("auto") or "").strip(),
                edited=bool(row.get("edited")),
                enabled=bool(row.get("enabled", True)),
                count=count,
                origin=ORIGIN_MANUAL if row.get("origin") == ORIGIN_MANUAL else ORIGIN_AUTO,
            )
        )
    return entries


def write_names(
    project_path: Path, entries: list[NameEntry], *, chapters_scanned: int = 0
) -> None:
    """Overwrite the name list (atomic temp-file + replace)."""
    path = names_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": _VERSION,
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chapters_scanned": chapters_scanned,
        "names": [e.as_dict() for e in entries],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_detected(
    stored: list[NameEntry], detected: list[tuple[str, str, int]]
) -> list[NameEntry]:
    """Fold a fresh detection into the stored list, keeping every user decision.

    **A detection never changes `reading` on an edited entry, and never removes an entry.**

    * in both — refresh `count` and `auto`; take the new reading ONLY if the user has not
      edited this one, so an untouched entry still tracks a better table later;
    * detected only — append it, disabled when there is no reading to substitute;
    * stored only — keep it with `count = 0`. This is what protects a hand-typed name the
      detector cannot see.
    """
    found = {source: (reading, count) for source, reading, count in detected}
    merged: list[NameEntry] = []

    for entry in stored:
        hit = found.pop(entry.source, None)
        if hit is None:
            merged.append(replace(entry, count=0))
            continue
        auto, count = hit
        reading = entry.reading if entry.edited else (auto or entry.reading)
        merged.append(replace(entry, reading=reading, auto=auto, count=count))

    for source, reading, count in detected:
        if source not in found:
            continue  # already merged above
        merged.append(
            NameEntry(
                source=source,
                reading=reading,
                auto=reading,
                edited=False,
                enabled=bool(reading),  # nothing to substitute → off until filled in
                count=count,
                origin=ORIGIN_AUTO,
            )
        )
    return merged


def applied_glossary(entries: list[NameEntry]) -> dict[str, str]:
    """`{chinese: vietnamese}` for the entries a translate run should actually substitute.

    The last two guards matter because `apply_glossary` is a blind `str.replace`: an empty
    reading would DELETE every occurrence of the name from the source, and a reading that
    still contains Han characters would both defeat the point and confuse the translator's
    leftover-CJK retry scoring.
    """
    return {
        e.source: e.reading
        for e in entries
        if e.enabled and e.reading and _has_cjk(e.source) and not _has_cjk(e.reading)
    }


def build_from_project(project) -> list[NameEntry]:
    """Detect names across the whole novel and wrap them as fresh entries.

    Uses the same corpus join the translate run has always used — titles and bodies of every
    chapter that has content.
    """
    from noveltrans.translators.names import detect_names

    chapters = [c for c in project.chapters() if c.content]
    corpus = "\n".join(c.title + "\n" + c.content for c in chapters)
    return merge_detected([], detect_names(corpus))

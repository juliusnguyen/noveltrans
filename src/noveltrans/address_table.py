"""The per-novel address table (bảng xưng hô): how each character addresses each other one.

Stored as `address.json` at the project root, beside `names.json`, and turned into a short
block of rules that goes into every translate request an LLM engine receives, so the same
pair of characters keeps the same "tôi/cậu" or "tao/mày" from chapter to chapter.

**Why a table and not a replacement list.** Chinese 我/你 (and English I/you) have no single
Vietnamese form: the right one depends on who is speaking to whom. Replacing "tao" with "tôi"
everywhere would break every other pair in the novel. So a row is keyed by the DIRECTED pair
(speaker → listener). A→B and B→A are separate rows, because "anh/em" from one side is
"em/anh" from the other.

**Why `from_chapter`.** Relationships change: strangers become friends, an identity is
revealed. Several rows for the same pair, each starting at a different chapter, express
that. A chapter uses the row with the largest `from_chapter` that is not after it. The
number is the one shown in the first column of the chapter table (`chapter.index + 1`), so
what the user types matches what they see.

**Narration rows.** A row whose speaker is `NARRATOR` covers how the narrator refers to a
character ("hắn" vs "anh ta"), which drifts just as badly. Only `other_term` matters there.

**Matching.** Names are substituted into the Chinese source before translation
(`apply_glossary`), so a row's names, written the same way as `names.json` readings, can be
found in the text the engine is about to see. A chapter only carries the rows whose
characters actually appear in it, which keeps a 200-pair novel from flooding every prompt.

Google Translate takes no prompt, so this has no effect there.

Pure: no Qt, no sqlite, no network.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_FILE_NAME = "address.json"
_VERSION = 1

NARRATOR = "(người kể)"
ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"

# Rows per chapter in a prompt. Past this a small local model starts ignoring the block,
# and a chapter rarely has more pairs than this actually talking.
MAX_PROMPT_ROWS = 40


@dataclass(frozen=True)
class AddressEntry:
    """How `speaker` addresses `listener`, from chapter `from_chapter` on."""

    speaker: str
    listener: str
    self_term: str = ""  # what the speaker calls themself ("tao")
    other_term: str = ""  # what the speaker calls the listener ("mày")
    note: str = ""
    from_chapter: int = 1  # the number shown in the chapter table; 1 = from the start
    enabled: bool = True
    origin: str = ORIGIN_MANUAL

    @property
    def is_narration(self) -> bool:
        return self.speaker == NARRATOR

    @property
    def usable(self) -> bool:
        """Has enough to say anything: two names and at least one term."""
        if not self.enabled or not self.speaker or not self.listener:
            return False
        if self.is_narration:
            return bool(self.other_term)
        return bool(self.self_term or self.other_term)

    def as_dict(self) -> dict:
        return {
            "speaker": self.speaker,
            "listener": self.listener,
            "self_term": self.self_term,
            "other_term": self.other_term,
            "note": self.note,
            "from_chapter": self.from_chapter,
            "enabled": self.enabled,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class AddressTable:
    general_note: str = ""  # novel-wide guidance for pairs with no row
    entries: tuple[AddressEntry, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.general_note.strip() and not any(e.usable for e in self.entries)


def address_path(project_path: Path) -> Path:
    """`address.json`, at the project root."""
    return Path(project_path) / _FILE_NAME


def _text(value) -> str:
    return str(value or "").strip()


def _flag(value, default: bool = True) -> bool:
    """A hand-edited boolean: `"false"`, `"0"`, `"no"`, `"off"` are False, not truthy text."""
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        return text not in {"false", "0", "no", "off", "không"}
    return bool(value)


def read_address(project_path: Path) -> AddressTable:
    """The stored table, or an empty one when the file is missing, empty or unreadable.

    Tolerant like `read_names`: a hand-edited or truncated file degrades to "no table"
    rather than failing a translate run.
    """
    try:
        raw = address_path(project_path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return AddressTable()
    if not isinstance(data, dict):
        return AddressTable()
    rows = data.get("entries")
    entries: list[AddressEntry] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        speaker, listener = _text(row.get("speaker")), _text(row.get("listener"))
        if not speaker or not listener:
            continue
        try:
            from_chapter = max(1, int(row.get("from_chapter") or 1))
        except (TypeError, ValueError):
            from_chapter = 1
        entries.append(
            AddressEntry(
                speaker=speaker,
                listener=listener,
                self_term=_text(row.get("self_term")),
                other_term=_text(row.get("other_term")),
                note=_text(row.get("note")),
                from_chapter=from_chapter,
                enabled=_flag(row.get("enabled"), True),
                origin=ORIGIN_AUTO if row.get("origin") == ORIGIN_AUTO else ORIGIN_MANUAL,
            )
        )
    return AddressTable(general_note=_text(data.get("general_note")), entries=tuple(entries))


def write_address(project_path: Path, table: AddressTable) -> None:
    """Overwrite the table (atomic temp-file + replace)."""
    path = address_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": _VERSION,
        "general_note": table.general_note,
        "entries": [e.as_dict() for e in table.entries],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def reverse_of(entry: AddressEntry) -> AddressEntry:
    """The other direction of a conversation pair: B→A with the two terms swapped.

    A pre-fill, not a rule: "anh/em" one way is usually "em/anh" the other, but "ta/ngươi"
    from a master is not "ta/ngươi" back from a servant. The user reviews it.
    """
    return AddressEntry(
        speaker=entry.listener,
        listener=entry.speaker,
        self_term=entry.other_term,
        other_term=entry.self_term,
        note=entry.note,
        from_chapter=entry.from_chapter,
        enabled=entry.enabled,
        origin=ORIGIN_MANUAL,
    )


def rows_for_chapter(table: AddressTable, chapter_number: int, text: str) -> list[AddressEntry]:
    """The rows that apply to one chapter, in table order, at most `MAX_PROMPT_ROWS`.

    `chapter_number` is the displayed number (`chapter.index + 1`). `text` is what the
    engine will see — the source AFTER name substitution — and a row is kept only when its
    characters appear in it (the narrator is always present). For each directed pair, the
    row with the largest `from_chapter` not after this chapter wins.
    """
    best: dict[tuple[str, str], AddressEntry] = {}
    for entry in table.entries:
        if not entry.usable or entry.from_chapter > chapter_number:
            continue
        key = (entry.speaker, entry.listener)
        current = best.get(key)
        if current is None or entry.from_chapter >= current.from_chapter:
            best[key] = entry

    # Vietnamese can be stored precomposed (NFC) or decomposed (NFD); a name typed in the
    # table must match either way.
    text = nfc(text)

    def present(name: str) -> bool:
        return name == NARRATOR or nfc(name) in text

    rows = [e for e in best.values() if present(e.speaker) and present(e.listener)]
    order = {id(e): i for i, e in enumerate(table.entries)}
    rows.sort(key=lambda e: order[id(e)])
    return rows[:MAX_PROMPT_ROWS]


def _render_row(entry: AddressEntry) -> str:
    note = f" ({entry.note})" if entry.note else ""
    if entry.is_narration:
        return f"- Lời kể gọi {entry.listener} là '{entry.other_term}'{note}"
    parts = []
    if entry.self_term:
        parts.append(f"xưng '{entry.self_term}'")
    if entry.other_term:
        parts.append(f"gọi '{entry.other_term}'")
    return f"- {entry.speaker} nói với {entry.listener}: {', '.join(parts)}{note}"


def nfc(text: str) -> str:
    """`text` in precomposed form, so "ô" typed one way matches "ô" stored the other."""
    return unicodedata.normalize("NFC", text or "")


def render_prompt_block(
    rows: list[AddressEntry], general_note: str = "", *, for_translation: bool = True
) -> str:
    """The rules an engine receives, in Vietnamese. "" when there is nothing to say.

    "" is load-bearing: every caller passes the block only when it is non-empty, so a
    novel with no table is translated with exactly the prompt it always had.

    `for_translation=False` is for passes over an EXISTING translation (style rewrite,
    pronoun fix): there, pairs outside the table keep the pronouns they already have,
    instead of being told to "choose by context" — which contradicted those prompts' own
    "keep as original" rule.
    """
    general_note = (general_note or "").strip()
    if not rows and not general_note:
        return ""
    lines = [
        "BẢNG XƯNG HÔ của truyện này — bắt buộc dùng đúng khi dịch sang tiếng Việt, "
        "để cách xưng hô giữa các nhân vật giống nhau ở mọi chương:"
    ]
    lines.extend(_render_row(e) for e in rows)
    if general_note:
        lines.append(f"Ghi chú chung: {general_note}")
    if for_translation:
        lines.append(
            "Nhân vật không có trong bảng: chọn xưng hô theo ngữ cảnh"
            + (" và ghi chú chung" if general_note else "")
            + ", rồi giữ nguyên cách đó suốt chương. Không đổi xưng hô giữa chừng, trừ khi "
            "truyện thể hiện rõ quan hệ hay thái độ đã thay đổi (ví dụ nổi giận, trở mặt)."
        )
    else:
        lines.append(
            "Cặp nhân vật không có trong bảng: GIỮ NGUYÊN xưng hô như bản hiện tại."
        )
    return "\n".join(lines)


def check_stamp(block: str, chapter_fingerprint: str) -> str:
    """What the AI pronoun check judged: the rules it was given and the text.

    Stored per chapter (`qc_address_hash`), so a chapter that passed QC before the table
    existed — or before a row that covers it changed — is checked again, while one whose
    rules and text are unchanged is not paid for twice.
    """
    digest = hashlib.sha1()  # noqa: S324 — change detection, not security
    digest.update(block.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(chapter_fingerprint.encode("utf-8"))
    return digest.hexdigest()


def chapter_check_block(table: AddressTable, chapter) -> str:
    """The block the AI pronoun check sends for `chapter`; "" when no row covers it."""
    rows = rows_for_chapter(table, chapter.index + 1, chapter.translated)
    return render_prompt_block(rows, table.general_note) if rows else ""


def prompt_for_chapter(
    table: AddressTable, chapter_number: int, text: str, *, for_translation: bool = True
) -> str:
    """`render_prompt_block` of `rows_for_chapter` — the one call a translate run makes."""
    if table.empty:
        return ""
    return render_prompt_block(
        rows_for_chapter(table, chapter_number, text), table.general_note,
        for_translation=for_translation,
    )


def merge_extracted(
    entries: list[AddressEntry] | tuple[AddressEntry, ...], proposed: list[AddressEntry]
) -> list[AddressEntry]:
    """Add proposed rows for pairs the table does not have yet; never touch existing rows.

    **An extraction never changes or removes a row that is already there** — at any
    `from_chapter`. The table is the user's decision; a scan only fills the gaps, and its
    disagreements are reported as conflicts instead of being written over the user's row.
    """
    merged = list(entries)
    have = {(e.speaker, e.listener) for e in merged}
    for entry in proposed:
        key = (entry.speaker, entry.listener)
        if key not in have:
            merged.append(entry)
            have.add(key)
    return merged

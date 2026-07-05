"""NovelProject — one novel's on-disk project folder.

Layout:
    <library_dir>/<slug>-<hash8>/
        meta.json      # NovelMeta + created_at
        chapters.db    # SQLite, one row per chapter
        exports/       # default output dir for exporters
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from noveltrans.models import (
    STATUS_DOWNLOADED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSLATED,
    Chapter,
    ChapterRef,
    NovelMeta,
)

META_FILE = "meta.json"
DB_FILE = "chapters.db"
EXPORTS_DIR = "exports"
AUDIO_DIR = "audio"  # inside exports/

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chapters (
  idx              INTEGER PRIMARY KEY,
  title            TEXT NOT NULL,
  url              TEXT NOT NULL,
  content          TEXT NOT NULL DEFAULT '',
  translated       TEXT NOT NULL DEFAULT '',
  translated_title TEXT NOT NULL DEFAULT '',
  target_lang      TEXT NOT NULL DEFAULT '',
  translator       TEXT NOT NULL DEFAULT '',
  translate_seconds REAL NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'pending',
  error            TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL DEFAULT '',
  audio_path       TEXT NOT NULL DEFAULT '',
  audio_voice      TEXT NOT NULL DEFAULT '',
  audio_seconds    REAL NOT NULL DEFAULT 0,
  audio_error      TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 40) -> str:
    """ASCII-safe folder slug; CJK titles fall back to 'novel'."""
    text = text.replace("đ", "d").replace("Đ", "D")  # đ has no NFKD decomposition
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len] or "novel"


def _row_to_chapter(row: sqlite3.Row) -> Chapter:
    return Chapter(
        index=row["idx"],
        title=row["title"],
        url=row["url"],
        content=row["content"],
        translated=row["translated"],
        translated_title=row["translated_title"],
        target_lang=row["target_lang"],
        translator=row["translator"],
        translate_seconds=row["translate_seconds"],
        status=row["status"],
        error=row["error"],
        updated_at=row["updated_at"],
        audio_path=row["audio_path"],
        audio_voice=row["audio_voice"],
        audio_seconds=row["audio_seconds"],
        audio_error=row["audio_error"],
    )


class NovelProject:
    """One novel's folder: meta.json + chapters.db. Single writer at a time."""

    def __init__(self, path: Path, meta: NovelMeta):
        self.path = Path(path)
        self.meta = meta
        self._db = sqlite3.connect(self.path / DB_FILE)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a chapters.db created by an older version up to _SCHEMA."""
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(chapters)")}
        added = {
            "translator": "TEXT NOT NULL DEFAULT ''",
            "translate_seconds": "REAL NOT NULL DEFAULT 0",
            "audio_path": "TEXT NOT NULL DEFAULT ''",
            "audio_voice": "TEXT NOT NULL DEFAULT ''",
            "audio_seconds": "REAL NOT NULL DEFAULT 0",
            "audio_error": "TEXT NOT NULL DEFAULT ''",
        }
        with self._db:
            for name, ddl in added.items():
                if name not in columns:
                    self._db.execute(f"ALTER TABLE chapters ADD COLUMN {name} {ddl}")

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def create(cls, library_dir: Path, meta: NovelMeta, refs: list[ChapterRef]) -> "NovelProject":
        """Create the project folder and seed chapter rows from a TOC scan."""
        library_dir = Path(library_dir)
        digest = hashlib.sha1(meta.url.encode("utf-8")).hexdigest()[:8]
        folder = library_dir / f"{slugify(meta.title)}-{digest}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / EXPORTS_DIR).mkdir(exist_ok=True)

        (folder / META_FILE).write_text(
            json.dumps(
                {**meta.to_dict(), "created_at": _now()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        project = cls(folder, meta)
        project.replace_toc(refs)
        return project

    @classmethod
    def open(cls, path: Path) -> "NovelProject":
        path = Path(path)
        data = json.loads((path / META_FILE).read_text(encoding="utf-8"))
        return cls(path, NovelMeta.from_dict(data))

    def reload_meta(self) -> NovelMeta:
        """Re-read meta.json — picks up translations written by another instance."""
        data = json.loads((self.path / META_FILE).read_text(encoding="utf-8"))
        self.meta = NovelMeta.from_dict(data)
        return self.meta

    @staticmethod
    def is_project_dir(path: Path) -> bool:
        return (Path(path) / META_FILE).is_file() and (Path(path) / DB_FILE).is_file()

    def close(self) -> None:
        self._db.close()

    @property
    def exports_dir(self) -> Path:
        return self.path / EXPORTS_DIR

    @property
    def audio_dir(self) -> Path:
        return self.exports_dir / AUDIO_DIR

    # ---------------------------------------------------------------- TOC

    def replace_toc(self, refs: list[ChapterRef]) -> None:
        """Insert/refresh chapter rows from a TOC scan.

        Existing rows keep their content/translation; only titles/urls are
        updated, so re-scanning a novel to pick up new chapters is safe.
        """
        with self._db:
            for ref in refs:
                self._db.execute(
                    """
                    INSERT INTO chapters (idx, title, url, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(idx) DO UPDATE SET
                        title = excluded.title,
                        url = excluded.url
                    """,
                    (ref.index, ref.title, ref.url, _now()),
                )

    # ---------------------------------------------------------------- queries

    def chapters(self) -> list[Chapter]:
        rows = self._db.execute("SELECT * FROM chapters ORDER BY idx").fetchall()
        return [_row_to_chapter(r) for r in rows]

    def chapter(self, idx: int) -> Chapter | None:
        row = self._db.execute("SELECT * FROM chapters WHERE idx = ?", (idx,)).fetchone()
        return _row_to_chapter(row) if row else None

    def pending_download(self) -> list[Chapter]:
        """Chapters that still need their original content fetched."""
        rows = self._db.execute(
            "SELECT * FROM chapters WHERE content = '' ORDER BY idx"
        ).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def pending_translation(self, target_lang: str) -> list[Chapter]:
        """Downloaded chapters not yet translated into `target_lang`.

        A chapter translated into a *different* language counts as pending
        again (the old translation gets overwritten).
        """
        rows = self._db.execute(
            """
            SELECT * FROM chapters
            WHERE content != '' AND (translated = '' OR target_lang != ?)
            ORDER BY idx
            """,
            (target_lang,),
        ).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def pending_audio(self, voice: str = "") -> list[Chapter]:
        """Translated chapters that don't have audio in `voice` yet.

        Like pending_translation with a language switch: audio generated with a
        *different* voice counts as pending again (the old file gets replaced).
        Empty `voice` only checks for missing audio.
        """
        rows = self._db.execute(
            """
            SELECT * FROM chapters
            WHERE translated != '' AND (audio_path = '' OR (? != '' AND audio_voice != ?))
            ORDER BY idx
            """,
            (voice, voice),
        ).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def errored(self) -> list[Chapter]:
        rows = self._db.execute(
            "SELECT * FROM chapters WHERE status = ? ORDER BY idx", (STATUS_ERROR,)
        ).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def counts(self) -> dict:
        total = self._db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
        downloaded = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE content != ''"
        ).fetchone()[0]
        translated = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE translated != ''"
        ).fetchone()[0]
        errors = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE status = ?", (STATUS_ERROR,)
        ).fetchone()[0]
        audio = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE audio_path != ''"
        ).fetchone()[0]
        return {
            "total": total,
            "downloaded": downloaded,
            "translated": translated,
            "errors": errors,
            "audio": audio,
        }

    # ---------------------------------------------------------------- writes

    def save_content(self, idx: int, text: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET content = ?, status = ?, error = '', updated_at = ?"
                " WHERE idx = ?",
                (text, STATUS_DOWNLOADED, _now(), idx),
            )

    def save_translation(
        self,
        idx: int,
        title: str,
        text: str,
        lang: str,
        translator: str = "",
        seconds: float = 0.0,
    ) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET translated = ?, translated_title = ?, target_lang = ?,"
                " translator = ?, translate_seconds = ?, status = ?, error = '', updated_at = ?"
                " WHERE idx = ?",
                (text, title, lang, translator, seconds, STATUS_TRANSLATED, _now(), idx),
            )

    def edit_translation(
        self, idx: int, title: str | None = None, text: str | None = None
    ) -> None:
        """Manual edit of a chapter's translated title/text.

        Only the given fields change — engine, language, timing and status
        stay as the original translation run left them.
        """
        sets, params = [], []
        if title is not None:
            sets.append("translated_title = ?")
            params.append(title)
        if text is not None:
            sets.append("translated = ?")
            params.append(text)
        if not sets:
            return
        with self._db:
            self._db.execute(
                f"UPDATE chapters SET {', '.join(sets)}, updated_at = ? WHERE idx = ?",
                (*params, _now(), idx),
            )

    def mark_error(self, idx: int, message: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET status = ?, error = ?, updated_at = ? WHERE idx = ?",
                (STATUS_ERROR, message, _now(), idx),
            )

    def save_audio(self, idx: int, rel_path: str, voice: str, seconds: float) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET audio_path = ?, audio_voice = ?, audio_seconds = ?,"
                " audio_error = '', updated_at = ? WHERE idx = ?",
                (rel_path, voice, seconds, _now(), idx),
            )

    def mark_audio_error(self, idx: int, message: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET audio_error = ?, updated_at = ? WHERE idx = ?",
                (message, _now(), idx),
            )

    def clear_audio(self) -> None:
        """Reset all audio state so the novel can be re-voiced from scratch.

        Does not delete the audio files — the worker overwrites them.
        """
        with self._db:
            self._db.execute(
                "UPDATE chapters SET audio_path = '', audio_voice = '',"
                " audio_seconds = 0, audio_error = '', updated_at = ?",
                (_now(),),
            )

    def save_meta_translation(self, title: str, description: str, lang: str) -> None:
        """Persist the translated novel title/description into meta.json."""
        self.meta.translated_title = title
        self.meta.translated_description = description
        self.meta.translated_lang = lang
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(
            translated_title=title, translated_description=description, translated_lang=lang
        )
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear_translations(self) -> None:
        """Drop all translations so the novel can be re-translated from scratch."""
        with self._db:
            self._db.execute(
                "UPDATE chapters SET translated = '', translated_title = '', target_lang = '',"
                "  translator = '', translate_seconds = 0,"
                "  status = CASE WHEN content = '' THEN ? ELSE ? END, updated_at = ?",
                (STATUS_PENDING, STATUS_DOWNLOADED, _now()),
            )

    def reset_errors(self) -> None:
        """Put errored chapters back into the queue (status derived from data)."""
        with self._db:
            self._db.execute(
                "UPDATE chapters SET"
                "  status = CASE WHEN content = '' THEN ? ELSE ? END,"
                "  error = '', updated_at = ?"
                " WHERE status = ?",
                (STATUS_PENDING, STATUS_DOWNLOADED, _now(), STATUS_ERROR),
            )

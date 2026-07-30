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
VIDEO_DIR = "video"  # inside exports/

# Columns a find-and-replace may write. Whitelisted because apply_replacements
# interpolates column names into SQL; `title` is included (the user opted in) even
# though replace_toc reverts it on a TOC re-scan — the GUI warns about that.
EDITABLE_COLUMNS = frozenset({"title", "content", "translated", "translated_title"})

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
  audio_source     TEXT NOT NULL DEFAULT 'translated',
  audio_seconds    REAL NOT NULL DEFAULT 0,
  audio_error      TEXT NOT NULL DEFAULT '',
  -- 1 once the user has renamed this chapter by hand, so a re-scan leaves it alone
  title_custom     INTEGER NOT NULL DEFAULT 0,
  -- the site's own title, kept so a rename can be undone; refreshed by every scan
  title_source     TEXT NOT NULL DEFAULT ''
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
        audio_source=row["audio_source"],
        audio_seconds=row["audio_seconds"],
        audio_error=row["audio_error"],
        title_custom=bool(row["title_custom"]),
        title_source=row["title_source"],
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
            # existing audio predates the original/translation choice → it's translated
            "audio_source": "TEXT NOT NULL DEFAULT 'translated'",
            "audio_seconds": "REAL NOT NULL DEFAULT 0",
            "audio_error": "TEXT NOT NULL DEFAULT ''",
            # existing titles all came from a scan, so none of them is a manual rename
            "title_custom": "INTEGER NOT NULL DEFAULT 0",
            "title_source": "TEXT NOT NULL DEFAULT ''",
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

    @property
    def video_dir(self) -> Path:
        return self.exports_dir / VIDEO_DIR

    # ---------------------------------------------------------------- TOC

    def replace_toc(self, refs: list[ChapterRef]) -> None:
        """Insert/refresh chapter rows from a TOC scan.

        Existing rows keep their content/translation; only titles/urls are
        updated, so re-scanning a novel to pick up new chapters is safe.

        A title the user renamed by hand (`title_custom`) is left alone. Without that
        exception every re-scan would silently undo the renaming — and a re-scan is the
        normal way to pick up new chapters, so the work would rarely survive a day.
        """
        with self._db:
            for ref in refs:
                self._db.execute(
                    """
                    INSERT INTO chapters (idx, title, url, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(idx) DO UPDATE SET
                        title = CASE WHEN chapters.title_custom = 1
                                     THEN chapters.title ELSE excluded.title END,
                        -- kept current even under a rename, so "undo" restores the
                        -- site's LATEST title rather than one from months ago
                        title_source = excluded.title,
                        url = excluded.url
                    """,
                    (ref.index, ref.title, ref.url, _now()),
                )

    def add_chapters(self, titles: list[str]) -> list[int]:
        """Append hand-written chapters by name; returns the new 0-based indices.

        For novels the user writes themselves — there is no TOC to scan, so this is the
        only way rows get created. Blank names are dropped and the rest are stripped.

        **Appends at `max(idx) + 1`, so a gap left by a delete is never filled.** An idx
        is not just a row key: it is baked into the audio filename (`{index+1:04d}-…`),
        the merged part names and the YouTube upload records beside them, so dropping a
        new chapter into a hole would silently adopt the deleted one's audio file.

        The one number that *does* come back is the tail: delete the last chapter and
        `MAX(idx)` shrinks, so the next chapter takes its number again. Nothing here
        remembers a trimmed tail, and giving it that memory would mean persisting a
        high-water mark. It stays safe because `ScrapeTab._delete_chapter` unlinks the
        deleted chapter's audio and cue sidecar, leaving no stale take to inherit.
        """
        titles = [t.strip() for t in titles]
        titles = [t for t in titles if t]
        if not titles:
            return []
        # MAX() over an empty table is NULL, not 0 — a fresh local novel has no rows yet.
        highest = self._db.execute("SELECT MAX(idx) FROM chapters").fetchone()[0]
        next_idx = 0 if highest is None else highest + 1
        indices = list(range(next_idx, next_idx + len(titles)))
        with self._db:
            for idx, title in zip(indices, titles):
                self._db.execute(
                    "INSERT INTO chapters (idx, title, url, updated_at) VALUES (?, ?, '', ?)",
                    (idx, title, _now()),
                )
        return indices

    def delete_chapter(self, idx: int) -> Chapter | None:
        """Remove one chapter; returns the deleted row, or None if it wasn't there.

        The row is read before the DELETE so the caller can clean up what the database
        no longer points at — chiefly `audio_path`, which is otherwise an unreachable
        file in `exports/audio/`.

        The freed index stays a **gap**; nothing renumbers. `plan_merge_windows` already
        ranges and batches by 1-based chapter number precisely so a missing chapter can't
        shift later boundaries, and renumbering would instead invalidate every existing
        audio filename, rendered video part and upload record in one go.
        """
        chapter = self.chapter(idx)
        if chapter is None:
            return None
        with self._db:
            self._db.execute("DELETE FROM chapters WHERE idx = ?", (idx,))
        return chapter

    # ---------------------------------------------------------------- queries

    def chapters(self) -> list[Chapter]:
        rows = self._db.execute("SELECT * FROM chapters ORDER BY idx").fetchall()
        return [_row_to_chapter(r) for r in rows]

    def chapter(self, idx: int) -> Chapter | None:
        row = self._db.execute("SELECT * FROM chapters WHERE idx = ?", (idx,)).fetchone()
        return _row_to_chapter(row) if row else None

    def pending_download(
        self, start_idx: int = 0, end_idx: int | None = None
    ) -> list[Chapter]:
        """Chapters that still need their original content fetched.

        `start_idx`/`end_idx` bound the search to a 0-based, inclusive chapter-index
        range (default = the whole novel), so the caller can download from a chosen
        chapter or a range without re-fetching the ones before it.
        """
        sql = "SELECT * FROM chapters WHERE content = '' AND idx >= ?"
        params: list[object] = [start_idx]
        if end_idx is not None:
            sql += " AND idx <= ?"
            params.append(end_idx)
        sql += " ORDER BY idx"
        rows = self._db.execute(sql, params).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def chapters_in_range(self, start_idx: int, end_idx: int | None = None) -> list[Chapter]:
        """All chapters in a 0-based inclusive index range, regardless of status.

        Used for a forced re-download, which re-fetches even chapters that already
        have content.
        """
        sql = "SELECT * FROM chapters WHERE idx >= ?"
        params: list[object] = [start_idx]
        if end_idx is not None:
            sql += " AND idx <= ?"
            params.append(end_idx)
        sql += " ORDER BY idx"
        rows = self._db.execute(sql, params).fetchall()
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

    def pending_audio(self, voice: str = "", use_translation: bool = True) -> list[Chapter]:
        """Chapters that need audio in `voice` from the requested source.

        `use_translation` chooses which text gates availability — the translation
        (default) or the original `content`. A chapter is pending if it has that source
        text AND (has no audio yet, OR its audio used a *different* voice, OR its audio
        was made from the *other* source). Empty `voice` skips the voice-mismatch check.
        """
        src_col = "translated" if use_translation else "content"
        source = "translated" if use_translation else "original"
        rows = self._db.execute(
            f"""
            SELECT * FROM chapters
            WHERE {src_col} != ''
              AND (audio_path = '' OR (? != '' AND audio_voice != ?) OR audio_source != ?)
            ORDER BY idx
            """,
            (voice, voice, source),
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

    def edit_title(self, idx: int, title: str) -> None:
        """Rename one chapter by hand, and remember both that it was renamed and what it
        was called before.

        The `title_custom` flag is the whole point: the chapter title is what the export,
        the video and the TTS narration all use, and `replace_toc` would otherwise put the
        site's version back the next time the novel is scanned for new chapters.

        `title_source` keeps the site's own title so the rename can be undone. The first
        rename captures it; later ones must not, or the second edit would record the first
        edit as "the original" and the way back would be gone.

        Status, content and translation are untouched — renaming is not a re-download.
        """
        with self._db:
            self._db.execute(
                """
                UPDATE chapters SET
                    title_source = CASE WHEN title_custom = 1 THEN title_source
                                        ELSE title END,
                    title = ?, title_custom = 1, updated_at = ?
                WHERE idx = ?
                """,
                (title, _now(), idx),
            )

    def reset_title(self, idx: int) -> None:
        """Undo a rename: put the site's own title back and let scans update it again."""
        with self._db:
            self._db.execute(
                """
                UPDATE chapters SET
                    title = CASE WHEN title_source != '' THEN title_source ELSE title END,
                    title_custom = 0, updated_at = ?
                WHERE idx = ?
                """,
                (_now(), idx),
            )

    def edit_content(self, idx: int, text: str) -> None:
        """Manual edit of a chapter's original text — a correction, or the whole thing.

        The status move is deliberately one-way. A chapter that *gains* text goes
        pending/error -> downloaded, because that is how the user gets original text into
        a hand-written novel: without it the row still reads "Chưa tải" while its content
        is plainly on screen. A chapter that already reads `translated` is left alone —
        flipping it back to "downloaded" would wrongly re-queue it in pending_translation,
        and a text correction is not a re-download.

        Writing an empty string never changes status either, so clearing a chapter
        doesn't dress it up as downloaded.
        """
        with self._db:
            self._db.execute(
                "UPDATE chapters SET content = ?,"
                "  status = CASE WHEN ? != '' AND status IN (?, ?) THEN ? ELSE status END,"
                "  updated_at = ? WHERE idx = ?",
                (
                    text,
                    text,
                    STATUS_PENDING,
                    STATUS_ERROR,
                    STATUS_DOWNLOADED,
                    _now(),
                    idx,
                ),
            )

    def apply_replacements(self, changes: dict[int, dict[str, str]]) -> None:
        """Write find-and-replace results: {idx: {column: new_value}}.

        One transaction for the whole batch (atomic — no half-applied replace across a
        199-chapter novel). Bumps updated_at, never changes status/error. Column names
        are whitelisted because they are interpolated into the SQL; values stay bound.
        """
        with self._db:
            now = _now()
            for idx, cols in changes.items():
                if not cols:
                    continue
                bad = set(cols) - EDITABLE_COLUMNS
                if bad:
                    raise ValueError(f"non-editable column(s): {sorted(bad)}")
                assignments = ", ".join(f"{col} = ?" for col in cols)
                self._db.execute(
                    f"UPDATE chapters SET {assignments}, updated_at = ? WHERE idx = ?",
                    (*cols.values(), now, idx),
                )

    def mark_error(self, idx: int, message: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET status = ?, error = ?, updated_at = ? WHERE idx = ?",
                (STATUS_ERROR, message, _now(), idx),
            )

    def save_audio(
        self,
        idx: int,
        rel_path: str,
        voice: str,
        seconds: float,
        source: str = "translated",
    ) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET audio_path = ?, audio_voice = ?, audio_source = ?,"
                " audio_seconds = ?, audio_error = '', updated_at = ? WHERE idx = ?",
                (rel_path, voice, source, seconds, _now(), idx),
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

    def save_meta_translation(
        self, title: str, description: str, lang: str, author: str = ""
    ) -> None:
        """Persist the translated novel title/description/author into meta.json."""
        self.meta.translated_title = title
        self.meta.translated_description = description
        self.meta.translated_author = author
        self.meta.translated_lang = lang
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(
            translated_title=title, translated_description=description,
            translated_author=author, translated_lang=lang,
        )
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def refresh_meta(self, meta: NovelMeta) -> None:
        """Write a re-scan's scraped fields back into meta.json.

        Without this a re-scan updated the chapter list and nothing else, so a project
        created while a site adapter was buggy kept the wrong title and author **for
        ever** — re-scanning appeared to do nothing and the only cure was deleting the
        project. Found exactly that way on tieuthuyetmang (046).

        Only the SCRAPED fields move. The translated title/description/author, the
        generated tags and the thumbnail prompt are this app's own work, cost real time
        and money to produce, and a scan knows nothing about them — overwriting them with
        a fresh `NovelMeta`'s blank defaults would silently destroy them.
        """
        scraped = {
            "site": meta.site,
            "title": meta.title,
            "author": meta.author,
            "description": meta.description,
            "cover_url": meta.cover_url,
            "source_lang": meta.source_lang,
        }
        for field, value in scraped.items():
            setattr(self.meta, field, value)
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(scraped)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_tags(self, tags: str) -> None:
        """Persist the generated YouTube tag list (comma-joined) into meta.json."""
        self.meta.tags = tags
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(tags=tags)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_thumbnail_prompt(self, prompt: str) -> None:
        """Persist the AI image-generation prompt for the thumbnail into meta.json."""
        self.meta.thumbnail_prompt = prompt
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(thumbnail_prompt=prompt)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_display_title(self, title: str) -> None:
        """Persist the title override used on video output into meta.json.

        Deliberately does NOT touch `translated_title`: that one still keys the video
        filename slug, and moving it would strand every rendered part and upload record.
        """
        title = (title or "").strip()
        self.meta.display_title = title
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(display_title=title)
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

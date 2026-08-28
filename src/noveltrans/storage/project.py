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
    AUDIO_SOURCE_DOWNLOADED,
    STATUS_DOWNLOADED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSLATED,
    Chapter,
    ChapterRef,
    NovelMeta,
    SourceAudio,
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
  -- the translation as it stood before a style rewrite; non-empty == already rewritten
  translated_raw       TEXT NOT NULL DEFAULT '',
  translated_title_raw TEXT NOT NULL DEFAULT '',
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

-- Audio published by the source site. A SEPARATE edition of the work, not a property of
-- a chapter: releases cover chapter ranges and do not line up with rows one-for-one, so
-- keying them off `chapters` made a five-chapter volume look like one chapter's audio.
CREATE TABLE IF NOT EXISTS source_audio (
  number     INTEGER PRIMARY KEY,   -- the site's chapterNumber; what /nghe/<n> keys off
  title      TEXT NOT NULL DEFAULT '',
  ord        INTEGER NOT NULL DEFAULT 0,  -- 1-based position in the manifest's order
  path       TEXT NOT NULL DEFAULT '',    -- project-relative; '' until downloaded
  seconds    REAL NOT NULL DEFAULT 0,
  error      TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
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
        translated_raw=row["translated_raw"],
        translated_title_raw=row["translated_title_raw"],
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


def _row_to_source_audio(row: sqlite3.Row) -> SourceAudio:
    return SourceAudio(
        number=row["number"],
        title=row["title"],
        ord=row["ord"],
        path=row["path"],
        seconds=row["seconds"],
        error=row["error"],
        updated_at=row["updated_at"],
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
            # existing translations predate the rewrite pass, so none of them has a
            # pre-rewrite copy — empty is exactly right
            "translated_raw": "TEXT NOT NULL DEFAULT ''",
            "translated_title_raw": "TEXT NOT NULL DEFAULT ''",
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
        self._migrate_downloaded_audio()

    def _migrate_downloaded_audio(self) -> None:
        """Move site-downloaded audio off the chapter rows and into `source_audio`.

        Feature 059 first stored it on `chapters`, which is what made a five-chapter
        volume show up as chapter N's narration. The files themselves are 50-200 MB and
        may no longer be re-fetchable, so this MOVES the rows rather than dropping them —
        the chapter's audio fields are cleared only once the release row exists.

        The release number is read back out of the chapter URL (`/doc/<n>` or `/nghe/<n>`),
        which is exactly what wrote it. A row whose URL carries no number is left alone
        rather than guessed at.
        """
        rows = list(
            self._db.execute(
                "SELECT idx, url, title, audio_path, audio_seconds FROM chapters"
                " WHERE audio_source = 'downloaded' AND audio_path != ''"
            )
        )
        if not rows:
            return
        with self._db:
            for order, row in enumerate(rows, start=1):
                match = re.search(r"/(?:doc|nghe)/(\d+)", row["url"] or "")
                if not match:
                    continue
                self._db.execute(
                    "INSERT OR IGNORE INTO source_audio"
                    " (number, title, ord, path, seconds, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        int(match.group(1)), row["title"], order,
                        row["audio_path"], row["audio_seconds"], _now(),
                    ),
                )
                self._db.execute(
                    "UPDATE chapters SET audio_path = '', audio_voice = '',"
                    " audio_source = 'translated', audio_seconds = 0, audio_error = ''"
                    " WHERE idx = ?",
                    (row["idx"],),
                )

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

    def pending_rewrite(
        self, target_lang: str, start_idx: int = 0, end_idx: int | None = None
    ) -> list[Chapter]:
        """Translated chapters in `target_lang` whose style has not been rewritten yet.

        `translated_raw = ''` is the "not yet rewritten" flag, so an interrupted run
        resumes exactly where it stopped instead of paying for the same chapters twice.

        A legacy row with an empty `target_lang` — translated before the column was
        recorded — counts as a match. Skipping it would leave those chapters permanently
        ineligible with no way for the user to tell why.
        """
        sql = (
            "SELECT * FROM chapters"
            " WHERE translated != '' AND translated_raw = ''"
            "   AND (target_lang = ? OR target_lang = '')"
            "   AND idx >= ?"
        )
        params: list = [target_lang, start_idx]
        if end_idx is not None:
            sql += " AND idx <= ?"
            params.append(end_idx)
        sql += " ORDER BY idx"
        rows = self._db.execute(sql, params).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def pending_audio(
        self,
        voice: str = "",
        use_translation: bool = True,
        include_downloaded: bool = False,
    ) -> list[Chapter]:
        """Chapters that need audio in `voice` from the requested source.

        `use_translation` chooses which text gates availability — the translation
        (default) or the original `content`. A chapter is pending if it has that source
        text AND (has no audio yet, OR its audio used a *different* voice, OR its audio
        was made from the *other* source). Empty `voice` skips the voice-mismatch check.

        `include_downloaded` guards audio fetched from the source site. Such a row has
        audio_source = AUDIO_SOURCE_DOWNLOADED, which never equals the "translated" /
        "original" the voice-mismatch clause tests, so it would otherwise be pending on
        *every* pass — a bulk "tạo audio" run would re-voice it with TTS and the
        stale-file unlink in AudioWorker would delete the downloaded file. Excluded by
        default; pass True only when the caller genuinely means to overwrite narration.
        """
        src_col = "translated" if use_translation else "content"
        source = "translated" if use_translation else "original"
        keep_downloaded = "" if include_downloaded else " AND audio_source != ?"
        params: tuple = (voice, voice, source)
        if not include_downloaded:
            params += (AUDIO_SOURCE_DOWNLOADED,)
        rows = self._db.execute(
            f"""
            SELECT * FROM chapters
            WHERE {src_col} != ''
              AND (audio_path = '' OR (? != '' AND audio_voice != ?) OR audio_source != ?)
              {keep_downloaded}
            ORDER BY idx
            """,
            params,
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
        # Subset of `translated`: chapters whose translation has been style-rewritten,
        # so the rewrite dialog can say how much of the novel is left to do.
        rewritten = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE translated_raw != ''"
        ).fetchone()[0]
        errors = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE status = ?", (STATUS_ERROR,)
        ).fetchone()[0]
        audio = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE audio_path != ''"
        ).fetchone()[0]
        # Subset of `audio`, not a sibling of it: narration fetched from the source site
        # rather than synthesised, so the tab can report the two kinds apart.
        downloaded_audio = self._db.execute(
            "SELECT COUNT(*) FROM chapters WHERE audio_path != '' AND audio_source = ?",
            (AUDIO_SOURCE_DOWNLOADED,),
        ).fetchone()[0]
        return {
            "total": total,
            "downloaded": downloaded,
            "translated": translated,
            "rewritten": rewritten,
            "errors": errors,
            "audio": audio,
            "downloaded_audio": downloaded_audio,
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

    def save_rewrite(self, idx: int, title: str, text: str) -> None:
        """Replace a chapter's translation with its style-rewritten version.

        The first rewrite captures the pre-rewrite text; later ones must not, or a second
        pass would record the first pass's output as "the original" and the way back would
        be gone. That is the same trap `edit_title` documents for `title_source`, so it
        uses the same capture-once shape. Both CASE arms key off `translated_raw`: an
        empty title backup is a legal state (a title that rewrote to itself), so it cannot
        be the sentinel for itself.

        `target_lang` and `translator` are deliberately untouched. The chapter is still
        the same translation into the same language by the same engine — it has only been
        restyled — and writing `target_lang` here would risk re-flagging the chapter in
        `pending_translation` and queueing the whole novel for re-translation.

        `error` is cleared so a chapter that failed an earlier rewrite attempt comes back
        clean on success.
        """
        with self._db:
            self._db.execute(
                """
                UPDATE chapters SET
                    translated_raw = CASE WHEN translated_raw = '' THEN translated
                                          ELSE translated_raw END,
                    translated_title_raw = CASE WHEN translated_raw = ''
                                                THEN translated_title
                                                ELSE translated_title_raw END,
                    translated = ?, translated_title = ?,
                    status = ?, error = '', updated_at = ?
                WHERE idx = ?
                """,
                (text, title, STATUS_TRANSLATED, _now(), idx),
            )

    def restore_translation(self, idx: int | None = None) -> int:
        """Undo a style rewrite, putting the pre-rewrite translation back.

        `idx=None` restores the whole novel. Returns how many chapters were restored.
        Blanking the backup is what makes a chapter eligible for a fresh rewrite, so
        "already rewritten" and "can be rewritten" stay one flag read two ways.

        This restores the text as it stood **at the moment of the rewrite**: manual edits
        and find-and-replace runs made afterwards write `translated` without touching the
        backup, so they are lost. The GUI must say so before calling this.
        """
        sql = (
            "UPDATE chapters SET"
            "  translated = translated_raw,"
            "  translated_title = CASE WHEN translated_title_raw != ''"
            "                          THEN translated_title_raw"
            "                          ELSE translated_title END,"
            "  translated_raw = '', translated_title_raw = '', updated_at = ?"
            " WHERE translated_raw != ''"
        )
        params: list = [_now()]
        if idx is not None:
            sql += " AND idx = ?"
            params.append(idx)
        with self._db:
            cursor = self._db.execute(sql, params)
        return cursor.rowcount

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

    # ------------------------------------------------------- source audio (059)

    def sync_source_audio(self, entries: list[dict]) -> list[SourceAudio]:
        """Record the site's current release list, preserving anything already downloaded.

        Upserts titles and reading order from a fresh manifest without touching `path`,
        `seconds` or `error`: a re-listed novel must not lose the 1.7 GB already on disk
        just because the site reworded a volume title.
        """
        with self._db:
            for order, entry in enumerate(entries, start=1):
                number = entry.get("chapterNumber")
                if not isinstance(number, int):
                    continue
                title = str(entry.get("title") or f"Mục {number}")
                self._db.execute(
                    "INSERT INTO source_audio (number, title, ord, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(number) DO UPDATE SET title = excluded.title,"
                    " ord = excluded.ord",
                    (number, title, order, _now()),
                )
        return self.source_audio()

    def source_audio(self) -> list[SourceAudio]:
        """Every known release, in the manifest's reading order."""
        return [
            _row_to_source_audio(row)
            for row in self._db.execute(
                "SELECT * FROM source_audio ORDER BY ord, number"
            )
        ]

    def source_audio_at(self, number: int) -> SourceAudio | None:
        row = self._db.execute(
            "SELECT * FROM source_audio WHERE number = ?", (number,)
        ).fetchone()
        return _row_to_source_audio(row) if row is not None else None

    def save_source_audio(self, number: int, rel_path: str, seconds: float) -> None:
        with self._db:
            self._db.execute(
                "UPDATE source_audio SET path = ?, seconds = ?, error = '', updated_at = ?"
                " WHERE number = ?",
                (rel_path, seconds, _now(), number),
            )

    def mark_source_audio_error(self, number: int, message: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE source_audio SET error = ?, updated_at = ? WHERE number = ?",
                (message, _now(), number),
            )

    def mark_audio_error(self, idx: int, message: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE chapters SET audio_error = ?, updated_at = ? WHERE idx = ?",
                (message, _now(), idx),
            )

    def clear_audio(
        self, include_downloaded: bool = False, indices: list[int] | None = None
    ) -> int:
        """Reset audio state so the novel can be re-voiced. Returns rows affected.

        Does not delete the audio files — the worker overwrites them. That last part is
        exactly why downloaded narration is spared by default: nothing re-fetches it, so
        clearing its row would orphan the file on disk and lose the only record of where
        it came from, while the following TTS pass writes a *differently* named file. The
        user may also no longer be entitled to fetch it again. Pass True only for a
        deliberate "forget the downloads too".

        `indices` narrows it to specific chapters; None means the whole novel. The narrow
        form is what makes a source repair complete (feature 071): `pending_audio` re-queues
        on an empty `audio_path`, a voice mismatch or a source mismatch — never on the
        translation changing — so audio voiced from a bad translation would otherwise
        survive a repair and be consumed by the video pipeline as if it were fine.
        """
        clauses, params = [], [_now()]
        if not include_downloaded:
            clauses.append("audio_source != ?")
            params.append(AUDIO_SOURCE_DOWNLOADED)
        if indices is not None:
            if not indices:
                return 0
            clauses.append(f"idx IN ({','.join('?' * len(indices))})")
            params.extend(indices)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db:
            return self._db.execute(
                "UPDATE chapters SET audio_path = '', audio_voice = '',"
                " audio_seconds = 0, audio_error = '', updated_at = ?" + where,
                tuple(params),
            ).rowcount

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
        """Persist the generated YouTube tag list (comma-joined) into meta.json.

        Re-caps to the 500-char YouTube budget regardless of whether the caller already did —
        this is the single choke point every tags write goes through, so it's the one place
        that must never let an over-budget string reach disk.
        """
        from noveltrans.tts.tags import format_tags, parse_tags

        tags = format_tags(parse_tags(tags))
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

    def save_video_image_path(self, path: str) -> None:
        """Persist this novel's chosen video background image into meta.json.

        Per-novel, not a single global default: without this, the video tab's image box
        just held whatever was last picked for ANY novel, so switching novels could carry
        the previous novel's background image straight into this one's render.
        """
        path = (path or "").strip()
        self.meta.video_image_path = path
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(video_image_path=path)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_video_settings(self, values: dict) -> None:
        """Merge `values` into this novel's video settings and persist them.

        A merge, not a replace: the Video tab saves one key as you change it, and a
        replace would wipe every other setting the novel had. Same per-novel reasoning
        as `save_video_image_path` — see `noveltrans.video_settings` for why some keys
        are inherited from the user's last-used value and some are deliberately not.
        """
        merged = {**self.meta.video_settings, **values}
        self.meta.video_settings = merged
        # Keep the standalone field in step: it predates video_settings and is still what
        # older builds (and `save_video_image_path`) read, so letting the two disagree
        # would resurrect the very bug this exists to fix.
        if "video_image_path" in merged:
            self.meta.video_image_path = str(merged["video_image_path"] or "")
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(video_settings=merged, video_image_path=self.meta.video_image_path)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_upload_playlist(self, name: str) -> None:
        """Persist this novel's chosen YouTube playlist into meta.json. Same reasoning as
        `save_video_image_path` — one novel's playlist choice must not leak into another's."""
        name = (name or "").strip()
        self.meta.upload_playlist = name
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(upload_playlist=name)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_upload_visibility(self, key: str) -> None:
        """Persist this novel's chosen YouTube visibility ("private"/"unlisted"/"public"/
        "schedule") into meta.json — same per-novel reasoning as `save_video_image_path`,
        chosen explicitly by the user over the safer "always reset to Riêng tư" default."""
        key = (key or "").strip()
        self.meta.upload_visibility = key
        meta_path = self.path / META_FILE
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(upload_visibility=key)
        meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear_translations(self, indices: list[int] | None = None) -> int:
        """Drop translations so chapters can be re-translated. Returns rows affected.

        `indices` narrows it to specific chapters; None means the whole novel, so the
        "Dịch lại toàn bộ" path is unchanged. The narrow form exists for repairs where
        only some chapters were translated from bad source text (feature 071) — the rest
        of the novel's translations, including any hand edits, must survive untouched.
        """
        sql = (
            "UPDATE chapters SET translated = '', translated_title = '', target_lang = '',"
            "  translator = '', translate_seconds = 0,"
            # the backup belongs to the translation being dropped; keeping it would
            # leave every chapter flagged as rewritten, with an undo that restores
            # text from a translation that no longer exists
            "  translated_raw = '', translated_title_raw = '',"
            "  status = CASE WHEN content = '' THEN ? ELSE ? END, updated_at = ?"
        )
        params: tuple = (STATUS_PENDING, STATUS_DOWNLOADED, _now())
        if indices is not None:
            if not indices:
                return 0
            sql += f" WHERE idx IN ({','.join('?' * len(indices))})"
            params += tuple(indices)
        with self._db:
            return self._db.execute(sql, params).rowcount

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

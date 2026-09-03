"""Background QThread workers.

Workers never touch Qt widgets and never share a NovelProject/sqlite
connection across threads: they receive a *path* and open their own
NovelProject inside run(). The GUI keeps its own read connection.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from noveltrans.errors import (
    DailyLimitError,
    NovelTransError,
    RateLimitedError,
    UnsupportedSiteError,
)
from noveltrans.gui.pause import PauseGate
from noveltrans.models import AUDIO_SOURCE_DOWNLOADED, ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.storage import Library, NovelProject

# Some sites (e.g. medoctruyen.vn) throttle after a few chapters read quickly.
# On a rate-limit signal the download waits, then retries the same chapter.
_RATE_LIMIT_WAIT_SECONDS = 60
_RATE_LIMIT_MAX_RETRIES = 8


class PausableWorker(QThread):
    """A long batch worker that can be stopped, or held between items.

    Pause deliberately holds at the SAME points that already test `_cancelled` — the
    boundary between one chapter/part and the next. The item in flight always finishes,
    so nothing is ever half-written and resuming costs nothing. The price is that pause
    is not instant: a TTS chapter takes ~a minute, and a single ffmpeg merge or video
    encode can take far longer (see the `cancelled=` hand-offs, which must NOT gate).

    `cancel()` resumes the gate. That one line is why quitting with a paused job works:
    every tab's `shutdown()` calls `cancel()` and then `wait()` on the thread for up to
    two minutes, so a gate that stayed shut through a cancel would freeze the GUI thread
    and then abandon a running QThread. `PauseGate.wait` polls as well, so the two guards
    are independent (see `gui/pause.py`).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False
        self._gate = PauseGate()

    def cancel(self) -> None:
        self._cancelled = True
        self._gate.resume()  # never let a paused worker outlive its cancel

    def pause(self) -> None:
        self._gate.pause()

    def resume(self) -> None:
        self._gate.resume()

    def is_paused(self) -> bool:
        # A cancelled worker is on its way out, not paused, however the gate looks.
        return self._gate.paused and not self._cancelled

    def _checkpoint(self) -> bool:
        """Hold here while paused. True means the run should stop (cancelled)."""
        if self._cancelled:
            return True
        self._gate.wait(lambda: self._cancelled)
        return self._cancelled


class ScanWorker(QThread):
    """Fetch metadata + TOC for a URL and create/refresh the project."""

    progress = Signal(str)  # human-readable status (e.g. "opening a browser…")
    scanned = Signal(str, object, int)  # project path, NovelMeta, chapter count
    failed = Signal(str)

    def __init__(
        self, url: str, library_dir: Path, delay: float, cookies: str = "", parent=None
    ):
        super().__init__(parent)
        self.url = url
        self.library_dir = library_dir
        self.delay = delay
        self.cookies = cookies

    def run(self) -> None:
        adapter = None
        try:
            client = HttpClient(delay_seconds=self.delay)
            adapter = adapter_for_url(self.url, client)
            if adapter is None:
                raise UnsupportedSiteError(
                    f"Chưa hỗ trợ trang web này: {self.url}"
                )
            # Unconditional: the caller resolved which site's cookie this is
            # (`AppConfig.cookies_for_url`), and `set_cookies` ignores a blank string.
            client.set_cookies(self.cookies)
            adapter.on_status = self.progress.emit
            meta = adapter.fetch_metadata(self.url)
            refs = adapter.fetch_chapter_list(self.url)

            library = Library(self.library_dir)
            # Look the project up by BOTH the pasted URL and the adapter's canonical one.
            # Adapters that canonicalise (bookqq folds /book-read/<id>/<n> to the detail
            # page; giatocvuongtai normalises its slug) store the canonical form in
            # meta.url, while find_by_url is exact string equality — so a re-scan pasted
            # from a chapter page would miss the existing project, fall through to
            # create_project, and NovelProject.create overwrites meta.json wholesale,
            # discarding translated_title, tags, thumbnail_prompt and video_settings.
            # Chapter content survives (replace_toc preserves it) but everything
            # refresh_meta exists to protect would be gone.
            existing = library.find_by_url(self.url) or library.find_by_url(meta.url)
            if existing is not None:
                project = NovelProject.open(existing)
                project.replace_toc(refs)  # pick up newly published chapters
                project.refresh_meta(meta)  # …and a corrected title/author/description
            else:
                project = library.create_project(meta, refs)
            path = str(project.path)
            project.close()
            self.scanned.emit(path, meta, len(refs))
        except NovelTransError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # unexpected — still must not crash the app
            self.failed.emit(f"Lỗi không mong đợi: {exc!r}")
        finally:
            if adapter is not None:
                adapter.close()  # 69shuba holds a browser; don't leak it


class TranslateWorker(PausableWorker):
    """Translate pending chapters of a project (or specific ones), resumably."""

    progress = Signal(int, int, str)  # done, total, chapter title
    chapter_done = Signal(int)
    chapter_error = Signal(int, str)
    failed = Signal(str)  # engine could not even be constructed
    finished_ok = Signal(int, int)  # translated count, error count

    def __init__(
        self,
        project_path: Path,
        engine_name: str,
        target_lang: str,
        *,
        api_key: str = "",
        model: str = "",
        request_delay: float = 1.0,
        cli_command: str = "",
        base_url: str = "",
        indices: list[int] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.engine_name = engine_name
        self.target_lang = target_lang
        self.api_key = api_key
        self.model = model
        self.request_delay = request_delay
        self.cli_command = cli_command
        self.base_url = base_url
        self.indices = indices  # None = all pending; else re-translate exactly these


    def engine_label(self) -> str:
        """Human-readable record of what translated a chapter, e.g. 'CLI (agy)'."""
        if self.engine_name == "google":
            return "Google Translate"
        if self.engine_name == "claude":
            return f"Claude API ({self.model})" if self.model else "Claude API"
        if self.engine_name == "lmstudio":
            return f"LM Studio ({self.model})" if self.model else "LM Studio"
        parts = (self.cli_command or "").split()
        binary = parts[0] if parts else self.engine_name
        return f"CLI ({binary}, {self.model})" if self.model else f"CLI ({binary})"

    def _run_identity(self, project: NovelProject, pending: list) -> None:
        """Passthrough 'translation' when source_lang == target_lang: copy the
        original text into `translated` so downstream steps have data to work with.
        """
        total = len(pending)
        done = 0
        if project.meta.translated_lang != self.target_lang:
            project.save_meta_translation(
                project.meta.title, project.meta.description, self.target_lang,
                project.meta.author,  # identity: source already in the target language
            )
        for chapter in pending:
            if self._checkpoint():
                break
            self.progress.emit(done, total, chapter.title)
            project.save_translation(
                chapter.index,
                chapter.title,
                chapter.content,
                self.target_lang,
                "(nguyên bản)",
                seconds=0.0,
            )
            self.chapter_done.emit(chapter.index)
            done += 1
        self.progress.emit(done, total, "")
        self.finished_ok.emit(done, 0)

    def run(self) -> None:
        from noveltrans.name_glossary import (
            applied_glossary,
            build_from_project,
            read_names,
            write_names,
        )
        from noveltrans.translators.names import apply_glossary

        project = NovelProject.open(self.project_path)
        try:
            if self.indices is not None:
                chapters = (project.chapter(i) for i in self.indices)
                pending = [c for c in chapters if c is not None and c.content]
            else:
                pending = project.pending_translation(self.target_lang)

            # A source already in the target language (e.g. Vietnamese novels from
            # medoctruyen.vn with target "vi") needs no engine: copy the original
            # into `translated` so TTS/export work with the same DB shape.
            if project.meta.source_lang == self.target_lang:
                self._run_identity(project, pending)
                return

            from noveltrans.translators import get_translator

            try:
                translator = get_translator(
                    self.engine_name,
                    api_key=self.api_key,
                    model=self.model,
                    request_delay=self.request_delay,
                    cli_command=self.cli_command,
                    base_url=self.base_url,
                )
            except NovelTransError as exc:
                self.failed.emit(str(exc))
                return

            total = len(pending)
            done = 0
            errors = 0

            # Pre-replace recurring character names with their Hán-Việt reading, so the
            # same person is spelled the same way in every chapter.
            #
            # This used to run for Google only, on the reasoning that Google romanises
            # names to pinyin while the LLM engines are told to use Hán-Việt in their
            # prompt. But a prompt instruction only has scope over ONE request, and a long
            # chapter is several requests — so the LLM engines had nothing enforcing
            # consistency at all and re-derived every name from scratch. That is feature
            # 072's bug report: one novel's character came back with two different
            # spellings in different chapters.
            #
            # The list is read from the novel's own `names.json` rather than rebuilt each
            # run, so the user's corrections stick and the run is deterministic. A novel
            # that has never been scanned gets one built and saved here — the review dialog
            # is the CORRECTION path, not the activation path, or every existing user would
            # silently lose the substitution they have today.
            glossary: dict[str, str] = {}
            if self.target_lang == "vi":
                entries = read_names(project.path)
                if not entries:
                    self.progress.emit(0, total, "Đang dò tên nhân vật…")
                    entries = build_from_project(project)
                    if entries:
                        write_names(project.path, entries, chapters_scanned=total)
                glossary = applied_glossary(entries)
            if self._cancelled:
                return

            # translate the novel title/description once, for export front matter
            if project.meta.translated_lang != self.target_lang and not self._cancelled:
                try:
                    self.progress.emit(0, total, project.meta.title)
                    meta_title, meta_desc = translator.translate_chapter(
                        apply_glossary(project.meta.title, glossary),
                        apply_glossary(project.meta.description, glossary),
                        source=project.meta.source_lang,
                        target=self.target_lang,
                    )
                    meta_author = ""
                    if project.meta.author:
                        meta_author, _ = translator.translate_chapter(
                            apply_glossary(project.meta.author, glossary), "",
                            source=project.meta.source_lang,
                            target=self.target_lang,
                        )
                    project.save_meta_translation(
                        meta_title, meta_desc, self.target_lang, meta_author
                    )
                except Exception:  # noqa: BLE001 — non-fatal, chapters still translate
                    pass

            for chapter in pending:
                if self._checkpoint():
                    break
                self.progress.emit(done, total, chapter.title)
                try:
                    source_title = apply_glossary(chapter.title, glossary)
                    source_content = apply_glossary(chapter.content, glossary)
                    started = time.monotonic()
                    title, text = translator.translate_chapter(
                        source_title,
                        source_content,
                        source=project.meta.source_lang,
                        target=self.target_lang,
                    )
                    project.save_translation(
                        chapter.index,
                        title,
                        text,
                        self.target_lang,
                        self.engine_label(),
                        seconds=time.monotonic() - started,
                    )
                    self.chapter_done.emit(chapter.index)
                except NovelTransError as exc:
                    errors += 1
                    project.mark_error(chapter.index, str(exc))
                    self.chapter_error.emit(chapter.index, str(exc))
                except Exception as exc:  # keep the batch going
                    errors += 1
                    project.mark_error(chapter.index, repr(exc))
                    self.chapter_error.emit(chapter.index, repr(exc))
                done += 1
            self.progress.emit(done, total, "")
            self.finished_ok.emit(done - errors, errors)
        finally:
            project.close()


def chapters_to_rewrite(
    project: NovelProject,
    target_lang: str,
    *,
    indices: list[int] | None = None,
    start_idx: int = 0,
    end_idx: int | None = None,
    force: bool = False,
) -> list:
    """Chapters a rewrite run would touch, in reading order.

    Only ever chapters that HAVE a translation: the pass restyles `translated`, it cannot
    create one. `force` re-runs chapters already rewritten once; without it the resume
    query skips them.

    Shared with `RewriteDialog` on purpose — the count the dialog shows before the user
    commits to hours of work has to be the same set the worker then processes.
    """
    if indices is not None:
        chapters = (project.chapter(i) for i in indices)
        return [c for c in chapters if c is not None and c.translated]
    if force:
        return [
            c
            for c in project.chapters_in_range(start_idx, end_idx)
            if c.translated and c.target_lang in (target_lang, "")
        ]
    return project.pending_rewrite(target_lang, start_idx, end_idx)


class RewriteWorker(PausableWorker):
    """Rewrite the style of already-translated chapters, resumably.

    A separate, user-triggered pass — never folded into translation. Translating is free
    and instant for a Vietnamese-source novel (`TranslateWorker._run_identity` just
    copies the text), so bolting an hours-long LLM run onto the same button would be a
    surprise cost on the user's own quota or API key. It also has to reach novels that
    were merely translated badly, which the identity path never touches.

    Signals mirror `TranslateWorker`'s exactly so the tab can reuse its handlers.

    **A chapter that fails validation keeps its existing translation, byte for byte.**
    `save_rewrite` is simply not reached: the chapter is marked errored and the batch
    moves on. Unlike a failed translation, where the alternative is no text at all, the
    alternative here is the perfectly good translation already on screen.
    """

    progress = Signal(int, int, str)  # done, total, chapter title
    chapter_done = Signal(int)
    chapter_error = Signal(int, str)
    failed = Signal(str)  # engine could not even be constructed / cannot do this
    finished_ok = Signal(int, int)  # rewritten count, error count
    preview_ready = Signal(int, str, str)  # dry run only: index, title, body

    def __init__(
        self,
        project_path: Path,
        engine_name: str,
        target_lang: str = "vi",
        *,
        api_key: str = "",
        model: str = "",
        cli_command: str = "",
        base_url: str = "",
        indices: list[int] | None = None,
        start_idx: int = 0,
        end_idx: int | None = None,
        force: bool = False,
        dry_run: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.engine_name = engine_name
        self.target_lang = target_lang
        self.api_key = api_key
        self.model = model
        self.cli_command = cli_command
        self.base_url = base_url
        self.indices = indices  # None = every eligible chapter in range
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.force = force  # re-rewrite chapters already rewritten once
        self.dry_run = dry_run  # preview: emit the result, write nothing

    def run(self) -> None:
        from noveltrans.translators import get_translator
        from noveltrans.translators.rewrite import rewrite_chapter

        project = NovelProject.open(self.project_path)
        try:
            try:
                translator = get_translator(
                    self.engine_name,
                    api_key=self.api_key,
                    model=self.model,
                    cli_command=self.cli_command,
                    base_url=self.base_url,
                )
            except NovelTransError as exc:
                self.failed.emit(str(exc))
                return
            # Google can only translate. The dialog already hides it, but the choice is
            # persisted config and could be stale or hand-edited, so re-check here.
            if not translator.supports_completion:
                self.failed.emit(
                    "Engine này không viết lại được — hãy chọn CLI Agent, Claude "
                    "hoặc LM Studio."
                )
                return

            pending = chapters_to_rewrite(
                project,
                self.target_lang,
                indices=self.indices,
                start_idx=self.start_idx,
                end_idx=self.end_idx,
                force=self.force,
            )
            total = len(pending)
            done = 0
            errors = 0

            for chapter in pending:
                if self._checkpoint():
                    break
                self.progress.emit(done, total, chapter.translated_title or chapter.title)
                try:
                    # `complete` is called with ONE positional argument and no system
                    # prompt — the only signature all three LLM engines share (the CLI
                    # agent passes the prompt as an argv entry and has no second channel).
                    title, text = rewrite_chapter(
                        translator.complete,
                        chapter.translated_title,
                        chapter.translated,
                        max_chunk_chars=translator.max_chunk_chars,
                    )
                    if self.dry_run:
                        self.preview_ready.emit(chapter.index, title, text)
                    else:
                        project.save_rewrite(chapter.index, title, text)
                        self.chapter_done.emit(chapter.index)
                except NovelTransError as exc:
                    errors += 1
                    self._record_error(project, chapter.index, str(exc))
                except Exception as exc:  # keep the batch going
                    errors += 1
                    self._record_error(project, chapter.index, repr(exc))
                done += 1
            self.progress.emit(done, total, "")
            self.finished_ok.emit(done - errors, errors)
        finally:
            project.close()

    def _record_error(self, project: NovelProject, idx: int, message: str) -> None:
        """Report a chapter that could not be rewritten, leaving its translation alone.

        A preview writes nothing at all — not even the error — because the user is only
        being shown what a rewrite would do.
        """
        if not self.dry_run:
            project.mark_error(idx, message)
        self.chapter_error.emit(idx, message)


class NameScanWorker(QThread):
    """Detect character names across a whole novel, off the GUI thread.

    Reads only: the review dialog decides what to keep and writes the file itself. Joining
    a whole novel's text and initialising the segmenter takes seconds to minutes on a long
    novel, which is the only reason this is a thread at all.
    """

    scanned = Signal(list)  # list[NameEntry]

    def __init__(self, project_path, parent=None):
        super().__init__(parent)
        self.project_path = project_path

    def run(self) -> None:
        from noveltrans.name_glossary import build_from_project
        from noveltrans.storage import NovelProject

        # Its own handle: this runs on a different thread from whoever opened the project.
        project = NovelProject.open(self.project_path)
        try:
            entries = build_from_project(project)
        except Exception:  # noqa: BLE001 — a failed scan must not take the window down
            entries = []
        finally:
            project.close()
        self.scanned.emit(entries)


class CliModelsWorker(QThread):
    """List the models an agent CLI offers (`<binary> models`), for the model box."""

    models_listed = Signal(str, list)  # binary, model labels

    def __init__(self, binary: str, parent=None):
        super().__init__(parent)
        self.binary = binary

    def run(self) -> None:
        import subprocess

        try:
            result = subprocess.run(
                [self.binary, "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            models = (
                [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if result.returncode == 0
                else []
            )
        except Exception:  # missing binary, no `models` subcommand, timeout…
            models = []
        self.models_listed.emit(self.binary, models)


class LmStudioModelsWorker(QThread):
    """List the models an LM Studio server offers, for the model box."""

    models_listed = Signal(str, list)  # base_url, model ids

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self.base_url = base_url

    def run(self) -> None:
        from noveltrans.translators.lmstudio import list_models

        self.models_listed.emit(self.base_url, list_models(self.base_url))


class TagsWorker(QThread):
    """Generate the novel's YouTube tags via an LLM engine (like the '2. Dịch' engines).

    Takes the same engine params as TranslateWorker, prompts the chosen engine's
    `complete()`, parses/caps the reply to YouTube's 500-char budget, and persists the
    tags on the project's meta. Emits the comma-joined tag string on success.
    """

    finished_ok = Signal(str)  # comma-joined tags
    failed = Signal(str)

    def __init__(
        self,
        project_path: Path,
        engine_name: str,
        *,
        api_key: str = "",
        model: str = "",
        cli_command: str = "",
        base_url: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.engine_name = engine_name
        self.api_key = api_key
        self.model = model
        self.cli_command = cli_command
        self.base_url = base_url

    def run(self) -> None:
        from noveltrans.translators import get_translator
        from noveltrans.tts.tags import build_tags_prompt, format_tags, parse_tags

        project = NovelProject.open(self.project_path)
        try:
            try:
                translator = get_translator(
                    self.engine_name,
                    api_key=self.api_key,
                    model=self.model,
                    cli_command=self.cli_command,
                    base_url=self.base_url,
                )
            except NovelTransError as exc:
                self.failed.emit(str(exc))
                return
            if not translator.supports_completion:
                self.failed.emit(
                    "Engine này không tạo được tags — hãy chọn CLI Agent, Claude "
                    "hoặc LM Studio."
                )
                return
            meta = project.meta
            prompt = build_tags_prompt(
                vn_title=meta.display_name(),
                original_title=meta.title,
                author=meta.translated_author or meta.author,
                vn_description=meta.translated_description,
            )
            try:
                raw = translator.complete(prompt)
            except NovelTransError as exc:
                self.failed.emit(str(exc))
                return
            except Exception as exc:  # noqa: BLE001 — engine/library-specific errors
                self.failed.emit(f"Lỗi khi tạo tags: {exc!r}")
                return
            tags = format_tags(parse_tags(raw))
            if not tags:
                self.failed.emit("Không tạo được tags (phản hồi rỗng).")
                return
            project.save_tags(tags)
            self.finished_ok.emit(tags)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Lỗi khi tạo tags: {exc!r}")
        finally:
            project.close()


class CompletionWorker(QThread):
    """Run one free-form LLM prompt on a chosen engine and return its text.

    A generic helper (used e.g. to generate an image-generation prompt for the thumbnail):
    it takes the same engine params as TranslateWorker plus a prompt string, calls the
    engine's `complete()`, and emits the raw reply. Persistence, parsing, etc. are left to
    the caller. Requires an LLM engine (`supports_completion`).
    """

    finished_ok = Signal(str)  # the model's text reply
    failed = Signal(str)

    def __init__(
        self,
        engine_name: str,
        prompt: str,
        *,
        api_key: str = "",
        model: str = "",
        cli_command: str = "",
        base_url: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.engine_name = engine_name
        self.prompt = prompt
        self.api_key = api_key
        self.model = model
        self.cli_command = cli_command
        self.base_url = base_url

    def run(self) -> None:
        from noveltrans.translators import get_translator

        try:
            translator = get_translator(
                self.engine_name,
                api_key=self.api_key,
                model=self.model,
                cli_command=self.cli_command,
                base_url=self.base_url,
            )
        except NovelTransError as exc:
            self.failed.emit(str(exc))
            return
        if not translator.supports_completion:
            self.failed.emit(
                "Engine này không tạo được nội dung — hãy chọn CLI Agent, Claude "
                "hoặc LM Studio."
            )
            return
        try:
            reply = translator.complete(self.prompt)
        except NovelTransError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — engine/library-specific errors
            self.failed.emit(f"Lỗi: {exc!r}")
            return
        reply = (reply or "").strip()
        if not reply:
            self.failed.emit("Phản hồi rỗng.")
            return
        self.finished_ok.emit(reply)


class ShortenTitlesWorker(QThread):
    """Shorten a part's chapter titles via an LLM, in chunks, order preserved.

    Backs the video tab's "Shorten by AI" button. Takes the same engine params as
    `TagsWorker` but opens no project: it is handed plain strings and hands plain strings
    back, so the caller owns every read and write.

    Chunked because one part can hold hundreds of chapters, and a chunk whose reply doesn't
    parse falls back to that chunk's *original* titles instead of failing the whole run —
    losing 60 shortened titles because the model miscounted is worse than keeping them long,
    and returning fewer lines than chapters would silently misalign every timestamp after
    the gap. The returned list therefore always has exactly one entry per input title.

    Only a translator that can't be built, an engine without `complete()`, or *every* chunk
    falling back reaches `failed`.
    """

    CHUNK = 60

    progress = Signal(int, int)  # titles done, total
    finished_ok = Signal(list, int)  # shortened titles, chunks that fell back
    failed = Signal(str)

    def __init__(
        self,
        titles: list[str],
        engine_name: str,
        *,
        api_key: str = "",
        model: str = "",
        cli_command: str = "",
        base_url: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.titles = list(titles)
        self.engine_name = engine_name
        self.api_key = api_key
        self.model = model
        self.cli_command = cli_command
        self.base_url = base_url

    def run(self) -> None:
        from noveltrans.translators import get_translator
        from noveltrans.tts.description import build_shorten_prompt, parse_shortened_titles

        if not self.titles:
            self.finished_ok.emit([], 0)
            return
        try:
            translator = get_translator(
                self.engine_name,
                api_key=self.api_key,
                model=self.model,
                cli_command=self.cli_command,
                base_url=self.base_url,
            )
        except NovelTransError as exc:
            self.failed.emit(str(exc))
            return
        if not translator.supports_completion:
            self.failed.emit(
                "Engine này không rút gọn được tên chương — hãy chọn CLI Agent, Claude "
                "hoặc LM Studio."
            )
            return

        result: list[str] = []
        fell_back = 0
        chunks = 0
        for start in range(0, len(self.titles), self.CHUNK):
            chunk = self.titles[start:start + self.CHUNK]
            chunks += 1
            try:
                raw = translator.complete(build_shorten_prompt(chunk))
                shortened, ok = parse_shortened_titles(raw, chunk)
            except NovelTransError:
                shortened, ok = list(chunk), False
            except Exception:  # noqa: BLE001 — engine/library-specific errors
                shortened, ok = list(chunk), False
            if not ok:
                fell_back += 1
            result.extend(shortened)
            self.progress.emit(len(result), len(self.titles))

        if fell_back == chunks:
            self.failed.emit("Không rút gọn được tên chương (phản hồi không hợp lệ).")
            return
        self.finished_ok.emit(result, fell_back)


@dataclass
class _AudioResult:
    """One chapter's synthesis outcome, passed from a pool thread back to the
    orchestrator (which owns the sqlite connection and does all DB writes)."""

    index: int
    title: str
    status: str  # "ok" | "error" | "cancelled"
    rel_path: str = ""
    seconds: float = 0.0
    prev_audio_path: str = ""  # chapter.audio_path, for stale-file cleanup
    prev_audio_source: str = ""  # chapter.audio_source, so cleanup can spare downloads
    # Fingerprint of the text this thread actually voiced. Computed here rather than by the
    # orchestrator: by the time the result is committed the orchestrator only has a row id,
    # and re-reading the chapter to hash it could pick up an edit made mid-run — recording
    # a fingerprint for text that was never spoken.
    text_hash: str = ""
    error: str = ""


class AudioWorker(PausableWorker):
    """Generate audio for a project's translated (or original) chapters, resumably.

    A single orchestrator QThread: it loads one "probe" engine up front (fail-fast
    + voice resolution), owns the one NovelProject sqlite connection, and performs
    all DB writes. With workers == 1 it runs a plain sequential loop; with
    workers > 1 it drives a thread pool whose threads each reuse their own engine
    and only synthesize files, handing results back here to commit.
    """

    progress = Signal(int, int, str)  # done, total, chapter title / phase message
    chapter_done = Signal(int)
    chapter_error = Signal(int, str)
    failed = Signal(str)  # engine could not be constructed/loaded
    finished_ok = Signal(int, int)  # ok count, error count

    def __init__(
        self,
        project_path: Path,
        voice: str,
        out_format: str = "wav",  # "wav" or "mp3" (mp3 needs ffmpeg)
        indices: list[int] | None = None,
        use_translation: bool = True,  # False = voice the original `content`
        workers: int = 1,  # >1 synthesizes chapters in parallel (N engines in RAM)
        clean_text: bool = True,  # strip special chars before synthesis
        clean_extra_remove: str = "",  # extra chars to strip on top of the automatic clean
        gap_seconds: float | None = None,  # silence between chunks (None = engine default)
        speed: float = 1.0,  # playback tempo via ffmpeg atempo (1.0 = unchanged)
        volume: float = 1.0,  # linear gain (1.0 = unchanged)
        temperature: float = 0.0,  # VieNeu expressiveness (0.0 = model default)
        precision: str = "int8",  # VieNeu ONNX graph: "int8" (fast) or "fp32" (accurate)
        style: str = "",  # reading style ("" = model default), independent of voice
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
        self.out_format = out_format
        self.indices = indices  # None = all pending; else re-generate exactly these
        self.use_translation = use_translation
        self.workers = max(1, int(workers))
        self.clean_text = clean_text
        self.clean_extra_remove = clean_extra_remove
        self.gap_seconds = gap_seconds
        self.speed = speed
        self.volume = volume
        self.temperature = temperature
        self.precision = precision
        self.style = style


    def _effective_temperature(self) -> float | None:
        """0.0 (the config "unset" sentinel) → None, so the engine passes nothing."""
        return self.temperature if self.temperature and self.temperature > 0 else None

    def _apply_speed(self, out_path: Path, seconds: float) -> float:
        """Post-process the rendered WAV to `self.speed` and return the rescaled
        duration. No-op at 1.0. Needs ffmpeg — silently skipped if absent (the Settings
        control is gated on ffmpeg, so this only happens if ffmpeg was removed later)."""
        if self.speed == 1.0:
            return seconds
        from noveltrans.tts.convert import apply_tempo, ffmpeg_available

        if not ffmpeg_available():
            return seconds
        apply_tempo(out_path, self.speed)
        return seconds / self.speed

    def _write_cues(self, audio_path, cues, raw_seconds: float, seconds: float) -> None:
        """Persist this chapter's subtitle cues beside its final audio file.

        The rescale is the whole reason this lives in the worker: cues come out of the
        engine in PRE-speed time, and `_apply_speed` has just stretched the audio with
        `apply_tempo`. Deriving the factor from the two durations rather than from
        `self.speed` means it stays correct when `_apply_speed` silently no-ops (no
        ffmpeg), which is exactly when a hard-coded `1 / speed` would desync everything.

        Best-effort: a subtitle sidecar must never cost someone their rendered audio.
        """
        if not cues:
            return
        from noveltrans.tts.subtitles import scale_cues, write_cues

        try:
            factor = (seconds / raw_seconds) if raw_seconds > 0 else 1.0
            write_cues(audio_path, scale_cues(cues, factor), seconds=seconds)
        except Exception:  # noqa: BLE001 — never fail a good render over subtitles
            pass

    def run(self) -> None:
        from noveltrans.errors import TtsError
        from noveltrans.tts import get_tts_engine

        try:
            # The "probe" engine: fail fast on load errors and resolve the voice
            # once. With parallel workers it becomes the first pool thread's engine
            # (seeded below), so its ~334 MB load is never wasted.
            probe = get_tts_engine(
                "vieneu",
                voice=self.voice,
                temperature=self._effective_temperature(),
                precision=self.precision,
                style=self.style,
            )
            self.progress.emit(0, 0, "Đang tải model VieNeu (~330 MB lần đầu)…")
            probe.load()
        except TtsError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(f"Lỗi không mong đợi khi nạp TTS: {exc!r}")
            return

        # The engine may have substituted a stale/unknown voice for a real one at
        # load(); adopt the resolved voice so the filename, pending_audio dedup, and
        # stored audio_voice all reflect the voice actually spoken.
        self.voice = getattr(probe, "voice", self.voice)
        notice = getattr(probe, "voice_notice", "")
        if notice:
            self.progress.emit(0, 0, notice)

        project = NovelProject.open(self.project_path)
        try:
            source = "translated" if self.use_translation else "original"
            if self.indices is not None:
                chapters = (project.chapter(i) for i in self.indices)
                pending = [
                    c
                    for c in chapters
                    if c is not None and (c.translated if self.use_translation else c.content)
                ]
            else:
                pending = project.pending_audio(self.voice, self.use_translation)
            project.audio_dir.mkdir(parents=True, exist_ok=True)

            if self.workers == 1:
                self._run_sequential(project, probe, pending, source)
            else:
                self._run_parallel(project, probe, pending, source)
        finally:
            project.close()

    def _title_text_for(self, chapter) -> tuple[str, str]:
        """The (title, text) handed to the engine.

        Delegates to the model so that what gets VOICED and what gets FINGERPRINTED are
        by construction the same pair — a second copy of this rule here is exactly how
        staleness detection would quietly start comparing the wrong thing.
        """
        return chapter.audio_source_text(self.use_translation)

    def _run_sequential(self, project, engine, pending: list, source: str) -> None:
        """The original single-engine loop — used whenever workers == 1."""
        from noveltrans.errors import TtsError
        from noveltrans.storage.project import slugify

        total = len(pending)
        done = 0
        errors = 0
        for chapter in pending:
            if self._checkpoint():
                break
            title, text = self._title_text_for(chapter)
            self.progress.emit(done, total, title)
            # voice in the filename: re-voicing creates a NEW file, so audio
            # players that cached/imported the old one can't play stale audio
            name = f"{chapter.index + 1:04d}-{slugify(title)}-{slugify(self.voice)}.wav"
            out_path = project.audio_dir / name
            try:
                cues: list = []
                raw_seconds = engine.synthesize_chapter(
                    title,
                    text,
                    out_path,
                    # Cancel only — do NOT gate pause here. This callback is polled inside a
                    # deadline-bounded ffmpeg/TTS wait; holding it would trip the timeout, and
                    # synthesize_chapter buffers the whole chapter in RAM until it writes.
                    cancelled=lambda: self._cancelled,
                    clean=self.clean_text,
                    clean_extra_remove=self.clean_extra_remove,
                    gap_seconds=self.gap_seconds,
                    volume=self.volume,
                    cues_out=cues,
                )
                seconds = self._apply_speed(out_path, raw_seconds)
                if self.out_format == "mp3":
                    from noveltrans.tts.convert import convert_to_mp3

                    out_path = convert_to_mp3(out_path)
                rel_path = out_path.relative_to(project.path).as_posix()
                # Never unlink narration fetched from the source site: the TTS filename
                # always differs from the download's, so this would delete a file nothing
                # here can recreate and the user may no longer be entitled to re-fetch.
                # An orphan on disk is the cheap failure; losing the audio is not.
                stale = chapter.audio_path and chapter.audio_path != rel_path
                if stale and chapter.audio_source != AUDIO_SOURCE_DOWNLOADED:
                    # re-voiced with another format — drop the stale old file
                    (project.path / chapter.audio_path).unlink(missing_ok=True)
                    _drop_cues(project.path / chapter.audio_path)
                self._write_cues(out_path, cues, raw_seconds, seconds)
                project.save_audio(
                    chapter.index,
                    rel_path,
                    self.voice,
                    seconds,
                    source,
                    text_hash=chapter.audio_fingerprint(self.use_translation),
                )
                self.chapter_done.emit(chapter.index)
            except TtsError as exc:
                if self._cancelled:
                    break  # mid-chapter cancel, not a real error
                errors += 1
                project.mark_audio_error(chapter.index, str(exc))
                self.chapter_error.emit(chapter.index, str(exc))
            except Exception as exc:  # keep the batch going
                errors += 1
                project.mark_audio_error(chapter.index, repr(exc))
                self.chapter_error.emit(chapter.index, repr(exc))
            done += 1
        self.progress.emit(done, total, "")
        self.finished_ok.emit(done - errors, errors)

    def _engine_for_thread(self, tl: threading.local, seed: "queue.Queue"):
        """One TTS engine per pool thread, loaded once and reused across chapters.

        The first thread reuses the already-loaded probe from `seed`; later threads
        build+load their own. Only min(workers, #chapters) engines ever load, since
        this runs lazily as pool threads actually start pulling work.
        """
        engine = getattr(tl, "engine", None)
        if engine is None:
            try:
                engine = seed.get_nowait()  # reuse the loaded probe on the first thread
            except queue.Empty:
                from noveltrans.tts import get_tts_engine

                engine = get_tts_engine(  # voice already resolved
                    "vieneu",
                    voice=self.voice,
                    temperature=self._effective_temperature(),
                    precision=self.precision,
                    style=self.style,
                )
                engine.load()  # lazy: only when a new thread actually starts
            tl.engine = engine
        return engine

    def _synth_one(self, chapter, source: str, project_path, audio_dir, tl, seed) -> _AudioResult:
        """Synthesize one chapter to disk on a pool thread. No sqlite access — the
        orchestrator commits the returned result on the connection-owning thread."""
        from noveltrans.errors import TtsError
        from noveltrans.storage.project import slugify

        if self._cancelled:
            return _AudioResult(chapter.index, "", "cancelled")
        engine = self._engine_for_thread(tl, seed)
        title, text = self._title_text_for(chapter)
        name = f"{chapter.index + 1:04d}-{slugify(title)}-{slugify(self.voice)}.wav"
        out_path = audio_dir / name
        try:
            cues: list = []
            raw_seconds = engine.synthesize_chapter(
                title,
                text,
                out_path,
                # Cancel only — do NOT gate pause here. This callback is polled inside a
                # deadline-bounded ffmpeg/TTS wait; holding it would trip the timeout, and
                # synthesize_chapter buffers the whole chapter in RAM until it writes.
                cancelled=lambda: self._cancelled,
                clean=self.clean_text,
                clean_extra_remove=self.clean_extra_remove,
                gap_seconds=self.gap_seconds,
                volume=self.volume,
                cues_out=cues,
            )
            seconds = self._apply_speed(out_path, raw_seconds)
            if self.out_format == "mp3":
                from noveltrans.tts.convert import convert_to_mp3

                out_path = convert_to_mp3(out_path)
            # Written here on the pool thread, not handed back through _AudioResult: each
            # chapter owns a distinct path, so there is nothing to serialise, and the
            # orchestrator has no business carrying subtitle data it never reads.
            self._write_cues(out_path, cues, raw_seconds, seconds)
            rel_path = out_path.relative_to(project_path).as_posix()
            return _AudioResult(
                chapter.index,
                title,
                "ok",
                rel_path,
                seconds,
                chapter.audio_path or "",
                chapter.audio_source or "",
                chapter.audio_fingerprint(self.use_translation),
            )
        except TtsError as exc:
            if self._cancelled:
                return _AudioResult(chapter.index, title, "cancelled")  # mid-chapter cancel
            return _AudioResult(chapter.index, title, "error", error=str(exc))
        except Exception as exc:  # keep the batch going
            return _AudioResult(chapter.index, title, "error", error=repr(exc))

    def _run_parallel(self, project, probe, pending: list, source: str) -> None:
        """Synthesize chapters across a thread pool; commit results here, in order
        of completion, on the sqlite-owning orchestrator thread."""
        total = len(pending)
        done = 0
        errors = 0
        if total == 0:
            self.progress.emit(0, 0, "")
            self.finished_ok.emit(0, 0)
            return

        tl = threading.local()
        seed: queue.Queue = queue.Queue()
        seed.put(probe)  # first pool thread reuses the already-loaded probe
        pending_iter = iter(pending)
        inflight: set = set()
        n_workers = min(self.workers, total)
        pool = ThreadPoolExecutor(max_workers=n_workers)

        def submit_next() -> bool:
            # Paused means "start nothing new". Returning False here cannot end the run
            # early: the drain loop holds at its checkpoint while paused instead of
            # treating an empty pool as "finished".
            if self._cancelled or self.is_paused():
                return False
            try:
                chapter = next(pending_iter)
            except StopIteration:
                return False
            inflight.add(
                pool.submit(
                    self._synth_one, chapter, source, project.path, project.audio_dir, tl, seed
                )
            )
            return True

        try:
            for _ in range(n_workers):
                if not submit_next():
                    break
            while True:
                # Hold here while paused — never break out of the loop for a pause, or
                # the run would report itself finished. Nothing new is submitted while
                # held (submit_next gates on it too); chapters already on pool threads
                # keep going and are committed below once we resume.
                self._checkpoint()
                if not inflight:
                    # Pool is empty: either the batch is done, or a pause drained it and
                    # a resume should refill it. submit_next says which.
                    if not submit_next():
                        break
                    continue
                finished, still = wait(inflight, return_when=FIRST_COMPLETED)
                inflight = set(still)
                for fut in finished:
                    result = fut.result()
                    if result.status == "cancelled":
                        continue  # not counted, no write (matches sequential break)
                    if result.status == "ok":
                        stale = (
                            result.prev_audio_path
                            and result.prev_audio_path != result.rel_path
                            # spare downloaded narration — see _run_sequential
                            and result.prev_audio_source != AUDIO_SOURCE_DOWNLOADED
                        )
                        if stale:
                            # re-voiced with another format — drop the stale old file
                            (project.path / result.prev_audio_path).unlink(missing_ok=True)
                            _drop_cues(project.path / result.prev_audio_path)
                        project.save_audio(
                            result.index,
                            result.rel_path,
                            self.voice,
                            result.seconds,
                            source,
                            text_hash=result.text_hash,
                        )
                        self.chapter_done.emit(result.index)
                    else:  # "error"
                        errors += 1
                        project.mark_audio_error(result.index, result.error)
                        self.chapter_error.emit(result.index, result.error)
                    done += 1
                    self.progress.emit(done, total, result.title)
                    submit_next()  # backfill; a no-op once cancelled or exhausted
        finally:
            pool.shutdown(wait=True)  # let in-flight chapters finish/cancel cleanly
        self.progress.emit(done, total, "")
        self.finished_ok.emit(done - errors, errors)


class AudioManifestWorker(QThread):
    """List what audio a source site publishes for one novel, off-thread.

    One request (the landing page carries the whole TOC), so this is a plain QThread with
    no pause gate — there is nothing to pause between.
    """

    listed = Signal(list)  # manifest entries, as the adapter returns them
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, url: str, delay: float = 2.0, cookies: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.delay = max(float(delay), 2.0)  # the adapter's politeness floor
        self.cookies = cookies

    def run(self) -> None:
        try:
            client = HttpClient(delay_seconds=self.delay)
            adapter = adapter_for_url(self.url, client)
            if adapter is None or not hasattr(adapter, "fetch_audio_manifest"):
                self.failed.emit("Nguồn của truyện này không hỗ trợ tải audio.")
                return
            client.set_cookies(self.cookies)
            adapter.on_status = self.progress.emit
            self.listed.emit(list(adapter.fetch_audio_manifest(self.url)))
        except NovelTransError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 — a background list must never crash the GUI
            self.failed.emit(f"Lỗi không mong đợi khi đọc danh sách audio: {exc!r}")


class AudioDownloadWorker(PausableWorker):
    """Fetch narration published by the source site, instead of synthesizing it.

    Writes to `source_audio`, NOT to chapter rows. Releases are a separate edition of the
    work — the reference novel ships 21 of them against 122 chapters, each covering a
    five-chapter range — so landing one on a chapter row made a whole volume look like
    that chapter's narration, and put site audio in the chapter list where it does not
    belong.

    Single-site (tieuthuyetmang) by design: the adapter must expose `fetch_audio_manifest`
    and `fetch_audio_url`, and the worker refuses politely when it does not. `SiteAdapter`
    is deliberately NOT widened for two methods only one site can answer.

    Sequential only. The adapter's docstring forbids parallel requests, and the files are
    large enough (up to 203 MB; 1.7 GB for a whole novel) that concurrency would buy
    little but a rate-limit.
    """

    progress = Signal(int, int, str)  # done, total, release title / phase message
    item_done = Signal(int)  # release number
    item_error = Signal(int, str)
    failed = Signal(str)  # nothing could be attempted at all
    finished_ok = Signal(int, int)  # ok count, error count

    def __init__(
        self,
        project_path: Path,
        delay: float = 2.0,
        cookies: str = "",
        numbers: list[int] | None = None,
        skip_downloaded: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        # The adapter asks for >=2s and says why; a lower setting elsewhere in the app
        # must not quietly override a politeness rule for a small paid site.
        self.delay = max(float(delay), 2.0)
        self.cookies = cookies
        self.numbers = numbers  # None = every release the manifest offers
        # Audio already fetched is not re-fetched: the files are 50-200 MB each and the
        # bytes cannot change. Only the explicit "Tải lại" affordances turn this off.
        self.skip_downloaded = skip_downloaded
        self.skipped = 0  # set by _resolve; read by the tab for its summary line

    def run(self) -> None:
        from noveltrans.errors import AudioUnavailableError, AuthRequiredError, ScrapeError
        from noveltrans.errors import TtsError
        from noveltrans.scrapers.tieuthuyetmang import chapter_url, slug
        from noveltrans.storage.project import slugify
        from noveltrans.tts.convert import DownloadCancelled, download_media, probe_duration

        project = NovelProject.open(self.project_path)
        try:
            client = HttpClient(delay_seconds=self.delay)
            adapter = adapter_for_url(project.meta.url, client)
            if adapter is None or not hasattr(adapter, "fetch_audio_url"):
                self.failed.emit("Nguồn của truyện này không hỗ trợ tải audio.")
                return
            client.set_cookies(self.cookies)

            done = 0
            total = 0
            adapter.on_status = lambda msg: self.progress.emit(done, total, msg)
            try:
                manifest = adapter.fetch_audio_manifest(project.meta.url)
            except (ScrapeError, NovelTransError) as exc:
                self.failed.emit(str(exc))
                return

            novel_slug = slug(project.meta.url)
            releases = project.sync_source_audio(manifest)
            targets = self._resolve(releases)
            total = len(targets)
            if self.skipped:
                self.progress.emit(0, total, f"Bỏ qua {self.skipped} mục đã tải.")
            if not total:
                message = (
                    f"Đã tải đủ {self.skipped} mục audio."
                    if self.skipped
                    else "Không tìm thấy mục nào có audio."
                )
                self.progress.emit(0, 0, message)
                self.finished_ok.emit(0, 0)
                return

            project.audio_dir.mkdir(parents=True, exist_ok=True)
            errors = 0
            for release in targets:
                if self._checkpoint():
                    break
                self.progress.emit(done, total, release.title)
                try:
                    url = adapter.fetch_audio_url(
                        ChapterRef(
                            index=release.index,
                            title=release.title,
                            # `fetch_audio_url` only reads the URL, to recover the slug
                            # and the number it turns into /nghe/<n>.
                            url=chapter_url(novel_slug, release.number),
                        )
                    )
                    # The extension comes from the URL: this site publishes some volumes
                    # as .mp3 and others as .aac, and a hardcoded suffix would leave AAC
                    # bytes in a file called .mp3 for every player downstream to mishandle.
                    suffix = Path(url.split("?")[0]).suffix.lower() or ".mp3"
                    name = f"nguon-{release.ord:04d}-{slugify(release.title)}{suffix}"
                    out_path = project.audio_dir / name
                    self._download(release, url, out_path, done, total, download_media)
                    seconds = probe_duration(out_path)
                    rel_path = out_path.relative_to(project.path).as_posix()
                    # The only file this may unlink is our OWN earlier download of the
                    # same release, and only when the extension changed.
                    if release.path and release.path != rel_path:
                        (project.path / release.path).unlink(missing_ok=True)
                    project.save_source_audio(release.number, rel_path, seconds)
                    self.item_done.emit(release.number)
                except DownloadCancelled:
                    break  # .part kept on disk; the next run resumes it
                except (AudioUnavailableError, AuthRequiredError, ScrapeError, TtsError) as exc:
                    # Report and carry on: one volume the account cannot reach must not
                    # abandon the twenty it can.
                    errors += 1
                    project.mark_source_audio_error(release.number, str(exc))
                    self.item_error.emit(release.number, str(exc))
                done += 1

            self.progress.emit(done, total, "")
            self.finished_ok.emit(done - errors, errors)
        finally:
            project.close()

    def _download(self, release, url, out_path, done, total, download_media) -> None:
        """One file, with byte progress folded into the existing (done, total) signal."""
        label = release.title

        def on_progress(got: int, size: int) -> None:
            if size:
                self.progress.emit(done, total, f"{label} — {got / 1e6:.0f}/{size / 1e6:.0f} MB")

        download_media(
            url,
            out_path,
            cookies=self.cookies,
            cancelled=lambda: self._cancelled,
            on_progress=on_progress,
        )

    def _resolve(self, releases: list) -> list:
        """Which releases this run should fetch, in reading order."""
        wanted = set(self.numbers) if self.numbers is not None else None
        targets = []
        self.skipped = 0
        for release in releases:
            if wanted is not None and release.number not in wanted:
                continue
            already = release.has_audio and (self.project_path / release.path).exists()
            if self.skip_downloaded and already:
                # The file is on disk and byte-identical to what the site would send
                # again. Counted, not silently dropped, so the summary can say so.
                self.skipped += 1
                continue
            targets.append(release)
        return targets



class MergeWorker(PausableWorker):
    """Merge per-chapter audio into one or more files (all / range / batch), off-thread."""

    progress = Signal(int, int, str)  # windows done, total windows, label
    file_done = Signal(str)  # each output file path as it finishes
    finished_ok = Signal(int)  # number of files written
    failed = Signal(str)

    def __init__(
        self,
        project_path: Path,
        voice: str,
        fmt: str,  # "m4b" | "mp3"
        mode: str,  # "all" | "range" | "batch"
        start: int | None = None,
        end: int | None = None,
        batch: int | None = None,
        source_audio: bool = False,  # merge the site's audio edition (059.07)
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
        # Render the SITE's audio edition instead of chapter audio (059.07).
        self.source_audio = source_audio
        self.fmt = fmt
        self.mode = mode
        # NOTE: not `self.start` — that would shadow QThread.start() and the thread
        # would never launch. Same care for end/batch for symmetry.
        self.start_num = start
        self.end_num = end
        self.batch_size = batch


    def _nothing_message(self) -> str:
        """Why the plan came out empty — the two cases need different advice."""
        if self.source_audio:
            return "Chưa tải mục audio nào từ trang nguồn trong phạm vi đã chọn."
        return "Không có chương nào có audio giọng này trong phạm vi đã chọn."

    def run(self) -> None:
        from noveltrans.errors import TtsError
        from noveltrans.tts.merge import (
            MergeCancelled,
            MergeSegment,
            chapter_marker_title,
            merge_chapters,
            plan_merge_windows,
            plan_source_windows,
        )

        project = NovelProject.open(self.project_path)
        try:
            if self.source_audio:
                windows = plan_source_windows(
                    project.source_audio(),
                    self.mode,
                    start=self.start_num,
                    end=self.end_num,
                    batch=self.batch_size,
                )
            else:
                windows = plan_merge_windows(
                    project.chapters(),
                    self.voice,
                    self.mode,
                    start=self.start_num,
                    end=self.end_num,
                    batch=self.batch_size,
                )
            if not windows:
                self.failed.emit(self._nothing_message())
                return
            project.audio_dir.mkdir(parents=True, exist_ok=True)
            # slug_name(), not display_name(): the stem keys the merged file, so
            # renaming the novel must not move it. Pinned before the first write, so a
            # later re-translation cannot move it either. See NovelMeta.slug_name().
            slug = project.meta.slug_name()
            project.pin_slug(slug)
            ext = "m4b" if self.fmt == "m4b" else "mp3"
            total = len(windows)
            written = 0
            for i, window in enumerate(windows):
                if self._checkpoint():
                    break
                # resolve to on-disk segments, skipping any file that went missing
                segments = [
                    MergeSegment(
                        path=self.project_path / c.audio_path,
                        seconds=c.audio_seconds,
                        title=chapter_marker_title(c),
                    )
                    for c in window.chapters
                    if (self.project_path / c.audio_path).is_file()
                ]
                if not segments:
                    continue
                if total == 1 and self.mode == "all":
                    name = f"{slug}.{ext}"
                else:
                    name = f"{slug}-{window.first_num:04d}-{window.last_num:04d}.{ext}"
                out_path = project.audio_dir / name  # alongside the per-chapter files
                self.progress.emit(i, total, name)
                try:
                    # Cancel only — do NOT gate pause here. This callback is polled inside a
                    # deadline-bounded ffmpeg/TTS wait; holding it would trip the timeout, and
                    # synthesize_chapter buffers the whole chapter in RAM until it writes.
                    merge_chapters(segments, out_path, self.fmt, cancelled=lambda: self._cancelled)
                    written += 1
                    self.file_done.emit(str(out_path))
                except MergeCancelled:
                    break  # user stopped — partial files kept, not an error
                except TtsError as exc:
                    self.failed.emit(str(exc))
                    return
            self.progress.emit(total, total, "")
            self.finished_ok.emit(written)
        except Exception as exc:  # keep unexpected errors on-screen
            self.failed.emit(f"Lỗi khi ghép audio: {exc!r}")
        finally:
            project.close()


class VideoWorker(PausableWorker):
    """Render per-chapter audio into MP4 video(s) (all / range / batch), off-thread.

    A structural clone of MergeWorker: same window selection, same one-file-per-window
    loop, same cancel handling. Each window becomes a video (background image + audio +
    burned-in chapter titles) plus a companion YouTube-description .txt.
    """

    progress = Signal(int, int, str)  # windows done, total windows, label
    file_done = Signal(str)  # each output .mp4 path as it finishes
    finished_ok = Signal(int)  # number of videos written
    failed = Signal(str)

    def __init__(
        self,
        project_path: Path,
        voice: str,
        mode: str,  # "all" | "range" | "batch"
        image_path: Path,
        start: int | None = None,
        end: int | None = None,
        batch: int | None = None,
        width: int = 1920,
        height: int = 1080,
        fps: int = 25,  # motion video (waveform) — smoother than 019's static 12
        spin_vinyl: bool = True,  # False → static disc (skips the costly per-frame rotate)
        font: str = "",  # title font family; "" → the bundled default (FONT_NAME)
        font_key: str = "",  # font registry key for the in-video title font
        thumb_font_key: str = "",  # font registry key for the thumbnail text; "" → font_key
        thumb_title_pos: tuple[float, float] | None = None,  # cover title (x, y) fractions
        thumb_part_pos: tuple[float, float] | None = None,  # cover "PHẦN N" (x, y) fractions
        # cover text-size multipliers; None → the renderer's 1.0 (the original layout)
        thumb_title_scale: float | None = None,
        thumb_part_scale: float | None = None,
        thumb_tagline_scale: float | None = None,
        thumb_title_align: str = "",  # cover title flush edge; "" → the renderer's "left"
        burn_subtitles: bool = False,  # also burn the narration into the video
        bg_color: str = "",  # background hex "#rrggbb"; "" → the default pastel gradient
        skip_existing: bool = False,  # skip parts whose .mp4 already exists (batch "continue")
        part_num: int | None = None,  # explicit part # for a single range-mode re-render;
                                       # None → computed per-window (batch/whole-novel runs)
        explicit_windows: list | None = None,  # render exactly these MergeWindows (a
                                                # multi-select "Tạo video"), bypassing
                                                # `mode`-based planning entirely
        explicit_part_numbers: dict | None = None,  # {first_num: part_num} for
                                                      # `explicit_windows` — see `_part_number`
        credit: str = "",  # "Tạo bởi: …" line; "" → the default (Fox Novel)
        tagline: str = "",  # thumbnail subtitle under "PHẦN N"
        thumb_image_path: Path | str = "",  # thumbnail base image; "" → reuse image_path
        tags: str = "",  # novel-level YouTube tags (comma-joined) written per part
        source_audio: bool = False,  # render the site's audio edition (059.07)
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
        self.source_audio = source_audio
        self.mode = mode
        self.image_path = Path(image_path)
        # NOTE: not `self.start` — that shadows QThread.start() (same trap as MergeWorker).
        self.start_num = start
        self.end_num = end
        self.batch_size = batch
        self.width = width
        self.height = height
        self.fps = fps
        self.spin_vinyl = spin_vinyl
        self.font = font
        self.font_key = font_key
        self.thumb_font_key = thumb_font_key
        self.thumb_title_pos = thumb_title_pos
        self.thumb_part_pos = thumb_part_pos
        self.thumb_title_scale = thumb_title_scale
        self.thumb_part_scale = thumb_part_scale
        self.thumb_tagline_scale = thumb_tagline_scale
        self.thumb_title_align = thumb_title_align
        self.burn_subtitles = burn_subtitles
        self.bg_color = bg_color
        self.skip_existing = skip_existing
        self.part_num = part_num
        self.explicit_windows = explicit_windows
        self.explicit_part_numbers = explicit_part_numbers or {}
        self.credit = credit
        self.tagline = tagline
        self.thumb_image_path = str(thumb_image_path or "")
        self.tags = tags


    def _nothing_message(self) -> str:
        """Why the plan came out empty — the two cases need different advice."""
        if self.source_audio:
            return "Chưa tải mục audio nào từ trang nguồn trong phạm vi đã chọn."
        return "Không có chương nào có audio giọng này trong phạm vi đã chọn."

    def run(self) -> None:
        from noveltrans.errors import TtsError
        from noveltrans.tts.merge import (
            MergeCancelled,
            MergeSegment,
            chapter_marker_title,
            part_number,
            plan_merge_windows,
            plan_source_windows,
        )
        from noveltrans.tts.player_skin import hex_to_rgb
        from noveltrans.tts.thumbnail import render_thumbnail
        from noveltrans.tts.video import (
            FONT_NAME,
            _with_real_durations,
            build_upload_title,
            discover_committed_video_windows,
            fit_video_description,
            font_dir_context,
            plan_locked_video_windows,
            render_video,
            video_font,
            video_part_name,
        )
        from noveltrans.video_state import effective_created
        from noveltrans.video_windows import read_manual_windows

        bg_rgb = hex_to_rgb(self.bg_color)

        project = NovelProject.open(self.project_path)
        try:
            project.video_dir.mkdir(parents=True, exist_ok=True)
            # slug_name(), not display_name(): the stem keys <stem>.mp4 and every
            # sidecar beside it, so renaming the novel must not move them. Pinned before
            # the first write, so a later re-translation cannot move them either.
            slug = project.meta.slug_name()
            project.pin_slug(slug)
            novel_title = project.meta.display_name()

            # Batch mode always honors manual split/merge boundaries (see
            # `noveltrans.video_windows`) — even "Tạo lại tất cả video". A manual split
            # exists to solve a real constraint (a part over YouTube's 12h cap) that
            # doesn't go away just because the background image or quality changed; silently
            # re-merging it back together on redo-all would reintroduce the exact policy
            # violation the split was for. Auto-discovered "đã tạo" commits are different —
            # those are just a timing artifact and redo-all is free to blow past them.
            #
            # `skip_existing` batch runs ("Tạo video", never "Tạo lại tất cả") additionally
            # honor those commits: a part already committed with fewer than a full batch of
            # chapters stays that size; new chapters start the next part instead of
            # retroactively growing it. Range/whole-novel mode has no batch grid at all, so
            # neither kind of lock applies — see `plan_locked_video_windows`.
            # `explicit_windows` (a multi-select "Tạo video" from the context menu) skips
            # all of this planning — the caller already knows exactly which windows and
            # part numbers it wants, computed from the same locked/manual-aware plan the
            # table shows.
            #
            # A `source_audio` run skips it too, in EVERY mode — see its branch below.
            locked_part_numbers: dict[int, int] = {}
            if self.explicit_windows is not None:
                windows = self.explicit_windows
                locked_part_numbers = dict(self.explicit_part_numbers)
            elif self.source_audio:
                # BEFORE the batch branch: the site's audio edition has no chapter grid to
                # lock. Batch mode here is a plain fixed grid over releases, exactly what
                # the parts table previews (`_windows_for_current_selection`).
                # `MergeWorker.run()` orders its branches the same way for the same reason.
                #
                # Manual split/merge boundaries stay out because `video_manual_windows.json`
                # is a single per-novel map keyed by CHAPTER number, with no room for a
                # second number space — a manual split of "chương 1-10" would otherwise
                # reshape "phần 1-10" of the releases.
                #
                # Commit discovery stays out for a narrower reason than feature 066 gave.
                # 066 argued the semantics don't exist for releases; that was wrong — a
                # source window DOES grow retroactively when later releases are downloaded
                # after an earlier part was rendered, which is exactly what commit-locking
                # is for. Since 067 the two editions no longer share a filename namespace,
                # so `discover_committed_video_windows(..., source_audio=True)` would now
                # be well defined. What still blocks it is `plan_locked_video_windows`:
                # it filters on `c.audio_voice`, which `SourceAudio` does not have.
                windows = plan_source_windows(
                    project.source_audio(),
                    self.mode,
                    start=self.start_num,
                    end=self.end_num,
                    batch=self.batch_size,
                )
            elif self.mode == "batch":
                manual = read_manual_windows(project.path)
                committed = (
                    discover_committed_video_windows(project.video_dir, slug)
                    if self.skip_existing else {}
                )
                locked = plan_locked_video_windows(
                    project.chapters(), self.voice, self.batch_size,
                    {**committed, **manual},
                )
                windows = [w for _, w in locked]
                locked_part_numbers = {w.first_num: pn for pn, w in locked}
            else:
                windows = plan_merge_windows(
                    project.chapters(),
                    self.voice,
                    self.mode,
                    start=self.start_num,
                    end=self.end_num,
                    batch=self.batch_size,
                )
            if not windows:
                self.failed.emit(self._nothing_message())
                return
            total = len(windows)
            written = 0
            with font_dir_context() as font_dir:
                for i, window in enumerate(windows):
                    if self._checkpoint():
                        break
                    segments = [
                        MergeSegment(
                            path=self.project_path / c.audio_path,
                            seconds=c.audio_seconds,
                            title=chapter_marker_title(c),
                        )
                        for c in window.chapters
                        if (self.project_path / c.audio_path).is_file()
                    ]
                    if not segments:
                        continue
                    whole_novel = total == 1 and self.mode == "all"
                    name = video_part_name(
                        slug, window.first_num, window.last_num,
                        whole_novel=whole_novel, source_audio=self.source_audio,
                    )
                    # Each part goes in its own folder (video + sidecars) so it can be
                    # uploaded on its own; legacy flat renders still count for skip_existing.
                    out_path = project.video_dir / Path(name).stem / name
                    legacy_path = project.video_dir / name
                    # Same resolution `_part_output_path` uses in the tab — the EDITION as
                    # well as the layout: prefer the per-folder path, fall back to a
                    # pre-existing legacy flat file. If the two ever disagree about which
                    # edition a window belongs to, the tab shows one file and the worker
                    # writes another. This
                    # is the exact path the "Trạng thái" tick's sidecar sits beside, so a
                    # part manually marked "đã tạo" is skipped here too — not just file
                    # existence — even though no .mp4 has actually been rendered for it.
                    resolved = legacy_path if not out_path.is_file() and legacy_path.is_file() else out_path
                    if self.skip_existing and effective_created(resolved):
                        self.progress.emit(i + 1, total, "")  # already made (or marked) — skip
                        continue
                    self.progress.emit(i, total, name)
                    # From the chapter range, not `i`: rendering one part on its own used
                    # to title it "Phần 1" while its file kept the real range. Precedence:
                    # an explicit `self.part_num` (a single range-mode re-render, whose
                    # caller already knows the true number — grid arithmetic can't derive
                    # it once a batch window is locked), then the locked-plan's own number
                    # (batch + skip_existing), then plain grid arithmetic for everything
                    # else (redo-all, or a genuinely custom range with no batch grid).
                    if whole_novel:
                        part_num = None
                    elif self.part_num is not None:
                        part_num = self.part_num
                    elif window.first_num in locked_part_numbers:
                        part_num = locked_part_numbers[window.first_num]
                    else:
                        part_num = part_number(window.first_num, self.batch_size)
                    try:
                        render_video(
                            segments, self.image_path, out_path, font_dir, novel_title,
                            width=self.width, height=self.height, fps=self.fps,
                            spin_vinyl=self.spin_vinyl, font_name=self.font or FONT_NAME,
                            bg_color=bg_rgb, burn_subtitles=self.burn_subtitles,
                            # Cancel only — do NOT gate pause here. This callback is polled inside a
                            # deadline-bounded ffmpeg/TTS wait; holding it would trip the timeout, and
                            # synthesize_chapter buffers the whole chapter in RAM until it writes.
                            cancelled=lambda: self._cancelled,
                        )
                        self._write_metadata(
                            project, out_path, novel_title, segments, part_num, font_dir,
                            _with_real_durations, build_upload_title,
                            fit_video_description, video_font, render_thumbnail,
                        )
                        written += 1
                        self.file_done.emit(str(out_path))
                    except MergeCancelled:
                        break  # user stopped — partial files kept, not an error
                    except TtsError as exc:
                        self.failed.emit(str(exc))
                        return
            self.progress.emit(total, total, "")
            self.finished_ok.emit(written)
        except Exception as exc:  # keep unexpected errors on-screen
            self.failed.emit(f"Lỗi khi tạo video: {exc!r}")
        finally:
            project.close()

    def _write_metadata(
        self, project, out_path, novel_title, segments, part_num, font_dir,
        with_real_durations, build_upload_title, fit_video_description,
        video_font, render_thumbnail,
    ) -> None:
        """Write the title / description / tags / thumbnail sidecars next to `out_path`.

        A thumbnail failure is swallowed (a bad base image must not discard an otherwise
        good video); the text sidecars are cheap and always written.
        """
        from noveltrans.tts.thumbnail import (
            DEFAULT_PART_POS,
            DEFAULT_TEXT_SCALE,
            DEFAULT_TITLE_ALIGN,
            DEFAULT_TITLE_POS,
        )

        def sidecar(ext: str) -> Path:
            return out_path.parent / (out_path.stem + ext)

        timed = with_real_durations(segments)
        title = build_upload_title(novel_title, part_num)
        sidecar(".title.txt").write_text(title + "\n", encoding="utf-8")

        desc, _dropped = fit_video_description(
            timed,
            original_title=project.meta.title,
            vn_title=novel_title,
            original_author=project.meta.author,
            vn_author=project.meta.translated_author,
            total_chapters=project.counts()["total"],
            credit=self.credit or "Fox Novel",
        )
        # Richer than render_video's, and already capped to YouTube's 5000 characters —
        # `_dropped` (chapters lopped off the index to fit) is discarded here on purpose:
        # the parts table flags an over-long part *before* the render, which is when the
        # user can still do something about it.
        sidecar(".txt").write_text(desc, encoding="utf-8")

        if self.tags.strip():
            from noveltrans.tts.tags import format_tags, parse_tags

            capped = format_tags(parse_tags(self.tags))
            if capped:
                sidecar(".tags.txt").write_text(capped + "\n", encoding="utf-8")

        try:
            font_file = video_font(self.thumb_font_key or self.font_key)["file"]
            render_thumbnail(
                self.thumb_image_path or str(self.image_path),
                sidecar(".jpg"),
                vn_title=novel_title,
                part_num=part_num or 1,
                tagline=self.tagline,
                font_path=font_dir / font_file,
                width=1280, height=720,
                title_pos=self.thumb_title_pos or DEFAULT_TITLE_POS,
                part_pos=self.thumb_part_pos or DEFAULT_PART_POS,
                title_scale=self.thumb_title_scale or DEFAULT_TEXT_SCALE,
                part_scale=self.thumb_part_scale or DEFAULT_TEXT_SCALE,
                tagline_scale=self.thumb_tagline_scale or DEFAULT_TEXT_SCALE,
                title_align=self.thumb_title_align or DEFAULT_TITLE_ALIGN,
            )
        except Exception:  # noqa: BLE001 — never fail a good render over a thumbnail
            pass


class SubtitleUploadWorker(PausableWorker):
    """Upload each part's `.srt` to its YouTube video, in one browser session.

    Signal-for-signal a sibling of YouTubeThumbnailWorker, so the Video tab drives all four
    browser runs with one set of handlers and one cancel button.
    """

    progress = Signal(int, int, str)
    part_done = Signal(int, str, str)  # index, label ("" on failure), error
    finished_ok = Signal(int, int)  # uploaded, failed
    failed = Signal(str)
    needs_login = Signal(str)

    def __init__(self, requests: list, parent=None):
        super().__init__(parent)
        self.requests = list(requests)


    def run(self) -> None:
        from noveltrans.youtube_upload import (
            UploadCancelled,
            YouTubeUploadError,
            upload_subtitle_batch,
        )

        total = len(self.requests)
        done = 0
        errors = 0

        def on_part_done(index: int, label: str, error: str) -> None:
            nonlocal done, errors
            done += 1
            if error:
                errors += 1
            self.part_done.emit(index, label or "", error)
            name = self.requests[index].label or f"phần {index + 1}"
            self.progress.emit(done, total, f"{name}: {'lỗi' if error else 'xong'}")

        try:
            upload_subtitle_batch(
                self.requests,
                on_progress=lambda msg: self.progress.emit(done, total, msg),
                on_part_done=on_part_done,
                should_cancel=lambda: self._cancelled,
                on_checkpoint=self._checkpoint,
            )
        except UploadCancelled:
            # Nothing half-done survives: a track is published or it isn't, and an
            # unfinished one leaves the video exactly as it was.
            self.failed.emit(f"Đã dừng tải phụ đề. {done} phần đã xong vẫn giữ phụ đề.")
        except YouTubeUploadError as exc:
            (self.needs_login if exc.needs_login else self.failed).emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.finished_ok.emit(done - errors, errors)


class SubtitleWorker(PausableWorker):
    """Write each part's `.srt`, backfilling missing cues from the audio first.

    Two jobs, one button, because on their own neither is what the user wants: writing
    sidecars is instant but produces nothing for audio voiced before feature 040, and
    backfilling produces cues nobody has asked to be turned into a file.

    Deliberately does NOT re-render video. The `.srt` needs only the segment list and the
    cues; going through `render_video` to get one would cost ~26 minutes and ~250 MB per
    part to produce a 40 KB text file.
    """

    progress = Signal(int, int, str)  # parts done, total parts, status line
    finished_ok = Signal(int, int, int)  # srt files written, chapters backfilled, skipped
    failed = Signal(str)

    def __init__(
        self,
        project_path: Path,
        voice: str,
        mode: str,
        *,
        start=None,
        end=None,
        batch=None,
        use_translation: bool = True,
        clean_text: bool = True,
        clean_extra_remove: str = "",
        gap_seconds: float = 0.4,
        speed: float = 1.0,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
        self.mode = mode
        self.start_num = start
        self.end_num = end
        self.batch_size = batch
        self.use_translation = use_translation
        self.clean_text = clean_text
        self.clean_extra_remove = clean_extra_remove
        self.gap_seconds = gap_seconds
        self.speed = speed


    def _backfill(self, project, chapter) -> bool:
        """Recover one chapter's cues from its audio. False if it couldn't be trusted."""
        from noveltrans.tts.subtitles import backfill_cues, read_cues, write_cues
        from noveltrans.tts.vieneu import VieneuEngine

        audio = self.project_path / chapter.audio_path
        if not audio.is_file() or read_cues(audio)[0]:
            return False  # nothing to do, or already has real cues from synthesis
        title, text = (
            (chapter.translated_title or chapter.title, chapter.translated)
            if self.use_translation
            else (chapter.title, chapter.content)
        )
        cues = backfill_cues(
            audio, title, text,
            duration=chapter.audio_seconds,
            gap_seconds=self.gap_seconds,
            speed=self.speed,
            clean=self.clean_text,
            extra_remove=self.clean_extra_remove,
            max_chars=VieneuEngine.max_chunk_chars,
            min_chars=VieneuEngine.min_chunk_chars,
        )
        if cues is None:
            return False
        write_cues(audio, cues, seconds=chapter.audio_seconds)
        return True

    def run(self) -> None:
        from noveltrans.tts.merge import (
            MergeSegment,
            chapter_marker_title,
            part_number,
            plan_merge_windows,
        )
        from noveltrans.tts.subtitles import part_srt
        from noveltrans.tts.video import (
            _with_real_durations,
            video_part_name,
        )

        project = NovelProject.open(self.project_path)
        try:
            windows = plan_merge_windows(
                project.chapters(), self.voice, self.mode,
                start=self.start_num, end=self.end_num, batch=self.batch_size,
            )
            if not windows:
                self.failed.emit("Không có chương nào có audio giọng này trong phạm vi đã chọn.")
                return
            # Same stem as the render that produced these files. No pin_slug here: this
            # worker only ever writes beside parts that already exist.
            slug = project.meta.slug_name()
            total = len(windows)
            written = backfilled = skipped = 0

            for i, window in enumerate(windows):
                if self._checkpoint():
                    break
                label = (
                    "Toàn bộ"
                    if (total == 1 and self.mode == "all")
                    else f"Phần {part_number(window.first_num, self.batch_size)}"
                )
                self.progress.emit(i, total, f"{label}: dò mốc thời gian…")
                for chapter in window.chapters:
                    if self._checkpoint():
                        break
                    if self._backfill(project, chapter):
                        backfilled += 1

                segments = [
                    MergeSegment(
                        path=self.project_path / c.audio_path,
                        seconds=c.audio_seconds,
                        title=chapter_marker_title(c),
                    )
                    for c in window.chapters
                    if (self.project_path / c.audio_path).is_file()
                ]
                if not segments:
                    continue
                # Real durations, exactly as render_video does — so a sidecar written here
                # and one written by a render are the same file.
                segments = _with_real_durations(segments)
                srt, covered, _n = part_srt(segments)
                whole_novel = total == 1 and self.mode == "all"
                # No `source_audio` here, and none needed: this worker plans only with
                # `plan_merge_windows` filtered by `audio_voice`, so every window it can
                # produce is a CHAPTER window and its `.srt` belongs in the chapter
                # namespace. The source edition has no subtitle path at all — releases
                # carry no cues and no per-chapter text for `_backfill` to work from.
                name = video_part_name(
                    slug, window.first_num, window.last_num, whole_novel=whole_novel
                )
                out = project.video_dir / Path(name).stem / Path(name).with_suffix(".srt").name
                if not srt.strip():
                    skipped += 1
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(srt, encoding="utf-8")
                written += 1
                self.progress.emit(i + 1, total, f"{label}: {covered} chương có phụ đề")
        except Exception as exc:  # noqa: BLE001 — keep automation errors on-screen
            self.failed.emit(repr(exc))
            return
        finally:
            project.close()
        self.finished_ok.emit(written, backfilled, skipped)


class VideoPreviewWorker(QThread):
    """Render a single preview frame off-thread (a bake + one ffmpeg call — a couple secs)."""

    done = Signal(str)  # path to the rendered preview PNG
    failed = Signal(str)

    def __init__(
        self,
        image_path: Path,
        novel_title: str,
        sample_title: str,
        *,
        width: int = 1920,
        height: int = 1080,
        spin_vinyl: bool = True,
        font: str = "",
        bg_color: str = "",  # background hex "#rrggbb"; "" → the default pastel gradient
        parent=None,
    ):
        super().__init__(parent)
        self.image_path = Path(image_path)
        self.novel_title = novel_title
        self.sample_title = sample_title
        self.width = width
        self.height = height
        self.spin_vinyl = spin_vinyl
        self.font = font
        self.bg_color = bg_color

    def run(self) -> None:
        import tempfile

        from noveltrans.errors import TtsError
        from noveltrans.tts.player_skin import hex_to_rgb
        from noveltrans.tts.video import FONT_NAME, font_dir_context, render_preview_frame

        try:
            out = Path(tempfile.gettempdir()) / "noveltrans-preview.png"
            with font_dir_context() as font_dir:
                render_preview_frame(
                    self.image_path, out, font_dir, self.novel_title, self.sample_title,
                    width=self.width, height=self.height,
                    spin_vinyl=self.spin_vinyl, font_name=self.font or FONT_NAME,
                    bg_color=hex_to_rgb(self.bg_color),
                )
            self.done.emit(str(out))
        except TtsError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected errors on-screen
            self.failed.emit(f"Lỗi khi tạo ảnh xem trước: {exc!r}")


class TtsVoicesWorker(QThread):
    """List a TTS engine's voices without blocking the GUI."""

    voices_listed = Signal(list)  # (label, voice_id) pairs

    def run(self) -> None:
        from noveltrans.tts import get_tts_engine

        try:
            voices = get_tts_engine("vieneu").list_voices()  # presets, no model load
        except Exception:
            voices = []
        self.voices_listed.emit(list(voices))


class ExportWorker(QThread):
    """Export a project to one output format."""

    finished_ok = Signal(str)  # written file path
    failed = Signal(str)

    def __init__(
        self,
        project_path: Path,
        exporter_name: str,
        out_path: Path,
        use_translation: bool,
        number_chapters: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.exporter_name = exporter_name
        self.out_path = Path(out_path)
        self.use_translation = use_translation
        self.number_chapters = number_chapters

    def run(self) -> None:
        from noveltrans.exporters import get_exporter

        project = NovelProject.open(self.project_path)
        try:
            exporter = get_exporter(self.exporter_name)
            written = exporter.export(
                project.meta,
                project.chapters(),
                self.out_path,
                use_translation=self.use_translation,
                number_chapters=self.number_chapters,
            )
            self.finished_ok.emit(str(written))
        except NovelTransError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Lỗi không mong đợi: {exc!r}")
        finally:
            project.close()


class DownloadWorker(PausableWorker):
    """Download all pending chapters of a project, resumably."""

    progress = Signal(int, int, str)  # done, total, chapter title
    chapter_done = Signal(int)  # chapter index (GUI refreshes that row)
    chapter_error = Signal(int, str)
    daily_limit_hit = Signal(str, str)  # per-day cap stopped the batch: (message, unlock code)
    finished_ok = Signal(int, int)  # downloaded count, error count

    def __init__(
        self,
        project_path: Path,
        delay: float,
        cookies: str = "",
        parent=None,
        *,
        start_index: int = 0,
        end_index: int | None = None,
        force: bool = False,
        indices: list[int] | None = None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.delay = delay
        self.cookies = cookies
        # 0-based, inclusive chapter-index bounds for a partial download. Defaults
        # cover the whole novel, so the plain "download all" caller is unchanged.
        # `force` re-fetches chapters in range even if they already have content
        # (a single-chapter refresh); otherwise only missing chapters are fetched.
        self.start_index = start_index
        self.end_index = end_index
        self.force = force
        # An explicit, possibly scattered set of chapters — the residue repair (feature
        # 071) picks out damaged chapters from all over the novel, which no start/end
        # range can express. Implies force: every one of them already has content.
        # Same shape as TranslateWorker's `indices`.
        self.indices = indices

    def _select_chapters(self, project) -> list:
        """The chapters this run will fetch, honouring `indices`, the range and `force`."""
        if self.indices is not None:
            wanted = set(self.indices)
            return [c for c in project.chapters() if c.index in wanted]
        if self.force:
            return project.chapters_in_range(self.start_index, self.end_index)
        return project.pending_download(self.start_index, self.end_index)


    def _fetch_with_backoff(self, adapter, chapter, done: int, total: int) -> str:
        """Fetch a chapter, waiting and retrying when the site throttles reads."""
        ref = ChapterRef(index=chapter.index, title=chapter.title, url=chapter.url)
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            try:
                return adapter.fetch_chapter(ref)
            except RateLimitedError:
                if attempt >= _RATE_LIMIT_MAX_RETRIES or self._cancelled:
                    raise
                for remaining in range(_RATE_LIMIT_WAIT_SECONDS, 0, -1):
                    if self._cancelled:
                        raise
                    self.progress.emit(
                        done,
                        total,
                        f"⏳ Site giới hạn tốc độ đọc — chờ {remaining}s rồi thử lại: "
                        f"{chapter.title}",
                    )
                    time.sleep(1)
        raise RateLimitedError("Vẫn bị giới hạn sau nhiều lần thử", chapter.url)

    def run(self) -> None:
        project = NovelProject.open(self.project_path)
        adapter = None
        try:
            client = HttpClient(delay_seconds=self.delay)
            adapter = adapter_for_url(project.meta.url, client)
            if adapter is None:
                self.finished_ok.emit(0, 0)
                return
            # Unconditional: the caller resolved which site's cookie this is
            # (`AppConfig.cookies_for_url`), and `set_cookies` ignores a blank string.
            client.set_cookies(self.cookies)

            pending = self._select_chapters(project)
            total = len(pending)
            done = 0
            errors = 0
            # Reads `done`/`total` at call time (closure over run()'s locals), so a
            # mid-batch browser relaunch reports the real position, not 0.
            adapter.on_status = lambda msg: self.progress.emit(done, total, msg)
            for chapter in pending:
                if self._checkpoint():
                    break
                ref_title = chapter.title
                self.progress.emit(done, total, ref_title)
                try:
                    text = self._fetch_with_backoff(adapter, chapter, done, total)
                    if adapter.content_is_translated:
                        # This source (e.g. webtruyendich) serves a finished
                        # translation, not source text. Land it as `translated`
                        # directly and skip our own translators. `content` is also
                        # written: it *is* the text we fetched, and leaving it empty
                        # would make pending_download re-queue this chapter forever
                        # (and it keeps original-text TTS/export working).
                        project.save_content(chapter.index, text)
                        project.save_translation(
                            chapter.index,
                            chapter.title,
                            text,
                            adapter.translated_lang,
                            adapter.translator_label,
                        )
                    else:
                        project.save_content(chapter.index, text)
                    self.chapter_done.emit(chapter.index)
                except DailyLimitError as exc:
                    # A per-day cap blocks every remaining chapter — stop the batch
                    # and surface the unlock instructions instead of failing each row.
                    errors += 1
                    project.mark_error(chapter.index, str(exc))
                    self.chapter_error.emit(chapter.index, str(exc))
                    self.progress.emit(done, total, f"🔒 {exc}")
                    self.daily_limit_hit.emit(str(exc), exc.code)
                    break
                except NovelTransError as exc:
                    errors += 1
                    project.mark_error(chapter.index, str(exc))
                    self.chapter_error.emit(chapter.index, str(exc))
                except Exception as exc:  # keep the batch going
                    errors += 1
                    project.mark_error(chapter.index, repr(exc))
                    self.chapter_error.emit(chapter.index, repr(exc))
                done += 1
            self.progress.emit(done, total, "")
            self.finished_ok.emit(done - errors, errors)
        finally:
            if adapter is not None:
                adapter.close()  # 69shuba holds a browser for the whole batch
            project.close()


class UnlockWorker(QThread):
    """Run medoctruyen's Discord `/mochuong <code>` unlock off the GUI thread.

    Playwright's sync API blocks, so it can't run on the Qt event-loop thread. On
    success the scrape tab auto-resumes the download; `needs_login` tells it to
    prompt the one-time throwaway-account login instead of just failing.
    """

    unlocked = Signal()
    needs_login = Signal(str)  # message: profile has no valid Discord session yet
    failed = Signal(str)

    def __init__(self, channel_url: str, code: str, parent=None):
        super().__init__(parent)
        self.channel_url = channel_url
        self.code = code

    def run(self) -> None:
        # Imported here so a missing Playwright (optional dep) only bites when the
        # user actually turns auto-unlock on, not at app import time.
        from noveltrans.discord_unlock import DiscordUnlockError, run_unlock

        try:
            run_unlock(self.channel_url, self.code)
        except DiscordUnlockError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected automation errors on-screen
            self.failed.emit(repr(exc))
        else:
            self.unlocked.emit()


class DiscordLoginWorker(QThread):
    """Open the one-time Discord login window for the throwaway account off-thread."""

    done = Signal()
    failed = Signal(str)

    def run(self) -> None:
        from noveltrans.discord_unlock import DiscordUnlockError, open_login

        try:
            open_login()
        except DiscordUnlockError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.done.emit()


class YouTubeLoginWorker(QThread):
    """Open the one-time YouTube/Google login window for the channel, off-thread.

    `switch=True` drops the saved profile first so Google shows its account chooser —
    the way to move to a different channel once one is already connected.
    """

    done = Signal(str, str)  # channel id, channel name (either may be "")
    failed = Signal(str)

    def __init__(self, parent=None, *, switch: bool = False):
        super().__init__(parent)
        self.switch = switch

    def run(self) -> None:
        from noveltrans.youtube_upload import YouTubeUploadError, open_login

        try:
            channel_id, name = open_login(switch=self.switch)
        except YouTubeUploadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.done.emit(channel_id, name)


def _drop_cues(audio_path) -> None:
    """Delete a chapter's cue sidecar. Used when its audio file is replaced.

    Stale cues are worse than none: they describe a take that no longer exists, and the
    subtitles would confidently be wrong rather than absent.
    """
    from noveltrans.tts.subtitles import cues_path

    try:
        cues_path(audio_path).unlink(missing_ok=True)
    except OSError:
        pass


class YouTubeUploadWorker(PausableWorker):
    """Upload a list of rendered parts to YouTube through one browser session.

    Structurally a sibling of VideoWorker — same progress/finished/failed signal shape,
    same cooperative `cancel()` — so the Video tab can drive it with the wiring it
    already has. The requests are built on the GUI thread (they only read sidecar files)
    and handed over whole; this worker never touches a NovelProject.
    """

    progress = Signal(int, int, str)  # parts done, total parts, status line
    part_done = Signal(int, str, str)  # index, video url ("" if skipped/failed), error
    finished_ok = Signal(int, int)  # uploaded count, failed count
    failed = Signal(str)  # the whole run could not start
    needs_login = Signal(str)  # profile has no valid Google session

    def __init__(self, requests: list, parent=None):
        super().__init__(parent)
        self.requests = list(requests)


    def run(self) -> None:
        # Imported here so a missing Playwright (optional dep) only bites when the user
        # actually uploads, not at app import time.
        from noveltrans.youtube_upload import (
            UploadCancelled,
            YouTubeUploadError,
            upload_batch,
        )

        total = len(self.requests)
        done = 0
        errors = 0

        def on_part_done(index: int, result, error: str) -> None:
            nonlocal done, errors
            done += 1
            if error:
                errors += 1
            self.part_done.emit(index, getattr(result, "url", "") or "", error)
            label = self.requests[index].label or f"phần {index + 1}"
            self.progress.emit(done, total, f"{label}: {'lỗi' if error else 'xong'}")

        try:
            upload_batch(
                self.requests,
                on_progress=lambda msg: self.progress.emit(done, total, msg),
                on_part_done=on_part_done,
                should_cancel=lambda: self._cancelled,
                on_checkpoint=self._checkpoint,
            )
        except UploadCancelled as exc:
            # Cancelling mid-part can leave a draft on the channel; say so rather than
            # letting the user assume nothing happened.
            if exc.video_id:
                self.failed.emit(
                    f"Đã huỷ. Một video đang dở nằm trên kênh dưới dạng bản nháp "
                    f"(https://youtu.be/{exc.video_id}) — kiểm tra và xoá nếu không cần."
                )
            else:
                self.failed.emit("Đã huỷ tải lên.")
        except YouTubeUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected automation errors on-screen
            self.failed.emit(repr(exc))
        else:
            self.finished_ok.emit(done - errors, errors)


class PlaylistFetchWorker(QThread):
    """Read the logged-in channel's playlist titles, off the GUI thread.

    Its own worker rather than a blocking call because it opens a real browser on the
    shared profile — seconds at best, and a login prompt at worst.
    """

    fetched = Signal(list)  # playlist titles, in Studio's own order
    failed = Signal(str)
    needs_login = Signal(str)

    def run(self) -> None:
        from noveltrans.youtube_upload import YouTubeUploadError, fetch_playlists

        try:
            self.fetched.emit(fetch_playlists())
        except YouTubeUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected automation errors on-screen
            self.failed.emit(repr(exc))


class PlaylistSyncWorker(PausableWorker):
    """Empty a playlist, then add every part's video to it in order.

    Signal shape copied from YouTubeThumbnailWorker so the Video tab drives all three
    browser runs with one set of handlers and one cancel button.
    """

    progress = Signal(int, int, str)  # parts done, total parts, status line
    part_done = Signal(int, str, str)  # index, label ("" on failure), error
    finished_ok = Signal(int, int, int)  # removed, added, failed
    failed = Signal(str)
    needs_login = Signal(str)

    def __init__(self, playlist: str, requests: list, parent=None):
        super().__init__(parent)
        self.playlist = playlist
        self.requests = list(requests)


    def run(self) -> None:
        from noveltrans.youtube_upload import (
            UploadCancelled,
            YouTubeUploadError,
            sync_playlist_batch,
        )

        total = len(self.requests)
        done = 0
        errors = 0

        def on_part_done(index: int, label, error: str) -> None:
            nonlocal done, errors
            done += 1
            if error:
                errors += 1
            self.part_done.emit(index, label or "", error)
            name = self.requests[index].label or f"phần {index + 1}"
            self.progress.emit(done, total, f"{name}: {'lỗi' if error else 'xong'}")

        try:
            result = sync_playlist_batch(
                self.playlist,
                self.requests,
                on_progress=lambda msg: self.progress.emit(done, total, msg),
                on_part_done=on_part_done,
                should_cancel=lambda: self._cancelled,
                on_checkpoint=self._checkpoint,
            )
        except UploadCancelled:
            # Cancelling here CAN leave a half-filled playlist — the clear already ran.
            # Say so plainly rather than letting the user assume nothing happened.
            self.failed.emit(
                f"Đã dừng. Danh sách phát “{self.playlist}” đã bị xoá trước đó và mới "
                f"thêm lại {done} phần — kiểm tra trên YouTube."
            )
        except YouTubeUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.finished_ok.emit(result["removed"], len(result["added"]), errors)


class YouTubeThumbnailWorker(PausableWorker):
    """Replace the thumbnails of already-uploaded parts, in one browser session.

    A sibling of YouTubeUploadWorker down to the signal shapes, so the Video tab drives
    it with the wiring it already has. The requests are built on the GUI thread (they
    only read sidecar files) and handed over whole; this worker never touches a
    NovelProject.
    """

    progress = Signal(int, int, str)  # parts done, total parts, status line
    part_done = Signal(int, str, str)  # index, video url ("" on failure), error
    finished_ok = Signal(int, int)  # updated count, failed count
    failed = Signal(str)  # the whole run could not start
    needs_login = Signal(str)  # profile has no valid Google session

    def __init__(self, requests: list, parent=None):
        super().__init__(parent)
        self.requests = list(requests)


    def run(self) -> None:
        from noveltrans.youtube_upload import (
            UploadCancelled,
            YouTubeUploadError,
            update_thumbnail_batch,
        )

        total = len(self.requests)
        done = 0
        errors = 0

        def on_part_done(index: int, result, error: str) -> None:
            nonlocal done, errors
            done += 1
            if error:
                errors += 1
            self.part_done.emit(index, getattr(result, "url", "") or "", error)
            label = self.requests[index].label or f"phần {index + 1}"
            self.progress.emit(done, total, f"{label}: {'lỗi' if error else 'xong'}")

        try:
            update_thumbnail_batch(
                self.requests,
                on_progress=lambda msg: self.progress.emit(done, total, msg),
                on_part_done=on_part_done,
                should_cancel=lambda: self._cancelled,
                on_checkpoint=self._checkpoint,
            )
        except UploadCancelled:
            # Nothing half-done can be left behind here: a part is either saved or
            # untouched. So this is NOT the upload worker's "a stray draft is on your
            # channel" — say plainly that the parts already done keep their new cover.
            self.failed.emit(
                f"Đã dừng cập nhật ảnh bìa. {done} phần đã đổi vẫn giữ ảnh bìa mới."
            )
        except YouTubeUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected automation errors on-screen
            self.failed.emit(repr(exc))
        else:
            self.finished_ok.emit(done - errors, errors)


class OneDriveLoginWorker(QThread):
    """Open the one-time OneDrive/Microsoft login window off-thread.

    `switch=True` drops the saved profile first so Microsoft shows its account chooser —
    the way to move to a different account once one is already connected. Without it a
    valid session loads straight through and the window closes before the user can change
    anything.
    """

    done = Signal(str)  # account name or email ("" if it could not be read)
    failed = Signal(str)

    def __init__(self, parent=None, *, switch: bool = False):
        super().__init__(parent)
        self.switch = switch

    def run(self) -> None:
        from noveltrans.onedrive_upload import OneDriveUploadError, open_login

        try:
            account = open_login(switch=self.switch)
        except OneDriveUploadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.done.emit(account)


class OneDrivePushWorker(PausableWorker):
    """Mirror one novel's project folder to OneDrive, in a single browser session.

    Pausable because a sixty-gigabyte push is exactly the job someone wants to hold while
    they need the bandwidth back. **The gate holds between batches, never mid-transfer** —
    the same contract as every other pausable worker here, and stated because it is the
    question people ask: pausing mid-transfer would mean sitting on a half-sent batch with
    a browser holding it open.

    The request is built on the GUI thread (it only names a folder and a title); this
    worker never touches a NovelProject.
    """

    progress = Signal(int, int, str)  # files done, files planned, status line
    file_done = Signal(str, str)  # relpath, error ("" on success)
    finished_ok = Signal(int, int, int)  # uploaded, skipped, failed
    failed = Signal(str)  # the run could not start, or was cancelled
    needs_login = Signal(str)  # profile has no valid Microsoft session

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request

    def run(self) -> None:
        # Imported here so a missing Playwright (optional dep) only bites when the user
        # actually pushes, not at app import time.
        from noveltrans.onedrive_upload import (
            OneDriveCancelled,
            OneDriveUploadError,
            format_size,
            push_project,
        )

        try:
            result = push_project(
                self.request,
                on_progress=lambda done, total, msg: self.progress.emit(done, total, msg),
                on_file_done=lambda relpath, error: self.file_done.emit(relpath, error),
                should_cancel=lambda: self._cancelled,
                on_checkpoint=self._checkpoint,
            )
        except OneDriveCancelled as exc:
            # Nothing is left half-written on OneDrive: a file is either fully there or
            # not, and the manifest was flushed on the way out. So this is not the YouTube
            # worker's "a stray draft is on your channel" — the honest message is that the
            # files already up will simply be skipped next time.
            self.failed.emit(
                f"Đã dừng tải lên OneDrive. {exc.uploaded} file đã lên vẫn còn — "
                "chạy lại sẽ bỏ qua chúng."
            )
        except OneDriveUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected automation errors on-screen
            self.failed.emit(repr(exc))
        else:
            if result.bytes_sent:
                self.progress.emit(
                    result.uploaded,
                    result.uploaded,
                    f"Đã tải lên {format_size(result.bytes_sent)} vào {result.remote_root}",
                )
            self.finished_ok.emit(result.uploaded, result.skipped, result.failed)


class OneDriveFoldersWorker(QThread):
    """List the subfolders of one OneDrive path, off the GUI thread.

    Its own worker rather than a blocking call because it opens a real browser — seconds
    at best, and a sign-in prompt at worst. Same shape as `PlaylistFetchWorker`.
    """

    fetched = Signal(str, list)  # the path listed, its subfolder names
    failed = Signal(str)
    needs_login = Signal(str)

    def __init__(self, path: str = "", parent=None):
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        from noveltrans.onedrive_upload import (
            OneDriveUploadError,
            list_destination_folders,
        )

        try:
            folders = list_destination_folders(self.path)
        except OneDriveUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.fetched.emit(self.path, folders)


class OneDriveSyncScanWorker(QThread):
    """Work out what each novel in the library would send, without opening a browser.

    Its own thread because it snapshots every project's database to size it honestly —
    cheap per novel, but a library of thirty is long enough to freeze the GUI.

    Results are emitted per novel rather than as one list at the end, so a large library
    fills the table as it goes instead of showing nothing for half a minute.
    """

    # `qlonglong`, not `int`, for the byte count. Qt's `int` is 32-bit, so a novel with
    # more than ~2 GB to send overflows it and the emit raises OverflowError — measured
    # on a real library at 154 GB. The file count stays `int`; nobody has two billion
    # files, and if they did the scan would be the least of it.
    scanned = Signal(str, str, int, "qlonglong", str)  # path, title, files, bytes, error
    progress = Signal(int, int, str)  # novels scanned, total, current title
    finished_ok = Signal()

    def __init__(self, library_dir, root_folder: str, parent=None):
        super().__init__(parent)
        self.library_dir = library_dir
        self.root_folder = root_folder
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from noveltrans.onedrive_upload import PushRequest, preview_push
        from noveltrans.storage.library import Library

        try:
            library = Library(self.library_dir)
            paths = library.list_projects()
        except Exception as exc:
            self.progress.emit(0, 0, f"Không đọc được thư viện: {exc}")
            self.finished_ok.emit()
            return

        total = len(paths)
        for index, path in enumerate(paths):
            if self._cancelled:
                break
            title = ""
            try:
                meta = library.project_meta(path)
                title = meta.translated_title or meta.title or path.name
                self.progress.emit(index, total, title)
                preview = preview_push(
                    PushRequest(
                        project_path=path,
                        novel_title=title,
                        root_folder=self.root_folder,
                    )
                )
            except Exception as exc:
                # One unreadable novel must not stop the scan — the others are still
                # worth offering, and the row says why this one cannot be ticked.
                self.scanned.emit(str(path), title or path.name, 0, 0, str(exc))
                continue
            self.scanned.emit(
                str(path),
                title,
                len(preview.to_upload),
                sum(item.size for item in preview.to_upload),
                "",
            )
        self.progress.emit(total, total, "")
        self.finished_ok.emit()


class OneDriveSyncWorker(PausableWorker):
    """Back up several novels in one run, one after another.

    One browser per novel rather than one for the whole run: `push_project` owns its
    browser, and reusing it across novels would mean holding Chrome open through every
    gap. The launch costs seconds against a per-novel transfer measured in minutes.

    A novel that fails does not stop the run — the same rule `push_project` applies to a
    batch, one level up. `needs_login` is the exception: every remaining novel would fail
    the same way.
    """

    progress = Signal(int, int, str)  # novels done, total, status line
    novel_done = Signal(str, int, int, int, str)  # title, uploaded, skipped, failed, error
    finished_ok = Signal(int, int)  # novels backed up, novels with errors
    failed = Signal(str)
    needs_login = Signal(str)

    def __init__(self, requests: list, parent=None):
        super().__init__(parent)
        self.requests = list(requests)

    def run(self) -> None:
        from noveltrans.onedrive_upload import (
            OneDriveCancelled,
            OneDriveUploadError,
            push_project,
        )

        total = len(self.requests)
        done = 0
        errors = 0
        try:
            for request in self.requests:
                if self._checkpoint():
                    raise OneDriveCancelled(uploaded=done)
                title = request.novel_title
                self.progress.emit(done, total, f"⬆️ {title}")
                try:
                    result = push_project(
                        request,
                        on_progress=lambda _d, _t, msg, _n=title: self.progress.emit(
                            done, total, f"{_n}: {msg}"
                        ),
                        should_cancel=lambda: self._cancelled,
                        on_checkpoint=self._checkpoint,
                    )
                except OneDriveUploadError as exc:
                    if exc.needs_login:
                        raise
                    errors += 1
                    self.novel_done.emit(title, 0, 0, 0, str(exc))
                else:
                    self.novel_done.emit(
                        title, result.uploaded, result.skipped, result.failed, ""
                    )
                done += 1
                self.progress.emit(done, total, f"✅ {title}")
        except OneDriveCancelled:
            self.failed.emit(
                f"Đã dừng đồng bộ. {done}/{total} truyện đã xong — chạy lại sẽ bỏ qua "
                "những file đã lên."
            )
        except OneDriveUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.finished_ok.emit(done - errors, errors)


class OneDriveVerifyWorker(QThread):
    """Check which part-videos really are on OneDrive, before anything is deleted.

    Its own thread because it opens a browser and walks one folder per part. The answer
    it produces is the *only* thing that authorises deleting a video — the local manifest
    is not accepted as proof, having been measured wrong on a real library.
    """

    progress = Signal(int, int, str)  # folders checked, total, current folder
    done = Signal(list, list)  # confirmed, unconfirmed (lists of Removable)
    failed = Signal(str)
    needs_login = Signal(str)

    def __init__(self, project_path, candidates: list, parent=None):
        super().__init__(parent)
        self.project_path = project_path
        self.candidates = list(candidates)

    def run(self) -> None:
        from noveltrans.cleanup import verify_on_onedrive
        from noveltrans.onedrive_upload import OneDriveUploadError

        try:
            confirmed, unconfirmed = verify_on_onedrive(
                self.project_path,
                self.candidates,
                on_progress=lambda i, n, folder: self.progress.emit(i, n, folder),
            )
        except OneDriveUploadError as exc:
            if exc.needs_login:
                self.needs_login.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(repr(exc))
        else:
            self.done.emit(confirmed, unconfirmed)

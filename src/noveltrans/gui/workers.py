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
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.storage import Library, NovelProject

# Some sites (e.g. medoctruyen.vn) throttle after a few chapters read quickly.
# On a rate-limit signal the download waits, then retries the same chapter.
_RATE_LIMIT_WAIT_SECONDS = 60
_RATE_LIMIT_MAX_RETRIES = 8


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
            if adapter.name == "medoctruyen":
                client.set_cookies(self.cookies)
            adapter.on_status = self.progress.emit
            meta = adapter.fetch_metadata(self.url)
            refs = adapter.fetch_chapter_list(self.url)

            library = Library(self.library_dir)
            existing = library.find_by_url(self.url)
            if existing is not None:
                project = NovelProject.open(existing)
                project.replace_toc(refs)  # pick up newly published chapters
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


class TranslateWorker(QThread):
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
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

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
            if self._cancelled:
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
        from noveltrans.translators.names import apply_glossary, build_glossary

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

            # Google romanizes Chinese names to pinyin; for Vietnamese output,
            # pre-replace recurring names with their Hán-Việt reading so the
            # whole novel gets consistent, correctly-styled names.
            glossary: dict[str, str] = {}
            if self.engine_name == "google" and self.target_lang == "vi":
                corpus = "\n".join(
                    c.title + "\n" + c.content for c in project.chapters() if c.content
                )
                glossary = build_glossary(corpus)

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
                if self._cancelled:
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
                [self.binary, "models"], capture_output=True, text=True, timeout=15
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
                vn_title=meta.translated_title or meta.title,
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
    error: str = ""


class AudioWorker(QThread):
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
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

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
        if self.use_translation:
            return chapter.translated_title or chapter.title, chapter.translated
        return chapter.title, chapter.content

    def _run_sequential(self, project, engine, pending: list, source: str) -> None:
        """The original single-engine loop — used whenever workers == 1."""
        from noveltrans.errors import TtsError
        from noveltrans.storage.project import slugify

        total = len(pending)
        done = 0
        errors = 0
        for chapter in pending:
            if self._cancelled:
                break
            title, text = self._title_text_for(chapter)
            self.progress.emit(done, total, title)
            # voice in the filename: re-voicing creates a NEW file, so audio
            # players that cached/imported the old one can't play stale audio
            name = f"{chapter.index + 1:04d}-{slugify(title)}-{slugify(self.voice)}.wav"
            out_path = project.audio_dir / name
            try:
                seconds = engine.synthesize_chapter(
                    title,
                    text,
                    out_path,
                    cancelled=lambda: self._cancelled,
                    clean=self.clean_text,
                    clean_extra_remove=self.clean_extra_remove,
                    gap_seconds=self.gap_seconds,
                    volume=self.volume,
                )
                seconds = self._apply_speed(out_path, seconds)
                if self.out_format == "mp3":
                    from noveltrans.tts.convert import convert_to_mp3

                    out_path = convert_to_mp3(out_path)
                rel_path = str(out_path.relative_to(project.path))
                if chapter.audio_path and chapter.audio_path != rel_path:
                    # re-voiced with another format — drop the stale old file
                    (project.path / chapter.audio_path).unlink(missing_ok=True)
                project.save_audio(chapter.index, rel_path, self.voice, seconds, source)
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
            seconds = engine.synthesize_chapter(
                title,
                text,
                out_path,
                cancelled=lambda: self._cancelled,
                clean=self.clean_text,
                clean_extra_remove=self.clean_extra_remove,
                gap_seconds=self.gap_seconds,
                volume=self.volume,
            )
            seconds = self._apply_speed(out_path, seconds)
            if self.out_format == "mp3":
                from noveltrans.tts.convert import convert_to_mp3

                out_path = convert_to_mp3(out_path)
            rel_path = str(out_path.relative_to(project_path))
            return _AudioResult(
                chapter.index, title, "ok", rel_path, seconds, chapter.audio_path or ""
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
            if self._cancelled:
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
            while inflight:
                finished, still = wait(inflight, return_when=FIRST_COMPLETED)
                inflight = set(still)
                for fut in finished:
                    result = fut.result()
                    if result.status == "cancelled":
                        continue  # not counted, no write (matches sequential break)
                    if result.status == "ok":
                        if result.prev_audio_path and result.prev_audio_path != result.rel_path:
                            # re-voiced with another format — drop the stale old file
                            (project.path / result.prev_audio_path).unlink(missing_ok=True)
                        project.save_audio(
                            result.index, result.rel_path, self.voice, result.seconds, source
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


class MergeWorker(QThread):
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
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
        self.fmt = fmt
        self.mode = mode
        # NOTE: not `self.start` — that would shadow QThread.start() and the thread
        # would never launch. Same care for end/batch for symmetry.
        self.start_num = start
        self.end_num = end
        self.batch_size = batch
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from noveltrans.errors import TtsError
        from noveltrans.storage.project import slugify
        from noveltrans.tts.merge import (
            MergeCancelled,
            MergeSegment,
            chapter_marker_title,
            merge_chapters,
            plan_merge_windows,
        )

        project = NovelProject.open(self.project_path)
        try:
            windows = plan_merge_windows(
                project.chapters(),
                self.voice,
                self.mode,
                start=self.start_num,
                end=self.end_num,
                batch=self.batch_size,
            )
            if not windows:
                self.failed.emit("Không có chương nào có audio giọng này trong phạm vi đã chọn.")
                return
            project.audio_dir.mkdir(parents=True, exist_ok=True)
            slug = slugify(project.meta.translated_title or project.meta.title)
            ext = "m4b" if self.fmt == "m4b" else "mp3"
            total = len(windows)
            written = 0
            for i, window in enumerate(windows):
                if self._cancelled:
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


class VideoWorker(QThread):
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
        bg_color: str = "",  # background hex "#rrggbb"; "" → the default pastel gradient
        skip_existing: bool = False,  # skip parts whose .mp4 already exists (batch "continue")
        credit: str = "",  # "Tạo bởi: …" line; "" → the default (Fox Novel)
        tagline: str = "",  # thumbnail subtitle under "PHẦN N"
        thumb_image_path: Path | str = "",  # thumbnail base image; "" → reuse image_path
        tags: str = "",  # novel-level YouTube tags (comma-joined) written per part
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
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
        self.bg_color = bg_color
        self.skip_existing = skip_existing
        self.credit = credit
        self.tagline = tagline
        self.thumb_image_path = str(thumb_image_path or "")
        self.tags = tags
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from noveltrans.errors import TtsError
        from noveltrans.storage.project import slugify
        from noveltrans.tts.merge import (
            MergeCancelled,
            MergeSegment,
            chapter_marker_title,
            plan_merge_windows,
        )
        from noveltrans.tts.player_skin import hex_to_rgb
        from noveltrans.tts.thumbnail import render_thumbnail
        from noveltrans.tts.video import (
            FONT_NAME,
            _with_real_durations,
            build_upload_title,
            build_video_description,
            font_dir_context,
            render_video,
            video_font,
            video_part_name,
        )

        bg_rgb = hex_to_rgb(self.bg_color)

        project = NovelProject.open(self.project_path)
        try:
            windows = plan_merge_windows(
                project.chapters(),
                self.voice,
                self.mode,
                start=self.start_num,
                end=self.end_num,
                batch=self.batch_size,
            )
            if not windows:
                self.failed.emit("Không có chương nào có audio giọng này trong phạm vi đã chọn.")
                return
            project.video_dir.mkdir(parents=True, exist_ok=True)
            slug = slugify(project.meta.translated_title or project.meta.title)
            novel_title = project.meta.translated_title or project.meta.title
            total = len(windows)
            written = 0
            with font_dir_context() as font_dir:
                for i, window in enumerate(windows):
                    if self._cancelled:
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
                        slug, window.first_num, window.last_num, whole_novel=whole_novel
                    )
                    # Each part goes in its own folder (video + sidecars) so it can be
                    # uploaded on its own; legacy flat renders still count for skip_existing.
                    out_path = project.video_dir / Path(name).stem / name
                    legacy_path = project.video_dir / name
                    if self.skip_existing and (out_path.is_file() or legacy_path.is_file()):
                        self.progress.emit(i + 1, total, "")  # already made — skip
                        continue
                    self.progress.emit(i, total, name)
                    part_num = None if whole_novel else (i + 1)
                    try:
                        render_video(
                            segments, self.image_path, out_path, font_dir, novel_title,
                            width=self.width, height=self.height, fps=self.fps,
                            spin_vinyl=self.spin_vinyl, font_name=self.font or FONT_NAME,
                            bg_color=bg_rgb, cancelled=lambda: self._cancelled,
                        )
                        self._write_metadata(
                            project, out_path, novel_title, segments, part_num, font_dir,
                            _with_real_durations, build_upload_title,
                            build_video_description, video_font, render_thumbnail,
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
        with_real_durations, build_upload_title, build_video_description,
        video_font, render_thumbnail,
    ) -> None:
        """Write the title / description / tags / thumbnail sidecars next to `out_path`.

        A thumbnail failure is swallowed (a bad base image must not discard an otherwise
        good video); the text sidecars are cheap and always written.
        """
        from noveltrans.tts.thumbnail import DEFAULT_PART_POS, DEFAULT_TITLE_POS

        def sidecar(ext: str) -> Path:
            return out_path.parent / (out_path.stem + ext)

        timed = with_real_durations(segments)
        title = build_upload_title(novel_title, part_num)
        sidecar(".title.txt").write_text(title + "\n", encoding="utf-8")

        desc = build_video_description(
            timed,
            original_title=project.meta.title,
            vn_title=novel_title,
            original_author=project.meta.author,
            vn_author=project.meta.translated_author,
            total_chapters=project.counts()["total"],
            credit=self.credit or "Fox Novel",
        )
        sidecar(".txt").write_text(desc, encoding="utf-8")  # richer than render_video's

        if self.tags.strip():
            sidecar(".tags.txt").write_text(self.tags.strip() + "\n", encoding="utf-8")

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
            )
        except Exception:  # noqa: BLE001 — never fail a good render over a thumbnail
            pass


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


class DownloadWorker(QThread):
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
        self._cancelled = False

    def _select_chapters(self, project) -> list:
        """The chapters this run will fetch, honouring the range and `force`."""
        if self.force:
            return project.chapters_in_range(self.start_index, self.end_index)
        return project.pending_download(self.start_index, self.end_index)

    def cancel(self) -> None:
        self._cancelled = True

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
            if adapter.name == "medoctruyen":
                client.set_cookies(self.cookies)

            pending = self._select_chapters(project)
            total = len(pending)
            done = 0
            errors = 0
            # Reads `done`/`total` at call time (closure over run()'s locals), so a
            # mid-batch browser relaunch reports the real position, not 0.
            adapter.on_status = lambda msg: self.progress.emit(done, total, msg)
            for chapter in pending:
                if self._cancelled:
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

"""Background QThread workers.

Workers never touch Qt widgets and never share a NovelProject/sqlite
connection across threads: they receive a *path* and open their own
NovelProject inside run(). The GUI keeps its own read connection.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from noveltrans.errors import NovelTransError, UnsupportedSiteError
from noveltrans.models import ChapterRef
from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient
from noveltrans.storage import Library, NovelProject


class ScanWorker(QThread):
    """Fetch metadata + TOC for a URL and create/refresh the project."""

    scanned = Signal(str, object, int)  # project path, NovelMeta, chapter count
    failed = Signal(str)

    def __init__(self, url: str, library_dir: Path, delay: float, parent=None):
        super().__init__(parent)
        self.url = url
        self.library_dir = library_dir
        self.delay = delay

    def run(self) -> None:
        try:
            client = HttpClient(delay_seconds=self.delay)
            adapter = adapter_for_url(self.url, client)
            if adapter is None:
                raise UnsupportedSiteError(
                    f"Chưa hỗ trợ trang web này: {self.url}"
                )
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

    def run(self) -> None:
        from noveltrans.translators import get_translator
        from noveltrans.translators.names import apply_glossary, build_glossary

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

        project = NovelProject.open(self.project_path)
        try:
            if self.indices is not None:
                chapters = (project.chapter(i) for i in self.indices)
                pending = [c for c in chapters if c is not None and c.content]
            else:
                pending = project.pending_translation(self.target_lang)
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
                    project.save_meta_translation(meta_title, meta_desc, self.target_lang)
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


class AudioWorker(QThread):
    """Generate audio for translated chapters of a project, resumably."""

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
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.voice = voice
        self.out_format = out_format
        self.indices = indices  # None = all pending; else re-generate exactly these
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from noveltrans.errors import TtsError
        from noveltrans.storage.project import slugify
        from noveltrans.tts import get_tts_engine

        try:
            engine = get_tts_engine("vieneu", voice=self.voice)
            self.progress.emit(0, 0, "Đang tải model VieNeu (~330 MB lần đầu)…")
            engine.load()
        except TtsError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(f"Lỗi không mong đợi khi nạp TTS: {exc!r}")
            return

        project = NovelProject.open(self.project_path)
        try:
            if self.indices is not None:
                chapters = (project.chapter(i) for i in self.indices)
                pending = [c for c in chapters if c is not None and c.translated]
            else:
                pending = project.pending_audio(self.voice)
            total = len(pending)
            done = 0
            errors = 0
            project.audio_dir.mkdir(parents=True, exist_ok=True)

            for chapter in pending:
                if self._cancelled:
                    break
                title = chapter.translated_title or chapter.title
                self.progress.emit(done, total, title)
                # voice in the filename: re-voicing creates a NEW file, so audio
                # players that cached/imported the old one can't play stale audio
                name = f"{chapter.index + 1:04d}-{slugify(title)}-{slugify(self.voice)}.wav"
                out_path = project.audio_dir / name
                try:
                    seconds = engine.synthesize_chapter(
                        chapter.translated_title,
                        chapter.translated,
                        out_path,
                        cancelled=lambda: self._cancelled,
                    )
                    if self.out_format == "mp3":
                        from noveltrans.tts.convert import convert_to_mp3

                        out_path = convert_to_mp3(out_path)
                    rel_path = str(out_path.relative_to(project.path))
                    if chapter.audio_path and chapter.audio_path != rel_path:
                        # re-voiced with another format — drop the stale old file
                        (project.path / chapter.audio_path).unlink(missing_ok=True)
                    project.save_audio(chapter.index, rel_path, self.voice, seconds)
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
        finally:
            project.close()


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
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.exporter_name = exporter_name
        self.out_path = Path(out_path)
        self.use_translation = use_translation

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
    finished_ok = Signal(int, int)  # downloaded count, error count

    def __init__(self, project_path: Path, delay: float, parent=None):
        super().__init__(parent)
        self.project_path = Path(project_path)
        self.delay = delay
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        project = NovelProject.open(self.project_path)
        try:
            client = HttpClient(delay_seconds=self.delay)
            adapter = adapter_for_url(project.meta.url, client)
            if adapter is None:
                self.finished_ok.emit(0, 0)
                return

            pending = project.pending_download()
            total = len(pending)
            done = 0
            errors = 0
            for chapter in pending:
                if self._cancelled:
                    break
                ref_title = chapter.title
                self.progress.emit(done, total, ref_title)
                try:
                    text = adapter.fetch_chapter(
                        ChapterRef(index=chapter.index, title=chapter.title, url=chapter.url)
                    )
                    project.save_content(chapter.index, text)
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

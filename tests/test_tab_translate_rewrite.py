"""Feature 060 — tab-level wiring for the style rewrite.

The dialog's own logic and the worker's behaviour are covered in test_rewrite.py and
test_workers_rewrite.py; here we check the tab opens the dialog correctly, the
right-click actions appear only where they mean something, and — the part that would
abandon a running QThread if missed — that every lifecycle site knows about the second
worker.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMenu

import noveltrans.gui.tab_translate as tt
from noveltrans.config import AppConfig
from noveltrans.gui.rewrite_dialog import RewriteDialog
from noveltrans.gui.workers import chapters_to_rewrite
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import Library

CONVERT = (
    "Phó Thanh Từ đứng ở cửa, trong lòng hắn có một loại không cách nào nói nói tư vị.\n\n"
    "Giang Dư nhìn hắn thật lâu, sau đó mới chậm rãi mở miệng nói ra một câu."
)


class _RunningWorker:
    """Stands in for a live QThread without starting one."""

    def __init__(self):
        self.cancelled = False
        self.waited = 0

    def isRunning(self):
        return True

    def cancel(self):
        self.cancelled = True

    def wait(self, msec):
        self.waited = msec
        return True


def _tab(qapp, tmp_path, monkeypatch, *, translated: int = 2, lang: str = "vi"):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    config = AppConfig()
    monkeypatch.setattr(type(config), "library_dir", tmp_path / "lib")
    tab = tt.TranslateTab(config)

    library = Library(tmp_path / "lib")
    meta = NovelMeta(url="https://x/1", site="x", title="Truyện")
    refs = [ChapterRef(index=i, title=f"Chương {i + 1}", url=f"https://x/{i}") for i in range(3)]
    project = library.create_project(meta, refs)
    for idx in range(translated):
        project.save_content(idx, f"原文{idx}")
        project.save_translation(
            idx, f"Chương {idx + 1}", CONVERT, lang, translator="Google Translate"
        )
    tab.project = project
    tab.model.set_chapters(project.chapters())
    return tab, project


@pytest.fixture
def no_modal(monkeypatch):
    """Never actually render a modal; record that exec was reached."""
    opened: list[bool] = []
    monkeypatch.setattr(RewriteDialog, "exec", lambda self: opened.append(True))
    return opened


class TestOpenGuards:
    def test_it_is_guarded_when_no_project_is_selected(self, qapp, monkeypatch, no_modal):
        infos: list = []
        monkeypatch.setattr(tt.QMessageBox, "information", lambda *a, **k: infos.append(a))
        tab = tt.TranslateTab(AppConfig())
        tab.project = None
        tab._open_rewrite()
        assert infos and not no_modal

    def test_it_is_guarded_while_translating(self, qapp, tmp_path, monkeypatch, no_modal):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._worker = _RunningWorker()
        tab._open_rewrite()
        assert not no_modal
        assert "phiên dịch" in tab.status_label.text()

    def test_it_is_guarded_while_already_rewriting(self, qapp, tmp_path, monkeypatch, no_modal):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._rewrite_worker = _RunningWorker()
        tab._open_rewrite()
        assert not no_modal
        # naming which run is in the way — the two take very different times to finish
        assert "viết lại" in tab.status_label.text()

    def test_it_flushes_a_half_typed_edit_before_opening(
        self, qapp, tmp_path, monkeypatch, no_modal
    ):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        flushed: list[str] = []
        monkeypatch.setattr(tab, "_save_preview_edits", lambda: flushed.append("translated"))
        monkeypatch.setattr(tab, "_save_original_edits", lambda: flushed.append("original"))
        tab._open_rewrite()
        # both panes, before exec — a later focus-out must not overwrite the rewrite
        assert flushed == ["translated", "original"]
        assert no_modal


class TestDialog:
    def test_google_is_never_offered(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = RewriteDialog(project, tab.config, None)
        engines = [
            dialog.engine_combo.itemData(i) for i in range(dialog.engine_combo.count())
        ]
        assert "google" not in engines
        assert engines  # …and something usable is offered

    def test_it_refuses_a_translation_that_is_not_vietnamese(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch, lang="en")
        dialog = RewriteDialog(project, tab.config, None)
        assert "tiếng Việt" in dialog.estimate_label.text()
        assert not dialog.start_button.isEnabled()
        assert not dialog.preview_button.isEnabled()

    def test_it_refuses_a_novel_with_nothing_translated(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch, translated=0)
        dialog = RewriteDialog(project, tab.config, None)
        assert "chưa có chương nào được dịch" in dialog.estimate_label.text()
        assert not dialog.start_button.isEnabled()

    def test_the_count_it_shows_is_the_set_the_worker_would_process(
        self, qapp, tmp_path, monkeypatch
    ):
        # A dialog that promised a different number than the run delivers would be
        # lying about hours of work.
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = RewriteDialog(project, tab.config, None)
        expected = chapters_to_rewrite(project, "vi", start_idx=0, end_idx=2)
        assert len(dialog._eligible()) == len(expected) == 2
        assert "2 chương" in dialog.estimate_label.text()

    def test_it_warns_when_the_chapters_already_have_audio(
        self, qapp, tmp_path, monkeypatch
    ):
        # pending_audio never compares text, so nothing re-voices itself after a rewrite.
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        project.save_audio(0, "exports/audio/1.wav", "Ngọc Linh", seconds=10.0)
        dialog = RewriteDialog(project, tab.config, None)
        assert "đã có audio" in dialog.estimate_label.text()

    def test_applying_a_preview_writes_it_and_reports_the_chapter(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = RewriteDialog(project, tab.config, None)
        dialog.show_preview(0, "Chương 1", "Nội tâm hắn tràn ngập tư vị khó nói.")
        assert dialog.apply_button.isEnabled()

        applied: list[set] = []
        dialog.applied.connect(applied.append)
        dialog._apply_preview()

        assert applied == [{0}]
        chapter = project.chapter(0)
        assert chapter.is_rewritten
        assert chapter.translated_raw == CONVERT
        assert not dialog.apply_button.isEnabled()  # consumed, not re-appliable

    def test_a_failed_preview_offers_nothing_to_apply(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = RewriteDialog(project, tab.config, None)
        dialog.show_preview_error(0, "số đoạn không khớp")
        assert not dialog.apply_button.isEnabled()
        assert "số đoạn" in dialog.after_view.toPlainText()
        assert project.chapter(0).translated == CONVERT


class TestContextMenu:
    def _menu(self, tab, row: int) -> QMenu:
        # Parented to the tab: an unparented QMenu is collected as soon as this helper
        # returns, and reading its QActions afterwards raises "C++ object already deleted".
        menu = QMenu(tab)
        index = tab.model.index(row, tab.model.TITLE_COLUMN)
        tab.table.setCurrentIndex(index)
        tab._table_context_actions(menu, index)
        return menu

    def _labels(self, menu: QMenu) -> list[str]:
        return [a.text() for a in menu.actions() if a.text()]

    def test_a_translated_row_offers_rewrite(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        assert any("Viết lại chương này" in label for label in self._labels(self._menu(tab, 0)))

    def test_an_untranslated_row_offers_nothing(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        assert self._labels(self._menu(tab, 2)) == []

    def test_undo_appears_only_once_a_chapter_has_been_rewritten(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        assert not any("Hoàn tác" in label for label in self._labels(self._menu(tab, 0)))

        project.save_rewrite(0, "Chương 1", "Nội tâm hắn tràn ngập tư vị khó nói.")
        tab.model.set_chapters(project.chapters())
        assert any("Hoàn tác" in label for label in self._labels(self._menu(tab, 0)))

    def test_the_actions_are_disabled_while_a_run_is_going(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._worker = _RunningWorker()
        actions = [a for a in self._menu(tab, 0).actions() if "Viết lại" in a.text()]
        assert actions and not any(a.isEnabled() for a in actions)


class TestUndo:
    def test_it_restores_the_translation_and_refreshes_the_table(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        project.save_rewrite(0, "Chương 1", "Nội tâm hắn tràn ngập tư vị khó nói.")
        tab.model.set_chapters(project.chapters())

        tab._undo_rewrite(0)

        assert project.chapter(0).translated == CONVERT
        assert not tab.model.chapter_at(0).is_rewritten  # the ✍️ marker is gone
        assert "hoàn tác" in tab.status_label.text().lower()

    def test_undoing_nothing_says_nothing(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.status_label.setText("")
        tab._undo_rewrite(0)
        assert tab.status_label.text() == ""


class TestTwoWorkerLifecycle:
    """§5.9 — four sites assumed a single self._worker. Missing one leaks a QThread."""

    def test_translating_refuses_while_a_rewrite_runs(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._rewrite_worker = _RunningWorker()
        tab._start_translate()
        assert tab._worker is None
        assert "viết lại" in tab.status_label.text()

    def test_rewriting_refuses_while_a_translation_runs(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._worker = _RunningWorker()
        tab._start_rewrite({"engine_name": "cli", "target_lang": "vi"})
        assert tab._rewrite_worker is None

    def test_has_running_workers_sees_the_rewrite_worker(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        assert not tab.has_running_workers()
        tab._rewrite_worker = _RunningWorker()
        assert tab.has_running_workers()

    def test_cancel_reaches_the_rewrite_worker(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        worker = _RunningWorker()
        tab._rewrite_worker = worker
        tab._cancel()
        assert worker.cancelled

    def test_shutdown_cancels_and_joins_the_rewrite_worker(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        worker = _RunningWorker()
        tab._rewrite_worker = worker
        tab.shutdown()
        assert worker.cancelled and worker.waited == 60_000

    def test_reset_buttons_re_enables_the_rewrite_button(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.rewrite_button.setEnabled(False)
        tab._reset_buttons()
        assert tab.rewrite_button.isEnabled()

    def test_the_progress_line_says_which_job_is_running(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._progress_verb = "Đang viết lại"
        tab._on_progress(1, 2, "Chương 1")
        assert tab.status_label.text() == "Đang viết lại: Chương 1"

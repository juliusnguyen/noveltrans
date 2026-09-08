"""Feature 084 — tab-level wiring for translation QC.

The detectors, the loop and the workers are covered in test_translation_qc.py and
test_workers_qc.py. Here: the tab opens the dialogs correctly, the right-click action
appears only where it means something, the result view's selection reaches the existing
re-translate path, and — the part that would abandon a running QThread if missed — every
lifecycle site knows about the third worker.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMenu

import noveltrans.gui.tab_translate as tt
from noveltrans.config import AppConfig
from noveltrans.gui.qc_dialog import QcDialog, QcResultDialog
from noveltrans.models import QC_STATUS_FAIL, ChapterRef, NovelMeta
from noveltrans.storage import Library

GOOD_VI = "Hắn nhìn nàng một cái rồi quay đầu bỏ đi, trong lòng dâng lên nỗi buồn khó tả."


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
        project.save_translation(idx, f"Chương {idx + 1}", GOOD_VI, lang, "CLI (agy)")
    tab.project = project
    tab.model.set_chapters(project.chapters())
    return tab, project


@pytest.fixture
def no_modal(monkeypatch):
    """Never actually render a modal; record that exec was reached."""
    opened: list[bool] = []
    monkeypatch.setattr(QcDialog, "exec", lambda self: opened.append(True))
    monkeypatch.setattr(QcResultDialog, "exec", lambda self: opened.append(True))
    return opened


class TestOpenGuards:
    def test_it_is_guarded_when_no_project_is_selected(self, qapp, monkeypatch, no_modal):
        infos: list = []
        monkeypatch.setattr(tt.QMessageBox, "information", lambda *a, **k: infos.append(a))
        tab = tt.TranslateTab(AppConfig())
        tab.project = None
        tab._open_qc()
        assert infos and not no_modal

    def test_it_is_guarded_while_translating(self, qapp, tmp_path, monkeypatch, no_modal):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._worker = _RunningWorker()
        tab._open_qc()
        assert not no_modal
        assert "phiên dịch" in tab.status_label.text()

    def test_a_running_scan_names_itself_in_the_busy_message(
        self, qapp, tmp_path, monkeypatch, no_modal
    ):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._qc_worker = _RunningWorker()
        tab._open_qc()
        assert not no_modal
        assert "kiểm tra" in tab.status_label.text().lower()

    def test_it_flushes_a_half_typed_edit_before_opening(
        self, qapp, tmp_path, monkeypatch, no_modal
    ):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        flushed: list[str] = []
        monkeypatch.setattr(tab, "_save_preview_edits", lambda: flushed.append("translated"))
        monkeypatch.setattr(tab, "_save_original_edits", lambda: flushed.append("original"))
        tab._open_qc()
        assert flushed == ["translated", "original"]  # before exec, both panes
        assert no_modal


class TestLifecycle:
    """A third worker means six sites to update; missing one abandons a live QThread."""

    def test_a_running_scan_counts_as_busy(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        assert not tab.has_running_workers()
        tab._qc_worker = _RunningWorker()
        assert tab._busy() and tab.has_running_workers()

    def test_cancel_reaches_the_scan(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        worker = _RunningWorker()
        tab._qc_worker = worker
        tab._cancel()
        assert worker.cancelled

    def test_shutdown_joins_the_scan(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        worker = _RunningWorker()
        tab._qc_worker = worker
        tab.shutdown()
        assert worker.cancelled and worker.waited == 60_000

    def test_the_button_comes_back_when_a_run_ends(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.qc_button.setEnabled(False)
        tab._reset_buttons()
        assert tab.qc_button.isEnabled()


class TestContextMenu:
    def _menu(self, tab, rows):
        menu = QMenu()
        tab._add_qc_action(menu, rows)
        return [action.text() for action in menu.actions() if action.text()]

    def test_it_is_offered_on_a_translated_row(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        assert self._menu(tab, [0]) == ["🔍 Kiểm tra chất lượng chương này"]

    def test_it_counts_the_selection(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        assert self._menu(tab, [0, 1]) == ["🔍 Kiểm tra chất lượng 2 chương"]

    def test_it_is_absent_where_nothing_is_translated(self, qapp, tmp_path, monkeypatch):
        # Offering a check on an untranslated chapter would only mislead.
        tab, _ = _tab(qapp, tmp_path, monkeypatch, translated=0)
        assert self._menu(tab, [0, 1, 2]) == []

    def test_it_is_disabled_while_busy_and_says_why(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab._worker = _RunningWorker()
        menu = QMenu()
        tab._add_qc_action(menu, [0])
        action = next(a for a in menu.actions() if a.text())
        assert not action.isEnabled()
        assert action.toolTip() == tab._busy_message()


class TestQcSettingsHandoff:
    def test_the_worker_gets_none_while_qc_is_off(self, qapp, tmp_path, monkeypatch):
        """The backward-compatibility guarantee, at the tab boundary."""
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_enabled = False
        assert tab._qc_settings("cli", "", "") is None

    def test_an_empty_chain_falls_back_to_the_tabs_own_engine(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_enabled = True
        settings = tab._qc_settings("cli", "sonnet", "")
        assert [spec.engine_name for spec in settings.chain] == ["cli"]
        assert settings.chain[0].model == "sonnet"

    def test_the_configured_chain_is_used_in_order(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_enabled = True
        tab.config.qc_engine_chain = [("claude_cli", "", 2), ("cli", "", 3)]
        settings = tab._qc_settings("cli", "", "")
        assert [(s.engine_name, s.attempts) for s in settings.chain] == [
            ("claude_cli", 2), ("cli", 3),
        ]

    def test_turning_the_judge_off_leaves_the_cheap_checks(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_enabled = True
        tab.config.qc_use_llm_judge = False
        assert tab._qc_settings("cli", "", "").judge is None


class TestResultView:
    def _failed(self, project, *indices):
        for idx in indices:
            chapter = project.chapter(idx)
            project.save_qc_verdict(
                idx, QC_STATUS_FAIL, "not_vietnamese", "bản dịch ra tiếng Anh",
                chapter.qc_fingerprint(), 2,
            )
        return project.qc_failures()

    def test_it_lists_the_failures_with_a_readable_class(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcResultDialog(self._failed(project, 0, 1))
        assert dialog.table.rowCount() == 2
        assert dialog.table.item(0, dialog.ERROR_COLUMN).text() == "Dịch ra tiếng Anh"
        assert "tiếng Anh" in dialog.table.item(0, dialog.DETAIL_COLUMN).text()

    def test_everything_starts_selected_and_can_be_cleared(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcResultDialog(self._failed(project, 0, 1))
        assert dialog.checked_indices() == [0, 1]
        dialog._set_all_checked(False)
        assert dialog.checked_indices() == []

    def test_the_selection_reaches_the_tabs_existing_retranslate_path(
        self, qapp, tmp_path, monkeypatch
    ):
        """The whole "review then choose" step is this one connection: the result view
        does not re-translate anything itself."""
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        requested: list[list[int]] = []
        dialog = QcResultDialog(self._failed(project, 0, 1))
        dialog.retranslate_requested.connect(requested.append)
        dialog._set_all_checked(False)
        dialog.table.item(1, dialog.CHECK_COLUMN).setCheckState(
            dialog.table.item(1, dialog.CHECK_COLUMN).checkState().Checked
        )
        dialog._request_retranslate()
        assert requested == [[1]]

    def test_an_empty_selection_does_nothing(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        requested: list = []
        dialog = QcResultDialog(self._failed(project, 0))
        dialog.retranslate_requested.connect(requested.append)
        dialog._set_all_checked(False)
        dialog._request_retranslate()
        assert requested == []

    def test_double_clicking_asks_the_tab_to_open_the_chapter(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcResultDialog(self._failed(project, 0, 1))
        activated: list[int] = []
        dialog.chapter_activated.connect(activated.append)
        dialog._on_double_click(dialog.table.model().index(1, dialog.TITLE_COLUMN))
        assert activated == [1]  # read it before believing the verdict


class TestRetranslateFromTheResultView:
    """The path from "chọn chương lỗi" to an actual re-translation.

    It crashed in the real app on `self._reload_table()` — a method that has never existed
    (feature 072 wrote the call; nothing had exercised it since). No test reached this
    handler, so nothing caught it.
    """

    def test_it_clears_the_translations_and_queues_exactly_those_chapters(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        started: list = []
        monkeypatch.setattr(tab, "_start_translate", lambda **kw: started.append(kw))

        tab._retranslate_indices([1])

        assert started == [{"indices": [1]}]
        assert project.chapter(1).translated == ""  # dropped, ready to redo
        assert project.chapter(0).translated == GOOD_VI  # the others are untouched

    def test_it_refreshes_the_table_the_user_is_looking_at(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        monkeypatch.setattr(tab, "_start_translate", lambda **kw: None)
        tab._retranslate_indices([0])
        row = tab.model.row_for_index(0)
        assert tab.model.chapter_at(row).translated == ""

    def test_nothing_happens_without_a_selection(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        started: list = []
        monkeypatch.setattr(tab, "_start_translate", lambda **kw: started.append(kw))
        tab._retranslate_indices([])
        assert started == []
        assert project.chapter(0).translated == GOOD_VI


class TestQcDialog:
    def test_google_is_never_offered_as_a_judge(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcDialog(project, tab.config)
        engines = [dialog.engine_combo.itemData(i) for i in range(dialog.engine_combo.count())]
        assert "google" not in engines and engines

    def test_it_refuses_a_translation_that_is_not_vietnamese(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch, lang="en")
        dialog = QcDialog(project, tab.config)
        assert "tiếng Việt" in dialog.estimate_label.text()
        assert not dialog.start_button.isEnabled()

    def test_it_refuses_a_novel_with_nothing_translated(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch, translated=0)
        dialog = QcDialog(project, tab.config)
        assert "chưa có chương nào" in dialog.estimate_label.text()
        assert not dialog.start_button.isEnabled()

    def test_the_estimate_names_the_quota_cost_when_the_judge_is_on(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_use_llm_judge = True
        dialog = QcDialog(project, tab.config)
        assert "lượt gọi AI" in dialog.estimate_label.text()

    def test_turning_the_judge_off_says_it_is_free(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcDialog(project, tab.config)
        dialog.judge_check.setChecked(False)
        assert "không tốn quota" in dialog.estimate_label.text()

    def test_the_chain_round_trips_through_the_table(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_engine_chain = [("cli", "", 2), ("claude_cli", "sonnet", 3)]
        dialog = QcDialog(project, tab.config)
        assert dialog._chain() == [("cli", "", 2), ("claude_cli", "sonnet", 3)]

    def test_reordering_keeps_every_field(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        tab.config.qc_engine_chain = [("cli", "", 2), ("claude_cli", "sonnet", 3)]
        dialog = QcDialog(project, tab.config)
        dialog.chain_table.setCurrentCell(1, 0)
        dialog._move_chain_row(-1)
        assert dialog._chain() == [("claude_cli", "sonnet", 3), ("cli", "", 2)]

    def test_a_chain_row_is_tall_enough_to_read(self, qapp, tmp_path, monkeypatch):
        """A cell widget does not drive the row height, so the default section size clipped
        the engine combo and cut its text in half."""
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcDialog(project, tab.config)
        dialog._add_chain_row("cli", "", 2)
        combo = dialog.chain_table.cellWidget(0, 0)
        assert dialog.chain_table.rowHeight(0) >= combo.sizeHint().height()

    def test_no_label_hides_a_character_behind_a_qt_mnemonic(self, qapp, tmp_path, monkeypatch):
        """Qt eats a bare "&" in a widget label as a keyboard accelerator: "kiểm tra &
        dịch lại" rendered as "kiểm tra _dịch lại"."""
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcDialog(project, tab.config)
        for widget in (dialog.auto_check, dialog.judge_check):
            text = widget.text()
            assert "&" not in text.replace("&&", ""), text

    def test_starting_remembers_the_choices(self, qapp, tmp_path, monkeypatch):
        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcDialog(project, tab.config)
        monkeypatch.setattr(dialog, "accept", lambda: None)
        dialog.auto_check.setChecked(True)
        dialog._add_chain_row("cli", "", 4)
        params: list[dict] = []
        dialog.start_requested.connect(params.append)
        dialog._request_start()
        assert params and params[0]["target_lang"] == "vi"
        assert tab.config.qc_enabled is True
        assert tab.config.qc_engine_chain == [("cli", "", 4)]

    def test_the_dry_run_asks_for_a_handful_of_chapters(self, qapp, tmp_path, monkeypatch):
        from noveltrans.gui.qc_dialog import DRY_RUN_CHAPTERS

        tab, project = _tab(qapp, tmp_path, monkeypatch)
        dialog = QcDialog(project, tab.config)
        monkeypatch.setattr(dialog, "accept", lambda: None)
        params: list[dict] = []
        dialog.start_requested.connect(params.append)
        dialog._request_start(limit=DRY_RUN_CHAPTERS)
        assert params[0]["limit"] == DRY_RUN_CHAPTERS

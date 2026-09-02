"""TranslateTab: translate every selected chapter from one right-click.

The table has always been multi-select (`enable_cell_copy` puts it in ExtendedSelection)
and the right-click menu has always been built from the selection — but only the *rewrite*
actions read it. Translating twenty chapters still meant twenty clicks on the per-row
"↻ Dịch lại" button. These tests pin the two halves that make the selection actionable:
the menu names what it will do, and the batch and the per-row button run the same guards.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QAbstractItemView, QMenu

import noveltrans.gui.tab_translate as tt
from noveltrans.config import AppConfig
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import Library


class _RunningWorker:
    """Stands in for a live QThread without starting one."""

    def isRunning(self):
        return True


def _tab(tmp_path, monkeypatch, *, downloaded: int = 6, translated: int = 6):
    """A tab over a real project: `downloaded` chapters have source text, of which the
    first `translated` also have a translation."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    config = AppConfig()
    monkeypatch.setattr(type(config), "library_dir", tmp_path / "lib")
    tab = tt.TranslateTab(config)

    library = Library(tmp_path / "lib")
    meta = NovelMeta(url="https://x/1", site="x", title="Truyện")
    refs = [ChapterRef(index=i, title=f"Chương {i + 1}", url=f"https://x/{i}") for i in range(6)]
    project = library.create_project(meta, refs)
    for idx in range(downloaded):
        project.save_content(idx, f"原文{idx}")
    for idx in range(translated):
        project.save_translation(
            idx, f"Chương {idx + 1}", f"Bản dịch {idx}", "vi", translator="Google Translate"
        )
    tab.project = project
    tab.model.set_chapters(project.chapters())
    return tab, project


def _select_rows(tab, rows: list[int]) -> None:
    """Select model rows through the VIEW.

    `tab.table.model()` is the sort proxy, and a selection model only understands indices
    from its own model. Unsorted the proxy maps 1:1, so these are the same rows.
    """
    selection = tab.table.selectionModel()
    selection.clearSelection()
    view_model = tab.table.model()
    for row in rows:
        selection.select(
            view_model.index(row, 0),
            selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
        )


def _menu(tab, row: int) -> QMenu:
    # Parented to the tab: an unparented QMenu is collected as soon as this helper
    # returns, and reading its QActions afterwards raises "C++ object already deleted".
    menu = QMenu(tab)
    tab._table_context_actions(menu, tab.table.model().index(row, 0))
    return menu


def _labels(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if a.text()]


def _translate_action(menu: QMenu):
    found = [a for a in menu.actions() if a.text().endswith("chương") or "chương này" in a.text()]
    return next((a for a in found if "Dịch" in a.text()), None)


class TestSelection:
    def test_the_table_allows_multi_row_selection(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        assert tab.table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
        _select_rows(tab, [1, 3, 4])
        assert tab._selected_rows() == [1, 3, 4]

    def test_selected_rows_dedups_the_cells_of_one_row(self, qapp, tmp_path, monkeypatch):
        # SelectRows hands back one index per *cell*; the helper must not count a row twice.
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [2])
        assert len(tab.table.selectionModel().selectedIndexes()) > 1
        assert tab._selected_rows() == [2]

    def test_right_clicking_inside_the_selection_keeps_it(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0, 1, 2])
        assert tab._menu_rows(tab.table.model().index(1, 0)) == [0, 1, 2]

    def test_right_clicking_outside_the_selection_collapses_to_that_row(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0, 1])
        assert tab._menu_rows(tab.table.model().index(4, 0)) == [4]


class TestTranslatableIndices:
    def test_it_returns_chapter_indices_in_chapter_order(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        assert tab._translatable_indices([4, 1, 2]) == ([1, 2, 4], 0)

    def test_it_drops_and_counts_chapters_without_source_text(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=3, translated=0)
        assert tab._translatable_indices([1, 2, 3, 4]) == ([1, 2], 2)

    def test_it_is_empty_without_a_project(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        tab.project = None
        assert tab._translatable_indices([0, 1]) == ([], 0)


class TestMenu:
    def test_it_names_the_selected_count(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0, 2, 5])
        assert "↻ Dịch lại 3 chương" in _labels(_menu(tab, 2))

    def test_it_is_singular_for_one_row(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [4])
        assert "↻ Dịch lại chương này" in _labels(_menu(tab, 4))

    def test_it_says_dich_when_a_target_has_no_translation_yet(
        self, qapp, tmp_path, monkeypatch
    ):
        # "Dịch lại" would be plainly wrong for a chapter that was never translated.
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=6, translated=2)
        _select_rows(tab, [0, 3])
        assert "🔤 Dịch 2 chương" in _labels(_menu(tab, 3))

    def test_it_says_dich_lai_when_every_target_is_already_translated(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=6, translated=2)
        _select_rows(tab, [0, 1])
        assert "↻ Dịch lại 2 chương" in _labels(_menu(tab, 0))

    def test_it_offers_nothing_when_no_chapter_has_source_text(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=0, translated=0)
        _select_rows(tab, [0, 1])
        assert _labels(_menu(tab, 0)) == []

    def test_it_comes_before_the_rewrite_actions(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0])
        labels = _labels(_menu(tab, 0))
        assert labels.index("↻ Dịch lại chương này") < labels.index("✍️ Viết lại chương này")

    def test_it_is_disabled_while_translating(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0, 1])
        tab._worker = _RunningWorker()
        action = _translate_action(_menu(tab, 0))
        assert action is not None and not action.isEnabled()
        assert "phiên dịch" in action.toolTip()

    def test_it_is_disabled_while_rewriting(self, qapp, tmp_path, monkeypatch):
        # The two batches write the same rows — a translate started mid-rewrite would race it.
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0, 1])
        tab._rewrite_worker = _RunningWorker()
        action = _translate_action(_menu(tab, 0))
        assert action is not None and not action.isEnabled()
        assert "viết lại" in action.toolTip()

    def test_it_reports_skipped_chapters_in_its_tooltip(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=2, translated=0)
        _select_rows(tab, [0, 1, 2, 3])
        action = _translate_action(_menu(tab, 0))
        assert action is not None and "Bỏ qua 2 chương" in action.toolTip()

    def test_triggering_it_translates_the_whole_selection(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        _select_rows(tab, [1, 3])
        action = _translate_action(_menu(tab, 1))
        action.trigger()
        assert started == [[1, 3]]


class TestTranslateRows:
    def test_it_starts_one_job_for_every_selected_chapter(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows([4, 1, 2])
        assert started == [[1, 2, 4]]  # one job, chapter order, not click order

    def test_it_skips_chapters_without_source_text_and_says_so(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=2, translated=0)
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows([0, 1, 2, 3])
        assert started == [[0, 1]]
        assert "Bỏ qua 2 chương" in tab.status_label.text()

    def test_it_does_nothing_when_no_selected_chapter_has_text(
        self, qapp, tmp_path, monkeypatch
    ):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=0, translated=0)
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows([0, 1])
        assert started == []
        assert "chưa có nội dung gốc" in tab.status_label.text()

    def test_it_is_guarded_while_a_run_is_going(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._worker = _RunningWorker()
        tab._translate_rows([0, 1])
        assert started == []
        assert "phiên dịch" in tab.status_label.text()

    def test_it_is_guarded_when_no_project_is_selected(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        tab.project = None
        infos: list = []
        monkeypatch.setattr(tt.QMessageBox, "information", lambda *a, **k: infos.append(a))
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows([0])
        assert infos and started == []

    @pytest.mark.parametrize("count", [1, tt.TRANSLATE_CONFIRM_FROM - 1])
    def test_a_small_batch_runs_without_asking(self, qapp, tmp_path, monkeypatch, count):
        tab, _ = _tab(tmp_path, monkeypatch)
        asked: list = []
        monkeypatch.setattr(tt.QMessageBox, "question", lambda *a, **k: asked.append(a))
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows(list(range(count)))
        assert not asked and started == [list(range(count))]

    def test_a_large_batch_confirms_first(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        monkeypatch.setattr(
            tt.QMessageBox, "question", lambda *a, **k: tt.QMessageBox.StandardButton.Yes
        )
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows(list(range(tt.TRANSLATE_CONFIRM_FROM)))
        assert started == [list(range(tt.TRANSLATE_CONFIRM_FROM))]

    def test_declining_the_confirmation_starts_nothing(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        monkeypatch.setattr(
            tt.QMessageBox, "question", lambda *a, **k: tt.QMessageBox.StandardButton.No
        )
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._translate_rows(list(range(tt.TRANSLATE_CONFIRM_FROM)))
        assert started == []


class TestPerRowButton:
    def test_it_still_translates_exactly_the_clicked_chapter(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch)
        _select_rows(tab, [0, 1, 2])  # a selection must not widen the button's reach
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._retranslate_row(tab.table.model().index(3, 0))
        assert started == [[3]]

    def test_it_reports_a_chapter_with_no_source_text(self, qapp, tmp_path, monkeypatch):
        tab, _ = _tab(tmp_path, monkeypatch, downloaded=2, translated=0)
        started: list[list[int] | None] = []
        tab._start_translate = lambda indices=None: started.append(indices)
        tab._retranslate_row(tab.table.model().index(4, 0))
        assert started == []
        assert "chưa có nội dung gốc" in tab.status_label.text()

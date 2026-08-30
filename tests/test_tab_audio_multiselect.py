"""AudioTab: re-voice every selected chapter from one right-click.

The table has always been multi-select (enable_cell_copy puts it in ExtendedSelection),
but nothing acted on more than the row under the cursor. These tests pin the two halves
that make a selection actionable: the menu is built from the selection, and the batch and
the per-row 🔊 button run through the same guards.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QAbstractItemView, QMenu

from noveltrans.config import AppConfig
from noveltrans.gui.tab_audio import AudioTab
from noveltrans.models import Chapter


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.tts_use_translation = True
    config.tts_clean_text = False
    return config


def _chapters(count: int = 6, *, translated: bool = True) -> list[Chapter]:
    return [
        Chapter(
            index=i,
            title=f"Chương {i + 1}",
            url=f"https://x/{i}",
            content=f"Bản gốc {i}",
            translated=f"Bản dịch {i}" if translated else "",
        )
        for i in range(count)
    ]


class _FakeProject:
    """Just enough NovelProject for the regenerate guards — no DB, no filesystem."""

    def __init__(self, chapters):
        self._chapters = chapters
        self.path = "/nowhere"

    def counts(self):
        return {"translated": len(self._chapters), "downloaded": len(self._chapters), "audio": 0}


def _tab(qapp, tmp_path, *, count: int = 6, translated: bool = True) -> AudioTab:
    tab = AudioTab(_config(tmp_path))
    chapters = _chapters(count, translated=translated)
    tab.project = _FakeProject(chapters)
    tab.model.set_chapters(chapters)
    return tab


def _select_rows(tab: AudioTab, rows: list[int]) -> None:
    """Select model rows through the VIEW.

    `tab.table.model()` is a QSortFilterProxyModel since 074, and a selection model only
    understands indices from its own model — handing it `tab.table.model().index(...)` selects
    silently wrong rows. Unsorted, the proxy maps 1:1, so these are the same rows.
    """
    selection = tab.table.selectionModel()
    selection.clearSelection()
    view_model = tab.table.model()
    for row in rows:
        selection.select(
            view_model.index(row, 0),
            selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
        )


def test_table_allows_multi_row_selection(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)
    assert tab.table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    _select_rows(tab, [1, 3, 4])
    assert tab._selected_rows() == [1, 3, 4]  # deduped across the row's cells


def test_selected_rows_dedups_the_cells_of_one_row(qapp, tmp_path):
    # SelectRows hands back one index per *cell*; the helper must not count a row twice.
    tab = _tab(qapp, tmp_path)
    _select_rows(tab, [2])
    assert len(tab.table.selectionModel().selectedIndexes()) > 1
    assert tab._selected_rows() == [2]


def test_menu_action_names_the_selected_count(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)
    _select_rows(tab, [0, 2, 5])
    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(2, 0))
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels == ["🔊 Tạo lại 3 chương"]


def test_menu_action_is_singular_for_one_row(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)
    _select_rows(tab, [4])
    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(4, 0))
    assert [a.text() for a in menu.actions() if a.text()] == ["🔊 Tạo lại chương này"]


def test_menu_falls_back_to_the_clicked_row_when_nothing_selected(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)
    tab.table.selectionModel().clearSelection()
    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(3, 0))
    assert [a.text() for a in menu.actions() if a.text()] == ["🔊 Tạo lại chương này"]


def test_menu_offers_nothing_when_no_chapter_has_source_text(qapp, tmp_path):
    # Reading the translation of a novel that was never translated: no action at all,
    # rather than one that silently does nothing.
    tab = _tab(qapp, tmp_path, translated=False)
    _select_rows(tab, [0, 1])
    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(0, 0))
    assert [a.text() for a in menu.actions() if a.text()] == []


def test_regenerate_rows_starts_one_job_for_every_selected_chapter(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows([4, 1, 2])
    assert started == [[1, 2, 4]]  # one job, chapter order, not click order


def test_regenerate_rows_skips_chapters_without_source_text(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)
    tab.model.chapter_at(2).translated = ""
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows([1, 2, 3])
    assert started == [[1, 3]]
    assert "Bỏ qua 1 chương" in tab.status_label.text()


def test_regenerate_rows_reports_when_nothing_is_regenerable(qapp, tmp_path):
    tab = _tab(qapp, tmp_path, translated=False)
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows([0, 1])
    assert started == []
    assert "chưa có bản dịch để đọc" in tab.status_label.text()


def test_regenerate_rows_confirms_a_large_batch(qapp, tmp_path, monkeypatch):
    from noveltrans.gui import tab_audio

    tab = _tab(qapp, tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(
        tab_audio.QMessageBox,
        "question",
        lambda *args, **kwargs: asked.append(args[2])
        or tab_audio.QMessageBox.StandardButton.No,
    )
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows(list(range(tab_audio.REGENERATE_CONFIRM_FROM)))
    assert started == []  # declined, so no job
    assert f"{tab_audio.REGENERATE_CONFIRM_FROM} chương" in asked[0]


def test_regenerate_rows_does_not_confirm_a_small_batch(qapp, tmp_path, monkeypatch):
    from noveltrans.gui import tab_audio

    tab = _tab(qapp, tmp_path)
    monkeypatch.setattr(
        tab_audio.QMessageBox,
        "question",
        lambda *a, **k: pytest.fail("a small batch must not ask"),
    )
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows([0, 1])
    assert started == [[0, 1]]


def test_row_button_and_batch_share_one_path(qapp, tmp_path):
    # The per-row 🔊 button is now just a one-row batch — same guards, no confirmation.
    tab = _tab(qapp, tmp_path)
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_row(tab.table.model().index(3, 0))
    assert started == [[3]]


def test_regenerate_refuses_while_a_job_runs(qapp, tmp_path):
    tab = _tab(qapp, tmp_path)

    class _Running:
        def isRunning(self):
            return True

    tab._worker = _Running()
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows([0, 1])
    assert started == []
    assert "Đang có phiên tạo audio chạy" in tab.status_label.text()

    menu = QMenu()
    _select_rows(tab, [0, 1])
    tab._add_regenerate_actions(menu, tab.table.model().index(0, 0))
    action = next(a for a in menu.actions() if a.text())
    assert not action.isEnabled()  # offered, but greyed out with a reason


def test_regenerate_uses_the_original_source_when_that_radio_is_on(qapp, tmp_path):
    # "Bản gốc" reads chapter.content, so an untranslated novel is still regenerable.
    tab = _tab(qapp, tmp_path, translated=False)
    tab.original_radio.setChecked(True)
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    tab._regenerate_rows([0, 1])
    assert started == [[0, 1]]


def test_right_click_inside_a_selection_keeps_it(qapp, tmp_path):
    # enable_cell_copy used to setCurrentIndex() unconditionally, collapsing the
    # selection to the clicked row — which made "Tạo lại N chương" unreachable. Driven
    # through the extracted helper rather than customContextMenuRequested, because the
    # real path ends in a modal menu.exec() that would hang the run.
    from noveltrans.gui.widgets import focus_index_keeping_selection

    tab = _tab(qapp, tmp_path)
    _select_rows(tab, [1, 2, 3])
    focus_index_keeping_selection(tab.table, tab.table.model().index(2, 0))
    assert tab._selected_rows() == [1, 2, 3]  # selection survived the right-click
    assert tab.table.currentIndex().row() == 2  # but the clicked row became current

    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(2, 0))
    assert "🔊 Tạo lại 3 chương" in [a.text() for a in menu.actions()]


def test_right_click_outside_a_selection_reselects_that_row(qapp, tmp_path):
    # Unchanged from before: clicking away from the selection behaves like a left-click.
    from noveltrans.gui.widgets import focus_index_keeping_selection

    tab = _tab(qapp, tmp_path)
    _select_rows(tab, [1, 2])
    focus_index_keeping_selection(tab.table, tab.table.model().index(5, 0))
    assert tab._selected_rows() == [5]

    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(5, 0))
    assert "🔊 Tạo lại chương này" in [a.text() for a in menu.actions()]


def test_menu_acts_on_the_clicked_row_when_it_is_outside_the_selection(qapp, tmp_path):
    # Belt-and-braces for a stale selection model: the hook itself prefers the clicked
    # row when it is not part of the selection, without relying on the widgets fix.
    tab = _tab(qapp, tmp_path)
    _select_rows(tab, [0, 1])
    started: list[list[int] | None] = []
    tab._start_generate = lambda indices=None: started.append(indices)
    menu = QMenu()
    tab._add_regenerate_actions(menu, tab.table.model().index(4, 0))
    next(a for a in menu.actions() if a.text()).trigger()
    assert started == [[4]]


def test_row_button_ignores_a_right_click(qapp, tmp_path):
    # Right-clicking the 🔊 cell used to fire the row's job AND open the menu.
    from PySide6.QtCore import QEvent, QPointF, QRect
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QStyleOptionViewItem

    tab = _tab(qapp, tmp_path)
    delegate = tab._row_button_delegate
    # The signal carries a QModelIndex since 074 — a row number is a screen position once
    # a proxy sorts the view.
    fired: list[int] = []
    delegate.clicked.connect(lambda index: fired.append(index.row()))
    index = tab.table.model().index(1, tab.model.REGENERATE_COLUMN)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 100, 24)

    def _release(button):
        point = QPointF(10, 10)
        return QMouseEvent(  # local + global positions: the 5-arg form is deprecated
            QEvent.Type.MouseButtonRelease,
            point,
            point,
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )

    assert delegate.editorEvent(_release(Qt.MouseButton.RightButton), tab.model, option, index) is False
    assert fired == []  # the menu handles right-clicks; the button must stay out of it
    assert delegate.editorEvent(_release(Qt.MouseButton.LeftButton), tab.model, option, index) is True
    assert fired == [1]  # left-click still works exactly as before

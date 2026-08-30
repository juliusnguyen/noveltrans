"""The focus-widget dispatchers behind ⌘C/⌘X/⌘V/⌘A (offscreen Qt).

Two things here are regression guards rather than feature tests, and both would fail
silently in the app:

* `test_a_selectable_label_still_copies` — QLabel copies on ⌘C today only because nothing
  else claims the key. The Edit menu claims it, and QLabel has no copy() slot, so the
  naive menu quietly breaks the "Thông tin truyện" header.
* `test_a_read_only_editor_is_not_pasted_into` — a read-only QPlainTextEdit answers ⌘C but
  does NOT claim ⌘V, so the menu's "Dán" reaches it and would write into a preview pane.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
)

from noveltrans.gui import shortcuts


@pytest.fixture
def table(qapp):
    t = QTableWidget(3, 2)
    for row in range(3):
        for column in range(2):
            t.setItem(row, column, QTableWidgetItem(f"r{row}c{column}"))
    yield t
    t.deleteLater()


class TestSelectionAsText:
    def test_a_multi_cell_selection_is_tsv(self, table):
        table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 1), True)
        assert shortcuts.selection_as_text(table) == "r0c0\tr0c1\nr1c0\tr1c1"

    def test_it_prefers_a_long_tooltip_over_the_elided_display(self, table):
        item = table.item(0, 0)
        item.setToolTip("Lỗi kết nối: HTTPSConnectionPool(host='example.com', port=443)")
        table.setCurrentItem(item)
        assert "HTTPSConnectionPool" in shortcuts.selection_as_text(table)

    def test_with_nothing_selected_it_falls_back_to_the_current_cell(self, table):
        table.clearSelection()
        table.setCurrentCell(2, 1)
        assert shortcuts.selection_as_text(table) == "r2c1"

    def test_empty_cells_do_not_leave_trailing_tabs(self, table):
        # The delegate-drawn button columns have no display text.
        table.setItem(1, 1, QTableWidgetItem(""))
        table.clearSelection()
        table.setCurrentCell(1, 0)
        table.item(1, 0).setSelected(True)
        table.item(1, 1).setSelected(True)
        assert shortcuts.selection_as_text(table) == "r1c0"


class TestCopyFrom:
    def test_a_table_copies_its_selection(self, qapp, table):
        QApplication.clipboard().clear()
        table.clearSelection()
        table.setCurrentCell(0, 1)
        assert shortcuts.copy_from(table) is True
        assert QApplication.clipboard().text() == "r0c1"

    def test_a_selectable_label_still_copies(self, qapp):
        """The flag-2 regression guard. QLabel has no copy() slot."""
        label = QLabel("Tên truyện: 测试小说")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        assert not hasattr(label, "copy")  # the reason this branch has to exist
        QApplication.clipboard().clear()
        assert shortcuts.copy_from(label) is True
        assert QApplication.clipboard().text() == "Tên truyện: 测试小说"

    def test_a_line_edit_uses_its_own_copy(self, qapp):
        edit = QLineEdit("xin chào")
        edit.selectAll()
        QApplication.clipboard().clear()
        assert shortcuts.copy_from(edit) is True
        assert QApplication.clipboard().text() == "xin chào"

    def test_an_unrecognised_widget_is_declined(self, qapp):
        # False, so the filter returns False and Qt's own handling proceeds untouched.
        assert shortcuts.copy_from(QPushButton("Tải")) is False

    def test_no_widget_is_declined(self):
        assert shortcuts.copy_from(None) is False


class TestWriteGuards:
    def test_a_read_only_editor_is_not_pasted_into(self, qapp):
        editor = QPlainTextEdit("bản xem trước")
        editor.setReadOnly(True)
        QApplication.clipboard().setText("HỎNG")
        assert shortcuts.paste_into(editor) is True  # swallowed, not passed on
        assert editor.toPlainText() == "bản xem trước"

    def test_a_read_only_editor_is_not_cut_from(self, qapp):
        editor = QPlainTextEdit("bản xem trước")
        editor.setReadOnly(True)
        assert shortcuts.cut_from(editor) is True
        assert editor.toPlainText() == "bản xem trước"

    def test_an_editable_editor_does_paste(self, qapp):
        editor = QPlainTextEdit("")
        QApplication.clipboard().setText("chương 1")
        assert shortcuts.paste_into(editor) is True
        assert editor.toPlainText() == "chương 1"

    def test_a_table_swallows_paste_rather_than_letting_it_fall_through(self, table):
        assert shortcuts.paste_into(table) is True


class TestSelectAll:
    def test_a_table_selects_every_cell(self, table):
        assert shortcuts.select_all_in(table) is True
        assert len(table.selectionModel().selectedIndexes()) == 6

    def test_a_label_selects_its_whole_text(self, qapp):
        label = QLabel("một hai ba")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        assert shortcuts.select_all_in(label) is True
        assert label.selectedText() == "một hai ba"

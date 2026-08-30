"""EditShortcutFilter, driven with synthetic key events (offscreen Qt).

Shaped like TestDockActivate in test_main_window.py: the filter is a free-standing QObject
precisely so it can be handed an event without a real window, a real focus chain or a real
event loop. Focus is forced with setFocus() + QApplication.setActiveWindow(), because an
offscreen widget that was never activated has no focus widget at all.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QLineEdit, QTableWidget, QTableWidgetItem

from noveltrans.gui.shortcuts import EditShortcutFilter


def _key(sequence: QKeySequence.StandardKey) -> QKeyEvent:
    """The first binding Qt offers for a StandardKey, as a KeyPress.

    Reading the binding rather than hard-coding Ctrl+C is the same discipline the app
    itself follows: the macOS build resolves this to ⌘C and the Windows build to Ctrl+C,
    and the test passes on both without a platform branch.
    """
    binding = QKeySequence.keyBindings(sequence)[0]
    combination = binding[0]
    return QKeyEvent(
        QEvent.Type.KeyPress,
        combination.key().value,
        combination.keyboardModifiers(),
    )


@pytest.fixture
def focused_table(qapp):
    table = QTableWidget(2, 1)
    table.setItem(0, 0, QTableWidgetItem("chương một"))
    table.setItem(1, 0, QTableWidgetItem("chương hai"))
    table.show()
    QApplication.setActiveWindow(table)
    table.setFocus()
    yield table
    table.close()


class TestEditShortcutFilter:
    def test_copy_over_a_table_is_handled(self, focused_table):
        focused_table.setCurrentCell(1, 0)
        QApplication.clipboard().clear()
        handled = EditShortcutFilter().eventFilter(
            focused_table, _key(QKeySequence.StandardKey.Copy)
        )
        assert handled is True
        assert QApplication.clipboard().text() == "chương hai"

    def test_copy_over_a_line_edit_is_declined(self, qapp):
        # Declined on purpose. QLineEdit copies on ⌘C by itself; consuming the event here
        # would replace Qt's implementation with ours for no gain. The filter exists only
        # for the widgets Qt leaves unserved — see shortcuts._needs_help.
        edit = QLineEdit("xin chào")
        edit.show()
        QApplication.setActiveWindow(edit)
        edit.setFocus()
        handled = EditShortcutFilter().eventFilter(edit, _key(QKeySequence.StandardKey.Copy))
        assert handled is False
        edit.close()

    def test_select_all_over_a_table_is_handled(self, focused_table):
        handled = EditShortcutFilter().eventFilter(
            focused_table, _key(QKeySequence.StandardKey.SelectAll)
        )
        assert handled is True
        assert len(focused_table.selectionModel().selectedIndexes()) == 2

    def test_an_unrelated_key_falls_through(self, focused_table):
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F5, Qt.KeyboardModifier.NoModifier)
        assert EditShortcutFilter().eventFilter(focused_table, event) is False

    def test_a_non_key_event_falls_through(self, focused_table):
        event = QEvent(QEvent.Type.ApplicationActivate)
        assert EditShortcutFilter().eventFilter(focused_table, event) is False

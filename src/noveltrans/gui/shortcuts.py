"""One place that answers "what does ⌘C mean right now?".

Qt already gives every text widget the standard edit keys for free: a QLineEdit, an
editable *or* read-only QPlainTextEdit, and even a selectable QLabel all copy on ⌘C with
no menu and no QShortcut, because they claim the key via `QEvent::ShortcutOverride`
before the shortcut map ever sees it. What Qt does **not** give anyone is copy on an item
view — `QAbstractItemView` has `selectAll` and nothing else — so every table in this app
was a dead end for the keyboard until now.

Two entry points share the dispatchers below, and both are necessary:

* the **Edit menu** (`build_edit_menu`), which is what makes the keys discoverable and
  puts them in the native macOS menu bar; and
* an **application event filter** (`EditShortcutFilter`), because a QAction owned by
  MainWindow — even with `ShortcutContext.ApplicationShortcut` — does *not* fire while a
  modal QDialog is up. Measured. CleanupDialog, NameGlossaryDialog, FindReplaceDialog and
  the two OneDrive dialogs are all modal and all hold a list, so a menu alone would leave
  ⌘C broken in exactly the places a user most wants to copy an error out of.

They cannot double-fire: when the menu's shortcut matches, Qt consumes the key as a
ShortcutOverride/Shortcut event and it never becomes the KeyPress the filter watches for.
Even if it did, copy and select-all are idempotent.

Everything here matches on `QKeySequence.StandardKey`, never on a literal "Ctrl+C", so
the macOS build gets ⌘ and the Windows build gets Ctrl out of the same code.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLabel, QMenu

def selection_as_text(view: QAbstractItemView) -> str:
    """The view's selection as TSV — rows by newline, columns by tab.

    Falls back to the current cell when nothing is selected, which is exactly what
    `enable_cell_copy` did before this module existed: right-click a cell, ⌘C, paste.
    The upgrade is that selecting twenty failed chapters and pressing ⌘C now yields
    twenty rows instead of one cell.

    Prefers the tooltip over the display text when there is one, because the tables
    elide long error messages into the tooltip — copying what is painted would hand the
    user "Lỗi kết nối: HTTPSConnectionPool(host='…" and nothing useful.
    """
    model = view.model()
    if model is None:
        return ""
    selection = view.selectionModel()
    indexes = list(selection.selectedIndexes()) if selection is not None else []
    if not indexes:
        current = view.currentIndex()
        return _index_text(current) if current.isValid() else ""

    cells: dict[int, dict[int, str]] = {}
    for index in indexes:
        if view.isColumnHidden(index.column()):
            continue
        # An empty display is a delegate-drawn button column (RETRANSLATE_COLUMN,
        # REGENERATE_COLUMN). Emitting it blank would put a trailing tab on every line.
        text = _index_text(index)
        if not text:
            continue
        cells.setdefault(index.row(), {})[index.column()] = text
    return "\n".join(
        "\t".join(columns[c] for c in sorted(columns)) for _, columns in sorted(cells.items())
    )


def _index_text(index) -> str:
    if not index.isValid():
        return ""
    display = index.data(Qt.ItemDataRole.DisplayRole)
    tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
    text = tooltip if tooltip else display
    return str(text) if text else ""


def _is_writable(widget) -> bool:
    """Can this widget accept a paste or a cut?

    A read-only QPlainTextEdit answers ⌘C and ⌘A itself but does **not** claim ⌘V/⌘X, so
    without this guard the Edit menu's "Dán" would happily write into a preview pane the
    user cannot otherwise edit.
    """
    is_read_only = getattr(widget, "isReadOnly", None)
    return not (callable(is_read_only) and is_read_only())


def copy_from(widget) -> bool:
    """True if this widget's copy was handled here."""
    if widget is None:
        return False
    if isinstance(widget, QAbstractItemView):
        text = selection_as_text(widget)
        if text:
            QApplication.clipboard().setText(text)
        return True
    if isinstance(widget, QLabel):
        # QLabel has NO copy() slot — only selectedText()/setSelection(). It copies on ⌘C
        # today purely because nothing else claims the key; the moment an Edit menu owns
        # ⌘C the menu action wins and this branch is the only thing that keeps the
        # selectable "Thông tin truyện" labels (tab_scrape.py) working. Do not delete it
        # without also deleting the menu.
        text = widget.selectedText() or widget.text()
        if text:
            QApplication.clipboard().setText(text)
        return True
    return _call(widget, "copy")


def cut_from(widget) -> bool:
    if widget is None:
        return False
    if isinstance(widget, (QAbstractItemView, QLabel)):
        return True  # nothing to cut, and falling through would be surprising
    if not _is_writable(widget):
        return True
    return _call(widget, "cut")


def paste_into(widget) -> bool:
    if widget is None:
        return False
    if isinstance(widget, (QAbstractItemView, QLabel)):
        return True  # a table is not a paste target
    if not _is_writable(widget):
        return True
    return _call(widget, "paste")


def select_all_in(widget) -> bool:
    if widget is None:
        return False
    if isinstance(widget, QLabel):
        widget.setSelection(0, len(widget.text()))
        return True
    return _call(widget, "selectAll")


def undo_in(widget) -> bool:
    if widget is None or isinstance(widget, (QAbstractItemView, QLabel)):
        return widget is not None
    if not _is_writable(widget):
        return True
    return _call(widget, "undo")


def redo_in(widget) -> bool:
    if widget is None or isinstance(widget, (QAbstractItemView, QLabel)):
        return widget is not None
    if not _is_writable(widget):
        return True
    return _call(widget, "redo")


def _call(widget, name: str) -> bool:
    """Invoke `widget.name()` if it has one. False means "not ours" — the caller then
    lets Qt handle the key as it always did, so an unrecognised widget is never worse
    off than before this module."""
    method = getattr(widget, name, None)
    if not callable(method):
        return False
    method()
    return True


# StandardKey → dispatcher. Ordered as the Edit menu shows them.
_HANDLERS = (
    (QKeySequence.StandardKey.Copy, copy_from),
    (QKeySequence.StandardKey.Cut, cut_from),
    (QKeySequence.StandardKey.Paste, paste_into),
    (QKeySequence.StandardKey.SelectAll, select_all_in),
    (QKeySequence.StandardKey.Undo, undo_in),
    (QKeySequence.StandardKey.Redo, redo_in),
)


def _needs_help(widget) -> bool:
    """Is this a widget Qt leaves without the standard edit keys?

    Only two kinds, and the list is deliberately closed rather than "anything with a
    copy() slot". A QLineEdit or a QPlainTextEdit reaches this filter too — claiming the
    ShortcutOverride means the key is delivered as an ordinary KeyPress, which an
    application event filter still sees — and handling it here would *consume* the event
    so the widget's own keyPressEvent never ran. That swaps Qt's implementation for ours
    with no gain and one new place to be wrong.

    So the filter only serves what nobody else does: item views (QAbstractItemView has
    selectAll and no copy at all) and QLabel (which has neither, and loses ⌘C the moment
    the Edit menu claims the key). The menu's own handlers still cover every widget,
    because clicking "Sao chép" with the mouse has to work everywhere.
    """
    return isinstance(widget, (QAbstractItemView, QLabel))


class EditShortcutFilter(QObject):
    """⌘C/⌘X/⌘V/⌘A/⌘Z/⇧⌘Z for a focused list or label, anywhere in the app.

    A free-standing filter rather than a QApplication subclass, for the same reason
    `DockActivateFilter` is one: the tests' shared QApplication keeps working and the
    filter can be handed a synthetic event.

    Installed on the *application*, which is the whole point — this is the only form that
    reaches a focused table inside a modal dialog. Measured: an ApplicationShortcut
    QAction on MainWindow does not fire there; an app-level event filter does.

    Returns True only when a dispatcher actually handled the key. Everything else falls
    through untouched — see `_needs_help`.
    """

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        widget = QApplication.focusWidget()
        if not _needs_help(widget):
            return super().eventFilter(obj, event)
        for key, handler in _HANDLERS:
            if event.matches(key) and handler(widget):
                return True
        return super().eventFilter(obj, event)


def build_edit_menu(window) -> QMenu:
    """The "&Sửa" menu, in the house style of MainWindow._build_menu.

    Every action targets `QApplication.focusWidget()` at trigger time rather than a
    widget captured now, because the menu outlives every table it will ever copy from.

    Undo/Redo get NoRole explicitly: macOS relocates PreferencesRole and QuitRole items
    into the application menu, and there is no reason to let a heuristic guess at these.
    """
    menu = window.menuBar().addMenu("&Sửa")
    specs = (
        ("&Hoàn tác", QKeySequence.StandardKey.Undo, undo_in),
        ("&Làm lại", QKeySequence.StandardKey.Redo, redo_in),
        None,  # separator
        ("&Cắt", QKeySequence.StandardKey.Cut, cut_from),
        ("&Sao chép", QKeySequence.StandardKey.Copy, copy_from),
        ("&Dán", QKeySequence.StandardKey.Paste, paste_into),
        None,
        ("Chọn &tất cả", QKeySequence.StandardKey.SelectAll, select_all_in),
    )
    for spec in specs:
        if spec is None:
            menu.addSeparator()
            continue
        label, key, handler = spec
        action = QAction(label, window)
        action.setShortcut(key)
        action.setMenuRole(QAction.MenuRole.NoRole)
        action.triggered.connect(lambda _checked=False, fn=handler: fn(QApplication.focusWidget()))
        menu.addAction(action)
    return menu

"""Shared GUI widgets and models."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QTableView,
)

from PySide6.QtGui import QColor, QKeySequence, QShortcut

from noveltrans.models import (
    STATUS_DOWNLOADED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSLATED,
    Chapter,
)
from noveltrans.storage import Library

STATUS_LABELS = {
    STATUS_PENDING: "Chưa tải",
    STATUS_DOWNLOADED: "Đã tải",
    STATUS_TRANSLATED: "Đã dịch",
    STATUS_ERROR: "Lỗi",
}

def format_duration(seconds: float) -> str:
    """Compact duration for the chapter table: 42s / 3m05s / 1h02m ("" if unset)."""
    seconds = int(round(seconds))
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


STATUS_COLORS = {
    STATUS_PENDING: QColor("gray"),
    STATUS_DOWNLOADED: QColor("#2e7d32"),  # green
    STATUS_TRANSLATED: QColor("#1565c0"),  # blue
    STATUS_ERROR: QColor("#c62828"),  # red
}


class ProjectPicker(QComboBox):
    """Dropdown of NovelProjects in the library. Emits the selected path."""

    project_selected = Signal(str)  # project path ("" when none)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._library_dir: Path | None = None
        self.currentIndexChanged.connect(self._on_index_changed)

    def refresh(self, library_dir: Path, select_path: str = "") -> None:
        """Re-list projects; keep (or set) the selection when possible."""
        self._library_dir = Path(library_dir)
        current = select_path or (self.currentData() or "")
        library = Library(self._library_dir)
        self.blockSignals(True)
        self.clear()
        for path in library.list_projects():
            meta = library.project_meta(path)
            self.addItem(meta.title, str(path))
        index = self.findData(current)
        self.setCurrentIndex(index if index >= 0 else (0 if self.count() else -1))
        self.blockSignals(False)
        self._on_index_changed(self.currentIndex())

    def selected_path(self) -> str:
        return self.currentData() or ""

    def _on_index_changed(self, _index: int) -> None:
        self.project_selected.emit(self.selected_path())


class RowButtonDelegate(QStyledItemDelegate):
    """Paints a per-row push button without creating row widgets.

    The button shows only when the cell's UserRole data is truthy.
    """

    clicked = Signal(int)  # table row

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text

    def paint(self, painter, option, index) -> None:
        if not index.data(Qt.ItemDataRole.UserRole):
            return  # row not eligible for this action
        button = QStyleOptionButton()
        button.rect = option.rect.adjusted(4, 3, -4, -3)
        button.text = self.text
        button.state = QStyle.StateFlag.State_Enabled
        if option.state & QStyle.StateFlag.State_MouseOver:
            button.state |= QStyle.StateFlag.State_MouseOver
        QApplication.style().drawControl(
            QStyle.ControlElement.CE_PushButton, button, painter
        )

    def editorEvent(self, event, model, option, index) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and index.data(Qt.ItemDataRole.UserRole)
            and option.rect.contains(event.position().toPoint())
        ):
            self.clicked.emit(index.row())
            return True
        return False


class RetranslateButtonDelegate(RowButtonDelegate):
    def __init__(self, parent=None):
        super().__init__("↻ Dịch lại", parent)


class AudioChapterTableModel(QAbstractTableModel):
    """Read-only table over Chapter rows, audio-pipeline view."""

    COLUMNS = ("#", "Tên chương (dịch)", "Âm thanh", "Thời lượng", "Giọng", "Lỗi", "")
    TITLE_COLUMN = 1
    STATUS_COLUMN = 2
    DURATION_COLUMN = 3
    VOICE_COLUMN = 4
    ERROR_COLUMN = 5
    REGENERATE_COLUMN = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chapters: list[Chapter] = []

    def set_chapters(self, chapters: list[Chapter]) -> None:
        self.beginResetModel()
        self._chapters = list(chapters)
        self.endResetModel()

    def update_chapter(self, chapter: Chapter) -> None:
        for row, existing in enumerate(self._chapters):
            if existing.index == chapter.index:
                self._chapters[row] = chapter
                self.dataChanged.emit(
                    self.index(row, 0), self.index(row, self.columnCount() - 1)
                )
                return

    def chapter_at(self, row: int) -> Chapter | None:
        return self._chapters[row] if 0 <= row < len(self._chapters) else None

    def _audio_status(self, chapter: Chapter) -> tuple[str, QColor]:
        if not chapter.translated:
            return "Chưa dịch", STATUS_COLORS[STATUS_PENDING]
        if chapter.audio_error:
            return "Lỗi", STATUS_COLORS[STATUS_ERROR]
        if chapter.has_audio:
            return "Đã tạo", STATUS_COLORS[STATUS_TRANSLATED]
        return "Chưa tạo", STATUS_COLORS[STATUS_DOWNLOADED]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._chapters)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        chapter = self._chapters[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return chapter.index + 1
            if column == self.TITLE_COLUMN:
                return chapter.translated_title or chapter.title
            if column == self.STATUS_COLUMN:
                return self._audio_status(chapter)[0]
            if column == self.DURATION_COLUMN:
                return format_duration(chapter.audio_seconds)
            if column == self.VOICE_COLUMN:
                return chapter.audio_voice
            if column == self.ERROR_COLUMN:
                return chapter.audio_error
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == self.ERROR_COLUMN
            and chapter.audio_error
        ):
            return chapter.audio_error  # full text on hover (cell is truncated)
        if role == Qt.ItemDataRole.ForegroundRole and column == self.STATUS_COLUMN:
            return self._audio_status(chapter)[1]
        if role == Qt.ItemDataRole.TextAlignmentRole and column == self.DURATION_COLUMN:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if column == self.REGENERATE_COLUMN:
            if role == Qt.ItemDataRole.UserRole:
                return bool(chapter.translated)
            if role == Qt.ItemDataRole.ToolTipRole and chapter.translated:
                return "Tạo (lại) audio riêng chương này"
        return None


class ChapterTableModel(QAbstractTableModel):
    """Table over a list of Chapter rows; 'Tên dịch' is editable in place."""

    translated_title_edited = Signal(int, str)  # chapter.index, new title

    COLUMNS = ("#", "Tên chương", "Tên dịch", "Trạng thái", "Dịch bằng", "Thời gian", "Lỗi", "")
    TITLE_COLUMN = 1
    TRANSLATED_TITLE_COLUMN = 2
    STATUS_COLUMN = 3
    TRANSLATOR_COLUMN = 4
    DURATION_COLUMN = 5
    ERROR_COLUMN = 6
    RETRANSLATE_COLUMN = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chapters: list[Chapter] = []

    # ------------------------------------------------------------- population

    def set_chapters(self, chapters: list[Chapter]) -> None:
        self.beginResetModel()
        self._chapters = list(chapters)
        self.endResetModel()

    def update_chapter(self, chapter: Chapter) -> None:
        """Refresh one row in place (chapters are keyed by index order)."""
        for row, existing in enumerate(self._chapters):
            if existing.index == chapter.index:
                self._chapters[row] = chapter
                self.dataChanged.emit(
                    self.index(row, 0), self.index(row, self.columnCount() - 1)
                )
                return

    def chapter_at(self, row: int) -> Chapter | None:
        return self._chapters[row] if 0 <= row < len(self._chapters) else None

    # ---------------------------------------------------------------- Qt API

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._chapters)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        chapter = self._chapters[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return chapter.index + 1
            if column == self.TITLE_COLUMN:
                return chapter.title
            if column == self.TRANSLATED_TITLE_COLUMN:
                return chapter.translated_title
            if column == self.STATUS_COLUMN:
                return STATUS_LABELS.get(chapter.status, chapter.status)
            if column == self.TRANSLATOR_COLUMN:
                return chapter.translator
            if column == self.DURATION_COLUMN:
                return format_duration(chapter.translate_seconds)
            if column == self.ERROR_COLUMN:
                return chapter.error
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == self.ERROR_COLUMN
            and chapter.error
        ):
            return chapter.error  # full text on hover (cell is truncated)
        if role == Qt.ItemDataRole.EditRole and column == self.TRANSLATED_TITLE_COLUMN:
            return chapter.translated_title
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == self.TRANSLATED_TITLE_COLUMN
            and chapter.is_translated
        ):
            return "Nháy đúp để sửa tên dịch"
        if role == Qt.ItemDataRole.ForegroundRole and column == self.STATUS_COLUMN:
            return STATUS_COLORS.get(chapter.status)
        if role == Qt.ItemDataRole.TextAlignmentRole and column == self.DURATION_COLUMN:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if column == self.RETRANSLATE_COLUMN:
            if role == Qt.ItemDataRole.UserRole:
                return bool(chapter.content)
            if role == Qt.ItemDataRole.ToolTipRole and chapter.content:
                return "Dịch lại riêng chương này"
        return None

    def flags(self, index):
        flags = super().flags(index)
        if (
            index.isValid()
            and index.column() == self.TRANSLATED_TITLE_COLUMN
            and self._chapters[index.row()].is_translated
        ):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if (
            not index.isValid()
            or role != Qt.ItemDataRole.EditRole
            or index.column() != self.TRANSLATED_TITLE_COLUMN
        ):
            return False
        chapter = self._chapters[index.row()]
        title = str(value).strip()
        if not title or title == chapter.translated_title:
            return False
        chapter.translated_title = title
        self.dataChanged.emit(index, index)
        self.translated_title_edited.emit(chapter.index, title)
        return True


def _copy_index_text(index) -> None:
    """Put a cell's text on the clipboard (its tooltip if longer than the display)."""
    if not index.isValid():
        return
    display = index.data(Qt.ItemDataRole.DisplayRole)
    tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
    text = tooltip if tooltip else display
    if text:
        QApplication.clipboard().setText(str(text))


def enable_cell_copy(table: QTableView) -> None:
    """Let the user copy a table cell (e.g. a long error message) via Ctrl+C or a
    right-click "Sao chép" menu, so it's easy to paste elsewhere."""
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def copy_current() -> None:
        _copy_index_text(table.currentIndex())

    def show_menu(pos: QPoint) -> None:
        index = table.indexAt(pos)
        if not index.isValid():
            return
        table.setCurrentIndex(index)
        menu = QMenu(table)
        menu.addAction("Sao chép", lambda: _copy_index_text(index))
        menu.exec(table.viewport().mapToGlobal(pos))

    shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
    shortcut.activated.connect(copy_current)
    table.customContextMenuRequested.connect(show_menu)

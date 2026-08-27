"""Shared GUI widgets and models."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QPushButton,
    QTableView,
)

from PySide6.QtGui import QColor, QKeySequence, QShortcut

from noveltrans.gui.jobs import job_registry
from noveltrans.models import (
    SourceAudio,
    AUDIO_SOURCE_DOWNLOADED,
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


def _picker_label(meta) -> str:
    """One row of the novel picker: "Bản dịch — 原文 — site.com".

    `project_meta` already parses the whole meta.json, so the translated title is in hand
    here — no project open, no extra disk read. `NovelMeta.novel_label` owns the format,
    shared with the novel tab bar and the "Thông tin truyện" header so the three cannot
    drift apart; each part is dropped when absent.
    """
    return meta.novel_label()


class ProjectPicker(QComboBox):
    """Dropdown of NovelProjects in the library. Emits the selected path."""

    project_selected = Signal(str)  # project path ("" when none)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._library_dir: Path | None = None
        self.currentIndexChanged.connect(self._on_index_changed)

    def refresh(
        self, library_dir: Path, select_path: str = "", default_to_first: bool = True
    ) -> None:
        """Re-list projects; keep (or set) the selection when possible.

        `default_to_first=False` leaves the picker with no selection when nothing
        matches — used to populate a fresh workspace's list without auto-opening a
        novel it never asked for.
        """
        self._library_dir = Path(library_dir)
        current = select_path or (self.currentData() or "")
        library = Library(self._library_dir)
        self.blockSignals(True)
        self.clear()
        for path in library.list_projects():
            meta = library.project_meta(path)
            self.addItem(_picker_label(meta), str(path))
        index = self.findData(current)
        if index < 0 and default_to_first and self.count():
            index = 0
        self.setCurrentIndex(index)
        self.blockSignals(False)
        self._on_index_changed(self.currentIndex())

    def selected_path(self) -> str:
        return self.currentData() or ""

    def _on_index_changed(self, _index: int) -> None:
        self.project_selected.emit(self.selected_path())


class CheckableHeaderView(QHeaderView):
    """A horizontal header with a check indicator in one section — "toggle every row".

    QHeaderView has no checkable section, so the indicator is painted over the ordinary
    section and clicks landing inside it are intercepted. Only clicks on the indicator
    itself count: the section is wide and toggling every row at once is consequential,
    so a stray click on the label must not trigger it.

    The view only *displays* a state. What toggling means is the owner's decision — it
    handles `toggled` and calls `set_state` to reflect whatever actually happened.
    """

    toggled = Signal(bool)  # True = the user asked to check all, False = uncheck all

    def __init__(self, column: int, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._column = column
        self._state = Qt.CheckState.Unchecked
        self.setSectionsClickable(True)

    def set_state(self, state: Qt.CheckState) -> None:
        if state != self._state:
            self._state = state
            self.updateSection(self._column)

    def check_state(self) -> Qt.CheckState:
        return self._state

    def _indicator_rect(self, rect: QRect) -> QRect:
        size = self.style().pixelMetric(
            QStyle.PixelMetric.PM_IndicatorWidth, QStyleOptionButton(), self
        )
        return QRect(
            rect.left() + 4, rect.top() + (rect.height() - size) // 2, size, size
        )

    def paintSection(self, painter, rect, logicalIndex) -> None:
        painter.save()
        super().paintSection(painter, rect, logicalIndex)
        painter.restore()
        if logicalIndex != self._column:
            return
        option = QStyleOptionButton()
        option.rect = self._indicator_rect(rect)
        option.state = QStyle.StateFlag.State_Enabled | {
            Qt.CheckState.Checked: QStyle.StateFlag.State_On,
            Qt.CheckState.Unchecked: QStyle.StateFlag.State_Off,
            Qt.CheckState.PartiallyChecked: QStyle.StateFlag.State_NoChange,
        }[self._state]
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorCheckBox, option, painter, self
        )

    def mousePressEvent(self, event) -> None:
        index = self.logicalIndexAt(event.position().toPoint())
        if index == self._column:
            section = QRect(
                self.sectionViewportPosition(index), 0, self.sectionSize(index), self.height()
            )
            if self._indicator_rect(section).contains(event.position().toPoint()):
                # Partially checked reads as "not all" → the useful action is check-all.
                self.toggled.emit(self._state != Qt.CheckState.Checked)
                return
        super().mousePressEvent(event)


class CellEditorDelegate(QStyledItemDelegate):
    """Makes an in-cell editor actually fit the cell.

    The app stylesheet gives every `QLineEdit` `padding: 6px 9px` and a 1px border, which
    is right for a form field and ~14px too tall for a table row. Qt sizes an editor to
    the cell rectangle, so the styled editor's *content* area ended up shorter than the
    text and the chapter name was clipped mid-glyph — visible, unreadable, and easy to
    mistake for lost text.

    Two things fix it together: strip the padding so the editor matches the row's own
    metrics, and let the editor grow past a short row rather than clipping (Qt allows an
    editor to overflow its cell — this is what a compact table is supposed to do).
    """

    _EDITOR_QSS = "QLineEdit { padding: 0 4px; border-radius: 4px; margin: 0; }"

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is None:  # Qt returns None for a column it has no editor for
            return editor
        editor.setStyleSheet(self._EDITOR_QSS)
        return editor

    def updateEditorGeometry(self, editor, option, index) -> None:
        if editor is None:
            return
        rect = QRect(option.rect)
        wanted = editor.sizeHint().height()
        if wanted > rect.height():
            # Grow symmetrically so the text stays on the row's own baseline.
            grow = wanted - rect.height()
            rect.setTop(rect.top() - grow // 2)
            rect.setHeight(wanted)
        editor.setGeometry(rect)


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
        QApplication.style().drawControl(QStyle.ControlElement.CE_PushButton, button, painter)

    def editorEvent(self, event, model, option, index) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            # Left button only: without this a right-click on the button cell both fired
            # the row's action and opened the context menu — so right-clicking a
            # multi-row selection in the button column started a one-chapter job and
            # then offered a greyed-out "Tạo lại N chương".
            and event.button() == Qt.MouseButton.LeftButton
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

    COLUMNS = ("#", "Tên chương (dịch)", "Ký tự", "Âm thanh", "Thời lượng", "Giọng", "Lỗi", "")
    TITLE_COLUMN = 1
    CHARS_COLUMN = 2
    STATUS_COLUMN = 3
    DURATION_COLUMN = 4
    VOICE_COLUMN = 5
    ERROR_COLUMN = 6
    REGENERATE_COLUMN = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chapters: list[Chapter] = []
        self._use_translation = True  # which text the audio is voiced from

    def set_chapters(self, chapters: list[Chapter]) -> None:
        self.beginResetModel()
        self._chapters = list(chapters)
        self.endResetModel()

    def set_source(self, use_translation: bool) -> None:
        """Switch the source-text the status column reflects (Bản dịch / Bản gốc)."""
        if use_translation == self._use_translation:
            return
        self.beginResetModel()
        self._use_translation = use_translation
        self.endResetModel()

    def update_chapter(self, chapter: Chapter) -> None:
        for row, existing in enumerate(self._chapters):
            if existing.index == chapter.index:
                self._chapters[row] = chapter
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
                return

    def chapter_at(self, row: int) -> Chapter | None:
        return self._chapters[row] if 0 <= row < len(self._chapters) else None

    def _audio_status(self, chapter: Chapter) -> tuple[str, QColor]:
        if self._use_translation and not chapter.translated:
            return "Chưa dịch", STATUS_COLORS[STATUS_PENDING]
        if not self._use_translation and not chapter.content:
            return "Chưa tải", STATUS_COLORS[STATUS_PENDING]
        if chapter.audio_error:
            return "Lỗi", STATUS_COLORS[STATUS_ERROR]
        if chapter.has_audio and chapter.audio_source == AUDIO_SOURCE_DOWNLOADED:
            # Narration fetched from the source site, not synthesised here — worth its
            # own label so the user can see at a glance what a re-voice would destroy.
            return "Đã tải", STATUS_COLORS[STATUS_TRANSLATED]
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
                if self._use_translation:
                    return chapter.translated_title or chapter.title
                return chapter.title
            if column == self.CHARS_COLUMN:
                text = chapter.translated if self._use_translation else chapter.content
                return f"{len(text):,}" if text else ""
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
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (
            self.CHARS_COLUMN,
            self.DURATION_COLUMN,
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if column == self.REGENERATE_COLUMN:
            has_source = bool(chapter.translated if self._use_translation else chapter.content)
            # A downloaded row has nothing to re-synthesise: pressing 🔊 would replace
            # fetched narration with TTS. Both the delegate's painter and its click
            # handler gate on UserRole, so returning False removes the button entirely.
            downloadable = chapter.audio_source == AUDIO_SOURCE_DOWNLOADED
            if role == Qt.ItemDataRole.UserRole:
                return has_source and not downloadable
            if role == Qt.ItemDataRole.ToolTipRole and has_source and not downloadable:
                return "Tạo (lại) audio riêng chương này"
        return None


def audio_source_label(voice: str, pretty: dict[str, str] | None = None) -> str:
    """A readable label for an `audio_voice` value.

    That column holds two unrelated kinds of thing: a TTS engine voice id, and — for
    narration fetched from a source site — the ADAPTER's name. A combo showing a bare
    "tieuthuyetmang" among engine voices gives the user no way to tell which is which.

    The set of adapter names is read from the registry rather than hardcoded, so a second
    site that grows audio support is labelled correctly without touching this.
    """
    from noveltrans.scrapers import ADAPTERS

    if any(cls.name == voice and hasattr(cls, "fetch_audio_url") for cls in ADAPTERS):
        return f"{voice} (tải từ trang)"
    return (pretty or {}).get(voice, voice)


class AudioSourceTableModel(QAbstractTableModel):
    """Read-only table over the audio a source site publishes.

    Rows are `SourceAudio` releases, not chapters, and there is deliberately no "Chương"
    column: a release covers a range of chapters and belongs to a different edition of the
    work, so pinning it to one row is exactly the confusion this table exists to undo.
    """

    COLUMNS = ("#", "Tên mục audio", "Âm thanh", "Thời lượng", "Lỗi", "")
    TITLE_COLUMN = 1
    STATUS_COLUMN = 2
    DURATION_COLUMN = 3
    ERROR_COLUMN = 4
    REDOWNLOAD_COLUMN = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[SourceAudio] = []

    def set_items(self, items: list[SourceAudio]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item_at(self, row: int) -> SourceAudio | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def items(self) -> list[SourceAudio]:
        return list(self._items)

    def update_item(self, release: SourceAudio) -> None:
        """Replace the row for `release.number`, so a finished download shows at once."""
        for row, existing in enumerate(self._items):
            if existing.number == release.number:
                self._items[row] = release
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
                return

    def _status(self, item: SourceAudio) -> tuple[str, QColor]:
        if item.error:
            return "Lỗi", STATUS_COLORS[STATUS_ERROR]
        if item.has_audio:
            return "Đã tải", STATUS_COLORS[STATUS_TRANSLATED]
        return "Chưa tải", STATUS_COLORS[STATUS_DOWNLOADED]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self._items[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return item.ord or index.row() + 1
            if column == self.TITLE_COLUMN:
                return item.title
            if column == self.STATUS_COLUMN:
                return self._status(item)[0]
            if column == self.DURATION_COLUMN:
                return format_duration(item.seconds)
            if column == self.ERROR_COLUMN:
                return item.error
        if role == Qt.ItemDataRole.ToolTipRole and column == self.ERROR_COLUMN:
            return item.error
        if role == Qt.ItemDataRole.ForegroundRole and column == self.STATUS_COLUMN:
            return self._status(item)[1]
        if role == Qt.ItemDataRole.TextAlignmentRole and column == self.DURATION_COLUMN:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if column == self.REDOWNLOAD_COLUMN:
            # On every row, including ones already downloaded — re-fetching one release is
            # what this button is for, and it is the only affordance that overrides the
            # skip-what-we-have rule.
            if role == Qt.ItemDataRole.UserRole:
                return True
            if role == Qt.ItemDataRole.ToolTipRole:
                return "Tải lại mục audio này từ trang nguồn"
        return None


class ChapterTableModel(QAbstractTableModel):
    """Table over a list of Chapter rows; 'Tên dịch' is editable in place.

    'Tên chương' is editable too, but only where a tab has opted in with
    `set_title_editable(True)` and connected `title_edited`. Off by default on purpose:
    an editable cell whose edits nobody saves looks like it worked and loses the text on
    the next refresh.
    """

    translated_title_edited = Signal(int, str)  # chapter.index, new title
    title_edited = Signal(int, str)  # chapter.index, new chapter title

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
        self._title_editable = False

    def set_title_editable(self, editable: bool) -> None:
        """Opt this table in to renaming chapters. Connect `title_edited` as well."""
        self._title_editable = editable

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
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
                return

    def chapter_at(self, row: int) -> Chapter | None:
        return self._chapters[row] if 0 <= row < len(self._chapters) else None

    def row_for_index(self, chapter_index: int) -> int | None:
        """The table row showing chapter `chapter_index`, or None.

        Not the identity function: a deleted chapter leaves a gap in the index sequence,
        so row N and chapter N part ways as soon as one is removed.
        """
        for row, chapter in enumerate(self._chapters):
            if chapter.index == chapter_index:
                return row
        return None

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
                # A suffix rather than a ninth column: a new column would shift
                # RETRANSLATE_COLUMN and every hard-coded index that follows it, for a
                # fact two characters convey at a glance.
                if not chapter.is_rewritten:
                    return chapter.translator
                return f"{chapter.translator} ✍️".strip()
            if column == self.DURATION_COLUMN:
                return format_duration(chapter.translate_seconds)
            if column == self.ERROR_COLUMN:
                return chapter.error
        if role == Qt.ItemDataRole.ToolTipRole and column == self.ERROR_COLUMN and chapter.error:
            return chapter.error  # full text on hover (cell is truncated)
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == self.TRANSLATOR_COLUMN
            and chapter.is_rewritten
        ):
            return "Đã viết lại văn phong — chuột phải để hoàn tác"
        if role == Qt.ItemDataRole.EditRole and column == self.TRANSLATED_TITLE_COLUMN:
            return chapter.translated_title
        if role == Qt.ItemDataRole.EditRole and column == self.TITLE_COLUMN:
            return chapter.title
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == self.TRANSLATED_TITLE_COLUMN
            and chapter.is_translated
        ):
            return "Nháy đúp để sửa tên dịch"
        if role == Qt.ItemDataRole.ToolTipRole and column == self.TITLE_COLUMN:
            if chapter.title_custom:
                original = (
                    f"\nTên gốc: {chapter.title_source}" if chapter.title_source else ""
                )
                return (
                    f"{chapter.title}\n(tên bạn đặt — quét lại sẽ không ghi đè){original}"
                )
            if self._title_editable:
                return f"{chapter.title}\nNháy đúp để sửa tên chương"
            return chapter.title
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
        if not index.isValid():
            return flags
        if (
            index.column() == self.TRANSLATED_TITLE_COLUMN
            and self._chapters[index.row()].is_translated
        ):
            flags |= Qt.ItemFlag.ItemIsEditable
        if index.column() == self.TITLE_COLUMN and self._title_editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        chapter = self._chapters[index.row()]
        title = str(value).strip()
        if index.column() == self.TITLE_COLUMN:
            # A blank name is a mis-edit, not an instruction: the title is what the
            # export, the video and the TTS narration all use.
            if not self._title_editable or not title or title == chapter.title:
                return False
            chapter.title = title
            chapter.title_custom = True
            self.dataChanged.emit(index, index)
            self.title_edited.emit(chapter.index, title)
            return True
        if index.column() != self.TRANSLATED_TITLE_COLUMN:
            return False
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


def focus_index_keeping_selection(table: QTableView, index) -> None:
    """Make `index` the current cell without throwing away a multi-row selection.

    QAbstractItemView.setCurrentIndex issues ClearAndSelect, so calling it from a
    context-menu handler collapses the selection to the row under the cursor. Qt's own
    mousePressEvent already leaves an already-selected row alone — but
    customContextMenuRequested fires from contextMenuEvent, *after* the press, so the
    naive call undoes what Qt preserved. That broke "Tạo lại N chương", which reads the
    selection out of the very menu the right-click opens.

    Right-clicking outside the selection still collapses to that row, like every desktop
    table. The NoUpdate branch still moves the *current* index, so Ctrl+C and anything
    reading currentIndex() (e.g. the audio tab's "Xem trước văn bản") follow the cell you
    actually clicked instead of wherever a drag happened to end.
    """
    selection = table.selectionModel()
    if selection is not None and selection.isSelected(index):
        selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    else:
        table.setCurrentIndex(index)


def enable_cell_copy(table: QTableView, extra_actions=None) -> None:
    """Let the user copy a table cell (e.g. a long error message) via Ctrl+C or a
    right-click "Sao chép" menu, so it's easy to paste elsewhere.

    `extra_actions`, if given, is called as `extra_actions(menu, index)` while the
    right-click menu is being built, so a caller can append its own actions (e.g.
    "download from this chapter") to the same menu rather than fighting over the
    table's single context-menu signal."""
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def copy_current() -> None:
        _copy_index_text(table.currentIndex())

    def show_menu(pos: QPoint) -> None:
        index = table.indexAt(pos)
        if not index.isValid():
            return
        focus_index_keeping_selection(table, index)
        menu = QMenu(table)
        menu.addAction("Sao chép", lambda: _copy_index_text(index))
        if extra_actions is not None:
            extra_actions(menu, index)
        menu.exec(table.viewport().mapToGlobal(pos))

    shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
    shortcut.activated.connect(copy_current)
    table.customContextMenuRequested.connect(show_menu)


class PauseButton(QPushButton):
    """⏸ Tạm dừng / ▶ Tiếp tục for one job, wherever it is shown.

    The same class backs the button in a tab's button row and the one in the menu-bar
    popup, and neither holds any state: both read `Job.paused` and both write through
    `job_registry.toggle`. That is what stops the two copies from ever disagreeing.

    Bound to a job id rather than a Job so a tab can keep one button across many runs —
    `set_job(...)` rebinds it, `set_job(None)` parks it.
    """

    def __init__(self, job_id: int | None = None, registry=None, parent=None):
        super().__init__(parent)
        # Injectable so a popup built against a throwaway registry (tests) doesn't drive
        # the app-wide one. Production always passes none of it and gets the singleton.
        self.registry = registry if registry is not None else job_registry
        self._job_id: int | None = None
        # Appended to the tooltip on every state change rather than set once by the
        # caller: `_apply` rewrites the tooltip each time the job flips, so a tooltip
        # assigned from outside would vanish on the first press — which is exactly when
        # a warning like "this leaves Chrome open" needs to still be there.
        self._extra_hint = ""
        self.clicked.connect(self._toggle)
        self.registry.job_changed.connect(self._on_job_changed)
        self.registry.job_removed.connect(self._on_job_removed)
        self.set_job(job_id)

    def set_job(self, job_id: int | None) -> None:
        self._job_id = job_id
        job = self.registry.job(job_id) if job_id is not None else None
        self.setEnabled(job is not None and job.pausable)
        self._apply(job)

    def set_extra_hint(self, hint: str) -> None:
        """A job-kind-specific warning kept on the tooltip through every state change."""
        self._extra_hint = hint or ""
        self._apply(self.registry.job(self._job_id) if self._job_id is not None else None)

    def _apply(self, job) -> None:
        paused = bool(job is not None and job.paused)
        self.setText("▶ Tiếp tục" if paused else "⏸ Tạm dừng")
        tip = (
            "Chạy tiếp từ chỗ đang dừng."
            if paused
            else "Dừng lại sau khi xong mục đang chạy (không bỏ dở chương nào)."
        )
        if self._extra_hint:
            tip = f"{tip}\n\n{self._extra_hint}"
        self.setToolTip(tip)

    def _toggle(self) -> None:
        if self._job_id is not None:
            self.registry.toggle(self._job_id)

    def _on_job_changed(self, job) -> None:
        if job.id == self._job_id:
            self._apply(job)

    def _on_job_removed(self, job_id: int) -> None:
        if job_id == self._job_id:
            self.set_job(None)

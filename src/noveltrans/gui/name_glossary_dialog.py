"""Tên nhân vật — review and correct the per-novel character-name list.

The list is what makes a character's name identical in every chapter: recurring Chinese
names are replaced with their Hán-Việt reading before the text reaches the engine, so
nothing depends on the model remembering what it did last chapter. `name_glossary.py`
owns the file and the merge rule; this file is only the Qt wiring.

The reason it is editable rather than automatic: the Hán-Việt table has one reading per
character, but a name can have a conventional spelling that differs from it, and some
characters have no reading at all so the detector drops the whole name. Both are things
only the reader of that novel can settle, and neither is visible until it is shown in a
list. Correcting a reading here pins it — a later re-detect will not undo it.

The dialog does not own a batch worker: it emits what the user asked for and the tab
starts it, keeping `has_running_workers` / `shutdown` / the job registry in one place. It
does own the name *scan*, which writes nothing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from noveltrans.find_replace import FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE, scan
from noveltrans.name_glossary import (
    ORIGIN_MANUAL,
    NameEntry,
    merge_detected,
    read_names,
    write_names,
)
from noveltrans.gui.widgets import SortableItem, enable_table_sorting
from noveltrans.gui.workers import NameScanWorker
from noveltrans.storage import NovelProject
from noveltrans.translators.names import to_hanviet

_COL_SOURCE = 0  # also carries the "use this name" checkbox
_COL_READING = 1
_COL_AUTO = 2
_COL_COUNT = 3

# Below this, a reading is too short to replace safely across a whole novel — a
# one-syllable name collides with ordinary Vietnamese words.
_MIN_REPAIR_SYLLABLES = 2


class NameGlossaryDialog(QDialog):
    """The per-novel name list: review, correct, add, and optionally repair what is done."""

    applied = Signal(set)  # {chapter index, …} rewritten in place
    retranslate_requested = Signal(list)  # [chapter index, …] to translate again

    def __init__(self, project: NovelProject, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tên nhân vật")
        self.resize(760, 540)
        self.project = project
        self._entries: list[NameEntry] = read_names(project.path)
        self._original = {e.source: e.reading for e in self._entries}
        self._scan_worker: NameScanWorker | None = None
        self._filling = False  # guards the itemChanged handler against its own writes

        self.header = QLabel("")
        self.header.setWordWrap(True)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Tên gốc (✓ = dùng)", "Cách viết", "Máy đề xuất", "Số lần"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            _COL_READING, QHeaderView.ResizeMode.Stretch
        )
        self.table.itemChanged.connect(self._on_item_changed)
        # Most-seen first, which is the order `_reload_table` already builds — so turning
        # sorting on changes nothing until the user clicks a header.
        enable_table_sorting(self.table, default_column=_COL_COUNT, ascending=False)

        self.scan_button = QPushButton("🔍 Dò lại từ bản gốc")
        self.scan_button.setToolTip(
            "Quét lại toàn bộ truyện để tìm tên mới. Cách viết bạn đã sửa sẽ được giữ nguyên."
        )
        self.scan_button.clicked.connect(self._rescan)
        self.add_button = QPushButton("＋ Thêm tên")
        self.add_button.setToolTip("Thêm một tên mà máy không tự tìm ra.")
        self.add_button.clicked.connect(self._add_row)
        self.remove_button = QPushButton("Xoá dòng")
        self.remove_button.clicked.connect(self._remove_row)
        self.save_button = QPushButton("Lưu")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save)
        self.close_button = QPushButton("Đóng")
        self.close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.scan_button)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(buttons)

        self._reload_table()
        if not self._entries:
            self._rescan()  # nothing stored yet — offer something to look at immediately

    # ------------------------------------------------------------------ table

    def _reload_table(self) -> None:
        # Sorting off while filling: every setItem would otherwise re-sort and move the
        # row being built. `_rows()` reads the table by walking it, and each row carries
        # its entry in UserRole, so the order it comes back in does not matter.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for entry in sorted(self._entries, key=lambda e: (-e.count, e.source)):
            self._insert_row(entry)
        self.table.setSortingEnabled(True)
        self._refresh_header()

    def _insert_row(self, entry: NameEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        source = QTableWidgetItem(entry.source)
        source.setFlags(source.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        source.setCheckState(
            Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked
        )
        source.setData(Qt.ItemDataRole.UserRole, entry)
        self.table.setItem(row, _COL_SOURCE, source)

        reading = QTableWidgetItem(entry.reading)
        if entry.edited:
            reading.setToolTip("Bạn đã sửa cách viết này — máy sẽ không ghi đè.")
        self.table.setItem(row, _COL_READING, reading)

        auto = QTableWidgetItem(entry.auto or "— không đọc được")
        auto.setFlags(auto.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, _COL_AUTO, auto)

        # Keyed on the number: "—" and "12" and "9" do not sort as text.
        count = SortableItem(str(entry.count) if entry.count else "—", entry.count or 0)
        count.setFlags(count.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, _COL_COUNT, count)

    def _refresh_header(self) -> None:
        if not self._entries:
            self.header.setText(
                "⚠️ Chưa có danh sách tên. Bấm “Dò lại từ bản gốc” để máy tìm giúp."
            )
            return
        used = sum(1 for e in self._entries if e.enabled and e.reading)
        self.header.setText(
            f"{len(self._entries)} tên, {used} tên đang được dùng khi dịch. "
            "Sửa cột “Cách viết” để ghim cách viết bạn muốn — lần dò sau sẽ không ghi đè."
        )

    def _rows(self) -> list[NameEntry]:
        """The table's current state, as entries. `edited` is derived, never trusted."""
        out: list[NameEntry] = []
        for row in range(self.table.rowCount()):
            source_item = self.table.item(row, _COL_SOURCE)
            reading_item = self.table.item(row, _COL_READING)
            if source_item is None:
                continue
            stored: NameEntry = source_item.data(Qt.ItemDataRole.UserRole)
            source = source_item.text().strip()
            reading = (reading_item.text() if reading_item else "").strip()
            if not source:
                continue
            out.append(
                NameEntry(
                    source=source,
                    reading=reading,
                    auto=stored.auto if stored else "",
                    # Editing a reading back to what the machine suggested is no longer an
                    # override, so the flag comes off again and the entry resumes tracking.
                    edited=bool(reading) and reading != (stored.auto if stored else ""),
                    enabled=source_item.checkState() == Qt.CheckState.Checked,
                    count=stored.count if stored else 0,
                    origin=stored.origin if stored else ORIGIN_MANUAL,
                )
            )
        return out

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Fill in a hand-typed name: suggest a reading, and say how often it occurs.

        The occurrence count is the important half. A name typed by hand is exactly where
        a typo, or the traditional form pasted into a simplified novel, goes unnoticed —
        and either one saves an entry that silently never matches anything. Showing "0 lần
        trong truyện" the moment it is typed catches both before the list is saved.
        """
        if item.column() != _COL_SOURCE or self._filling:
            return
        source = item.text().strip()
        if not source:
            return
        self._filling = True
        try:
            reading_item = self.table.item(item.row(), _COL_READING)
            if reading_item is not None and not reading_item.text().strip():
                reading_item.setText(to_hanviet(source) or "")
            occurrences = sum(
                (c.content or "").count(source) for c in self.project.chapters()
            )
            count_item = self.table.item(item.row(), _COL_COUNT)
            if count_item is not None:
                count_item.setText(str(occurrences) if occurrences else "0 ⚠️")
                count_item.setToolTip(
                    "Không tìm thấy tên này trong bản gốc — kiểm tra lại chữ, hoặc có thể "
                    "bạn đang dán bản phồn thể vào truyện giản thể (hay ngược lại)."
                    if not occurrences
                    else f"Xuất hiện {occurrences} lần trong bản gốc."
                )
        finally:
            self._filling = False

    # ------------------------------------------------------------------ actions

    def _add_row(self) -> None:
        self._insert_row(NameEntry(source="", reading="", origin=ORIGIN_MANUAL))
        self.table.scrollToBottom()
        self.table.editItem(self.table.item(self.table.rowCount() - 1, _COL_SOURCE))

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _rescan(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.scan_button.setEnabled(False)
        self.header.setText("Đang dò tên nhân vật trong toàn bộ truyện…")
        self._scan_worker = NameScanWorker(self.project.path, self)
        self._scan_worker.scanned.connect(self._on_scanned)
        self._scan_worker.start()

    def _on_scanned(self, detected: list) -> None:
        self.scan_button.setEnabled(True)
        # Merge into what the table currently shows, not into what was last saved, so an
        # edit made just before pressing "Dò lại" is not thrown away.
        self._entries = merge_detected(
            self._rows(), [(e.source, e.auto, e.count) for e in detected]
        )
        self._reload_table()

    def _save(self) -> None:
        entries = self._rows()
        bad = [e.source for e in entries if e.enabled and not e.reading]
        if bad:
            QMessageBox.warning(
                self,
                "Thiếu cách viết",
                "Những tên này đang bật nhưng chưa có cách viết:\n\n"
                + ", ".join(bad[:10])
                + "\n\nHãy điền cách viết, hoặc bỏ tick để không dùng.",
            )
            return

        changed = [
            (e.source, self._original[e.source], e.reading)
            for e in entries
            if e.source in self._original and self._original[e.source] != e.reading
        ]
        write_names(self.project.path, entries, chapters_scanned=self.project.counts()["total"])
        self._entries = entries
        self._original = {e.source: e.reading for e in entries}
        self._offer_repair(changed)
        self.accept()

    # ------------------------------------------------------------------ repair

    def _offer_repair(self, changed: list[tuple[str, str, str]]) -> None:
        """Offer to fix chapters already translated with the old spelling.

        Only for a rename of two syllables or more: a one-syllable reading is short enough
        to appear inside ordinary Vietnamese words, and a blind replace across a whole
        novel would hit them all. Those fall through to Tìm & thay thế, where the user sees
        every match first.
        """
        for _source, old, new in changed:
            if not old or not new or len(old.split()) < _MIN_REPAIR_SYLLABLES:
                continue
            matches = scan(
                self.project.chapters(),
                old,
                new,
                [FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE],
                case_sensitive=True,
            )
            if not matches:
                continue

            hits = sum(m.count for m in matches)
            indices = [m.index for m in matches]
            answer = QMessageBox.question(
                self,
                "Sửa bản dịch cũ?",
                f"Đã đổi “{old}” → “{new}”.\n\n"
                f"Tìm thấy {hits} chỗ trong {len(indices)} chương đã dịch.\n\n"
                "• Chọn “Yes” để sửa thẳng trong bản dịch (nhanh, không tốn AI).\n"
                "• Chọn “No” để dịch lại những chương đó — bản dịch cũ sẽ bị xoá, "
                "kể cả sửa tay và bản viết lại.\n"
                "• Chọn “Cancel” để giữ nguyên.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                updates = {
                    m.index: {c.field: c.new for c in m.changes} for m in matches
                }
                self.project.apply_replacements(updates)
                self.applied.emit(set(indices))
            elif answer == QMessageBox.StandardButton.No:
                self.retranslate_requested.emit(indices)

    # ------------------------------------------------------------------ teardown

    def shutdown(self) -> None:
        """Stop the scan thread. Safe to call twice; never raises."""
        worker = self._scan_worker
        if worker is not None and worker.isRunning():
            worker.wait(5000)
        self._scan_worker = None

    def reject(self) -> None:
        self.shutdown()
        super().reject()

    def accept(self) -> None:
        self.shutdown()
        super().accept()

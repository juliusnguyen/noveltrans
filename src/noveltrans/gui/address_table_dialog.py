"""Xưng hô: review and edit the per-novel address table (feature 113).

One row says how one character addresses another ("Lâm Phong nói với Tô Tuyết: xưng tớ,
gọi cậu"). The rows that apply to a chapter go into every request an LLM engine receives
for it, so the same pair keeps the same pronouns in every chapter. `address_table.py` owns
the file, the matching and the prompt; this file is only the Qt wiring.

Rows are directed, so "⇄ Thêm chiều ngược lại" pre-fills the other side with the two terms
swapped, for the user to check. A name is matched against the source AFTER the Tên nhân vật
substitution, so it has to be spelled exactly like a reading in that list; a name that is
not there is marked ⚠️ so the user can see the row will probably never fire.

Phase 2 adds the two things that make the table useful for a novel already translated:

* **🔎 Dò từ bản dịch** drafts rows from a sample of chapters (`AddressScanWorker`) and lists
  every pair used more than one way under "Mâu thuẫn" — the inconsistency report, with the
  chapters attached. A scan only fills gaps: it never changes a row already in the table.
* **After saving**, the chapters a changed row applies to (plus the scan's conflicting
  chapters) can be fixed in place by AI, retranslated, or left alone. The dialog only
  emits the request; the tab runs the batch, like `NameGlossaryDialog`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from noveltrans.address_table import (
    NARRATOR,
    ORIGIN_MANUAL,
    AddressEntry,
    AddressTable,
    merge_extracted,
    read_address,
    reverse_of,
    rows_for_chapter,
    write_address,
)
from noveltrans.name_glossary import read_names
from noveltrans.storage import NovelProject

_COL_SPEAKER = 0  # also carries the "use this row" checkbox
_COL_LISTENER = 1
_COL_SELF = 2
_COL_OTHER = 3
_COL_FROM = 4
_COL_NOTE = 5

_HEADERS = ["Người nói (✓ = dùng)", "Người nghe", "Xưng", "Gọi", "Từ chương", "Ghi chú"]

# Scan threads still finishing an LLM call when their dialog closed. A scan only stops
# between chapters, and one CLI call can take minutes; a QThread destroyed while running
# aborts the app, so the thread is detached from the dialog and kept alive here instead.
_DETACHED: set = set()

# Engines with no prompt: the table cannot reach them.
_NO_PROMPT_ENGINES = frozenset({"google"})


class _NameDelegate(QStyledItemDelegate):
    """A line edit that completes character names from the Tên nhân vật list."""

    def __init__(self, names: list[str], parent=None):
        super().__init__(parent)
        self._names = names

    def createEditor(self, parent, option, index):  # noqa: N802 — Qt override
        editor = QLineEdit(parent)
        completer = QCompleter(self._names, editor)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        editor.setCompleter(completer)
        return editor


def _row_key(entry: AddressEntry) -> tuple:
    """What makes two rows say the same thing — `origin` is bookkeeping, not meaning."""
    return (entry.speaker, entry.listener, entry.self_term, entry.other_term,
            entry.from_chapter, entry.note)


class AddressTableDialog(QDialog):
    """The per-novel address table: review, draft, add, reverse, remove, save, repair."""

    fix_requested = Signal(list)  # [chapter index, …] to fix in place by AI
    retranslate_requested = Signal(list)  # [chapter index, …] to translate again

    def __init__(self, project: NovelProject, parent=None, *, engine: str = "", config=None):
        super().__init__(parent)
        self._engine = engine  # the translate tab's engine — what "Dịch lại" would use
        self.setWindowTitle("Xưng hô")
        self.resize(920, 720)
        self.project = project
        self._table_data: AddressTable = read_address(project.path)
        self._known_names = sorted(
            {e.reading for e in read_names(project.path) if e.enabled and e.reading}
        )
        self._filling = False
        self.config = config
        self._original_keys = {_row_key(e) for e in self._table_data.entries if e.usable}
        self._conflict_chapters: set[int] = set()  # chapter numbers, from the last scan
        self._scan_worker = None

        intro = QLabel(
            "Mỗi dòng là cách <b>một nhân vật nói với một nhân vật khác</b>. Khi dịch bằng AI, "
            "hệ thống có thể tự động dò các cặp mới từ bản gốc để cập nhật vào bảng (không ghi "
            "đè dòng bạn đã lưu), rồi gửi kèm các dòng có nhân vật xuất hiện trong chương nên "
            "cùng một cặp xưng hô giống nhau ở mọi chương. A nói với B và B nói với A là hai dòng "
            "riêng. Quan hệ đổi giữa truyện (người lạ → bạn bè) thì thêm dòng mới với "
            "“Từ chương” lớn hơn. Người nói là “(người kể)” = cách lời kể gọi nhân vật đó "
            "(hắn, anh ta, nàng…), chỉ cần cột “Gọi”. Tên phải viết đúng như trong "
            "👤 Tên nhân vật."
        )
        intro.setWordWrap(True)
        self.engine_warning = QLabel(
            "⚠️ Engine đang chọn là Google Translate — Google không đọc được bảng xưng hô. "
            "Hãy chọn Claude, CLI Agent, Codex hoặc LM Studio để bảng có tác dụng."
        )
        self.engine_warning.setWordWrap(True)
        self.engine_warning.setVisible(engine in _NO_PROMPT_ENGINES)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            _COL_NOTE, QHeaderView.ResizeMode.Stretch
        )
        for column, width in ((_COL_SPEAKER, 170), (_COL_LISTENER, 140), (_COL_SELF, 80),
                              (_COL_OTHER, 80), (_COL_FROM, 80)):
            self.table.setColumnWidth(column, width)
        delegate = _NameDelegate([NARRATOR, *self._known_names], self.table)
        self.table.setItemDelegateForColumn(_COL_SPEAKER, delegate)
        self.table.setItemDelegateForColumn(_COL_LISTENER, delegate)
        self.table.itemChanged.connect(self._on_item_changed)

        self.general_note = QPlainTextEdit(self._table_data.general_note)
        self.general_note.setPlaceholderText(
            "Ghi chú chung cho cả truyện (không bắt buộc), ví dụ: Truyện hiện đại học đường; "
            "bạn cùng lớp xưng tớ/cậu, người lớn tuổi xưng tôi/cháu…"
        )
        self.general_note.setFixedHeight(64)

        # --- AI used for the draft scan and the in-place fix (not for translation)
        self.ai_box = QGroupBox("AI dùng để dò bảng và sửa xưng hô")
        ai_layout = QVBoxLayout(self.ai_box)
        ai_row = QHBoxLayout()
        self.ai_engine = QComboBox()
        self.ai_model = QLineEdit()
        self.ai_model.setPlaceholderText("model mặc định của engine")
        self.sample_spin = QSpinBox()
        self.sample_spin.setRange(1, 500)
        self.sample_spin.setValue(20)
        self.sample_spin.setToolTip(
            "Số chương gửi cho AI để dò (mỗi chương một lần gọi): nửa đầu lấy từ đầu "
            "truyện, nửa sau rải đều cả truyện."
        )
        self.scan_button = QPushButton("🔎 Dò từ bản dịch")
        self.scan_button.setToolTip(
            "Nhờ AI đọc một số chương và đề xuất bảng xưng hô. Chỉ thêm cặp chưa có — dòng "
            "bạn đã có không bị đổi. Cặp nào dùng nhiều cách khác nhau sẽ hiện ở “Mâu thuẫn”."
        )
        self.scan_button.clicked.connect(self._toggle_scan)
        ai_row.addWidget(self.ai_engine, stretch=1)
        ai_row.addWidget(QLabel("Model:"))
        ai_row.addWidget(self.ai_model, stretch=1)
        ai_row.addWidget(QLabel("Số chương:"))
        ai_row.addWidget(self.sample_spin)
        ai_row.addWidget(self.scan_button)
        ai_layout.addLayout(ai_row)

        options_row = QHBoxLayout()
        self.auto_detect_check = QCheckBox("Tự động dò từ bản gốc khi dịch từng chương")
        self.auto_detect_check.setToolTip(
            "Khi dịch bằng AI, chỉ tự dò thêm khi chương xuất hiện nhân vật hoàn toàn chưa "
            "có dòng nào trong bảng, rồi cập nhật vào bảng (không ghi đè dòng đã có)."
        )
        self.prefer_source_check = QCheckBox("Dò từ bản gốc (kể cả chương đã dịch)")
        self.prefer_source_check.setToolTip(
            "Khi bấm Dò, luôn đọc bản gốc để nắm rõ ngữ nghĩa quan hệ nhân vật rồi đề xuất "
            "xưng hô tiếng Việt, thay vì đọc bản đã dịch."
        )
        self.prefer_source_check.toggled.connect(self._update_scan_button_label)
        options_row.addWidget(self.auto_detect_check)
        options_row.addWidget(self.prefer_source_check)
        options_row.addStretch(1)
        ai_layout.addLayout(options_row)

        self.scan_status = QLabel("")
        self.scan_status.setWordWrap(True)
        if config is not None:
            from noveltrans.config import LLM_ENGINES, translator_labels

            labels = translator_labels(config)
            for key in LLM_ENGINES:  # Google cannot do either job
                self.ai_engine.addItem(labels.get(key, key), key)
            index = self.ai_engine.findData(config.address_ai_engine)
            self.ai_engine.setCurrentIndex(index if index >= 0 else 0)
            self.ai_model.setText(config.address_ai_model)
            self.auto_detect_check.setChecked(config.auto_detect_address)
            self.auto_detect_check.toggled.connect(
                lambda checked: setattr(config, "auto_detect_address", checked)
            )
            # One model string shared across engines — reset it on switch, exactly like
            # the rewrite dialog, so a Claude model never reaches Codex.
            self.ai_engine.currentIndexChanged.connect(
                lambda: self.ai_model.setText(
                    config.model_for_engine(self.ai_engine.currentData() or "")
                )
            )
        else:
            self.ai_box.setVisible(False)

        self.conflict_label = QLabel("Mâu thuẫn (cùng một cặp, nhiều cách xưng hô):")
        self.conflict_list = QListWidget()
        self.conflict_list.setMaximumHeight(110)
        self.conflict_label.setVisible(False)
        self.conflict_list.setVisible(False)

        self.add_button = QPushButton("＋ Thêm")
        self.add_button.clicked.connect(lambda: self._add_row(AddressEntry("", "")))
        self.narration_button = QPushButton("＋ Lời kể")
        self.narration_button.setToolTip("Cách lời kể gọi một nhân vật (hắn, anh ta, nàng…).")
        self.narration_button.clicked.connect(
            lambda: self._add_row(AddressEntry(NARRATOR, ""), column=_COL_LISTENER)
        )
        self.reverse_button = QPushButton("⇄ Thêm chiều ngược lại")
        self.reverse_button.setToolTip(
            "Thêm dòng B nói với A từ dòng đang chọn, với “xưng” và “gọi” đổi chỗ — "
            "kiểm tra lại, vì hai chiều không phải lúc nào cũng đối xứng."
        )
        self.reverse_button.clicked.connect(self._add_reverse)
        self.remove_button = QPushButton("Xoá dòng")
        self.remove_button.clicked.connect(self._remove_row)
        self.save_button = QPushButton("Lưu")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save)
        self.close_button = QPushButton("Đóng")
        self.close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        for button in (self.add_button, self.narration_button, self.reverse_button,
                       self.remove_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.engine_warning)
        layout.addWidget(self.ai_box)
        layout.addWidget(self.scan_status)
        layout.addWidget(self.table, stretch=1)
        layout.addWidget(self.conflict_label)
        layout.addWidget(self.conflict_list)
        layout.addWidget(QLabel("Ghi chú chung:"))
        layout.addWidget(self.general_note)
        layout.addLayout(buttons)

        for entry in self._table_data.entries:
            self._insert_row(entry)

    # ------------------------------------------------------------------ table

    def _insert_row(self, entry: AddressEntry) -> int:
        self._filling = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)
            speaker = QTableWidgetItem(entry.speaker)
            speaker.setFlags(speaker.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            speaker.setCheckState(
                Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked
            )
            speaker.setData(Qt.ItemDataRole.UserRole, entry.origin)
            self.table.setItem(row, _COL_SPEAKER, speaker)
            self.table.setItem(row, _COL_LISTENER, QTableWidgetItem(entry.listener))
            self.table.setItem(row, _COL_SELF, QTableWidgetItem(entry.self_term))
            self.table.setItem(row, _COL_OTHER, QTableWidgetItem(entry.other_term))
            self.table.setItem(row, _COL_FROM, QTableWidgetItem(str(entry.from_chapter)))
            self.table.setItem(row, _COL_NOTE, QTableWidgetItem(entry.note))
        finally:
            self._filling = False
        self._mark_unknown(row, _COL_SPEAKER)
        self._mark_unknown(row, _COL_LISTENER)
        return row

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def _from_chapter(self, row: int) -> int:
        """The row's "Từ chương", 1 when blank or not a number."""
        try:
            return max(1, int(self._cell(row, _COL_FROM) or 1))
        except ValueError:
            return 1

    def _mark_unknown(self, row: int, column: int) -> None:
        """⚠️ a name that is not in the Tên nhân vật list — the row likely never matches."""
        item = self.table.item(row, column)
        if item is None:
            return
        name = item.text().strip()
        unknown = bool(name) and name != NARRATOR and name not in self._known_names
        self._filling = True
        try:
            item.setToolTip(
                "⚠️ Tên này không có trong 👤 Tên nhân vật. Dòng chỉ có tác dụng nếu tên "
                "xuất hiện đúng y như vậy trong bản gốc sau khi thay tên."
                if unknown else ""
            )
            # None restores the theme's own colour, so dark mode stays readable.
            item.setData(
                Qt.ItemDataRole.ForegroundRole,
                QBrush(Qt.GlobalColor.darkYellow) if unknown else None,
            )
        finally:
            self._filling = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        if item.column() in (_COL_SPEAKER, _COL_LISTENER):
            self._mark_unknown(item.row(), item.column())

    def _rows(self) -> list[AddressEntry]:
        """The table's current state. Rows with no speaker or listener are dropped."""
        out: list[AddressEntry] = []
        for row in range(self.table.rowCount()):
            speaker_item = self.table.item(row, _COL_SPEAKER)
            speaker, listener = self._cell(row, _COL_SPEAKER), self._cell(row, _COL_LISTENER)
            if speaker_item is None or not speaker or not listener:
                continue
            out.append(
                AddressEntry(
                    speaker=speaker,
                    listener=listener,
                    self_term="" if speaker == NARRATOR else self._cell(row, _COL_SELF),
                    other_term=self._cell(row, _COL_OTHER),
                    note=self._cell(row, _COL_NOTE),
                    from_chapter=self._from_chapter(row),
                    enabled=speaker_item.checkState() == Qt.CheckState.Checked,
                    origin=speaker_item.data(Qt.ItemDataRole.UserRole) or ORIGIN_MANUAL,
                )
            )
        return out

    # ------------------------------------------------------------------ actions

    def _add_row(self, entry: AddressEntry, *, column: int = _COL_SPEAKER) -> None:
        row = self._insert_row(entry)
        self.table.scrollToBottom()
        self.table.setCurrentCell(row, column)
        self.table.editItem(self.table.item(row, column))

    def _add_reverse(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        speaker, listener = self._cell(row, _COL_SPEAKER), self._cell(row, _COL_LISTENER)
        if not speaker or not listener or NARRATOR in (speaker, listener):
            QMessageBox.information(
                self, "Không đảo được",
                "Chọn một dòng có đủ người nói và người nghe (không phải lời kể).",
            )
            return
        source = AddressEntry(
            speaker=speaker,
            listener=listener,
            self_term=self._cell(row, _COL_SELF),
            other_term=self._cell(row, _COL_OTHER),
            from_chapter=self._from_chapter(row),
        )
        reverse = reverse_of(source)
        existing = {(e.speaker, e.listener, e.from_chapter) for e in self._rows()}
        if (reverse.speaker, reverse.listener, reverse.from_chapter) in existing:
            QMessageBox.information(
                self, "Đã có", f"Đã có dòng {reverse.speaker} nói với {reverse.listener}."
            )
            return
        self._add_row(reverse, column=_COL_SELF)

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _save(self) -> None:
        entries = self._rows()
        empty = [f"{e.speaker} → {e.listener}" for e in entries if e.enabled and not e.usable]
        if empty:
            QMessageBox.warning(
                self,
                "Thiếu cách xưng hô",
                "Những dòng này đang bật nhưng chưa điền “Xưng” hay “Gọi”:\n\n"
                + "\n".join(empty[:10])
                + "\n\nHãy điền, hoặc bỏ tick để không dùng.",
            )
            return
        seen: set[tuple[str, str, int]] = set()
        dupes = []
        for e in entries:
            key = (e.speaker, e.listener, e.from_chapter)
            if key in seen:
                dupes.append(f"{e.speaker} → {e.listener} (từ chương {e.from_chapter})")
            seen.add(key)
        if dupes:
            QMessageBox.warning(
                self,
                "Dòng trùng",
                "Mỗi cặp chỉ nên có một dòng cho mỗi “Từ chương”:\n\n" + "\n".join(dupes[:10]),
            )
            return
        self._table_data = AddressTable(
            general_note=self.general_note.toPlainText().strip(), entries=tuple(entries)
        )
        write_address(self.project.path, self._table_data)
        self._remember_ai_choice()
        affected = self.affected_chapters()
        self._original_keys = {_row_key(e) for e in entries if e.usable}
        if affected:
            self._offer_repair(affected)
        self.accept()

    def affected_chapters(self) -> list[int]:
        """Translated chapters (indices) that a just-saved change, or the last scan's
        conflicts, could touch.

        A chapter is affected when a row that is new or changed in this save applies to it
        (`rows_for_chapter` on its translation — Vietnamese names match there too), or the
        scan found it using a minority form.
        """
        changed = {
            _row_key(e) for e in self._table_data.entries if e.usable
        } - self._original_keys
        out: list[int] = []
        for chapter in self.project.chapters():
            if not chapter.translated or not chapter.enabled:
                continue
            number = chapter.index + 1
            if number in self._conflict_chapters or any(
                _row_key(row) in changed
                for row in rows_for_chapter(self._table_data, number, chapter.translated)
            ):
                out.append(chapter.index)
        return out

    def _offer_repair(self, indices: list[int]) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Sửa các chương đã dịch?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"Bảng xưng hô mới áp dụng cho {len(indices)} chương đã dịch.\n\n"
            "• Sửa xưng hô: AI chỉ đổi đại từ theo bảng, giữ nguyên phần còn lại của bản "
            "dịch (kể cả sửa tay). Chương nào AI đổi thêm chữ khác thì giữ nguyên bản cũ. "
            "Hoàn tác được: chuột phải chương → “Hoàn tác sửa xưng hô”.\n"
            + (
                # Re-translating with an engine that never reads the table would erase the
                # user's edits and rewrites for a result that ignores the table.
                "• Dịch lại: không có — engine đang chọn (Google) không đọc được bảng xưng "
                "hô. Đổi sang engine AI ở tab Dịch nếu muốn dịch lại.\n"
                if self._engine in _NO_PROMPT_ENGINES
                else "• Dịch lại: dịch lại từ đầu theo bảng — chính xác hơn nhưng mất sửa tay "
                "và bản viết lại.\n"
            )
            + "• Giữ nguyên: bảng chỉ áp dụng cho lần dịch sau."
        )
        fix = box.addButton("Sửa xưng hô (AI)", QMessageBox.ButtonRole.AcceptRole)
        redo = (
            None
            if self._engine in _NO_PROMPT_ENGINES
            else box.addButton("Dịch lại", QMessageBox.ButtonRole.DestructiveRole)
        )
        box.addButton("Giữ nguyên", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(fix)
        box.exec()
        if box.clickedButton() is fix:
            self.fix_requested.emit(indices)
        elif redo is not None and box.clickedButton() is redo:
            self.retranslate_requested.emit(indices)

    def ai_choice(self) -> tuple[str, str]:
        """(engine, model) picked for the scan and the fix."""
        return self.ai_engine.currentData() or "", self.ai_model.text().strip()

    def _update_scan_button_label(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.scan_button.setText(
            "🔎 Dò từ bản gốc" if self.prefer_source_check.isChecked() else "🔎 Dò từ bản dịch"
        )

    def _remember_ai_choice(self) -> None:
        if self.config is not None:
            if self.ai_engine.count():
                engine, model = self.ai_choice()
                self.config.address_ai_engine = engine
                self.config.address_ai_model = model
            self.config.auto_detect_address = self.auto_detect_check.isChecked()

    # ------------------------------------------------------------------ scan

    def _toggle_scan(self) -> None:
        from noveltrans.gui.workers import AddressScanWorker

        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self.scan_status.setText("Đang dừng…")
            return
        if self.config is None:
            return
        engine, model = self.ai_choice()
        self._remember_ai_choice()
        worker = AddressScanWorker(
            self.project.path, engine, api_key=self.config.claude_api_key, model=model,
            cli_command=self.config.cli_command_for(engine),
            base_url=self.config.lmstudio_url if engine == "lmstudio" else "",
            sample_size=self.sample_spin.value(), parent=self, existing=self._rows(),
            prefer_source=self.prefer_source_check.isChecked(),
        )
        worker.progress.connect(
            lambda done, total, title: self.scan_status.setText(
                f"Đang dò {done}/{total}" + (f" — {title}" if title else "")
            )
        )
        worker.scanned.connect(self.apply_scan)
        worker.stopped.connect(self._on_scan_stopped)
        worker.failed.connect(self._on_scan_failed)
        worker.finished.connect(self._update_scan_button_label)
        self._scan_worker = worker
        self.scan_button.setText("■ Dừng dò")
        worker.start()

    def _on_scan_stopped(self, proposed: list, conflicts: list, done: int, total: int) -> None:
        """Dừng: keep what the chapters already scanned found, and say it is partial."""
        if not done:
            self.scan_status.setText("Đã dừng — chưa dò xong chương nào.")
            return
        self.apply_scan(proposed, conflicts)
        self.scan_status.setText(
            f"Đã dừng sau {done}/{total} chương. " + self.scan_status.text()
        )

    def _on_scan_failed(self, message: str) -> None:
        self.scan_status.setText(f"⚠️ Không dò được: {message}")

    def apply_scan(self, proposed: list, conflicts: list) -> None:
        """Fold a scan into what the table shows now (not what was last saved), so an
        edit made during the scan survives; then show the conflicts."""
        current = self._rows()
        merged = merge_extracted(current, proposed)
        added = merged[len(current):]
        for entry in added:
            self._insert_row(entry)
        self.conflict_list.clear()
        self._conflict_chapters = set()
        for conflict in conflicts:
            self.conflict_list.addItem(conflict.describe())
            self._conflict_chapters.update(conflict.minority_chapters)
        self.conflict_label.setVisible(bool(conflicts))
        self.conflict_list.setVisible(bool(conflicts))
        self.scan_status.setText(
            f"Dò xong: thêm {len(added)} dòng mới, {len(conflicts)} cặp xưng hô không thống "
            f"nhất ({len(self._conflict_chapters)} chương lệch). Kiểm tra lại các dòng mới, "
            "sửa “Từ chương” nếu quan hệ đổi giữa truyện, rồi bấm Lưu."
            if proposed or conflicts
            else "Dò xong: AI không tìm thấy cách xưng hô nào."
        )

    # ------------------------------------------------------------------ teardown

    def shutdown(self) -> None:
        """Stop the scan thread. Safe to call twice; never raises."""
        worker = self._scan_worker
        self._scan_worker = None
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        if worker.wait(2000):
            return
        # Still inside an engine call: let it finish on its own, talking to nobody.
        for signal in (
            worker.progress, worker.scanned, worker.stopped, worker.failed, worker.finished
        ):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        worker.setParent(None)
        _DETACHED.add(worker)
        worker.finished.connect(lambda w=worker: _DETACHED.discard(w))

    def reject(self) -> None:
        self.shutdown()
        super().reject()

    def accept(self) -> None:
        self.shutdown()
        super().accept()

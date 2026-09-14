"""Kiểm tra chất lượng dịch — the Dịch tab's translation-QC dialog and result view.

Two windows, one job. `QcDialog` configures the check — who judges, which engines re-do a
bad chapter and how many tries each gets — and starts a scan. `QcResultDialog` shows what
the scan found, lets the user READ a suspect chapter before believing the verdict, and
hands the ones they pick back to the tab to re-translate.

That split is the point of the feature rather than an implementation detail: a verdict is
an opinion, sometimes a wrong one, and the expensive irreversible act — re-translating —
stays behind a human choosing it. The judging itself lives in `translators/qc.py`; the
batch lives in `workers.QcScanWorker`. This file is only the Qt wiring.

Like `RewriteDialog`, neither window owns a worker: they emit what the user asked for and
the tab starts it, so `has_running_workers`, `shutdown` and the job registry stay in one
place.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from noveltrans.config import (
    DEFAULT_QC_ATTEMPTS,
    LLM_ENGINES,
    MAX_QC_ATTEMPTS,
    AppConfig,
    translator_labels,
)
from noveltrans.gui.workers import chapters_to_qc
from noveltrans.storage import NovelProject
from noveltrans.translators.qc import QC_LABELS

DRY_RUN_CHAPTERS = 3  # what "Thử N chương" checks before committing to a whole novel
SAVE_LABEL = "💾 Lưu cài đặt"
SAVED_LABEL = "✓ Đã lưu"
SAVED_FLASH_MS = 1500  # how long the button admits it saved before going back
# Đóng with unsaved changes. Spelled out rather than using QMessageBox's standard buttons,
# whose text arrives in English unless a Qt translation is loaded — the rest of this app is
# Vietnamese and a lone "Save / Discard / Cancel" row would read as a different program.
CLOSE_SAVE_LABEL = "Lưu rồi đóng"
CLOSE_DISCARD_LABEL = "Đóng, bỏ thay đổi"
CLOSE_CANCEL_LABEL = "Quay lại"


class QcDialog(QDialog):
    """Configure the quality check, then scan (or dry-run) a range of chapters."""

    start_requested = Signal(dict)  # scan choices; the tab builds the worker
    results_requested = Signal()  # reopen the verdicts already on disk

    def __init__(
        self,
        project: NovelProject,
        config: AppConfig,
        parent=None,
    ):
        super().__init__(parent)
        self.project = project
        self.config = config

        self.setWindowTitle("Kiểm tra chất lượng dịch")
        self.setMinimumWidth(660)

        chapters = project.chapters()
        total = len(chapters)
        # The detectors and the judge prompt are Vietnamese-specific — an English pass would
        # need different rules, not a flag — so refuse rather than mislead.
        langs = {c.target_lang for c in chapters if c.translated and c.target_lang}
        self._target_lang = "vi"
        self._blocked = ""
        if not any(c.translated for c in chapters):
            self._blocked = "Truyện này chưa có chương nào được dịch."
        elif langs and "vi" not in langs:
            self._blocked = "Tính năng này chỉ dành cho bản dịch tiếng Việt."

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Kiểm tra bản dịch có bị <b>ra tiếng Anh</b>, <b>còn nguyên chữ Hán</b>, bị cắt "
            "ngắn, hay <b>đặc Hán-Việt</b> (giọng convert) không — rồi cho phép dịch lại "
            "những chương hỏng."
        )
        intro.setWordWrap(True)  # long line; the dialog is resizable and this must reflow
        layout.addWidget(intro)

        # --- auto mode
        self.auto_check = QCheckBox("Tự động kiểm tra và dịch lại ngay trong lúc dịch")
        self.auto_check.setChecked(config.qc_enabled)
        self.auto_check.setToolTip(
            "Bật thì mỗi chương vừa dịch xong sẽ được kiểm tra ngay; chương không đạt được "
            "dịch lại theo thứ tự engine bên dưới, kèm lời phê của lần trước.\n\nTắt thì "
            "việc dịch chạy đúng như trước, và bạn kiểm tra thủ công bằng nút bên dưới."
        )
        layout.addWidget(self.auto_check)

        # --- judge
        judge_box = QGroupBox("Chấm chất lượng bằng")
        judge_layout = QVBoxLayout(judge_box)
        judge_row = QHBoxLayout()
        self.engine_combo = QComboBox()
        labels = translator_labels(config)
        for key in LLM_ENGINES:  # Google can only translate, never judge
            self.engine_combo.addItem(labels.get(key, key), key)
        index = self.engine_combo.findData(config.qc_ai_engine)
        self.engine_combo.setCurrentIndex(index if index >= 0 else 0)
        self.model_edit = QLineEdit(config.qc_ai_model)
        self.model_edit.setPlaceholderText("model mặc định của engine")
        # Connected AFTER the saved value is in, so opening the dialog never rewrites it.
        self.engine_combo.currentIndexChanged.connect(
            lambda _i: self.model_edit.setText(
                self._remembered_model(self.engine_combo.currentData())
            )
        )
        judge_row.addWidget(self.engine_combo, stretch=1)
        judge_row.addWidget(QLabel("Model:"))
        judge_row.addWidget(self.model_edit, stretch=1)
        judge_layout.addLayout(judge_row)

        self.judge_check = QCheckBox("Dùng AI chấm văn phong Hán-Việt (tốn thêm 1 lượt/chương)")
        self.judge_check.setChecked(config.qc_use_llm_judge)
        self.judge_check.setToolTip(
            "Lỗi \"đặc Hán-Việt\" không có cách nào đo bằng máy: truyện tiên hiệp DÙNG "
            "nhiều từ Hán-Việt là đúng, nên phải hỏi AI.\n\nTắt đi thì việc kiểm tra hoàn "
            "toàn miễn phí và tức thì, vẫn bắt được lỗi ra tiếng Anh, còn chữ Hán, rỗng và "
            "bị cắt ngắn."
        )
        self.judge_check.toggled.connect(self._on_judge_toggled)
        judge_layout.addWidget(self.judge_check)
        layout.addWidget(judge_box)

        # --- the re-translate chain
        chain_box = QGroupBox("Dịch lại bằng — theo thứ tự")
        chain_layout = QVBoxLayout(chain_box)
        chain_hint = QLabel(
            "Chương không đạt sẽ được dịch lại bằng engine đầu tiên; hết số lần thử mà vẫn "
            "hỏng thì chuyển sang engine tiếp theo. Để trống = dùng engine đang chọn ở tab "
            f"Dịch, {DEFAULT_QC_ATTEMPTS} lần."
        )
        chain_hint.setWordWrap(True)
        chain_layout.addWidget(chain_hint)
        self.chain_table = QTableWidget(0, 3)
        self.chain_table.setHorizontalHeaderLabels(["Engine", "Model", "Số lần thử"])
        self.chain_table.verticalHeader().setVisible(False)
        self.chain_table.setMaximumHeight(170)
        header = self.chain_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        chain_layout.addWidget(self.chain_table)

        chain_buttons = QHBoxLayout()
        for text, slot, tip in (
            ("+", self._add_chain_row, "Thêm một engine vào cuối danh sách"),
            ("−", self._remove_chain_row, "Bỏ engine đang chọn"),
            ("↑", lambda: self._move_chain_row(-1), "Đưa engine đang chọn lên trước"),
            ("↓", lambda: self._move_chain_row(1), "Đưa engine đang chọn xuống sau"),
        ):
            button = QPushButton(text)
            button.setFixedWidth(36)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            chain_buttons.addWidget(button)
        chain_buttons.addStretch(1)
        chain_layout.addLayout(chain_buttons)
        layout.addWidget(chain_box)
        for engine_name, model, attempts in config.qc_engine_chain:
            self._add_chain_row(engine_name, model, attempts)

        # --- scope
        scope_box = QGroupBox("Phạm vi kiểm tra")
        scope_layout = QVBoxLayout(scope_box)
        range_row = QHBoxLayout()
        self.start_spin = QSpinBox()
        self.end_spin = QSpinBox()
        for spin, value in ((self.start_spin, 1), (self.end_spin, max(total, 1))):
            spin.setMinimum(1)
            spin.setMaximum(max(total, 1))
            spin.setValue(value)
            spin.valueChanged.connect(self._refresh_estimate)
        range_row.addWidget(QLabel("Từ chương"))
        range_row.addWidget(self.start_spin)
        range_row.addWidget(QLabel("đến chương"))
        range_row.addWidget(self.end_spin)
        range_row.addStretch(1)
        scope_layout.addLayout(range_row)

        self.scope_pending = QRadioButton("Chỉ những chương chưa kiểm tra")
        self.scope_pending.setChecked(True)
        self.scope_all = QRadioButton("Tất cả chương đã dịch trong khoảng (kiểm tra lại từ đầu)")
        for radio in (self.scope_pending, self.scope_all):
            radio.toggled.connect(self._refresh_estimate)
            scope_layout.addWidget(radio)
        layout.addWidget(scope_box)

        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)

        # --- actions
        self.dry_run_button = QPushButton(f"👁 Thử {DRY_RUN_CHAPTERS} chương")
        self.dry_run_button.setToolTip(
            "Chạy đúng quy trình sẽ dùng cho cả truyện nhưng chỉ vài chương — để xem AI "
            "chấm có hợp lý không TRƯỚC khi quét cả nghìn chương."
        )
        self.dry_run_button.clicked.connect(lambda: self._request_start(limit=DRY_RUN_CHAPTERS))
        self.start_button = QPushButton("🔍 Bắt đầu kiểm tra")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(lambda: self._request_start())
        self.results_button = QPushButton("Xem chương lỗi đã tìm được")
        self.results_button.clicked.connect(self._request_results)
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.setToolTip(
            "Ghi nhớ engine, model và chuỗi engine ở trên mà không chạy gì cả.\n\n"
            "Không bấm thì thay đổi chỉ được ghi khi bắt đầu kiểm tra hoặc thử "
            f"{DRY_RUN_CHAPTERS} chương — đóng hộp thoại là mất."
        )
        self.save_button.clicked.connect(self._save_settings)
        close_button = QPushButton("Đóng")
        close_button.clicked.connect(self.reject)
        action_row = QHBoxLayout()
        action_row.addWidget(self.start_button)
        action_row.addWidget(self.dry_run_button)
        action_row.addWidget(self.results_button)
        action_row.addStretch(1)
        action_row.addWidget(self.save_button)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)

        # Parented to the dialog so it dies with it — a bare `QTimer.singleShot` would
        # still fire after the user closed the dialog and call a method on a deleted
        # widget.
        self._saved_flash = QTimer(self)
        self._saved_flash.setSingleShot(True)
        self._saved_flash.timeout.connect(self._restore_save_button)

        self._refresh_estimate()
        if self._blocked:
            self.estimate_label.setText(self._blocked)
            for widget in (
                self.auto_check, self.engine_combo, self.model_edit, self.judge_check,
                self.chain_table, self.start_spin, self.end_spin, self.scope_pending,
                self.scope_all, self.start_button, self.dry_run_button, self.results_button,
                self.save_button,  # nothing above can be edited, so there is nothing to save
            ):
                widget.setEnabled(False)

        # What Đóng compares against. A snapshot of the WIDGETS as first shown, not of the
        # config: `qc_ai_engine` may hold an engine this combo does not offer (it defaults
        # to the translator, which can be Google), and the combo then falls back to its
        # first item. Comparing widgets to config would call that a change the user made
        # and prompt on a dialog they only looked at.
        self._saved_state = self._current_settings()

    # -------------------------------------------------------------- the chain

    def _remembered_model(self, engine: str) -> str:
        """This engine's own model — see `AppConfig.model_for_engine`.

        The Translate tab has always reloaded the model per engine; the boxes here did not,
        so switching an engine silently kept the previous one's model — the one way this
        dialog could produce a chain that cannot translate anything at all.
        """
        return self.config.model_for_engine(engine or "")

    def _add_chain_row(self, engine_name: str = "", model: str = "", attempts: int = 0) -> None:
        row = self.chain_table.rowCount()
        self.chain_table.insertRow(row)
        combo = QComboBox()
        labels = translator_labels(self.config)
        for key in LLM_ENGINES:
            combo.addItem(labels.get(key, key), key)
        index = combo.findData(engine_name or self.config.qc_ai_engine)
        combo.setCurrentIndex(index if index >= 0 else 0)
        self.chain_table.setCellWidget(row, 0, combo)
        model_edit = QLineEdit(model)
        model_edit.setPlaceholderText("model mặc định của engine")
        self.chain_table.setCellWidget(row, 1, model_edit)
        # Connected after both widgets carry their saved values, so building a row (and
        # `_move_chain_row`, which rebuilds every row) never rewrites the model.
        combo.currentIndexChanged.connect(
            lambda _i, c=combo, e=model_edit: e.setText(self._remembered_model(c.currentData()))
        )
        spin = QSpinBox()
        spin.setRange(1, MAX_QC_ATTEMPTS)
        spin.setValue(attempts or DEFAULT_QC_ATTEMPTS)
        self.chain_table.setCellWidget(row, 2, spin)
        # A cell widget does not drive the row height, so the default section size clips a
        # combo box and its text is cut off vertically. Size the row from the widgets
        # themselves rather than a hard-coded number, which would break on another platform
        # or at a different font size.
        self.chain_table.setRowHeight(
            row, max(combo.sizeHint().height(), spin.sizeHint().height()) + 8
        )

    def _remove_chain_row(self) -> None:
        row = self.chain_table.currentRow()
        if row >= 0:
            self.chain_table.removeRow(row)

    def _move_chain_row(self, delta: int) -> None:
        """Reorder by re-reading the rows and rebuilding — cell widgets do not survive a
        naive row swap, and the chain is at most a handful of entries."""
        row = self.chain_table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.chain_table.rowCount():
            return
        chain = self._chain()
        chain[row], chain[target] = chain[target], chain[row]
        self.chain_table.setRowCount(0)
        for engine_name, model, attempts in chain:
            self._add_chain_row(engine_name, model, attempts)
        self.chain_table.setCurrentCell(target, 0)

    def _chain(self) -> list[tuple[str, str, int]]:
        chain = []
        for row in range(self.chain_table.rowCount()):
            combo = self.chain_table.cellWidget(row, 0)
            model_edit = self.chain_table.cellWidget(row, 1)
            spin = self.chain_table.cellWidget(row, 2)
            chain.append((combo.currentData(), model_edit.text().strip(), spin.value()))
        return chain

    # ------------------------------------------------------------------ state

    def _eligible(self) -> list:
        return chapters_to_qc(
            self.project,
            self._target_lang,
            start_idx=self.start_spin.value() - 1,
            end_idx=self.end_spin.value() - 1,
            force=self.scope_all.isChecked(),
        )

    def _on_judge_toggled(self, on: bool) -> None:
        self.engine_combo.setEnabled(on)
        self.model_edit.setEnabled(on)
        self._refresh_estimate()

    def _current_settings(self) -> tuple:
        """Everything `_remember_choices` would write, as one comparable value."""
        return (
            self.auto_check.isChecked(),
            self.judge_check.isChecked(),
            self.engine_combo.currentData(),
            self.model_edit.text().strip(),
            tuple(self._chain()),
        )

    def _ask_unsaved(self) -> str:
        """Ask what to do with unsaved changes: "save", "discard" or "cancel"."""
        box = QMessageBox(self)
        box.setWindowTitle("Chưa lưu thay đổi")
        box.setText(
            "Cài đặt kiểm tra chất lượng đã thay đổi nhưng chưa được lưu.\n\n"
            "Đóng bây giờ thì các thay đổi này sẽ mất."
        )
        save_button = box.addButton(CLOSE_SAVE_LABEL, QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(
            CLOSE_DISCARD_LABEL, QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(CLOSE_CANCEL_LABEL, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)  # Enter keeps the work, never throws it away
        box.exec()

        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "cancel"  # includes the message box's own close button

    def reject(self) -> None:
        """Đóng — still a way to throw a change away, but no longer a silent one.

        Reached by the Đóng button, Esc, and the window's X alike: `QDialog.closeEvent`
        rejects. `accept()` is untouched, so starting a scan or opening the results (both
        of which save first) never asks.
        """
        if self._current_settings() != self._saved_state:
            answer = self._ask_unsaved()
            if answer == "cancel":
                return  # stay open, nothing written
            if answer == "save":
                self._remember_choices()
        super().reject()

    def _save_settings(self) -> None:
        """Persist the choices without running anything — the dialog stays open.

        Everything `_remember_choices` writes used to be written ONLY by `_request_start`
        and `_request_results`, so changing the engine and closing threw the change away
        with nothing on screen to say so. `accept()` is deliberately not called here:
        saving a setting and starting a scan over a thousand chapters are different acts,
        and the user asked for the first without the second.
        """
        self._remember_choices()
        self.save_button.setText(SAVED_LABEL)
        self.save_button.setEnabled(False)
        self._saved_flash.start(SAVED_FLASH_MS)

    def _restore_save_button(self) -> None:
        self.save_button.setText(SAVE_LABEL)
        self.save_button.setEnabled(not self._blocked)

    def _remember_choices(self) -> None:
        self.config.qc_enabled = self.auto_check.isChecked()
        self.config.qc_use_llm_judge = self.judge_check.isChecked()
        self.config.qc_ai_engine = self.engine_combo.currentData()
        self.config.qc_ai_model = self.model_edit.text()
        self.config.qc_engine_chain = self._chain()
        # The single place anything is written, so the single place the baseline moves —
        # every caller (the Save button, starting a scan, opening the results) is covered.
        self._saved_state = self._current_settings()

    def _refresh_estimate(self, *_args) -> None:
        if self._blocked:
            return
        if self.end_spin.value() < self.start_spin.value():
            self.end_spin.setValue(self.start_spin.value())
            return  # the valueChanged this triggers re-runs us
        counts = self.project.counts()
        eligible = self._eligible()
        parts = [
            f"{counts['translated']} chương đã dịch — {counts['qc_ok']} đạt, "
            f"{counts['qc_failed']} lỗi, {counts['qc_unchecked']} chưa kiểm tra. "
            f"Lượt này: <b>{len(eligible)} chương</b>."
        ]
        if eligible and self.judge_check.isChecked():
            minutes = len(eligible) * 0.4  # one short agent call per chapter
            duration = f"{minutes / 60:.1f} giờ" if minutes >= 60 else f"{minutes:.0f} phút"
            parts.append(
                f"Mỗi chương tốn <b>một lượt gọi AI</b> để chấm văn phong — khoảng "
                f"{duration}. Tắt mục \"AI chấm văn phong\" thì miễn phí và gần như tức thì."
            )
        elif eligible:
            parts.append("Chỉ kiểm tra nhanh — không gọi AI, không tốn quota.")
        self.estimate_label.setText("<br>".join(parts))
        self.start_button.setEnabled(bool(eligible))
        self.dry_run_button.setEnabled(bool(eligible))
        self.results_button.setEnabled(counts["qc_failed"] > 0)

    # --------------------------------------------------------------- requests

    def _request_start(self, limit: int = 0) -> None:
        if not self._eligible():
            return
        self._remember_choices()
        self.start_requested.emit(
            {
                "target_lang": self._target_lang,
                "start_idx": self.start_spin.value() - 1,
                "end_idx": self.end_spin.value() - 1,
                "force": self.scope_all.isChecked(),
                "limit": limit,
            }
        )
        self.accept()

    def _request_results(self) -> None:
        self._remember_choices()
        self.results_requested.emit()
        self.accept()


class QcResultDialog(QDialog):
    """The chapters that failed, for the user to read, choose, and re-translate."""

    retranslate_requested = Signal(list)  # chapter indices
    chapter_activated = Signal(int)  # double-click: show it in the tab's preview panes

    CHECK_COLUMN = 0
    INDEX_COLUMN = 1
    TITLE_COLUMN = 2
    ERROR_COLUMN = 3
    DETAIL_COLUMN = 4

    def __init__(self, chapters: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chương dịch chưa đạt")
        self.setMinimumSize(760, 420)

        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "#", "Tên chương", "Lỗi", "Chi tiết"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.DETAIL_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table, stretch=1)

        layout.addWidget(
            QLabel(
                "Nhấp đúp vào một dòng để mở chương đó ra đọc trước khi quyết định. "
                "Dịch lại sẽ <b>xoá bản dịch hiện tại</b> của những chương được chọn."
            )
        )

        select_all = QPushButton("Chọn tất cả")
        select_all.clicked.connect(lambda: self._set_all_checked(True))
        select_none = QPushButton("Bỏ chọn")
        select_none.clicked.connect(lambda: self._set_all_checked(False))
        self.retranslate_button = QPushButton("↻ Dịch lại chương đã chọn")
        self.retranslate_button.setProperty("primary", True)
        self.retranslate_button.clicked.connect(self._request_retranslate)
        close_button = QPushButton("Đóng")
        close_button.clicked.connect(self.reject)
        action_row = QHBoxLayout()
        action_row.addWidget(self.retranslate_button)
        action_row.addWidget(select_all)
        action_row.addWidget(select_none)
        action_row.addStretch(1)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)

        self.set_chapters(chapters)

    def set_chapters(self, chapters: list) -> None:
        """(Re)fill the table — also used to refresh after a re-translate run."""
        self.table.setRowCount(0)
        for chapter in chapters:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            check.setData(Qt.ItemDataRole.UserRole, chapter.index)
            self.table.setItem(row, self.CHECK_COLUMN, check)
            self.table.setItem(row, self.INDEX_COLUMN, QTableWidgetItem(str(chapter.index + 1)))
            self.table.setItem(
                row, self.TITLE_COLUMN,
                QTableWidgetItem(chapter.translated_title or chapter.title),
            )
            label = QC_LABELS.get(chapter.qc_code) or "Không đạt"
            self.table.setItem(row, self.ERROR_COLUMN, QTableWidgetItem(label))
            self.table.setItem(row, self.DETAIL_COLUMN, QTableWidgetItem(chapter.qc_reason))
        self.table.resizeColumnsToContents()
        self.summary_label.setText(
            f"<b>{len(chapters)} chương</b> chưa đạt. Đọc lại rồi chọn những chương muốn "
            "dịch lại — bản dịch cũ của chương KHÔNG chọn vẫn giữ nguyên."
            if chapters
            else "Không có chương nào bị đánh dấu lỗi."
        )
        self.retranslate_button.setEnabled(bool(chapters))

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            self.table.item(row, self.CHECK_COLUMN).setCheckState(state)

    def checked_indices(self) -> list[int]:
        return [
            self.table.item(row, self.CHECK_COLUMN).data(Qt.ItemDataRole.UserRole)
            for row in range(self.table.rowCount())
            if self.table.item(row, self.CHECK_COLUMN).checkState() == Qt.CheckState.Checked
        ]

    def _on_double_click(self, index) -> None:
        item = self.table.item(index.row(), self.CHECK_COLUMN)
        if item is not None:
            self.chapter_activated.emit(item.data(Qt.ItemDataRole.UserRole))

    def _request_retranslate(self) -> None:
        indices = self.checked_indices()
        if not indices:
            return
        self.retranslate_requested.emit(indices)
        self.accept()

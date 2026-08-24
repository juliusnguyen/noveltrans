"""Viết lại văn phong — the Dịch tab's style-rewrite dialog.

Rewrites an existing Vietnamese translation into natural Vietnamese prose: the input is
"convert" text that kept Chinese word order, the output is the same sentences reordered.
The rewriting itself lives in `translators/rewrite.py`; the batch lives in
`workers.RewriteWorker`. This file is only the Qt wiring.

It carries an engine picker, a chapter range, a **one-chapter preview**, the cost
warning and the undo. The preview matters more than it looks: a whole-novel run is hours
of quota or money, and it runs the identical code path — same chunking, same validation,
same retries — so what the user sees is what the batch will do.

The dialog does not own the worker. It emits what the user asked for and the tab starts
it, so `has_running_workers`, `shutdown` and the menu-bar job registry all stay in one
place. Being modal, it also blocks the tab's preview-pane editing while open.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
)

from noveltrans.config import LLM_ENGINES, AppConfig, translator_labels
from noveltrans.gui.workers import chapters_to_rewrite
from noveltrans.storage import NovelProject


class RewriteDialog(QDialog):
    """Pick an engine and a range, preview one chapter, then start (or undo) a rewrite."""

    start_requested = Signal(dict)  # worker choices; the tab builds the rest
    preview_requested = Signal(dict)  # same, plus indices=[i] and dry_run=True
    undo_requested = Signal(object)  # a chapter index, or None for the whole novel
    applied = Signal(set)  # {chapter index, …} written straight from the preview

    def __init__(
        self,
        project: NovelProject,
        config: AppConfig,
        preselected_index: int | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.project = project
        self.config = config
        self._preselected = preselected_index
        self._preview: tuple[int, str, str] | None = None

        self.setWindowTitle("Viết lại văn phong")
        self.setMinimumWidth(620)

        chapters = project.chapters()
        total = len(chapters)
        # The rewrite's rules — Hán-Việt names, hắn/nàng/y/thị — are Vietnamese-specific.
        # An English pass would need a different prompt, not a flag, so refuse instead.
        langs = {c.target_lang for c in chapters if c.translated and c.target_lang}
        self._target_lang = "vi"
        self._blocked = ""
        if not any(c.translated for c in chapters):
            self._blocked = "Truyện này chưa có chương nào được dịch."
        elif langs and "vi" not in langs:
            self._blocked = "Tính năng này chỉ dành cho bản dịch tiếng Việt."

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Dùng AI viết lại bản dịch cho đúng văn phong tiếng Việt — sắp xếp lại "
                "trật tự từ,\nbỏ chỗ lặp thừa. <b>Giữ nguyên tên riêng Hán-Việt và xưng "
                "hô</b> (hắn, nàng, y, thị…),\nkhông tóm tắt, không thêm bớt nội dung."
            )
        )

        # --- engine + model
        engine_box = QGroupBox("Viết lại bằng")
        engine_row = QHBoxLayout(engine_box)
        self.engine_combo = QComboBox()
        labels = translator_labels(config)
        for key in LLM_ENGINES:  # Google is translate-only and is never offered
            self.engine_combo.addItem(labels.get(key, key), key)
        index = self.engine_combo.findData(config.rewrite_ai_engine)
        self.engine_combo.setCurrentIndex(index if index >= 0 else 0)
        self.engine_combo.currentIndexChanged.connect(self._refresh_estimate)
        self.model_edit = QLineEdit(config.rewrite_ai_model)
        self.model_edit.setPlaceholderText("model mặc định của engine")
        engine_row.addWidget(self.engine_combo, stretch=1)
        engine_row.addWidget(QLabel("Model:"))
        engine_row.addWidget(self.model_edit, stretch=1)
        layout.addWidget(engine_box)

        # --- scope
        scope_box = QGroupBox("Phạm vi")
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

        self.scope_pending = QRadioButton("Chỉ những chương chưa viết lại")
        self.scope_pending.setChecked(True)
        self.scope_all = QRadioButton("Tất cả chương trong khoảng (viết lại cả chương đã làm)")
        self.scope_all.setToolTip(
            "Viết lại lần nữa sẽ viết lại trên BẢN ĐÃ VIẾT LẠI, mỗi lượt trôi xa bản gốc "
            "thêm một chút. Bản dịch trước lần viết lại đầu tiên vẫn được giữ, nên muốn "
            "đổi engine thì nên Hoàn tác trước rồi viết lại."
        )
        for radio in (self.scope_pending, self.scope_all):
            radio.toggled.connect(self._refresh_estimate)
            scope_layout.addWidget(radio)
        layout.addWidget(scope_box)

        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)

        # --- preview
        self.preview_button = QPushButton("👁 Xem thử 1 chương")
        self.preview_button.setToolTip(
            "Chạy đúng quy trình sẽ dùng cho cả truyện, nhưng chỉ một chương và KHÔNG ghi "
            "gì — để xem thử chất lượng trước khi tốn hàng giờ."
        )
        self.preview_button.clicked.connect(self._request_preview)
        self.apply_button = QPushButton("Áp dụng chương này")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply_preview)
        preview_row = QHBoxLayout()
        preview_row.addWidget(self.preview_button)
        preview_row.addWidget(self.apply_button)
        preview_row.addStretch(1)
        layout.addLayout(preview_row)

        self.before_view = QPlainTextEdit()
        self.after_view = QPlainTextEdit()
        for view, placeholder in (
            (self.before_view, "Hiện tại"),
            (self.after_view, "Sau khi viết lại"),
        ):
            view.setReadOnly(True)
            view.setPlaceholderText(placeholder)
        self.preview_split = QSplitter(Qt.Orientation.Horizontal)
        self.preview_split.addWidget(self.before_view)
        self.preview_split.addWidget(self.after_view)
        self.preview_split.setMinimumHeight(200)
        layout.addWidget(self.preview_split, stretch=1)

        # --- actions
        self.start_button = QPushButton("✍️ Bắt đầu viết lại")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self._request_start)
        self.undo_button = QPushButton("↩︎ Hoàn tác viết lại (toàn truyện)")
        self.undo_button.clicked.connect(self._request_undo)
        close_button = QPushButton("Đóng")
        close_button.clicked.connect(self.reject)
        action_row = QHBoxLayout()
        action_row.addWidget(self.start_button)
        action_row.addWidget(self.undo_button)
        action_row.addStretch(1)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)

        self._refresh_estimate()
        if self._blocked:
            self.estimate_label.setText(self._blocked)
            for widget in (
                self.engine_combo, self.model_edit, self.start_spin, self.end_spin,
                self.scope_pending, self.scope_all, self.preview_button,
                self.start_button, self.undo_button,
            ):
                widget.setEnabled(False)

    # ------------------------------------------------------------------ state

    def _eligible(self) -> list:
        """Exactly the chapters a run with the current choices would touch."""
        return chapters_to_rewrite(
            self.project,
            self._target_lang,
            start_idx=self.start_spin.value() - 1,
            end_idx=self.end_spin.value() - 1,
            force=self.scope_all.isChecked(),
        )

    def _choices(self) -> dict:
        return {
            "engine_name": self.engine_combo.currentData(),
            "model": self.model_edit.text().strip(),
            "target_lang": self._target_lang,
            "start_idx": self.start_spin.value() - 1,
            "end_idx": self.end_spin.value() - 1,
            "force": self.scope_all.isChecked(),
        }

    def _remember_choices(self) -> None:
        self.config.rewrite_ai_engine = self.engine_combo.currentData()
        self.config.rewrite_ai_model = self.model_edit.text()

    def _refresh_estimate(self, *_args) -> None:
        if self._blocked:
            return
        if self.end_spin.value() < self.start_spin.value():
            self.end_spin.setValue(self.start_spin.value())
            return  # the valueChanged this triggers re-runs us
        counts = self.project.counts()
        eligible = self._eligible()
        parts = [
            f"{counts['translated']} chương đã dịch, {counts['rewritten']} đã viết lại. "
            f"Lượt này: <b>{len(eligible)} chương</b>."
        ]
        if eligible:
            minutes = len(eligible) * 0.5  # CLI Agent runs ~30s per chapter
            duration = (
                f"{minutes / 60:.1f} giờ" if minutes >= 60 else f"{minutes:.0f} phút"
            )
            parts.append(
                f"Mỗi chương tốn ít nhất một yêu cầu tới AI — khoảng {duration} nếu dùng "
                "CLI Agent. Có thể tạm dừng và chạy tiếp sau."
            )
            with_audio = sum(1 for c in eligible if c.has_audio)
            if with_audio:
                parts.append(
                    f"⚠️ {with_audio} chương trong số này <b>đã có audio</b>. Viết lại chỉ "
                    "đổi chữ, file audio cũ vẫn giữ nguyên nên sẽ không còn khớp với bản "
                    "dịch — muốn khớp thì phải tạo lại audio cho những chương đó."
                )
        self.estimate_label.setText("<br>".join(parts))
        self.start_button.setEnabled(bool(eligible))
        self.undo_button.setEnabled(counts["rewritten"] > 0)

    # --------------------------------------------------------------- requests

    def _request_start(self) -> None:
        eligible = self._eligible()
        if not eligible:
            return
        self._remember_choices()
        self.start_requested.emit(self._choices())
        self.accept()

    def _preview_index(self) -> int | None:
        """The chapter to preview: the one open in the tab if it qualifies, else the first."""
        eligible = self._eligible()
        if not eligible:
            return None
        if self._preselected is not None:
            if any(c.index == self._preselected for c in eligible):
                return self._preselected
        return eligible[0].index

    def _request_preview(self) -> None:
        index = self._preview_index()
        if index is None:
            return
        chapter = self.project.chapter(index)
        if chapter is None:
            return
        self._preview = None
        self.apply_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.before_view.setPlainText(f"{chapter.translated_title}\n\n{chapter.translated}")
        self.after_view.setPlainText("Đang viết lại…")
        self._remember_choices()
        params = self._choices()
        params.update(indices=[index], dry_run=True)
        self.preview_requested.emit(params)

    def _request_undo(self) -> None:
        rewritten = self.project.counts()["rewritten"]
        if not rewritten:
            return
        answer = QMessageBox.question(
            self,
            "Hoàn tác viết lại?",
            f"Sẽ trả {rewritten} chương về bản dịch trước khi viết lại.\n\n"
            "Lưu ý: bản được trả lại là bản dịch tại thời điểm viết lại — những sửa tay "
            "hoặc tìm & thay thế bạn làm SAU lần viết lại sẽ mất.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.undo_requested.emit(None)
        self._refresh_estimate()

    # -------------------------------------------------- callbacks from the tab

    def show_preview(self, index: int, title: str, body: str) -> None:
        self._preview = (index, title, body)
        self.after_view.setPlainText(f"{title}\n\n{body}")
        self.apply_button.setEnabled(True)
        self.preview_button.setEnabled(True)

    def show_preview_error(self, _index: int, message: str) -> None:
        self._preview = None
        self.after_view.setPlainText(f"Không viết lại được chương này.\n\n{message}")
        self.apply_button.setEnabled(False)
        self.preview_button.setEnabled(True)

    def preview_finished(self) -> None:
        """Re-enable the button even when the run produced neither result nor error."""
        self.preview_button.setEnabled(True)

    def _apply_preview(self) -> None:
        if self._preview is None:
            return
        index, title, body = self._preview
        self.project.save_rewrite(index, title, body)
        self._preview = None
        self.apply_button.setEnabled(False)
        self.applied.emit({index})
        self._refresh_estimate()

"""Tìm & thay thế — the Dịch tab's find-and-replace dialog.

Literal (non-regex) substring replace across chapter text, guarded by a mandatory
preview: the user sees a match count and per-chapter breakdown before anything is
written. The counting/replacing logic lives in `noveltrans.find_replace`; this file is
only the Qt wiring. Being modal, it blocks the tab's preview-pane editing while open,
so a replace-all can't race the tab's save-on-blur handler.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QRadioButton,
    QVBoxLayout,
)

from noveltrans import find_replace
from noveltrans.find_replace import (
    FIELD_CONTENT,
    FIELD_TITLE,
    FIELD_TRANSLATED,
    FIELD_TRANSLATED_TITLE,
)
from noveltrans.storage import NovelProject


class FindReplaceDialog(QDialog):
    """Preview-then-apply find & replace. Emits `applied` with the changed indices."""

    applied = Signal(set)  # {chapter index, …} that were written

    def __init__(self, project: NovelProject, preview_idx: int | None, parent=None):
        super().__init__(parent)
        self.project = project
        self._preview_idx = preview_idx
        self._matches: list = []  # cached scan result; applied verbatim

        self.setWindowTitle("Tìm & thay thế")
        self.setMinimumWidth(460)

        form = QFormLayout()
        self.search_edit = QLineEdit()
        self.replace_edit = QLineEdit()
        form.addRow("Tìm:", self.search_edit)
        form.addRow("Thay bằng:", self.replace_edit)

        self.case_check = QCheckBox("Phân biệt hoa/thường")
        form.addRow("", self.case_check)

        # Scope: the previewed chapter vs. the whole project.
        self.scope_current = QRadioButton("Chương hiện tại")
        self.scope_all = QRadioButton("Tất cả chương")
        if preview_idx is None:
            self.scope_current.setEnabled(False)
            self.scope_all.setChecked(True)
        else:
            self.scope_current.setChecked(True)
        scope_row = QHBoxLayout()
        scope_row.addWidget(self.scope_current)
        scope_row.addWidget(self.scope_all)
        form.addRow("Phạm vi:", scope_row)

        # Fields. Translated body + title default on; the two original-side fields are
        # opt-in (the motivating use case is fixing the translated output).
        self.field_translated = QCheckBox("Bản dịch")
        self.field_translated_title = QCheckBox("Tên chương dịch")
        self.field_content = QCheckBox("Bản gốc")
        self.field_title = QCheckBox("Tên chương gốc")
        self.field_translated.setChecked(True)
        self.field_translated_title.setChecked(True)
        self._field_checks = {
            FIELD_TRANSLATED: self.field_translated,
            FIELD_TRANSLATED_TITLE: self.field_translated_title,
            FIELD_CONTENT: self.field_content,
            FIELD_TITLE: self.field_title,
        }
        fields_row = QVBoxLayout()
        for check in self._field_checks.values():
            fields_row.addWidget(check)
        form.addRow("Áp dụng cho:", fields_row)

        # The original title is the one field replace_toc reverts on re-scan.
        self.title_warning = QLabel(
            "⚠️ Thay thế trong “Tên chương gốc” sẽ bị ghi đè khi quét lại mục lục."
        )
        self.title_warning.setProperty("muted", True)
        self.title_warning.setWordWrap(True)
        self.title_warning.setVisible(False)
        form.addRow("", self.title_warning)

        self.summary_label = QLabel("Nhập từ khoá rồi bấm “Xem trước”.")
        self.summary_label.setWordWrap(True)
        self.breakdown = QListWidget()

        buttons = QDialogButtonBox()
        self.preview_button = buttons.addButton(
            "Xem trước", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.apply_button = buttons.addButton(
            "Thay thế", QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.addButton("Đóng", QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_button.setProperty("primary", True)
        self.apply_button.setEnabled(False)  # gated behind a fresh preview with matches

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.breakdown, stretch=1)
        layout.addWidget(buttons)

        self.preview_button.clicked.connect(self._preview)
        self.apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)

        # Any change to the inputs invalidates a prior preview — the applied change must
        # always equal what was shown.
        self.search_edit.textChanged.connect(self._invalidate)
        self.replace_edit.textChanged.connect(self._invalidate)
        self.case_check.toggled.connect(self._invalidate)
        self.scope_current.toggled.connect(self._invalidate)
        self.scope_all.toggled.connect(self._invalidate)
        for check in self._field_checks.values():
            check.toggled.connect(self._invalidate)
        self.field_title.toggled.connect(self.title_warning.setVisible)

    # -- helpers -----------------------------------------------------------------

    def _selected_fields(self) -> list[str]:
        return [field for field, check in self._field_checks.items() if check.isChecked()]

    def _target_chapters(self) -> list:
        if self.scope_all.isChecked() or self._preview_idx is None:
            return self.project.chapters()
        chapter = self.project.chapter(self._preview_idx)
        return [chapter] if chapter is not None else []

    def _invalidate(self) -> None:
        """A stale preview must never be applied."""
        self._matches = []
        self.apply_button.setEnabled(False)
        self.breakdown.clear()
        self.summary_label.setText("Nhập từ khoá rồi bấm “Xem trước”.")

    # -- actions -----------------------------------------------------------------

    def _preview(self) -> None:
        search = self.search_edit.text()
        fields = self._selected_fields()
        if not search:
            self.summary_label.setText("Hãy nhập từ khoá cần tìm.")
            return
        if not fields:
            self.summary_label.setText("Hãy chọn ít nhất một mục để áp dụng.")
            return

        self._matches = find_replace.scan(
            self._target_chapters(),
            search,
            self.replace_edit.text(),
            fields,
            case_sensitive=self.case_check.isChecked(),
        )
        self.breakdown.clear()
        total = find_replace.total_matches(self._matches)
        if total == 0:
            self.summary_label.setText("Không tìm thấy kết quả nào.")
            self.apply_button.setEnabled(False)
            return

        chapters = find_replace.chapter_count(self._matches)
        self.summary_label.setText(f"{total} khớp trong {chapters} chương.")
        for match in self._matches:
            self.breakdown.addItem(f"{match.label}: {match.count} khớp")
        self.apply_button.setEnabled(True)

    def _apply(self) -> None:
        if not self._matches:  # apply is gated, but guard anyway
            return
        changes = {
            match.index: {change.field: change.new for change in match.changes}
            for match in self._matches
        }
        self.project.apply_replacements(changes)
        self.applied.emit(set(changes))
        self.accept()

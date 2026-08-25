"""Tìm & thay thế — the Dịch tab's find-and-replace dialog.

Literal (non-regex) substring replace across chapter text, guarded by a mandatory
preview: the user sees a match count and per-chapter breakdown before anything is
written. The counting/replacing logic lives in `noveltrans.find_replace`; this file is
only the Qt wiring.

Modeless on purpose: double-clicking a row in the breakdown jumps the tab to that
chapter and selects the match, so not every hit has to be fixed by a blanket replace —
some want a hand edit of the sentence around them, which a modal dialog would block.
The price is that chapter text can now change underneath a preview, so `_apply` re-reads
every scanned field first and refuses rather than writing pre-computed text over an edit
the user made in between.
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
    # A row was double-clicked: (chapter index, field id, search text, case sensitive).
    # The search text travels with it so the receiver can re-find the hit in the pane,
    # where the text is laid out as "title\n\nbody" and a raw field offset would be wrong.
    chapter_activated = Signal(int, str, str, bool)

    def __init__(self, project: NovelProject, preview_idx: int | None, parent=None):
        super().__init__(parent)
        self.project = project
        self._preview_idx = preview_idx
        self._matches: list = []  # cached scan result; applied verbatim

        self.setWindowTitle("Tìm & thay thế")
        self.setMinimumWidth(460)
        # Modeless: the tab behind stays usable, which is the whole point of the
        # double-click jump. See the module docstring for what that costs `_apply`.
        self.setModal(False)

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
        self.breakdown.setToolTip(
            "Nháy đúp một chương để mở chương đó và nhảy tới chỗ khớp đầu tiên "
            "(sửa tay ngay trong ô bên dưới bảng)."
        )
        self.breakdown.itemDoubleClicked.connect(self._on_breakdown_activated)

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

    def set_current_chapter(self, index: int | None) -> None:
        """Follow the tab's open chapter — modeless, so it moves while we are up.

        “Chương hiện tại” has to mean the chapter the user is actually looking at, and a
        preview taken under that scope describes a different chapter once it moves — so
        it is dropped, exactly as toggling the radio would.
        """
        if index == self._preview_idx:
            return
        self._preview_idx = index
        self.scope_current.setEnabled(index is not None)
        if index is None and self.scope_current.isChecked():
            self.scope_all.setChecked(True)  # invalidates via the toggled signal
        elif self.scope_current.isChecked():
            self._invalidate()

    def _on_breakdown_activated(self, item) -> None:
        """Double-click: ask the tab to open this chapter and select the match.

        Rows are added 1:1 with `_matches` in `_preview`, so the row index IS the match.
        The field is the first one that matched, and `_selected_fields` orders them the
        way the checkboxes do — translated body before its title, original last — which
        is the pane the user most likely wants to be typing in.
        """
        row = self.breakdown.row(item)
        if not 0 <= row < len(self._matches):
            return
        match = self._matches[row]
        self.chapter_activated.emit(
            match.index,
            match.changes[0].field,
            self.search_edit.text(),
            self.case_check.isChecked(),
        )

    def _is_stale(self) -> bool:
        """True if any scanned field no longer holds the text the preview was built on.

        The guard that makes a modeless dialog safe: `_apply` writes `change.new`, which
        was computed from `change.old`. If the user (or a worker) has touched the chapter
        since, that pre-computed text would silently undo their edit.
        """
        for match in self._matches:
            chapter = self.project.chapter(match.index)
            if chapter is None:
                return True
            if any(
                (getattr(chapter, change.field, "") or "") != change.old
                for change in match.changes
            ):
                return True
        return False

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
        self.summary_label.setText(
            f"{total} khớp trong {chapters} chương. "
            "Nháy đúp một chương để mở và sửa tay chỗ khớp."
        )
        for match in self._matches:
            self.breakdown.addItem(f"{match.label}: {match.count} khớp")
        self.apply_button.setEnabled(True)

    def _apply(self) -> None:
        if not self._matches:  # apply is gated, but guard anyway
            return
        if self._is_stale():
            self._invalidate()
            self.summary_label.setText(
                "Nội dung đã thay đổi kể từ lần xem trước — hãy bấm “Xem trước” lại."
            )
            return
        changes = {
            match.index: {change.field: change.new for change in match.changes}
            for match in self._matches
        }
        self.project.apply_replacements(changes)
        self.applied.emit(set(changes))
        self.accept()

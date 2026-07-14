"""Tab 3 — Export: pick a novel and a format; write DOCX / Markdown / EPUB."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.exporters import EXPORTER_NAMES, get_exporter
from noveltrans.gui.widgets import ProjectPicker
from noveltrans.gui.workers import ExportWorker
from noveltrans.models import NovelMeta
from noveltrans.storage import NovelProject
from noveltrans.storage.project import slugify


def default_export_name(meta: NovelMeta, use_translation: bool, extension: str) -> str:
    """Default save-dialog filename — the translated title slugs far better than CJK."""
    title = meta.translated_title if use_translation and meta.translated_title else meta.title
    return slugify(title) + extension


class ExportTab(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._worker: ExportWorker | None = None
        self._last_export: str = ""

        # --- novel picker
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._on_project_selected)
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Truyện:"))
        picker_row.addWidget(self.picker, stretch=1)

        # --- options
        self.format_combo = QComboBox()
        for key, label in EXPORTER_NAMES.items():
            self.format_combo.addItem(label, key)

        self.translated_radio = QRadioButton("Bản dịch")
        self.translated_radio.setChecked(True)
        self.original_radio = QRadioButton("Bản gốc")
        lang_row = QHBoxLayout()
        lang_row.addWidget(self.translated_radio)
        lang_row.addWidget(self.original_radio)
        lang_row.addStretch()

        options_box = QGroupBox("Tùy chọn xuất")
        form = QFormLayout(options_box)
        form.addRow("Định dạng:", self.format_combo)
        form.addRow("Nội dung:", lang_row)

        # --- summary + actions
        self.summary_label = QLabel("—")
        self.summary_label.setWordWrap(True)

        self.export_button = QPushButton("Xuất file…")
        self.export_button.setProperty("primary", True)
        self.export_button.clicked.connect(self._start_export)
        self.open_button = QPushButton("Mở thư mục")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_folder)
        action_row = QHBoxLayout()
        action_row.addWidget(self.export_button)
        action_row.addWidget(self.open_button)
        action_row.addStretch()

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(picker_row)
        layout.addWidget(options_box)
        layout.addWidget(self.summary_label)
        layout.addLayout(action_row)
        layout.addWidget(self.status_label)
        layout.addStretch()

    # -------------------------------------------------------------- projects

    def refresh_projects(self, select_path: str = "") -> None:
        self.picker.refresh(self.config.library_dir, select_path)

    def showEvent(self, event) -> None:
        if self._worker is None or not self._worker.isRunning():
            self.refresh_projects()
        super().showEvent(event)

    def _on_project_selected(self, path: str) -> None:
        if self.project is not None:
            self.project.close()
            self.project = None
        if path:
            self.project = NovelProject.open(path)
            counts = self.project.counts()
            self.summary_label.setText(
                f"{counts['total']} chương — {counts['downloaded']} đã tải, "
                f"{counts['translated']} đã dịch."
            )
        else:
            self.summary_label.setText("—")

    # ---------------------------------------------------------------- export

    def _start_export(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        use_translation = self.translated_radio.isChecked()
        counts = self.project.counts()
        available = counts["translated"] if use_translation else counts["downloaded"]
        if available == 0:
            QMessageBox.warning(
                self,
                "Chưa có nội dung",
                "Chưa có chương nào "
                + (
                    "đã dịch (hãy dịch ở Tab 2)."
                    if use_translation
                    else "đã tải (hãy tải ở Tab 1)."
                ),
            )
            return
        if available < counts["total"]:
            answer = QMessageBox.question(
                self,
                "Xuất thiếu chương?",
                f"Chỉ {available}/{counts['total']} chương có nội dung. Vẫn xuất chứ?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        exporter = get_exporter(self.format_combo.currentData())
        self.project.reload_meta()  # pick up a title translated after this tab opened
        default_name = default_export_name(self.project.meta, use_translation, exporter.extension)
        out_path, _selected = QFileDialog.getSaveFileName(
            self,
            "Lưu file",
            str(self.project.exports_dir / default_name),
            f"{exporter.display_name} (*{exporter.extension})",
        )
        if not out_path:
            return

        self.export_button.setEnabled(False)
        self.status_label.setText("Đang xuất…")
        self._worker = ExportWorker(
            self.project.path, exporter.name, Path(out_path), use_translation
        )
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_finished(self, path: str) -> None:
        self.export_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self._last_export = path
        self.status_label.setText(f"Đã xuất: {path}")

    def _on_failed(self, message: str) -> None:
        self.export_button.setEnabled(True)
        self.status_label.setText("")
        QMessageBox.warning(self, "Xuất thất bại", message)

    def _open_folder(self) -> None:
        if self._last_export:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._last_export).parent)))

    def has_running_workers(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(30_000)

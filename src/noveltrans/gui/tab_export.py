"""Tab 3 — Export: pick a novel and a format; write DOCX / Markdown / EPUB.

Also hosts the OneDrive backup (feature 051). It lives here rather than in a sixth tab
because "everything this novel produced, out of the app" is already what this tab means —
and unlike the Video tab's YouTube actions, a whole-project push is not a per-part action.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.exporters import EXPORTER_NAMES, get_exporter
from noveltrans.gui.jobs import job_registry
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import PauseButton, ProjectPicker
from noveltrans.gui.workers import ExportWorker, OneDrivePushWorker
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
        self._push_worker: OneDrivePushWorker | None = None
        self._push_job = None
        self._push_total = 0

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

        self.number_checkbox = QCheckBox("Thêm số chương vào tiêu đề")
        self.number_checkbox.setToolTip(
            "Đặt tiêu đề mỗi chương thành “Chương 1: <tên chương>”, “Chương 2: …” — "
            "thêm số thứ tự vào trước tên chương đã tải."
        )

        options_box = QGroupBox("Tùy chọn xuất")
        form = QFormLayout(options_box)
        form.addRow("Định dạng:", self.format_combo)
        form.addRow("Nội dung:", lang_row)
        form.addRow("Tiêu đề chương:", self.number_checkbox)

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
        layout.addWidget(self._build_onedrive_box())
        layout.addStretch()

    # -------------------------------------------------------------- OneDrive

    def _build_onedrive_box(self) -> QGroupBox:
        box = QGroupBox("Sao lưu OneDrive")

        self.push_button = QPushButton("Đẩy lên OneDrive")
        self.push_button.setToolTip(
            "Sao chép toàn bộ thư mục truyện (meta.json, chapters.db, exports/ với audio, "
            "video và các file đi kèm) lên /NovelTrans/<tên truyện>/ trên OneDrive."
        )
        self.push_button.clicked.connect(self._start_push)

        self.push_pause_button = PauseButton()
        self.push_pause_button.set_extra_hint(
            "Tạm dừng giữa các đợt file — đợt đang gửi vẫn chạy nốt, và cửa sổ Chrome "
            "vẫn mở."
        )

        self.push_cancel_button = QPushButton("Dừng")
        self.push_cancel_button.setEnabled(False)
        self.push_cancel_button.clicked.connect(self._cancel_push)

        self.push_forget_button = QPushButton("Quên trạng thái")
        self.push_forget_button.setToolTip(
            "Xoá ghi chép “đã tải lên những file nào”. Lần sau sẽ tải lại toàn bộ."
        )
        self.push_forget_button.clicked.connect(self._forget_push_state)

        # Cleanup lives beside the backup because it depends on it: a part-video may only
        # be deleted once OneDrive is confirmed to hold it.
        self.cleanup_button = QPushButton("Dọn dẹp ổ đĩa…")
        self.cleanup_button.setToolTip(
            "Xoá audio đã nằm trong video, và video phần đã đăng YouTube + đã có trên "
            "OneDrive. Xem trước và tự chọn trước khi xoá."
        )
        self.cleanup_button.clicked.connect(self._open_cleanup)

        push_row = QHBoxLayout()
        push_row.addWidget(self.push_button)
        push_row.addWidget(self.push_pause_button)
        push_row.addWidget(self.push_cancel_button)
        push_row.addWidget(self.push_forget_button)
        push_row.addWidget(self.cleanup_button)
        push_row.addStretch()

        self.push_progress = QProgressBar()
        self.push_progress.setVisible(False)
        self.push_status = QLabel("")
        self.push_status.setWordWrap(True)

        hint = QLabel(
            "Sao lưu một chiều: file trùng tên trên OneDrive sẽ bị ghi đè bằng bản trên "
            "máy. Những file không đổi từ lần trước được bỏ qua. Cần đăng nhập một lần ở "
            "Settings → “Đăng nhập OneDrive”."
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)

        inner = QVBoxLayout(box)
        inner.addLayout(push_row)
        inner.addWidget(self.push_progress)
        inner.addWidget(self.push_status)
        inner.addWidget(hint)
        return box

    def _push_request(self, *, force: bool):
        from noveltrans.onedrive_upload import PushRequest

        self.project.reload_meta()  # a title translated since this tab opened
        meta = self.project.meta
        return PushRequest(
            project_path=self.project.path,
            novel_title=meta.translated_title or meta.title,
            force=force,
            root_folder=self.config.onedrive_root,
        )

    def _start_push(self) -> None:
        """Work out what would be sent, show it, and only then open a browser."""
        from noveltrans.onedrive_upload import format_size, preview_push

        if self.project is None:
            QMessageBox.information(
                self, "Chưa chọn truyện", "Hãy chọn một truyện trước khi sao lưu."
            )
            return
        if self.has_running_workers():
            QMessageBox.information(
                self, "Đang bận", "Đang có việc chạy ở tab này — chờ xong rồi thử lại."
            )
            return

        try:
            preview = preview_push(self._push_request(force=False))
        except Exception as exc:  # a corrupt db, an unreadable folder
            QMessageBox.warning(self, "Sao lưu OneDrive", f"Không đọc được dữ liệu: {exc}")
            return

        if not preview.to_upload:
            QMessageBox.information(
                self,
                "Sao lưu OneDrive",
                f"Mọi thứ đã có trên OneDrive rồi ({len(preview.to_skip)} file, "
                f"{format_size(preview.skip_bytes)}).\n\nNếu muốn tải lại toàn bộ, bấm "
                "“Quên trạng thái” trước.",
            )
            return

        # The counts are the point: 12 files / 4 GB and 3 200 files / 61 GB are a coffee
        # break and an overnight run, and nothing else on screen says which one this is.
        lines = [
            f"Đích: {preview.remote_root}",
            f"Sẽ tải lên: {len(preview.to_upload)} file "
            f"({format_size(preview.upload_bytes)})",
        ]
        if preview.to_skip:
            lines.append(
                f"Bỏ qua (không đổi): {len(preview.to_skip)} file "
                f"({format_size(preview.skip_bytes)})"
            )
        if preview.manifest_note:
            lines.append(f"⚠️ {preview.manifest_note} — sẽ tải lại từ đầu.")
        if preview.root_note:
            lines.append(f"ℹ️ {preview.root_note}")
        lines.append("")
        lines.append("Các file trùng tên trên OneDrive sẽ bị GHI ĐÈ bằng bản trên máy.")
        lines.append("Tiếp tục?")

        if (
            QMessageBox.question(self, "Sao lưu OneDrive", "\n".join(lines))
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._push_total = len(preview.to_upload)
        self.push_progress.setRange(0, self._push_total)
        self.push_progress.setValue(0)
        self.push_progress.setVisible(True)
        self.push_status.setText(f"Chuẩn bị tải lên {preview.remote_root}…")
        self._set_push_running(True)

        self._push_worker = OneDrivePushWorker(self._push_request(force=False))
        self._push_worker.progress.connect(self._on_push_progress)
        self._push_worker.finished_ok.connect(self._on_push_finished)
        self._push_worker.failed.connect(self._on_push_failed)
        self._push_worker.needs_login.connect(self._on_push_needs_login)
        track_worker(self._push_worker)  # a 60 GB push must survive an idle Mac
        self._push_job = job_registry.register(
            self._push_worker, kind="Sao lưu OneDrive", novel=self._job_novel()
        )
        self.push_pause_button.set_job(self._push_job.id if self._push_job else None)
        self._push_worker.start()

    def _open_cleanup(self) -> None:
        """Show what can be deleted. Refused while a push runs — the two touch the same
        files, and deleting one mid-upload would leave a half-mirrored folder."""
        from noveltrans.gui.cleanup_dialog import CleanupDialog

        if self.project is None:
            QMessageBox.information(
                self, "Chưa chọn truyện", "Hãy chọn một truyện trước khi dọn dẹp."
            )
            return
        if self.has_running_workers():
            QMessageBox.information(
                self,
                "Đang bận",
                "Đang có việc chạy ở tab này — chờ xong rồi mới dọn dẹp được.",
            )
            return
        dialog = CleanupDialog(self.project.path, self._job_novel(), self)
        dialog.exec()
        if dialog.freed:
            from noveltrans.onedrive_upload import format_size

            self.push_status.setText(
                f"🧹 Đã dọn {format_size(dialog.freed)} khỏi ổ đĩa."
            )

    def _job_novel(self) -> str:
        if self.project is None:
            return ""
        meta = self.project.meta
        return meta.translated_title or meta.title

    def _set_push_running(self, running: bool) -> None:
        """One switch for the whole group, so the two actions can never overlap.

        The export button goes with it: both write into the same project folder, and a
        push that reads a .docx mid-write would mirror half a file.
        """
        self.push_button.setEnabled(not running)
        self.push_forget_button.setEnabled(not running)
        self.push_cancel_button.setEnabled(running)
        self.export_button.setEnabled(not running)
        self.cleanup_button.setEnabled(not running)

    def _on_push_progress(self, done: int, total: int, message: str) -> None:
        if total and total != self.push_progress.maximum():
            self.push_progress.setRange(0, total)
        self.push_progress.setValue(done)
        if message:
            self.push_status.setText(message)

    def _on_push_finished(self, uploaded: int, skipped: int, failed: int) -> None:
        self._reset_push_ui()
        parts = [f"{uploaded} file đã tải lên"]
        if skipped:
            parts.append(f"{skipped} bỏ qua (không đổi)")
        if failed:
            parts.append(f"{failed} lỗi")
        summary = ", ".join(parts) + "."
        self.push_status.setText(f"✅ {summary}")
        if failed:
            QMessageBox.warning(
                self,
                "Sao lưu OneDrive",
                f"{summary}\n\nChạy lại để thử tiếp những file lỗi — những file đã lên "
                "sẽ được bỏ qua.",
            )

    def _on_push_failed(self, message: str) -> None:
        self._reset_push_ui()
        self.push_status.setText("")
        QMessageBox.warning(self, "Sao lưu OneDrive", message)

    def _on_push_needs_login(self, message: str) -> None:
        """"Sign in once" is actionable; it must not read as "something broke"."""
        self._reset_push_ui()
        self.push_status.setText("")
        QMessageBox.information(
            self,
            "Chưa đăng nhập OneDrive",
            f"{message}\n\nVào Settings → “Đăng nhập OneDrive” để đăng nhập một lần, "
            "rồi thử lại.",
        )

    def _reset_push_ui(self) -> None:
        self._set_push_running(False)
        self.push_progress.setVisible(False)
        self.push_pause_button.set_job(None)

    def _cancel_push(self) -> None:
        if self._push_worker is not None and self._push_worker.isRunning():
            self.push_status.setText("Đang dừng…")
            self.push_cancel_button.setEnabled(False)
            self._push_worker.cancel()

    def _forget_push_state(self) -> None:
        """Drop the manifest so the next push re-sends everything.

        Deliberately *not* wrapped in the kind of warning `clear_upload_state` needs. This
        cannot create a duplicate or publish anything — the worst it costs is a re-upload,
        and saying so plainly is the point. A warning that isn't warranted trains people
        to ignore the ones that are.
        """
        from noveltrans.onedrive_upload import clear_manifest, format_size, preview_push

        if self.project is None:
            return
        try:
            size = preview_push(self._push_request(force=True)).upload_bytes
        except Exception:
            size = 0  # advisory only — a number we can't work out must not block the fix

        detail = f" (khoảng {format_size(size)})" if size else ""
        if (
            QMessageBox.question(
                self,
                "Quên trạng thái OneDrive",
                f"Xoá ghi chép “đã tải lên những file nào”. Lần sao lưu sau sẽ tải lại "
                f"toàn bộ{detail}.\n\nKhông có gì trên OneDrive bị xoá. Tiếp tục?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if clear_manifest(self.project.path):
            self.push_status.setText("Đã quên trạng thái — lần sau sẽ tải lại toàn bộ.")
        else:
            self.push_status.setText("Chưa có trạng thái nào để quên.")

    # -------------------------------------------------------------- projects

    def refresh_projects(self, select_path: str = "") -> None:
        self.picker.refresh(self.config.library_dir, select_path)

    def showEvent(self, event) -> None:
        # Never re-pick the project out from under a running job — including a push,
        # which can run for hours while the user works in other tabs.
        if not self.has_running_workers():
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
            self.project.path,
            exporter.name,
            Path(out_path),
            use_translation,
            number_chapters=self.number_checkbox.isChecked(),
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
        return any(
            worker is not None and worker.isRunning()
            for worker in (self._worker, self._push_worker)
        )

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(30_000)
        # Cancel before waiting: a push can be hours from finishing on its own, and it
        # owns a Chrome process that would outlive the app if the thread were abandoned.
        if self._push_worker is not None and self._push_worker.isRunning():
            self._push_worker.cancel()
            self._push_worker.wait(120_000)

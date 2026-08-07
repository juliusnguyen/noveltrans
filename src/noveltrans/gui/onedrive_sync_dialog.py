"""Back several novels up to OneDrive in one go: pick them, then watch them go.

Two dialogs, because they answer two different questions and take very different amounts
of time:

* `OneDriveSyncPickerDialog` — modal and quick. Scans the library (no browser: it only
  reads files) and shows what each novel would send, so the choice is made against real
  numbers rather than a guess. Novels already fully mirrored are shown and pre-unticked;
  hiding them would leave the user wondering whether they were missed.
* `OneDriveSyncWindow` — modeless and slow. Owns the run. Modeless because a library sync
  can take hours and a modal dialog would lock the whole app for the duration; it
  registers with `job_registry`, so the menu-bar popup shows it alongside every other
  long job and can pause it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from noveltrans.gui.jobs import job_registry
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import PauseButton
from noveltrans.gui.workers import OneDriveSyncScanWorker, OneDriveSyncWorker
from noveltrans.onedrive_upload import PushRequest, format_size


class OneDriveSyncPickerDialog(QDialog):
    """Tick the novels to back up. `requests` holds the chosen ones after `accept()`."""

    def __init__(self, library_dir, root_folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Đồng bộ lên OneDrive")
        self.resize(560, 460)
        self.root_folder = root_folder
        self.requests: list[PushRequest] = []
        self._rows: list[dict] = []
        self._worker: OneDriveSyncScanWorker | None = None

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Truyện", "Sẽ tải lên", "Dung lượng"])
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 300)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.itemChanged.connect(self._on_tick)

        self.scan_progress = QProgressBar()
        self.status = QLabel("Đang xem thư viện…")
        self.status.setWordWrap(True)
        self.total_label = QLabel("")

        select_all = QPushButton("Chọn tất cả")
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none = QPushButton("Bỏ chọn")
        select_none.clicked.connect(lambda: self._set_all(False))
        row = QHBoxLayout()
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Bắt đầu đồng bộ")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Đích: {root_folder} — mỗi truyện một thư mục con."))
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(row)
        layout.addWidget(self.scan_progress)
        layout.addWidget(self.total_label)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self._start_scan(library_dir)

    # ------------------------------------------------------------------ scan

    def _start_scan(self, library_dir) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._worker = OneDriveSyncScanWorker(library_dir, self.root_folder, self)
        self._worker.scanned.connect(self._on_scanned)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.finished_ok.connect(self._on_scan_done)
        self._worker.start()

    def _on_scan_progress(self, done: int, total: int, title: str) -> None:
        self.scan_progress.setRange(0, max(total, 1))
        self.scan_progress.setValue(done)
        if title:
            self.status.setText(f"Đang xem “{title}”… ({done + 1}/{total})")

    def _on_scanned(self, path: str, title: str, files: int, size: int, error: str) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)

        name_item = QTableWidgetItem(title)
        name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        # Pre-ticked only when there is something to send. A novel already mirrored is
        # still listed — hiding it would leave the user wondering whether it was missed.
        eligible = bool(files) and not error
        name_item.setCheckState(
            Qt.CheckState.Checked if eligible else Qt.CheckState.Unchecked
        )
        if not eligible:
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        self.table.setItem(index, 0, name_item)

        if error:
            detail, amount = "không đọc được", error
        elif files:
            detail, amount = f"{files} file", format_size(size)
        else:
            detail, amount = "đã đồng bộ", "—"
        self.table.setItem(index, 1, QTableWidgetItem(detail))
        self.table.setItem(index, 2, QTableWidgetItem(amount))

        self._rows.append(
            {"path": path, "title": title, "files": files, "size": size, "error": error}
        )
        self._refresh_total()

    def _on_scan_done(self) -> None:
        self.scan_progress.setVisible(False)
        pending = [r for r in self._rows if r["files"] and not r["error"]]
        if not self._rows:
            self.status.setText("Thư viện chưa có truyện nào.")
        elif not pending:
            self.status.setText("Mọi truyện đã có trên OneDrive rồi — không có gì để gửi.")
        else:
            self.status.setText("")
        self._refresh_total()

    # ----------------------------------------------------------------- ticks

    def _set_all(self, checked: bool) -> None:
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )

    def _on_tick(self, _item) -> None:
        self._refresh_total()

    def _ticked(self) -> list[dict]:
        out = []
        for index, row in enumerate(self._rows):
            item = self.table.item(index, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(row)
        return out

    def _refresh_total(self) -> None:
        ticked = self._ticked()
        files = sum(r["files"] for r in ticked)
        size = sum(r["size"] for r in ticked)
        self.total_label.setText(
            f"Đã chọn {len(ticked)} truyện — {files} file, {format_size(size)}."
            if ticked
            else "Chưa chọn truyện nào."
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(ticked))

    # ------------------------------------------------------------- lifecycle

    def accept(self) -> None:
        from pathlib import Path

        self.requests = [
            PushRequest(
                project_path=Path(row["path"]),
                novel_title=row["title"],
                root_folder=self.root_folder,
            )
            for row in self._ticked()
        ]
        self._stop_scan()
        super().accept()

    def reject(self) -> None:
        self._stop_scan()
        super().reject()

    def _stop_scan(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(20_000)


class OneDriveSyncWindow(QWidget):
    """Runs a multi-novel sync, modeless, so the app stays usable for the hours it takes."""

    def __init__(self, requests: list, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Đang đồng bộ OneDrive")
        self.resize(520, 320)
        self._requests = list(requests)
        self._job = None

        self.progress = QProgressBar()
        self.progress.setRange(0, len(self._requests))
        self.status = QLabel("Đang bắt đầu…")
        self.status.setWordWrap(True)
        self.log = QTableWidget(0, 2)
        self.log.setHorizontalHeaderLabels(["Truyện", "Kết quả"])
        self.log.verticalHeader().setVisible(False)
        self.log.setColumnWidth(0, 240)
        # The result column carries the failure messages, which are sentences. A fixed
        # width truncates them to "OneDrive …", which is the one thing the user needs.
        self.log.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )

        self.pause_button = PauseButton()
        self.pause_button.set_extra_hint(
            "Tạm dừng giữa các đợt file — đợt đang gửi vẫn chạy nốt."
        )
        self.stop_button = QPushButton("Dừng")
        self.stop_button.clicked.connect(self._stop)
        self.close_button = QPushButton("Đóng")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self.pause_button)
        row.addWidget(self.stop_button)
        row.addStretch()
        row.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.log, stretch=1)
        layout.addLayout(row)

        self._worker = OneDriveSyncWorker(self._requests, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.novel_done.connect(self._on_novel_done)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.needs_login.connect(self._on_needs_login)
        track_worker(self._worker)  # a library sync must survive an idle Mac
        self._job = job_registry.register(
            self._worker, kind="Đồng bộ OneDrive", novel=f"{len(self._requests)} truyện"
        )
        self.pause_button.set_job(self._job.id if self._job else None)
        self._worker.start()

    def _on_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setValue(done)
        if message:
            self.status.setText(message)

    def _on_novel_done(
        self, title: str, uploaded: int, skipped: int, failed: int, error: str
    ) -> None:
        index = self.log.rowCount()
        self.log.insertRow(index)
        self.log.setItem(index, 0, QTableWidgetItem(title))
        if error:
            outcome = f"⚠️ {error}"
        else:
            parts = [f"{uploaded} file"]
            if skipped:
                parts.append(f"{skipped} bỏ qua")
            if failed:
                parts.append(f"{failed} lỗi")
            outcome = ("⚠️ " if failed else "✅ ") + ", ".join(parts)
        self.log.setItem(index, 1, QTableWidgetItem(outcome))

    def _done(self, message: str) -> None:
        self.status.setText(message)
        self.stop_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.pause_button.set_job(None)

    def _on_finished(self, synced: int, errors: int) -> None:
        summary = f"✅ Xong: {synced} truyện đã sao lưu"
        if errors:
            summary += f", {errors} truyện lỗi (chạy lại để thử tiếp)"
        self._done(summary + ".")

    def _on_failed(self, message: str) -> None:
        self._done(f"⚠️ {message}")

    def _on_needs_login(self, message: str) -> None:
        self._done(
            f"⚠️ {message} Vào Settings → “Đăng nhập OneDrive” rồi chạy lại."
        )

    def _stop(self) -> None:
        self.status.setText("Đang dừng…")
        self.stop_button.setEnabled(False)
        self._worker.cancel()

    def closeEvent(self, event) -> None:
        """Closing the window must not abandon a live browser."""
        if self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(120_000)
        super().closeEvent(event)

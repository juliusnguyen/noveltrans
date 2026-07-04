"""Tab 1 — Scrape / Download: URL -> scan metadata + TOC -> download chapters."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.gui.widgets import ChapterTableModel, ProjectPicker
from noveltrans.gui.workers import DownloadWorker, ScanWorker
from noveltrans.storage import NovelProject


class ScrapeTab(QWidget):
    project_changed = Signal(str)  # project path — other tabs refresh their pickers

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._scan_worker: ScanWorker | None = None
        self._download_worker: DownloadWorker | None = None

        # --- recent projects row: continue a novel without pasting its URL
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._load_project)
        recent_row = QHBoxLayout()
        recent_row.addWidget(QLabel("Truyện gần đây:"))
        recent_row.addWidget(self.picker, stretch=1)

        # --- URL row
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Dán URL trang tiểu thuyết, ví dụ: https://www.xbanxia.cc/books/331303.html")
        self.scan_button = QPushButton("Quét")
        self.scan_button.clicked.connect(self._start_scan)
        self.url_edit.returnPressed.connect(self._start_scan)
        url_row = QHBoxLayout()
        url_row.addWidget(self.url_edit, stretch=1)
        url_row.addWidget(self.scan_button)

        # --- metadata panel
        self.title_label = QLabel("—")
        self.author_label = QLabel("—")
        self.count_label = QLabel("—")
        self.desc_label = QLabel("—")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMaximumHeight(90)
        meta_box = QGroupBox("Thông tin truyện")
        meta_form = QFormLayout(meta_box)
        meta_form.addRow("Tên truyện:", self.title_label)
        meta_form.addRow("Tác giả:", self.author_label)
        meta_form.addRow("Số chương:", self.count_label)
        meta_form.addRow("Mô tả:", self.desc_label)

        # --- chapter table
        self.model = ChapterTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)

        # --- download row
        self.download_button = QPushButton("Tải các chương")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._start_download)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_download)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m chương")
        self.status_label = QLabel("")
        download_row = QHBoxLayout()
        download_row.addWidget(self.download_button)
        download_row.addWidget(self.cancel_button)
        download_row.addWidget(self.progress, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(recent_row)
        layout.addLayout(url_row)
        layout.addWidget(meta_box)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(download_row)
        layout.addWidget(self.status_label)

    # --------------------------------------------------------- load existing

    def refresh_recent(self, select_path: str = "") -> None:
        """Re-list library projects in the picker; optionally select one."""
        self.picker.refresh(self.config.library_dir, select_path)

    def _load_project(self, path: str) -> None:
        """Open an existing project (no network) so work can just continue."""
        if not path:
            return
        if self.project is not None:
            if str(self.project.path) == path:
                return
            self.project.close()
        self.project = NovelProject.open(path)
        meta = self.project.meta
        self.url_edit.setText(meta.url)
        self.title_label.setText(meta.title)
        self.author_label.setText(meta.author or "—")
        self.desc_label.setText(meta.description or "—")
        self._reload_table()
        counts = self.project.counts()
        self.count_label.setText(str(counts["total"]))
        self.download_button.setEnabled(True)
        self.status_label.setText(
            f"Đang làm: {counts['downloaded']}/{counts['total']} chương đã tải, "
            f"{counts['translated']} đã dịch. Bấm 'Quét' nếu truyện có chương mới."
        )
        self.project_changed.emit(path)

    # ------------------------------------------------------------------ scan

    def _start_scan(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            return
        self.scan_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.status_label.setText("Đang quét metadata và mục lục…")

        self._scan_worker = ScanWorker(url, self.config.library_dir, self.config.request_delay)
        self._scan_worker.scanned.connect(self._on_scanned)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(lambda: self.scan_button.setEnabled(True))
        self._scan_worker.start()

    def _on_scanned(self, path: str, meta, count: int) -> None:
        if self.project is not None:
            self.project.close()
        self.project = NovelProject.open(path)
        self.title_label.setText(meta.title)
        self.author_label.setText(meta.author or "—")
        self.count_label.setText(str(count))
        self.desc_label.setText(meta.description or "—")
        self._reload_table()
        self.download_button.setEnabled(True)
        counts = self.project.counts()
        self.status_label.setText(
            f"Đã quét xong: {counts['total']} chương, {counts['downloaded']} đã tải."
        )
        self.refresh_recent(select_path=path)  # same-path guard avoids a reload
        self.project_changed.emit(path)

    def _on_scan_failed(self, message: str) -> None:
        self.status_label.setText("")
        QMessageBox.warning(self, "Quét thất bại", message)

    # -------------------------------------------------------------- download

    def _start_download(self) -> None:
        if self.project is None:
            return
        pending = len(self.project.pending_download())
        if pending == 0:
            QMessageBox.information(self, "Đã đủ", "Tất cả các chương đã được tải về.")
            return

        self.download_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setMaximum(pending)
        self.progress.setValue(0)

        self._download_worker = DownloadWorker(self.project.path, self.config.request_delay)
        self._download_worker.progress.connect(self._on_progress)
        self._download_worker.chapter_done.connect(self._on_chapter_updated)
        self._download_worker.chapter_error.connect(
            lambda idx, _msg: self._on_chapter_updated(idx)
        )
        self._download_worker.finished_ok.connect(self._on_download_finished)
        self._download_worker.start()

    def _cancel_download(self) -> None:
        if self._download_worker is not None:
            self._download_worker.cancel()
            self.status_label.setText("Đang dừng sau chương hiện tại…")

    def _on_progress(self, done: int, total: int, title: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if title:
            self.status_label.setText(f"Đang tải: {title}")

    def _on_chapter_updated(self, idx: int) -> None:
        if self.project is None:
            return
        chapter = self.project.chapter(idx)
        if chapter is not None:
            self.model.update_chapter(chapter)

    def _on_download_finished(self, ok: int, errors: int) -> None:
        self.download_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        message = f"Tải xong: {ok} chương thành công"
        if errors:
            message += f", {errors} lỗi (bấm 'Tải các chương' để thử lại)"
        self.status_label.setText(message + ".")
        if self.project is not None:
            self.project_changed.emit(str(self.project.path))

    # ------------------------------------------------------------------ misc

    def _reload_table(self) -> None:
        if self.project is not None:
            self.model.set_chapters(self.project.chapters())

    def shutdown(self) -> None:
        """Cancel running workers and wait for them (called on window close)."""
        for worker in (self._scan_worker, self._download_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(30_000)

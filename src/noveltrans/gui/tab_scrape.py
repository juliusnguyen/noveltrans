"""Tab 1 — Scrape / Download: URL -> scan metadata + TOC -> download chapters."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
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
from noveltrans.discord_unlock import valid_channel_url
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.notify import clear_dock_badge, request_attention, set_dock_badge
from noveltrans.gui.widgets import ChapterTableModel, ProjectPicker, enable_cell_copy
from noveltrans.gui.workers import DownloadWorker, ScanWorker, UnlockWorker
from noveltrans.storage import NovelProject

_MAX_AUTO_UNLOCKS = 3  # consecutive auto-unlocks with no progress before giving up
# Grace period after the /mochuong command is sent before resuming the download.
# "Command sent" isn't "cap lifted": the bot needs a moment to process the code, and
# resuming too soon just re-hits the limit page (burning a fresh single-use code) and
# triggers a redundant second unlock.
_UNLOCK_SETTLE_MS = 6_000


class ScrapeTab(QWidget):
    project_changed = Signal(str)  # project path — other tabs refresh their pickers

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        # Host veto: returns False if this project is already open in another workspace,
        # so we never open the same SQLite project in two tabs. None = always allowed.
        self.can_open_project: Callable[[str], bool] | None = None
        self._scan_worker: ScanWorker | None = None
        self._download_worker: DownloadWorker | None = None
        self._unlock_worker: UnlockWorker | None = None
        self._auto_unlocking = False  # suppress the "download finished" chrome mid-unlock
        self._unlock_attempts = 0  # consecutive auto-unlocks with no chapter progress
        # last daily-limit hit, kept so the manual fallback can still offer the code
        # even when auto-unlock is what failed
        self._last_limit_message = ""
        self._last_limit_code = ""
        # Delays the post-unlock resume so the bot has time to lift the cap; owned by
        # self so shutdown() can stop it before the widget is torn down.
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._start_download)

        # --- recent projects row: continue a novel without pasting its URL
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._load_project)
        recent_row = QHBoxLayout()
        recent_row.addWidget(QLabel("Truyện gần đây:"))
        recent_row.addWidget(self.picker, stretch=1)

        # --- URL row
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Dán URL trang tiểu thuyết, ví dụ: https://medoctruyen.vn/tu-bao-tien-bon"
        )
        self.scan_button = QPushButton("Quét")
        self.scan_button.setProperty("primary", True)
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
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        enable_cell_copy(self.table)  # Ctrl+C / right-click to copy a cell (e.g. errors)

        # --- download row
        self.download_button = QPushButton("Tải các chương")
        self.download_button.setProperty("primary", True)
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._start_download)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_download)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m chương")
        self.status_label = QLabel("")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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

    def _select_in_picker(self, path: str) -> None:
        """Set the recent picker to `path` (or blank) without re-triggering a load."""
        self.picker.blockSignals(True)
        self.picker.setCurrentIndex(self.picker.findData(path) if path else -1)
        self.picker.blockSignals(False)

    def _load_project(self, path: str) -> None:
        """Open an existing project (no network) so work can just continue."""
        if not path:
            return
        if self.project is not None and str(self.project.path) == path:
            return
        # Refuse if another workspace already owns this project — revert the picker to
        # the current project (or blank) so nothing gets opened twice.
        if self.can_open_project is not None and not self.can_open_project(path):
            self._select_in_picker(str(self.project.path) if self.project else "")
            return
        if self.project is not None:
            self.project.close()
        self.project = NovelProject.open(path)
        meta = self.project.meta
        self.url_edit.setText(meta.url)
        self._show_meta(meta)
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

        self._scan_worker = ScanWorker(
            url,
            self.config.library_dir,
            self.config.request_delay,
            cookies=self.config.medoctruyen_cookies,
        )
        self._scan_worker.scanned.connect(self._on_scanned)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(lambda: self.scan_button.setEnabled(True))
        self._scan_worker.start()

    def _on_scanned(self, path: str, _meta, count: int) -> None:
        if self.project is not None:
            self.project.close()
        self.project = NovelProject.open(path)
        # display the on-disk meta — it keeps translations from earlier runs
        self._show_meta(self.project.meta)
        self.count_label.setText(str(count))
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
        # The unlock's resume lands here; we're downloading again, so the "unlock in
        # flight" state is over. If this resumed batch immediately re-hits the cap,
        # _on_daily_limit re-sets the flag before _on_download_finished reads it.
        self._auto_unlocking = False
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
        clear_dock_badge()  # starting fresh — drop any leftover limit badge

        self._download_worker = DownloadWorker(
            self.project.path,
            self.config.request_delay,
            cookies=self.config.medoctruyen_cookies,
        )
        self._download_worker.progress.connect(self._on_progress)
        self._download_worker.chapter_done.connect(self._on_chapter_done)
        self._download_worker.chapter_error.connect(lambda idx, _msg: self._on_chapter_updated(idx))
        self._download_worker.daily_limit_hit.connect(self._on_daily_limit)
        self._download_worker.finished_ok.connect(self._on_download_finished)
        track_worker(self._download_worker)  # keep the Mac awake for the batch
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

    def _on_chapter_done(self, idx: int) -> None:
        # A real download landed → the last unlock actually worked; reset the
        # runaway-loop guard so a genuinely long novel keeps unlocking as needed.
        self._unlock_attempts = 0
        self._on_chapter_updated(idx)

    def _on_chapter_updated(self, idx: int) -> None:
        if self.project is None:
            return
        chapter = self.project.chapter(idx)
        if chapter is not None:
            self.model.update_chapter(chapter)

    def _on_daily_limit(self, message: str, code: str) -> None:
        # The batch stopped on the site's per-day cap. If auto-unlock is configured,
        # run the site's Discord /mochuong <code> unlock and resume automatically;
        # otherwise flag the Dock and show the manual steps.
        self._last_limit_message = message
        self._last_limit_code = code
        if (
            self.config.discord_autounlock_enabled
            and code
            and valid_channel_url(self.config.discord_channel_url)
        ):
            self._start_auto_unlock(code)
            return
        set_dock_badge(1)
        request_attention(self.window())
        self._show_manual_unlock(message, code)

    # -------------------------------------------------------- auto-unlock (Discord)

    def _start_auto_unlock(self, code: str) -> None:
        # If several unlocks in a row buy no new chapters, the unlock isn't really
        # working (wrong channel, bot didn't act, still capped) — stop auto-looping
        # and hand off to the manual flow instead of hammering Discord.
        if self._unlock_attempts >= _MAX_AUTO_UNLOCKS:
            self._unlock_attempts = 0
            set_dock_badge(1)
            request_attention(self.window())
            QMessageBox.warning(
                self,
                "Tự mở khoá không hiệu quả",
                f"Đã thử tự mở khoá {_MAX_AUTO_UNLOCKS} lần nhưng vẫn bị giới hạn. "
                "Kiểm tra lại link kênh #mở-khoá và tài khoản Discord phụ, hoặc mở "
                "khoá thủ công.",
            )
            self._show_manual_unlock(self._last_limit_message, code)
            return
        self._unlock_attempts += 1
        self._auto_unlocking = True
        self.download_button.setEnabled(False)
        self.status_label.setText("🔓 Đang tự mở khoá qua Discord (/mochuong)… giữ cửa sổ mở.")
        self._unlock_worker = UnlockWorker(self.config.discord_channel_url, code)
        self._unlock_worker.unlocked.connect(self._on_unlocked)
        self._unlock_worker.needs_login.connect(self._on_unlock_needs_login)
        self._unlock_worker.failed.connect(self._on_unlock_failed)
        self._unlock_worker.start()

    def _on_unlocked(self) -> None:
        # Keep _auto_unlocking True through the settle wait: the resume can immediately
        # re-hit the cap, and _on_download_finished must still stay quiet if so.
        clear_dock_badge()
        self.status_label.setText("✅ Đã gửi lệnh mở khoá — chờ bot xử lý rồi tải tiếp…")
        # Wait before resuming: the bot lifts the cap a moment after the command lands;
        # resuming instantly re-hits the limit and wastes a fresh code (see constant).
        self._resume_timer.start(_UNLOCK_SETTLE_MS)

    def _on_unlock_needs_login(self, message: str) -> None:
        self._auto_unlocking = False
        self.download_button.setEnabled(True)
        set_dock_badge(1)
        request_attention(self.window())
        QMessageBox.warning(
            self,
            "Cần đăng nhập Discord",
            f"{message}\n\nVào Cài đặt → “Đăng nhập Discord” để đăng nhập tài khoản "
            "phụ một lần, rồi bấm “Tải các chương” lại.",
        )

    def _on_unlock_failed(self, message: str) -> None:
        # Auto-unlock failed — fall back to the manual flow so the user isn't stuck.
        self._auto_unlocking = False
        self.download_button.setEnabled(True)
        set_dock_badge(1)
        request_attention(self.window())
        QMessageBox.warning(
            self,
            "Tự mở khoá không thành công",
            f"Không tự chạy được /mochuong: {message}\n\n"
            "Bạn có thể mở khoá thủ công theo hướng dẫn tiếp theo.",
        )
        # Hand over the code the batch actually stopped on — this is exactly when the
        # user has to type /mochuong themselves.
        self._show_manual_unlock(self._last_limit_message, self._last_limit_code)

    def _show_manual_unlock(self, message: str, code: str) -> None:
        """Fallback: show the unlock steps and pre-copy the command to the clipboard."""
        self.status_label.setText(f"🔒 {message}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Đã đạt giới hạn đọc trong ngày")
        box.setText(message)
        open_channel = None
        if code:
            QGuiApplication.clipboard().setText(f"/mochuong {code}")
            box.setInformativeText(
                f"Đã copy lệnh “/mochuong {code}” vào clipboard — dán vào kênh #mở-khoá rồi Enter."
            )
            if valid_channel_url(self.config.discord_channel_url):
                open_channel = box.addButton("Mở kênh #mở-khoá", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if open_channel is not None and box.clickedButton() is open_channel:
            QDesktopServices.openUrl(QUrl(self.config.discord_channel_url))

    def _on_download_finished(self, ok: int, errors: int) -> None:
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self._auto_unlocking:
            # An auto-unlock is in flight (this "finished" is just the batch stopping
            # on the cap); leave the download button disabled and the status as-is.
            return
        self.download_button.setEnabled(True)
        message = f"Tải xong: {ok} chương thành công"
        if errors:
            message += f", {errors} lỗi (bấm 'Tải các chương' để thử lại)"
        self.status_label.setText(message + ".")
        if self.project is not None:
            self.project_changed.emit(str(self.project.path))

    # ------------------------------------------------------------------ misc

    def _show_meta(self, meta) -> None:
        """Fill the metadata panel; translated info shows next to the original."""
        if meta.translated_title:
            self.title_label.setText(f"{meta.title}  —  {meta.translated_title}")
        else:
            self.title_label.setText(meta.title)
        self.author_label.setText(meta.author or "—")
        if meta.translated_description:
            self.desc_label.setText(meta.translated_description)
            self.desc_label.setToolTip(meta.description)  # original on hover
        else:
            self.desc_label.setText(meta.description or "—")
            self.desc_label.setToolTip("")

    def showEvent(self, event) -> None:  # a translation run may have filled the meta
        busy = any(
            w is not None and w.isRunning()
            for w in (self._scan_worker, self._download_worker, self._unlock_worker)
        )
        if self.project is not None and not busy:
            self._show_meta(self.project.reload_meta())
        super().showEvent(event)

    def _reload_table(self) -> None:
        if self.project is not None:
            self.model.set_chapters(self.project.chapters())

    def current_title(self) -> str:
        """The loaded novel's title (for the workspace tab label), or ""."""
        return self.project.meta.title if self.project is not None else ""

    def has_running_workers(self) -> bool:
        return any(
            w is not None and w.isRunning()
            for w in (self._scan_worker, self._download_worker, self._unlock_worker)
        )

    def shutdown(self) -> None:
        """Cancel running workers and wait for them (called on window close)."""
        self._resume_timer.stop()  # don't let a pending resume fire post-teardown
        for worker in (self._scan_worker, self._download_worker, self._unlock_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(30_000)

"""Pick the OneDrive folder that backups go into, by browsing the account's own folders.

Typing a path works too — the field in Settings stays free-text — but a typo there does
not fail, it creates a folder, and someone with years of files in their OneDrive should
not have to find that out afterwards. Browsing removes the guess.

Only folders are listed: you cannot upload *into* a spreadsheet, and offering one would
end in a confusing error about a folder that would not open.

Each navigation opens a real browser, so the list arrives through a worker and the dialog
is explicit about being busy. That is also why it does NOT pre-fetch every level: one
listing per folder the user actually opens.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from noveltrans.gui.workers import OneDriveFoldersWorker


class OneDriveFolderDialog(QDialog):
    """Browse OneDrive and return a path. `selected_path` is valid after `accept()`."""

    def __init__(self, start_path: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn thư mục OneDrive")
        self.resize(460, 420)
        self._segments = [s for s in (start_path or "").split("/") if s.strip()]
        self._worker: OneDriveFoldersWorker | None = None

        self.path_label = QLabel()
        self.path_label.setWordWrap(True)

        self.up_button = QPushButton("⬆ Lên một cấp")
        self.up_button.clicked.connect(self._go_up)
        self.reload_button = QPushButton("Tải lại")
        self.reload_button.clicked.connect(self._reload)
        top = QHBoxLayout()
        top.addWidget(self.up_button)
        top.addWidget(self.reload_button)
        top.addStretch()

        self.list = QListWidget()
        # Double-click to enter, matching the OneDrive web UI itself — and matching what
        # the automation does, so the two cannot drift apart in the user's head.
        self.list.itemDoubleClicked.connect(self._enter)

        self.status = QLabel("")
        self.status.setProperty("muted", True)
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Dùng thư mục đang mở"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Bấm đúp để mở thư mục con. Bản sao lưu sẽ nằm trong "
                                "thư mục đang mở."))
        layout.addWidget(self.path_label)
        layout.addLayout(top)
        layout.addWidget(self.list, stretch=1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self._refresh_path()
        self._reload()

    # ------------------------------------------------------------------ state

    @property
    def selected_path(self) -> str:
        """The folder currently open — the one OK accepts. "/" means the OneDrive root."""
        return "/" + "/".join(self._segments)

    def _refresh_path(self) -> None:
        self.path_label.setText(f"📁 {self.selected_path}")
        self.up_button.setEnabled(bool(self._segments))

    # --------------------------------------------------------------- browsing

    def _reload(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.list.clear()
        self._set_busy(True, "Đang mở OneDrive…")
        self._worker = OneDriveFoldersWorker(self.selected_path, self)
        self._worker.fetched.connect(self._on_fetched)
        self._worker.failed.connect(self._on_failed)
        self._worker.needs_login.connect(self._on_needs_login)
        self._worker.start()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.list.setEnabled(not busy)
        self.up_button.setEnabled(not busy and bool(self._segments))
        self.reload_button.setEnabled(not busy)
        self.status.setText(message)

    def _on_fetched(self, path: str, folders: list) -> None:
        self._set_busy(False, "")
        for name in folders:
            self.list.addItem(QListWidgetItem(f"📁 {name}"))
        if not folders:
            self.status.setText(
                "Thư mục này không có thư mục con nào — vẫn có thể chọn nó."
            )

    def _on_failed(self, message: str) -> None:
        # The path stays where it was: dropping the user back to the root because one
        # listing failed would lose the place they had navigated to.
        self._set_busy(False, f"⚠️ {message}")

    def _on_needs_login(self, message: str) -> None:
        self._set_busy(
            False,
            f"⚠️ {message} Vào Settings → “Đăng nhập OneDrive” rồi mở lại.",
        )

    def _enter(self, item: QListWidgetItem) -> None:
        name = item.text().removeprefix("📁 ").strip()
        if not name:
            return
        self._segments.append(name)
        self._refresh_path()
        self._reload()

    def _go_up(self) -> None:
        if not self._segments:
            return
        self._segments.pop()
        self._refresh_path()
        self._reload()

    # ------------------------------------------------------------- lifecycle

    def reject(self) -> None:
        self._stop_worker()
        super().reject()

    def accept(self) -> None:
        self._stop_worker()
        super().accept()

    def _stop_worker(self) -> None:
        """Never leave a browser owned by a dialog that has closed."""
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(20_000)


def pick_onedrive_folder(parent, start_path: str = "") -> str:
    """Show the picker; return the chosen path, or "" if the user cancelled."""
    dialog = OneDriveFolderDialog(start_path, parent)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.selected_path
    return ""


__all__ = ["OneDriveFolderDialog", "pick_onedrive_folder"]

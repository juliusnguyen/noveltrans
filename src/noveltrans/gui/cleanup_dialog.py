"""Reclaim disk space: show what is safe to delete, and make the user say yes to it.

The dialog's job is to make an irreversible action legible. Every row says what would go,
how big it is, and **why it is safe** — never just a count. A user who cannot see why a
file is being offered has no way to catch a mistake before it costs them a video.

Audio and video are presented differently on purpose, because their evidence is:

  * **Audio** is verified locally — the rendered `.mp4` containing it is right there on
    disk — so it is ticked on arrival and can be deleted immediately.
  * **Video** needs OneDrive checked, and until that has happened its rows are shown but
    **cannot be ticked**. The manifest saying "backed up" is not accepted; measured on a
    real library, one part in twenty-nine was missing while the manifest was content.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from noveltrans.cleanup import (
    KIND_AUDIO,
    KIND_VIDEO,
    plan_audio_cleanup,
    remove_files,
    total_size,
    video_cleanup_candidates,
)
from noveltrans.gui.widgets import SortableItem, enable_table_sorting
from noveltrans.gui.workers import OneDriveVerifyWorker
from noveltrans.onedrive_upload import format_size

_STATUS_COLUMN = 3


class CleanupDialog(QDialog):
    """Pick what to delete. `freed` holds the bytes reclaimed after `exec()` returns."""

    def __init__(self, project_path, novel_title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dọn dẹp ổ đĩa")
        self.resize(720, 520)
        self.project_path = project_path
        self.freed = 0
        self._rows: list[dict] = []
        self._worker: OneDriveVerifyWorker | None = None

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Loại", "Dung lượng", "Vì sao xoá được"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(_STATUS_COLUMN, 250)
        self.table.itemChanged.connect(lambda _i: self._refresh_total())
        # Biggest first: "what do I delete to get the most space back" is the question this
        # dialog exists to answer. Safe to sort only because rows are UserRole-keyed above.
        enable_table_sorting(self.table, default_column=2, ascending=False)

        self.verify_button = QPushButton("Kiểm tra OneDrive…")
        self.verify_button.setToolTip(
            "Mở OneDrive và kiểm tra từng phần video thật sự đã có trên đó. Chỉ những "
            "phần kiểm tra được mới cho phép xoá."
        )
        self.verify_button.clicked.connect(self._verify)
        select_audio = QPushButton("Chọn audio")
        select_audio.clicked.connect(lambda: self._set_all(KIND_AUDIO, True))
        select_none = QPushButton("Bỏ chọn")
        select_none.clicked.connect(lambda: self._set_all(None, False))
        row = QHBoxLayout()
        row.addWidget(self.verify_button)
        row.addWidget(select_audio)
        row.addWidget(select_none)
        row.addStretch()

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.total_label = QLabel("")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("muted", True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Xoá các file đã chọn")
        self.buttons.accepted.connect(self._delete)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"Những file dưới đây của “{novel_title or 'truyện này'}” đã có bản khác giữ "
            "chỗ. Xoá là KHÔNG khôi phục được."
        )
        header.setWordWrap(True)
        layout.addWidget(header)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(row)
        layout.addWidget(self.progress)
        layout.addWidget(self.total_label)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self._scan()

    # ------------------------------------------------------------------ scan

    def _scan(self) -> None:
        """Local scan only — audio is provable from disk, video is not."""
        audio = plan_audio_cleanup(self.project_path)
        videos = video_cleanup_candidates(self.project_path)
        for item in audio:
            self._add_row(item, verified=True)
        for item in videos:
            self._add_row(item, verified=False)
        self.verify_button.setEnabled(bool(videos))
        if not self._rows:
            self.status.setText("Không có file nào xoá được — chưa render hoặc chưa đăng.")
        elif videos:
            self.status.setText(
                f"{len(videos)} phần video đang chờ kiểm tra OneDrive. Chưa kiểm tra thì "
                "chưa cho chọn — bản ghi trên máy đã từng báo sai."
            )
        self._refresh_total()

    def _add_row(self, item, *, verified: bool) -> None:
        index = self.table.rowCount()
        # Off while filling, back on after. Otherwise every setItem re-sorts and the row
        # being built moves out from under the next setItem call. Rows arrive one at a
        # time as the verify worker finds them, so this brackets each insert, not a batch.
        self.table.setSortingEnabled(False)
        self.table.insertRow(index)

        name = QTableWidgetItem(item.relpath)
        name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        # Audio arrives ticked because it is already proven; video arrives locked.
        name.setCheckState(
            Qt.CheckState.Checked if verified else Qt.CheckState.Unchecked
        )
        if not verified:
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        # The row's data rides on the ITEM, not on its position. `self._rows[index]` was
        # the old lookup, and it is wrong the instant a sort moves a row: `_ticked` would
        # hand `_delete` a different file than the one the user ticked, in a dialog whose
        # own header says "Xoá là KHÔNG khôi phục được". This is the fix, and it is worth
        # making whether or not the table ever sorts.
        name.setData(Qt.ItemDataRole.UserRole, item)
        self.table.setItem(index, 0, name)
        self.table.setItem(
            index, 1, QTableWidgetItem("Audio" if item.kind == KIND_AUDIO else "Video")
        )
        self.table.setItem(
            index, 2, SortableItem(format_size(item.size), item.size)  # bytes, not "1.2 GB"
        )
        self.table.setItem(index, _STATUS_COLUMN, QTableWidgetItem(item.reason))
        self._rows.append({"item": item, "verified": verified})
        self.table.setSortingEnabled(True)

    # ---------------------------------------------------------------- verify

    def _verify(self) -> None:
        candidates = [r["item"] for r in self._rows if not r["verified"]]
        if not candidates:
            return
        self.verify_button.setEnabled(False)
        self.progress.setRange(0, len(candidates))
        self.progress.setVisible(True)
        self.status.setText("Đang mở OneDrive để kiểm tra…")
        self._worker = OneDriveVerifyWorker(self.project_path, candidates, self)
        self._worker.progress.connect(self._on_verify_progress)
        self._worker.done.connect(self._on_verified)
        self._worker.failed.connect(self._on_verify_failed)
        self._worker.needs_login.connect(self._on_verify_needs_login)
        self._worker.start()

    def _on_verify_progress(self, done: int, total: int, folder: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.status.setText(f"Đang kiểm tra ({done + 1}/{total})…")

    def _on_verified(self, confirmed: list, unconfirmed: list) -> None:
        self.progress.setVisible(False)
        self.verify_button.setEnabled(False)
        ok = {item.relpath for item in confirmed}
        already = {r["item"].relpath for r in self._rows if r["verified"]}
        # Walk the ITEMS, not `enumerate(self._rows)`. The old positional walk unlocked
        # whichever row happened to sit at that position — under a sort, a *different*
        # file, ticked and ready to delete. Sorting is off across the loop so a setItem
        # cannot move a row while `name.row()` is being used.
        self.table.setSortingEnabled(False)
        for name in self._name_items():
            entry = name.data(Qt.ItemDataRole.UserRole)
            if entry is None or entry.relpath in already:
                continue
            if entry.relpath in ok:
                for row in self._rows:
                    if row["item"].relpath == entry.relpath:
                        row["verified"] = True
                name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name.setCheckState(Qt.CheckState.Checked)
                self.table.setItem(
                    name.row(),
                    _STATUS_COLUMN,
                    QTableWidgetItem("đã kiểm tra: có trên OneDrive"),
                )
            elif entry.kind == KIND_VIDEO:
                self.table.setItem(
                    name.row(),
                    _STATUS_COLUMN,
                    QTableWidgetItem("⚠️ KHÔNG thấy trên OneDrive — giữ lại"),
                )
        self.table.setSortingEnabled(True)
        message = f"Đã kiểm tra: {len(confirmed)} phần có trên OneDrive"
        if unconfirmed:
            message += (
                f", {len(unconfirmed)} phần KHÔNG có (giữ nguyên — hãy sao lưu trước)"
            )
        self.status.setText(message + ".")
        self._refresh_total()

    def _on_verify_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.verify_button.setEnabled(True)
        self.status.setText(f"⚠️ {message}")

    def _on_verify_needs_login(self, message: str) -> None:
        self.progress.setVisible(False)
        self.verify_button.setEnabled(True)
        self.status.setText(
            f"⚠️ {message} Vào Settings → “Đăng nhập OneDrive” rồi kiểm tra lại."
        )

    # ----------------------------------------------------------------- ticks

    def _name_items(self):
        """Column 0 of every row, each carrying its own CleanupItem in UserRole."""
        return [
            item
            for row in range(self.table.rowCount())
            if (item := self.table.item(row, 0)) is not None
        ]

    def _set_all(self, kind: str | None, checked: bool) -> None:
        for item in self._name_items():
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry is None or (kind is not None and entry.kind != kind):
                continue
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )

    def _ticked(self) -> list:
        """The files the user actually ticked — read off the items, never by position."""
        return [
            entry
            for item in self._name_items()
            if item.checkState() == Qt.CheckState.Checked
            and (entry := item.data(Qt.ItemDataRole.UserRole)) is not None
        ]

    def _refresh_total(self) -> None:
        ticked = self._ticked()
        self.total_label.setText(
            f"Đã chọn {len(ticked)} file — giải phóng {format_size(total_size(ticked))}."
            if ticked
            else "Chưa chọn file nào."
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(ticked))

    # ---------------------------------------------------------------- delete

    def _delete(self) -> None:
        ticked = self._ticked()
        if not ticked:
            return
        audio = [i for i in ticked if i.kind == KIND_AUDIO]
        video = [i for i in ticked if i.kind == KIND_VIDEO]
        lines = [
            f"Sẽ xoá vĩnh viễn {len(ticked)} file khỏi máy "
            f"({format_size(total_size(ticked))}):",
            "",
        ]
        if audio:
            lines.append(f"  • {len(audio)} file audio ({format_size(total_size(audio))})")
        if video:
            lines.append(f"  • {len(video)} video phần ({format_size(total_size(video))})")
        lines += [
            "",
            "Các file này vẫn còn ở nơi khác (video của phần / YouTube + OneDrive), "
            "nhưng bản trên máy sẽ KHÔNG khôi phục được.",
            "",
            "Tiếp tục xoá?",
        ]
        if (
            QMessageBox.question(self, "Xoá file", "\n".join(lines))
            != QMessageBox.StandardButton.Yes
        ):
            return

        deleted, freed, errors = remove_files(ticked)
        self.freed = freed
        if errors:
            QMessageBox.warning(
                self,
                "Dọn dẹp",
                f"Đã xoá {deleted} file, giải phóng {format_size(freed)}.\n\n"
                f"{len(errors)} file không xoá được:\n" + "\n".join(errors[:5]),
            )
        else:
            QMessageBox.information(
                self,
                "Dọn dẹp",
                f"Đã xoá {deleted} file, giải phóng {format_size(freed)}.",
            )
        self.accept()

    # ------------------------------------------------------------- lifecycle

    def reject(self) -> None:
        self._stop_worker()
        super().reject()

    def _stop_worker(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(60_000)

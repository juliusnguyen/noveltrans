"""The panel that drops out of the menu-bar icon: one row per running job.

A frameless `Qt::Popup` widget rather than a tray `QMenu` of `QWidgetAction`s. On macOS
Qt renders a tray menu as a native NSMenu, and while that is open AppKit owns the run
loop — queued cross-thread signal delivery (exactly how `worker.progress` reaches the GUI
thread) is not reliably pumped there. The whole point of this panel is a progress bar that
*moves while it is open*, so a native menu is the wrong vehicle. Being an ordinary widget
also means it styles with the app's QSS and can be built and asserted in offscreen tests
with no system tray at all.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from noveltrans.gui.jobs import job_registry
from noveltrans.gui.widgets import PauseButton

POPUP_WIDTH = 320
EMPTY_TEXT = "Không có tiến trình nào đang chạy."

# These drive a real Chrome window; pausing one parks a live Google session rather
# than just idling a thread, so the row says so.
BROWSER_KINDS = frozenset(
    {"Tải video lên", "Tải phụ đề lên", "Danh sách phát", "Đổi ảnh bìa"}
)
BROWSER_PAUSE_HINT = (
    "Tạm dừng sẽ để cửa sổ Chrome và phiên đăng nhập Google mở cho tới khi chạy tiếp."
)


def popup_at(anchor: QRect, screen: QRect, size) -> QPoint:
    """Top-left corner for a popup of `size` hanging under `anchor`, kept on `screen`.

    Pure geometry so it can be tested without a tray: the menu-bar rect sits at the top
    right on a wide screen, so an un-clamped popup would hang off the edge.
    """
    x = anchor.center().x() - size.width() // 2 if not anchor.isNull() else anchor.x()
    y = anchor.bottom() + 4
    x = max(screen.left(), min(x, screen.right() - size.width()))
    y = max(screen.top(), min(y, screen.bottom() - size.height()))
    return QPoint(x, y)


class JobRow(QWidget):
    """One job: label, progress bar, counter, and its pause toggle."""

    def __init__(self, job, registry=None, parent=None):
        super().__init__(parent)
        self.job_id = job.id
        self.registry = registry if registry is not None else job_registry

        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.counter_label = QLabel()
        self.counter_label.setObjectName("jobCounter")
        self.pause_button = PauseButton(job.id, registry=self.registry)
        if job.kind in BROWSER_KINDS:
            self.pause_button.set_extra_hint(BROWSER_PAUSE_HINT)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(0, 0, 0, 0)
        counter_row.addWidget(self.counter_label, stretch=1)
        counter_row.addWidget(self.pause_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.bar)
        layout.addLayout(counter_row)

        self.update_from(job)

    def update_from(self, job) -> None:
        self.title_label.setText(job.label)
        if job.total > 0:
            self.bar.setRange(0, job.total)
            self.bar.setValue(job.done)
            self.counter_label.setText(f"{job.done}/{job.total}")
        else:
            # No countable total yet (e.g. "Đang tải model VieNeu…") — a 0/0 bar reads as
            # stalled, so show motion and say what it is actually doing.
            self.bar.setRange(0, 0)
            self.counter_label.setText(job.message or "Đang chuẩn bị…")
        if job.paused:
            self.counter_label.setText(f"{self.counter_label.text()} · đã tạm dừng")


class JobPopup(QWidget):
    """The dropdown itself. Rebuilds rows from the registry; never holds a worker."""

    open_window = Signal()
    quit_app = Signal()

    def __init__(self, registry=None, parent=None):
        super().__init__(parent)
        self.registry = registry if registry is not None else job_registry
        self.setObjectName("jobPopup")
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setFixedWidth(POPUP_WIDTH)
        self._rows: dict[int, JobRow] = {}

        self.empty_label = QLabel(EMPTY_TEXT)
        self.empty_label.setObjectName("jobEmpty")
        self.empty_label.setWordWrap(True)
        self.empty_label.setContentsMargins(12, 10, 12, 10)

        self.rows_box = QVBoxLayout()
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(0)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("jobSeparator")

        self.open_button = QPushButton("Mở cửa sổ")
        self.open_button.setObjectName("jobCommand")
        self.open_button.clicked.connect(self._on_open)
        self.quit_button = QPushButton("Thoát")
        self.quit_button.setObjectName("jobCommand")
        self.quit_button.clicked.connect(self._on_quit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(2)
        layout.addWidget(self.empty_label)
        layout.addLayout(self.rows_box)
        layout.addWidget(separator)
        layout.addWidget(self.open_button)
        layout.addWidget(self.quit_button)

        self.registry.job_added.connect(self._on_added)
        self.registry.job_changed.connect(self._on_changed)
        self.registry.job_removed.connect(self._on_removed)
        self.refresh()

    # ------------------------------------------------------------------ rows

    def refresh(self) -> None:
        """Rebuild every row from the registry (used on construction and on show)."""
        for row in self._rows.values():
            self.rows_box.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for job in self.registry.jobs():
            self._add_row(job)
        self._sync_empty()

    def _add_row(self, job) -> None:
        row = JobRow(job, registry=self.registry)
        self._rows[job.id] = row
        self.rows_box.addWidget(row)

    def _sync_empty(self) -> None:
        self.empty_label.setVisible(not self._rows)

    def _on_added(self, job) -> None:
        if job.id not in self._rows:
            self._add_row(job)
        self._sync_empty()

    def _on_changed(self, job) -> None:
        row = self._rows.get(job.id)
        if row is not None:
            row.update_from(job)  # in place — never rebuild, it would fight the cursor

    def _on_removed(self, job_id: int) -> None:
        row = self._rows.pop(job_id, None)
        if row is not None:
            self.rows_box.removeWidget(row)
            row.deleteLater()
        self._sync_empty()

    # -------------------------------------------------------------- commands

    def _on_open(self) -> None:
        self.hide()
        self.open_window.emit()

    def _on_quit(self) -> None:
        self.hide()
        self.quit_app.emit()

    def show_at(self, anchor: QRect, screen: QRect) -> None:
        self.refresh()
        self.adjustSize()
        self.move(popup_at(anchor, screen, self.size()))
        self.show()
        self.raise_()
        self.activateWindow()  # a click that doesn't activate the app still gets events

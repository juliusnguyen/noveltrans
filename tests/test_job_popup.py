"""The menu-bar dropdown: one row per running job, with a live bar and a pause toggle."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, QRect, QSize, Signal

from noveltrans.gui.job_popup import EMPTY_TEXT, JobPopup, popup_at
from noveltrans.gui.jobs import JobRegistry


class _FakeWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.paused = False

    def isFinished(self) -> bool:  # noqa: N802 — mirrors QThread's Qt-cased API
        return False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


@pytest.fixture
def registry():
    return JobRegistry()


@pytest.fixture
def popup(qapp, registry):
    return JobPopup(registry)


def test_empty_state_shows_the_notice_and_no_rows(popup):
    assert popup.empty_label.text() == EMPTY_TEXT
    assert not popup.empty_label.isHidden()
    assert popup._rows == {}


def test_a_new_job_adds_a_row_and_hides_the_empty_notice(popup, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Tải truyện", novel="Truyện A")
    worker.progress.emit(142, 200, "Chương 143")

    row = popup._rows[job.id]
    assert row.title_label.text() == "Tải truyện — Truyện A"
    assert row.counter_label.text() == "142/200"
    assert row.bar.value() == 142 and row.bar.maximum() == 200
    assert popup.empty_label.isHidden()


def test_progress_updates_the_row_in_place(popup, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Dịch", novel="Truyện B")
    row = popup._rows[job.id]
    worker.progress.emit(1, 10, "x")
    worker.progress.emit(7, 10, "y")
    assert popup._rows[job.id] is row  # same widget — never rebuilt under the cursor
    assert row.counter_label.text() == "7/10"


def test_a_job_with_no_total_goes_indeterminate_and_shows_its_message(popup, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Nghe audio")
    worker.progress.emit(0, 0, "Đang tải model VieNeu…")
    row = popup._rows[job.id]
    assert (row.bar.minimum(), row.bar.maximum()) == (0, 0)  # busy indicator
    assert row.counter_label.text() == "Đang tải model VieNeu…"


def test_the_pause_button_flips_label_and_drives_the_registry(popup, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Nghe audio", novel="Truyện C")
    row = popup._rows[job.id]
    assert row.pause_button.text() == "⏸ Tạm dừng"

    row.pause_button.click()
    assert worker.paused
    assert row.pause_button.text() == "▶ Tiếp tục"
    assert "tạm dừng" in row.counter_label.text()

    row.pause_button.click()
    assert not worker.paused
    assert row.pause_button.text() == "⏸ Tạm dừng"


def test_finishing_removes_the_row_and_restores_the_empty_notice(popup, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Dịch")
    worker.finished.emit()
    assert job.id not in popup._rows
    assert not popup.empty_label.isHidden()


def test_rows_appear_in_start_order(popup, registry):
    first = registry.register(_FakeWorker(), kind="Tải truyện")
    second = registry.register(_FakeWorker(), kind="Dịch")
    order = [popup.rows_box.itemAt(i).widget().job_id for i in range(popup.rows_box.count())]
    assert order == [first.id, second.id]


def test_refresh_rebuilds_from_the_registry(popup, registry):
    registry.register(_FakeWorker(), kind="Tải truyện")
    registry.register(_FakeWorker(), kind="Dịch")
    popup.refresh()
    assert len(popup._rows) == 2


def test_the_commands_emit_and_close_the_popup(popup):
    opened, quit_calls = [], []
    popup.open_window.connect(lambda: opened.append(True))
    popup.quit_app.connect(lambda: quit_calls.append(True))

    popup.open_button.click()
    assert opened == [True]
    popup.quit_button.click()
    assert quit_calls == [True]


class TestPopupGeometry:
    SCREEN = QRect(0, 0, 1440, 900)
    SIZE = QSize(320, 200)

    def test_it_hangs_under_the_anchor(self):
        anchor = QRect(700, 0, 24, 24)
        point = popup_at(anchor, self.SCREEN, self.SIZE)
        assert point.y() > anchor.bottom()  # below the menu-bar item, not over it
        assert point.y() - anchor.bottom() <= 8  # …and tucked right under it
        # centred on the icon (QRect.center() is integer-floored, so compare loosely)
        assert abs(point.x() + self.SIZE.width() // 2 - anchor.center().x()) <= 1

    def test_it_is_clamped_to_the_right_edge(self):
        # The menu bar sits top-right, so this is the normal case, not the exotic one.
        point = popup_at(QRect(1430, 0, 24, 24), self.SCREEN, self.SIZE)
        assert point.x() + self.SIZE.width() <= self.SCREEN.right()

    def test_it_is_clamped_to_the_left_edge(self):
        point = popup_at(QRect(0, 0, 24, 24), self.SCREEN, self.SIZE)
        assert point.x() >= self.SCREEN.left()

    def test_it_is_clamped_to_the_bottom(self):
        point = popup_at(QRect(700, 880, 24, 24), self.SCREEN, self.SIZE)
        assert point.y() + self.SIZE.height() <= self.SCREEN.bottom()


class TestBrowserJobHint:
    """A paused upload parks a live Chrome + Google session, so the row must say so."""

    def test_the_warning_survives_a_pause(self, popup, registry):
        # It is rewritten on every state flip, so a tooltip merely assigned from outside
        # would vanish on the first press — exactly when the warning matters most.
        worker = _FakeWorker()
        job = registry.register(worker, kind="Tải video lên", novel="Truyện A")
        button = popup._rows[job.id].pause_button
        assert "Chrome" in button.toolTip()
        button.click()
        assert "Chrome" in button.toolTip()
        assert "Chạy tiếp" in button.toolTip()  # …alongside the normal text

    def test_a_local_job_carries_no_browser_warning(self, popup, registry):
        job = registry.register(_FakeWorker(), kind="Nghe audio", novel="Truyện A")
        assert "Chrome" not in popup._rows[job.id].pause_button.toolTip()

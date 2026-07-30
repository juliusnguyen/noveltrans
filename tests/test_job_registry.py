"""JobRegistry — what the menu-bar popup reads to know what is running."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

from noveltrans.gui.jobs import Job, JobRegistry


class _FakeWorker(QObject):
    """Stands in for a PausableWorker: same signals, same pause API, no thread."""

    progress = Signal(int, int, str)
    finished = Signal()

    def __init__(self, finished_already: bool = False):
        super().__init__()
        self._finished = finished_already
        self.paused = False

    def isFinished(self) -> bool:  # noqa: N802 — mirrors QThread's Qt-cased API
        return self._finished

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


@pytest.fixture
def registry():
    return JobRegistry()


def test_register_adds_a_job_and_emits(qapp, registry):
    seen: list[Job] = []
    registry.job_added.connect(seen.append)
    job = registry.register(_FakeWorker(), kind="Tải truyện", novel="Truyện A")
    assert job.kind == "Tải truyện"
    assert job.label == "Tải truyện — Truyện A"
    assert seen == [job]
    assert registry.jobs() == [job]


def test_a_job_without_a_novel_labels_as_just_the_kind(qapp, registry):
    job = registry.register(_FakeWorker(), kind="Ghép audio")
    assert job.label == "Ghép audio"


def test_ids_increase(qapp, registry):
    first = registry.register(_FakeWorker(), kind="A")
    second = registry.register(_FakeWorker(), kind="B")
    assert second.id > first.id


def test_progress_updates_the_record_and_emits(qapp, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Dịch", novel="Truyện B")
    changed: list[Job] = []
    registry.job_changed.connect(changed.append)

    worker.progress.emit(142, 200, "Chương 143")
    assert (job.done, job.total, job.message) == (142, 200, "Chương 143")
    assert changed == [job]


def test_finishing_removes_the_job_and_drops_the_worker(qapp, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Dịch")
    removed: list[int] = []
    registry.job_removed.connect(removed.append)

    worker.finished.emit()
    assert removed == [job.id]
    assert registry.jobs() == []
    assert job.worker is None  # must never outlive the thread


def test_progress_after_finish_is_ignored(qapp, registry):
    worker = _FakeWorker()
    registry.register(worker, kind="Dịch")
    worker.finished.emit()
    worker.progress.emit(5, 10, "late")  # must not resurrect the row
    assert registry.jobs() == []


def test_toggle_flips_paused_and_drives_the_worker(qapp, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Nghe audio")
    changed: list[Job] = []
    registry.job_changed.connect(changed.append)

    registry.toggle(job.id)
    assert job.paused and worker.paused
    registry.toggle(job.id)
    assert not job.paused and not worker.paused
    assert len(changed) == 2


def test_pause_and_resume_are_explicit_too(qapp, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Nghe audio")
    registry.pause(job.id)
    assert worker.paused
    registry.resume(job.id)
    assert not worker.paused


def test_a_non_pausable_job_is_never_paused(qapp, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Xuất file", pausable=False)
    registry.toggle(job.id)
    assert not job.paused
    assert not worker.paused


def test_toggling_an_unknown_id_is_a_no_op(qapp, registry):
    registry.toggle(999)  # must not raise
    assert registry.jobs() == []


def test_toggling_a_finished_job_is_a_no_op(qapp, registry):
    worker = _FakeWorker()
    job = registry.register(worker, kind="Dịch")
    worker.finished.emit()
    registry.toggle(job.id)  # must not raise, and must not touch the dead worker
    assert not worker.paused


def test_an_already_finished_worker_is_not_registered(qapp, registry):
    assert registry.register(_FakeWorker(finished_already=True), kind="Dịch") is None
    assert registry.jobs() == []


def test_jobs_keep_start_order_and_counts_are_right(qapp, registry):
    registry.register(_FakeWorker(), kind="Tải truyện")
    second = registry.register(_FakeWorker(), kind="Dịch")
    third = registry.register(_FakeWorker(), kind="Nghe audio")
    registry.toggle(second.id)

    assert [j.kind for j in registry.jobs()] == ["Tải truyện", "Dịch", "Nghe audio"]
    assert registry.running_count() == 3
    assert registry.paused_count() == 1
    assert registry.job(third.id) is third


def test_reset_clears_everything(qapp, registry):
    registry.register(_FakeWorker(), kind="Dịch")
    registry.reset()
    assert registry.jobs() == []
    assert registry.register(_FakeWorker(), kind="Dịch").id == 1


class _NoSignalsWorker:
    """A minimal duck type, like the fakes older tab tests monkeypatch in."""

    def start(self):
        pass

    def isRunning(self):
        return False


def test_a_worker_without_a_finished_signal_is_declined(qapp, registry):
    # Job tracking hangs off the launch path, so it must decline rather than raise —
    # and a job with nothing to clear it would sit in the popup for ever.
    assert registry.register(_NoSignalsWorker(), kind="Tạo video") is None
    assert registry.jobs() == []


def test_register_declines_none(qapp, registry):
    assert registry.register(None, kind="Tạo video") is None


def test_a_worker_without_isfinished_is_still_tracked(qapp, registry):
    class _NoIsFinished(QObject):
        progress = Signal(int, int, str)
        finished = Signal()

    worker = _NoIsFinished()
    job = registry.register(worker, kind="Tạo video")
    assert job is not None
    worker.progress.emit(1, 3, "phần 1")
    assert (job.done, job.total) == (1, 3)

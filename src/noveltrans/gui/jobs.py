"""JobRegistry — the one place that knows what long work is running, app-wide.

The menu-bar popup has to show every running job across N workspaces × 5 tabs, and each
tab privately owns its own worker and progress bar. Rather than thread a new signal up
through every tab → Workspace → MainWindow (≈14 plumbing points, and it would push
per-job state into MainWindow, which is deliberately app-global only), the tabs register
their worker here as they launch it — one line, right beside the existing
`track_worker(...)` call that already marks "a long batch is starting".

**Holds no widget references.** A `Job` keeps the QThread and some strings, nothing more,
and drops the thread the moment the worker finishes. `MainWindow._close_workspace`
deletes the workspace widget out from under a running job, so anything else would leave
the registry pointing at a dead object.

Worker signals arrive from worker threads; Qt queues them onto the GUI thread, so nothing
in here needs locking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal


@dataclass
class Job:
    """One running batch: what it is, which novel, and how far along."""

    id: int
    kind: str  # "Tải truyện", "Dịch", "Nghe audio", …
    novel: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    paused: bool = False
    pausable: bool = True
    worker: object = field(default=None, repr=False)

    @property
    def label(self) -> str:
        return f"{self.kind} — {self.novel}" if self.novel else self.kind


class JobRegistry(QObject):
    job_added = Signal(object)  # Job
    job_changed = Signal(object)  # Job
    job_removed = Signal(int)  # job id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: dict[int, Job] = {}  # insertion-ordered → rows appear in start order
        self._next_id = 1

    # -------------------------------------------------------------- lifecycle

    def register(self, worker, *, kind: str, novel: str = "", pausable: bool = True) -> Job | None:
        """Track `worker` until it finishes. Returns the Job, or None if it isn't trackable.

        Tracking is a side-channel onto the launch path, so it declines rather than
        raises: a worker with no `finished` signal is skipped outright (there would be
        nothing to remove the row, and a job that never clears is worse than none), and a
        worker that has already ended is never added at all.
        """
        if worker is None:
            return None
        finished = getattr(worker, "finished", None)
        if finished is None or not hasattr(finished, "connect"):
            return None
        if getattr(worker, "isFinished", lambda: False)():
            return None
        job = Job(id=self._next_id, kind=kind, novel=novel, pausable=pausable, worker=worker)
        self._next_id += 1
        self._jobs[job.id] = job
        job_id = job.id
        progress = getattr(worker, "progress", None)
        if progress is not None and hasattr(progress, "connect"):
            progress.connect(
                lambda done, total, message="", _id=job_id: self._on_progress(
                    _id, done, total, message
                )
            )
        finished.connect(lambda _id=job_id: self._on_finished(_id))
        self.job_added.emit(job)
        return job

    def _on_progress(self, job_id: int, done: int, total: int, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.done, job.total, job.message = done, total, message
        self.job_changed.emit(job)

    def _on_finished(self, job_id: int) -> None:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        job.worker = None  # never outlive the thread
        self.job_removed.emit(job_id)

    # ------------------------------------------------------------ pause/resume

    def pause(self, job_id: int) -> None:
        self._set_paused(job_id, True)

    def resume(self, job_id: int) -> None:
        self._set_paused(job_id, False)

    def toggle(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            self._set_paused(job_id, not job.paused)

    def _set_paused(self, job_id: int, paused: bool) -> None:
        job = self._jobs.get(job_id)
        if job is None or not job.pausable:
            return
        worker = job.worker
        if worker is None or not hasattr(worker, "pause"):
            return
        worker.pause() if paused else worker.resume()
        job.paused = paused
        self.job_changed.emit(job)

    # ---------------------------------------------------------------- queries

    def job(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def running_count(self) -> int:
        return len(self._jobs)

    def paused_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.paused)

    def reset(self) -> None:
        """Drop everything. Tests only — the app has exactly one registry for its life."""
        for job_id in list(self._jobs):
            self._on_finished(job_id)
        self._next_id = 1


job_registry = JobRegistry()

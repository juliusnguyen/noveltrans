"""PauseGate — hold a worker thread between items, and let cancel through.

Deliberately Qt-free: the same gate is used by QThreads and by the plain pool threads
inside `AudioWorker._run_parallel`, and being pure Python means it unit-tests without a
QApplication.

Two rules, both load-bearing:

* **Cancel always wins.** `wait()` takes a `cancelled` predicate and returns the moment
  it turns true, paused or not. A worker that is paused and then cancelled must exit —
  every tab's `shutdown()` does `cancel()` then `wait(30_000…120_000)` on the thread, so
  a gate that could hold through a cancel would freeze the GUI thread for up to two
  minutes per worker and then abandon a live QThread.

* **The wait is bounded on purpose.** `PausableWorker.cancel()` also calls `resume()`,
  which is the fast path; the 200 ms poll here is the second, independent guard. If a
  future worker forgets to resume its gate on cancel, that bug degrades to a 200 ms
  delay instead of a hang. Two mechanisms, so neither one is a single point of failure.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

# How long `wait` blocks before re-checking `cancelled`. Small enough that a cancel is
# never perceptibly delayed, large enough that a paused job costs nothing.
_POLL_SECONDS = 0.2


class PauseGate:
    """A resumable hold point. Starts open — `wait()` is free until `pause()` is called."""

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()  # open
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        self._resume.clear()

    def resume(self) -> None:
        self._paused = False
        self._resume.set()

    def wait(self, cancelled: Callable[[], bool] | None = None) -> None:
        """Block while paused. Returns immediately if not paused, or once cancelled."""
        while not self._resume.is_set():
            if cancelled is not None and cancelled():
                return
            self._resume.wait(_POLL_SECONDS)

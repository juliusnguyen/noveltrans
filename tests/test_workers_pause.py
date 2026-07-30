"""PausableWorker — pause holds between items, and cancel always gets through.

The parametrized test below is the one that must never be deleted: every tab's
`shutdown()` calls `cancel()` then `wait()` on the thread for up to two minutes, so a
worker whose `cancel()` stopped resuming its gate would freeze the GUI on quit.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from noveltrans.gui.workers import (
    AudioWorker,
    DownloadWorker,
    MergeWorker,
    PausableWorker,
    PlaylistSyncWorker,
    SubtitleUploadWorker,
    SubtitleWorker,
    TranslateWorker,
    VideoWorker,
    YouTubeThumbnailWorker,
    YouTubeUploadWorker,
)

# Every pausable worker, with the least arguments that construct one.
WORKERS = [
    pytest.param(lambda: TranslateWorker(Path("x"), "google", "vi"), id="translate"),
    pytest.param(lambda: AudioWorker(Path("x"), voice="v1"), id="audio"),
    pytest.param(lambda: MergeWorker(Path("x"), voice="v1", fmt="mp3", mode="all"), id="merge"),
    pytest.param(
        lambda: VideoWorker(Path("x"), voice="v1", mode="all", image_path=Path("i.png")),
        id="video",
    ),
    pytest.param(lambda: SubtitleWorker(Path("x"), voice="v1", mode="all"), id="subtitle"),
    pytest.param(lambda: DownloadWorker(Path("x"), 0.0), id="download"),
    pytest.param(lambda: SubtitleUploadWorker([]), id="subtitle-upload"),
    pytest.param(lambda: YouTubeUploadWorker([]), id="youtube-upload"),
    pytest.param(lambda: PlaylistSyncWorker("", []), id="playlist-sync"),
    pytest.param(lambda: YouTubeThumbnailWorker([]), id="thumbnail"),
]


@pytest.mark.parametrize("build", WORKERS)
def test_cancel_always_resumes_the_gate(qapp, build):
    # THE deadlock guard. Without it, quitting with a paused job blocks the GUI thread
    # in `worker.wait(...)` and then abandons a live QThread.
    worker = build()
    worker.pause()
    assert worker.is_paused()
    worker.cancel()
    assert not worker._gate.paused
    assert not worker.is_paused()  # cancelled is not "paused"


@pytest.mark.parametrize("build", WORKERS)
def test_a_fresh_worker_is_neither_paused_nor_cancelled(qapp, build):
    worker = build()
    assert not worker.is_paused()
    assert not worker._cancelled


class _CountingWorker(PausableWorker):
    """Walks a list, holding at the checkpoint between items."""

    def __init__(self, items):
        super().__init__()
        self.items = list(items)
        self.seen: list[int] = []
        self.done = threading.Event()

    def run(self) -> None:
        for item in self.items:
            if self._checkpoint():
                break
            self.seen.append(item)
            time.sleep(0.02)
        self.done.set()


def _wait_until(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pause_holds_the_run_and_resume_continues(qapp):
    worker = _CountingWorker(range(60))
    worker.start()
    assert _wait_until(lambda: len(worker.seen) > 2)

    worker.pause()
    assert _wait_until(lambda: True, 0.15)  # let the in-flight item land
    held = len(worker.seen)
    time.sleep(0.3)
    assert len(worker.seen) == held, "kept working while paused"

    worker.resume()
    assert worker.done.wait(5.0)
    # picked up exactly where it stopped — nothing repeated, nothing skipped
    assert worker.seen == list(range(60))


def test_cancelling_a_paused_run_exits_promptly(qapp):
    worker = _CountingWorker(range(10_000))
    worker.start()
    assert _wait_until(lambda: len(worker.seen) > 2)
    worker.pause()
    time.sleep(0.1)

    started = time.monotonic()
    worker.cancel()
    assert worker.wait(5_000), "paused worker did not exit on cancel"
    assert time.monotonic() - started < 2.0
    assert len(worker.seen) < 10_000  # stopped early, did not run to completion


def test_checkpoint_reports_cancellation(qapp):
    worker = _CountingWorker([])
    assert worker._checkpoint() is False
    worker.cancel()
    assert worker._checkpoint() is True


def test_pause_after_cancel_does_not_re_hold(qapp):
    # A stray pause() arriving after cancel must not resurrect the hold — the worker is
    # already on its way out and something has to join it.
    worker = _CountingWorker(range(10_000))
    worker.start()
    assert _wait_until(lambda: len(worker.seen) > 2)
    worker.cancel()
    worker.pause()
    assert worker.wait(5_000)

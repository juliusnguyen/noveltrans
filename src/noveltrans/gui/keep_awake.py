"""Keep macOS awake while the app is working (download / translate / TTS / merge).

An app-global, ref-counted wake-lock: the first active job spawns `caffeinate -i`, the
last one to finish terminates it, so the Mac idle-sleeps normally when idle. macOS-only
(silent no-op elsewhere). `caffeinate -w <app_pid>` is the leak backstop — it exits with
the app even if we never call terminate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


class _KeepAwake:
    def __init__(self) -> None:
        self._count = 0
        self._proc: subprocess.Popen | None = None
        self._enabled = True

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._stop()  # release immediately if the user opts out mid-run

    def acquire(self) -> None:
        self._count += 1
        if self._count == 1:
            self._start()

    def release(self) -> None:
        if self._count == 0:
            return
        self._count -= 1
        if self._count == 0:
            self._stop()

    def shutdown(self) -> None:
        """Force-release everything (called on app quit)."""
        self._count = 0
        self._stop()

    def _start(self) -> None:
        if not self._enabled or self._proc is not None:
            return
        if sys.platform != "darwin":  # macOS-only; silent no-op elsewhere
            return
        if shutil.which("caffeinate") is None:  # stock on macOS, but degrade safely
            return
        try:
            self._proc = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._proc = None

    def _stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


_manager = _KeepAwake()


def set_enabled(enabled: bool) -> None:
    _manager.set_enabled(enabled)


def shutdown() -> None:
    _manager.shutdown()


def track_worker(worker) -> None:
    """Hold the wake-lock for one worker run. Call right before `worker.start()`.

    Acquires now and releases on the QThread's built-in `finished` signal (emitted once
    when run() returns, even on error/cancel), so the lock balances without touching the
    worker's logic.
    """
    _manager.acquire()
    worker.finished.connect(_manager.release)

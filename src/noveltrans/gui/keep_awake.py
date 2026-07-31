"""Keep the OS awake while the app is working (download / translate / TTS / merge).

An app-global, ref-counted wake-lock: the first active job acquires it, the last one to
finish releases it, so the machine idle-sleeps normally when idle. Two backends, silent
no-op on anything else:

* **macOS**: spawns `caffeinate -i`. `caffeinate -w <app_pid>` is the leak backstop — it
  exits with the app even if we never call terminate.
* **Windows**: `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` — no process
  to spawn, just a flag on the calling thread; released by calling it again with
  `ES_CONTINUOUS` alone. Deliberately omits `ES_DISPLAY_REQUIRED`, matching
  `caffeinate -i`'s "block idle *sleep*, not display sleep".

The real Windows API call lives in `_win_set_execution_state`, isolated from `_start`/
`_stop` so tests can monkeypatch it directly — `ctypes.windll` doesn't exist as an
attribute at all off real Windows, so code that reached it unconditionally would break
every non-Windows dev/CI machine the instant a test flipped `sys.platform`.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _win_set_execution_state(flags: int) -> None:
    """The actual `SetThreadExecutionState` call — see the module docstring."""
    ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]


class _KeepAwake:
    def __init__(self) -> None:
        self._count = 0
        self._proc: subprocess.Popen | None = None
        self._win_active = False
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
        if not self._enabled or self._proc is not None or self._win_active:
            return
        if sys.platform == "darwin":
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
        elif sys.platform == "win32":
            try:
                _win_set_execution_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
                self._win_active = True
            except Exception:  # noqa: BLE001 — a missing/odd runtime must not crash the app
                self._win_active = False
        # else: no backend for this platform — silent no-op

    def _stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._win_active:
            try:
                _win_set_execution_state(_ES_CONTINUOUS)
            except Exception:  # noqa: BLE001, S110
                pass
            self._win_active = False


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

"""Ref-counting + guards for the keep-awake wake-lock (no real caffeinate)."""

from __future__ import annotations

import pytest

from noveltrans.gui import keep_awake


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


@pytest.fixture
def mgr(monkeypatch):
    """A fresh _KeepAwake on macOS with caffeinate present and Popen mocked."""
    m = keep_awake._KeepAwake()
    spawned: list[FakeProc] = []
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    monkeypatch.setattr(keep_awake.shutil, "which", lambda _name: "/usr/bin/caffeinate")

    def fake_popen(*_a, **_k):
        p = FakeProc()
        spawned.append(p)
        return p

    monkeypatch.setattr(keep_awake.subprocess, "Popen", fake_popen)
    return m, spawned


class TestRefCounting:
    def test_acquire_spawns_once(self, mgr):
        m, spawned = mgr
        m.acquire()
        m.acquire()  # second job — same lock, no second process
        assert len(spawned) == 1
        assert m._proc is not None

    def test_release_to_zero_terminates(self, mgr):
        m, spawned = mgr
        m.acquire()
        m.acquire()
        m.release()
        assert not spawned[0].terminated and m._proc is not None  # still one job
        m.release()
        assert spawned[0].terminated and m._proc is None  # last job done → released

    def test_extra_release_is_noop(self, mgr):
        m, _ = mgr
        m.release()  # never acquired
        assert m._count == 0 and m._proc is None

    def test_reacquire_after_release_spawns_again(self, mgr):
        m, spawned = mgr
        m.acquire()
        m.release()
        m.acquire()
        assert len(spawned) == 2

    def test_shutdown_force_releases(self, mgr):
        m, spawned = mgr
        m.acquire()
        m.acquire()
        m.shutdown()
        assert m._count == 0 and spawned[0].terminated and m._proc is None


class TestGuards:
    def test_unsupported_platform_never_spawns(self, monkeypatch):
        m = keep_awake._KeepAwake()
        monkeypatch.setattr(keep_awake.sys, "platform", "linux")
        monkeypatch.setattr(keep_awake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
        m.acquire()
        assert m._proc is None
        assert m._win_active is False

    def test_missing_binary_no_spawn(self, monkeypatch):
        m = keep_awake._KeepAwake()
        monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
        monkeypatch.setattr(keep_awake.shutil, "which", lambda _n: None)
        monkeypatch.setattr(keep_awake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
        m.acquire()
        assert m._proc is None

    def test_disabled_never_spawns(self, mgr):
        m, spawned = mgr
        m.set_enabled(False)
        m.acquire()
        assert not spawned  # opted out → no lock

    def test_disable_mid_lock_stops_process(self, mgr):
        m, spawned = mgr
        m.acquire()
        assert m._proc is not None
        m.set_enabled(False)
        assert spawned[0].terminated and m._proc is None


class TestWindows:
    """The SetThreadExecutionState backend — `_win_set_execution_state` is monkeypatched
    wholesale rather than run for real, since `ctypes.windll` doesn't exist off Windows."""

    @pytest.fixture
    def win_mgr(self, monkeypatch):
        m = keep_awake._KeepAwake()
        calls: list[int] = []
        monkeypatch.setattr(keep_awake.sys, "platform", "win32")
        monkeypatch.setattr(keep_awake, "_win_set_execution_state", calls.append)
        return m, calls

    def test_acquire_sets_continuous_and_system_required(self, win_mgr):
        m, calls = win_mgr
        m.acquire()
        assert calls == [keep_awake._ES_CONTINUOUS | keep_awake._ES_SYSTEM_REQUIRED]
        assert m._win_active is True

    def test_acquire_twice_calls_once(self, win_mgr):
        m, calls = win_mgr
        m.acquire()
        m.acquire()
        assert len(calls) == 1

    def test_release_to_zero_clears_continuous(self, win_mgr):
        m, calls = win_mgr
        m.acquire()
        m.release()
        assert calls[-1] == keep_awake._ES_CONTINUOUS
        assert m._win_active is False

    def test_a_failing_call_leaves_state_inactive(self, monkeypatch):
        m = keep_awake._KeepAwake()
        monkeypatch.setattr(keep_awake.sys, "platform", "win32")

        def _boom(_flags):
            raise OSError("no thread execution state for you")

        monkeypatch.setattr(keep_awake, "_win_set_execution_state", _boom)
        m.acquire()  # must not raise
        assert m._win_active is False

    def test_shutdown_force_releases(self, win_mgr):
        m, calls = win_mgr
        m.acquire()
        m.shutdown()
        assert calls[-1] == keep_awake._ES_CONTINUOUS
        assert m._count == 0 and m._win_active is False


def test_track_worker_acquires_and_releases_on_finished(qapp, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    calls = {"acquire": 0, "release": 0}
    monkeypatch.setattr(
        keep_awake._manager, "acquire", lambda: calls.__setitem__("acquire", calls["acquire"] + 1)
    )
    monkeypatch.setattr(
        keep_awake._manager, "release", lambda: calls.__setitem__("release", calls["release"] + 1)
    )

    class FakeWorker(QObject):
        finished = Signal()

    w = FakeWorker()
    keep_awake.track_worker(w)
    assert calls == {"acquire": 1, "release": 0}  # acquired immediately
    w.finished.emit()
    assert calls == {"acquire": 1, "release": 1}  # released when the run finishes

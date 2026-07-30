"""PauseGate — the hold point workers block on between items.

The deadlock case is the reason this file exists: a paused worker that is then cancelled
must exit, or every tab's `shutdown()` (cancel + wait up to 120 s) freezes the GUI.
"""

from __future__ import annotations

import threading
import time

from noveltrans.gui.pause import PauseGate


def test_an_open_gate_does_not_block():
    gate = PauseGate()
    started = time.monotonic()
    gate.wait(lambda: False)
    assert time.monotonic() - started < 0.1
    assert not gate.paused


def test_pause_blocks_until_resume():
    gate = PauseGate()
    gate.pause()
    released = threading.Event()

    def worker():
        gate.wait(lambda: False)
        released.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert not released.wait(0.3)  # still held

    gate.resume()
    assert released.wait(2.0), "resume() did not release the gate"
    thread.join(2.0)


def test_cancel_releases_a_paused_gate():
    # THE deadlock case. Without this, `cancel(); wait(120_000)` in a tab's shutdown
    # blocks the GUI thread for two minutes and then abandons a live QThread.
    gate = PauseGate()
    gate.pause()
    cancelled = threading.Event()
    returned = threading.Event()

    def worker():
        gate.wait(cancelled.is_set)
        returned.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert not returned.wait(0.3)

    cancelled.set()
    assert returned.wait(2.0), "wait() did not return once cancelled"
    # …and it returned because of the cancel, not because someone resumed it
    assert gate.paused
    thread.join(2.0)


def test_wait_returns_immediately_when_already_cancelled():
    gate = PauseGate()
    gate.pause()
    started = time.monotonic()
    gate.wait(lambda: True)
    assert time.monotonic() - started < 0.5


def test_wait_without_a_predicate_still_resumes():
    gate = PauseGate()
    gate.pause()
    released = threading.Event()
    threading.Thread(target=lambda: (gate.wait(), released.set()), daemon=True).start()
    gate.resume()
    assert released.wait(2.0)


def test_pause_and_resume_are_idempotent():
    gate = PauseGate()
    gate.pause()
    gate.pause()
    assert gate.paused
    gate.resume()
    gate.resume()
    assert not gate.paused
    gate.wait(lambda: False)  # open again, must not block


def test_resume_on_a_fresh_gate_is_a_no_op():
    gate = PauseGate()
    gate.resume()
    assert not gate.paused
    gate.wait(lambda: False)

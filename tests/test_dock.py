"""Showing/hiding the macOS Dock icon at runtime (050).

These touch the real Objective-C runtime, so every test that flips the policy restores it
— pytest's own process is the app being changed.
"""

from __future__ import annotations

import sys

import pytest

from noveltrans.gui import dock


@pytest.fixture
def restore_policy():
    """Put the process's activation policy back however the test leaves it."""
    before = dock.current_policy()
    yield
    if before is not None:
        dock._set_policy(before)


darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Dock only")


@darwin_only
def test_it_is_available_on_macos(qapp):
    assert dock.available()
    assert dock.current_policy() in (0, 1, 2)


@darwin_only
def test_hiding_and_showing_round_trips(qapp, restore_policy):
    assert dock.hide_dock_icon()
    assert dock.current_policy() == dock.POLICY_ACCESSORY
    assert dock.show_dock_icon()
    assert dock.current_policy() == dock.POLICY_REGULAR


@darwin_only
def test_hiding_twice_still_reports_success(qapp, restore_policy):
    # `setActivationPolicy:` returns NO when the policy is already the requested one, so
    # the result must be judged by where we ended up — otherwise a second hide reads as
    # a failure and a caller could conclude the Dock icon is still there.
    assert dock.hide_dock_icon()
    assert dock.hide_dock_icon()
    assert dock.current_policy() == dock.POLICY_ACCESSORY


@darwin_only
def test_showing_twice_still_reports_success(qapp, restore_policy):
    assert dock.show_dock_icon()
    assert dock.show_dock_icon()
    assert dock.current_policy() == dock.POLICY_REGULAR


def test_it_declines_rather_than_raises_off_macos(monkeypatch):
    # Losing the Dock icon must never be able to take the app down, and a caller that
    # cannot hide the icon should still hide the window.
    dock._reset_cache()
    monkeypatch.setattr(dock.sys, "platform", "linux")
    try:
        assert dock.available() is False
        assert dock.hide_dock_icon() is False
        assert dock.show_dock_icon() is False
        assert dock.current_policy() is None
    finally:
        dock._reset_cache()


def test_a_broken_objc_runtime_is_survivable(monkeypatch):
    dock._reset_cache()

    def boom(*_a, **_k):
        raise OSError("no libobjc here")

    monkeypatch.setattr(dock.ctypes, "CDLL", boom)
    try:
        assert dock.available() is False
        assert dock.hide_dock_icon() is False
    finally:
        dock._reset_cache()

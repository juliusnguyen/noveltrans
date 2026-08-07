"""The menu-bar icon and its controller.

Offscreen Qt reports no system tray, which is exactly the degrade path that must keep
the app quittable — so that branch is the one these tests can actually exercise.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QSystemTrayIcon

from noveltrans.gui import tray as tray_module
from noveltrans.gui.jobs import Job, JobRegistry
from noveltrans.gui.tray import TrayController, build_tray_icon, tray_tooltip


class _FakeWindow:
    def __init__(self):
        self.hide_to_tray_enabled = False
        self.shown = 0

    def show_from_tray(self):
        self.shown += 1

    def quit_app(self):
        pass


class TestIcon:
    def test_it_is_a_template_image_on_macos(self, qapp, monkeypatch):
        # macOS only recolours a menu-bar icon per theme if it is a mask; a coloured
        # icon looks wrong on a light bar or while the item is highlighted.
        monkeypatch.setattr(tray_module.sys, "platform", "darwin")
        icon = build_tray_icon()
        assert not icon.isNull()
        assert icon.isMask()

    def test_it_is_a_filled_color_icon_off_macos(self, qapp, monkeypatch):
        # Windows/Linux don't recolour a template/mask icon per-theme, so a pure-black
        # glyph would go invisible against a dark taskbar — it must be filled instead.
        monkeypatch.setattr(tray_module.sys, "platform", "win32")
        icon = build_tray_icon()
        assert not icon.isNull()
        assert not icon.isMask()

    def test_it_carries_a_1x_and_a_2x_pixmap(self, qapp):
        icon = build_tray_icon()
        assert not icon.pixmap(22, 22).isNull()
        assert not icon.pixmap(44, 44).isNull()


class TestTooltip:
    def test_nothing_running(self):
        assert tray_tooltip([]) == "NovelTrans"

    def test_one_job(self):
        assert tray_tooltip([Job(id=1, kind="Dịch")]) == "NovelTrans — 1 tiến trình đang chạy"

    def test_counts_the_paused_ones(self):
        jobs = [Job(id=1, kind="Dịch"), Job(id=2, kind="Nghe audio", paused=True)]
        assert tray_tooltip(jobs) == "NovelTrans — 2 tiến trình đang chạy (1 tạm dừng)"


class TestNoSystemTray:
    """The degrade path: with no tray, close must keep really quitting."""

    def test_it_reports_not_installed(self, qapp):
        assert not QSystemTrayIcon.isSystemTrayAvailable()  # offscreen Qt
        window = _FakeWindow()
        controller = TrayController(window, JobRegistry())
        assert controller.installed is False
        assert controller.tray is None
        assert controller.popup is None

    def test_it_leaves_the_window_quitting_on_close(self, qapp):
        window = _FakeWindow()
        TrayController(window, JobRegistry())
        # If this ever flipped True without a tray, the close button would hide the only
        # window and `setQuitOnLastWindowClosed(False)` would leave no way to quit.
        assert window.hide_to_tray_enabled is False

    def test_it_constructs_no_tray_icon_at_all(self, qapp, monkeypatch):
        monkeypatch.setattr(
            tray_module, "QSystemTrayIcon", _fail("QSystemTrayIcon must not be constructed")
        )
        TrayController(_FakeWindow(), JobRegistry())

    def test_show_popup_is_a_no_op(self, qapp):
        TrayController(_FakeWindow(), JobRegistry()).show_popup()  # must not raise

    def test_notify_finished_is_a_no_op(self, qapp):
        TrayController(_FakeWindow(), JobRegistry()).notify_finished("Dịch — Truyện A")


def _fail(message: str):
    class _Boom:
        # isSystemTrayAvailable is read before anything is built; keep it False so the
        # controller takes the degrade path, and explode on any construction attempt.
        @staticmethod
        def isSystemTrayAvailable() -> bool:  # noqa: N802 — mirrors Qt's API
            return False

        def __init__(self, *a, **k):
            pytest.fail(message)

    return _Boom

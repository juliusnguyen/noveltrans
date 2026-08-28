"""The menu-bar icon and its controller.

Offscreen Qt reports no system tray, which is exactly the degrade path that must keep
the app quittable — so that branch is the one these tests can actually exercise.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QSystemTrayIcon

from noveltrans.gui import tray as tray_module
from noveltrans.gui.icons import load_pixmap
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


def _ink(pixmap):
    """(x, y) of every pixel with enough alpha to be seen, as a set."""
    image = pixmap.toImage()
    return {
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 40
    }


class TestGlyphAsset:
    """The mark is a shipped PNG now (packaging/make_tray_glyph.py), not a drawing."""

    def test_the_master_loads_and_is_trimmed(self, qapp):
        master = load_pixmap(tray_module._GLYPH_ASSET)
        assert not master.isNull()
        # Deliberately says nothing about portrait vs landscape: the mark has shipped as
        # both. What must hold is that make_tray_glyph.py trimmed it, so the ink reaches
        # all four edges and the runtime scaling has no dead margin to waste.
        ink = _ink(master)
        assert min(x for x, _ in ink) == 0
        assert max(x for x, _ in ink) == master.width() - 1
        assert min(y for _, y in ink) == 0
        assert max(y for _, y in ink) == master.height() - 1

    def test_the_menu_bar_size_has_ink_but_is_not_a_solid_block(self, qapp):
        # The failure this catches: the asset regenerating blank (no ink at all) or with
        # the background keyed in as opaque (a filled square). Measured ≈0.25 at 22 px for
        # the current line-art mark; a solid fill of its bounding box would be ≈0.56. The
        # band is deliberately wide — how much ink the mark carries is a design choice,
        # only "none" and "all of it" are bugs.
        fraction = len(_ink(tray_module._glyph(22, QColor(Qt.GlobalColor.black)))) / (22 * 22)
        assert 0.12 < fraction < 0.50

    def test_the_glyph_fits_the_box_at_every_size(self, qapp):
        # The regression this guards: scaling by height alone. That was fine while the
        # mark was portrait, but a landscape mark scaled to 82 % of the box's height comes
        # out wider than the box, and the overflow is silently clipped by the pixmap.
        master = load_pixmap(tray_module._GLYPH_ASSET)
        want = master.width() / master.height()
        for size in (16, 22, 32, 44):
            ink = _ink(tray_module._glyph(size, QColor(Qt.GlobalColor.black)))
            width = max(x for x, _ in ink) - min(x for x, _ in ink) + 1
            height = max(y for _, y in ink) - min(y for _, y in ink) + 1
            assert width <= size, size
            assert height <= round(size * tray_module._GLYPH_HEIGHT_RATIO), size
            # Clipping an overflowing mark would square it up; rounding at these sizes
            # only moves the ratio by a hundredth or two.
            assert abs(width / height - want) < 0.06, size

    def test_it_is_recoloured_off_macos(self, qapp):
        # Proves the CompositionMode_SourceIn refill ran: without it the glyph would stay
        # the asset's own black and vanish against a dark taskbar.
        image = tray_module._glyph(22, tray_module._ACCENT_COLOR).toImage()
        opaque = [
            image.pixelColor(x, y)
            for y in range(22)
            for x in range(22)
            if image.pixelColor(x, y).alpha() > 240
        ]
        assert opaque
        # Compared channel-wise with a tolerance rather than by name: nothing here is
        # fully opaque, and round-tripping through premultiplied alpha moves a channel
        # by a unit or two.
        want = tray_module._ACCENT_COLOR
        for got in opaque:
            assert abs(got.red() - want.red()) <= 2
            assert abs(got.green() - want.green()) <= 2
            assert abs(got.blue() - want.blue()) <= 2

    def test_a_missing_asset_degrades_to_an_empty_glyph(self, qapp, monkeypatch):
        # A packaging slip must cost the app its icon, not its startup.
        monkeypatch.setattr(tray_module, "load_pixmap", lambda name: QPixmap())
        pixmap = tray_module._glyph(22, QColor(Qt.GlobalColor.black))
        assert pixmap.size().toTuple() == (22, 22)
        assert not _ink(pixmap)


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

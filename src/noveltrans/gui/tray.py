"""The system tray/menu-bar icon, and the controller that ties it to the job popup.

Rules worth knowing before changing anything here:

* **On macOS the icon must be a template image.** macOS recolours menu-bar icons itself —
  dark glyph on a light bar, light on dark, inverted while the item is highlighted — but
  only if the image is a mask. The shipped glyph is pure black on transparent and the icon
  is marked `setIsMask(True)`; a coloured PNG would look wrong in one theme or the other.

* **Elsewhere (Windows/Linux) the icon is filled, not masked.** Those platforms don't
  recolour a `setIsMask` icon per-theme the way macOS does, so a pure-black glyph would go
  invisible/low-contrast against a dark taskbar. Instead the same silhouette is refilled
  through `CompositionMode_SourceIn` with the UI's accent, which reads on both light and
  dark taskbars.

* **The glyph is an asset now, not a drawing.** `gui/assets/tray-glyph.png` is generated
  from `design/bar-icon.png` by `packaging/make_tray_glyph.py` and ships as package data
  (declared in `pyproject.toml`). Both PyInstaller specs still need no change: they call
  `collect_data_files("noveltrans")`, which sweeps it up along with the fonts.

* **No tray means close really quits.** `TrayController` sets
  `window.hide_to_tray_enabled` only after a tray icon actually installs. On a machine
  with no system tray (and under offscreen Qt in tests) the flag stays False, so the
  close button keeps its old behaviour. Combined with
  `app.setQuitOnLastWindowClosed(False)` in `app.py`, getting this backwards would make
  the app impossible to quit.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from noveltrans.gui.icons import load_pixmap
from noveltrans.gui.job_popup import JobPopup
from noveltrans.gui.jobs import job_registry
from noveltrans.gui.notify import set_dock_badge
from noveltrans.gui.style import ACCENT

_GLYPH_ASSET = "tray-glyph.png"

# The UI's accent — used as the filled glyph color on platforms that don't recolour a
# template/mask icon per-theme. Taken from the theme rather than the logo: the mark is
# monochrome by design, and tracking `style.ACCENT` keeps it from drifting out of sync.
_ACCENT_COLOR = QColor(ACCENT)

# How much of the icon box the mark may fill, per axis. Vertical is the tight one: macOS
# puts ~18 pt of content in a 22 pt menu-bar item, and the same proportion looks right in a
# Windows notification area. Horizontal is nearly the full box, since the padding either
# side of a menu-bar item is the system's to decide, not ours.
#
# Both axes are honoured rather than just the height: the mark has shipped both portrait
# and landscape (see packaging/make_tray_glyph.py), and scaling a wide mark by height alone
# would push it past the edges of the square pixmap and clip the ears off.
_GLYPH_HEIGHT_RATIO = 0.82
_GLYPH_WIDTH_RATIO = 0.98


def _glyph(size: int, color: QColor) -> QPixmap:
    """The wolf mark centred in a transparent `size`x`size` square, filled with `color`."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    master = load_pixmap(_GLYPH_ASSET)
    if master.isNull():
        return pixmap  # no asset, no icon — never a crash at startup

    scaled = master.scaled(
        max(1, round(size * _GLYPH_WIDTH_RATIO)),
        max(1, round(size * _GLYPH_HEIGHT_RATIO)),
        Qt.AspectRatioMode.KeepAspectRatio,  # fits inside both budgets; never clips
        Qt.TransformationMode.SmoothTransformation,
    )

    painter = QPainter(pixmap)
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    # Repaint the silhouette in `color` while keeping its alpha — the asset itself is
    # black, which is already what macOS's template mask wants.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), color)
    painter.end()
    return pixmap


def build_tray_icon() -> QIcon:
    """A tray/menu-bar QIcon at several sizes.

    macOS gets a pure-black template mask it can recolour per-theme; every other
    platform gets the same glyph filled with the app's accent color instead, since only
    macOS treats `setIsMask` icons specially — see the module docstring.
    """
    is_macos = sys.platform == "darwin"
    color = QColor(Qt.GlobalColor.black) if is_macos else _ACCENT_COLOR
    icon = QIcon()
    sizes = (22, 44) if is_macos else (16, 22, 32, 44)
    for size in sizes:
        icon.addPixmap(_glyph(size, color))
    if is_macos:
        icon.setIsMask(True)  # let macOS invert it per theme — see the module docstring
    return icon


def tray_tooltip(jobs) -> str:
    """Hover text for the menu-bar icon."""
    if not jobs:
        return "NovelTrans"
    paused = sum(1 for job in jobs if job.paused)
    text = f"NovelTrans — {len(jobs)} tiến trình đang chạy"
    if paused:
        text += f" ({paused} tạm dừng)"
    return text


class TrayController(QObject):
    """Owns the menu-bar icon and the popup. `installed` is False where there is no tray."""

    def __init__(self, window, registry=None, parent=None):
        super().__init__(parent)
        self.window = window
        self.registry = registry if registry is not None else job_registry
        self.tray: QSystemTrayIcon | None = None
        self.popup: JobPopup | None = None
        self.installed = False
        self._labels: dict[int, str] = {}

        # Checked before anything is constructed: on a system with no tray we must leave
        # the window's close behaviour alone (see the module docstring).
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray = QSystemTrayIcon(build_tray_icon(), self)
        self.tray.setToolTip(tray_tooltip(self.registry.jobs()))
        self.tray.activated.connect(self._on_activated)

        self.popup = JobPopup(self.registry)
        self.popup.open_window.connect(self.window.show_from_tray)
        self.popup.quit_app.connect(self.window.quit_app)

        self.registry.job_added.connect(self._on_job_added)
        self.registry.job_changed.connect(self._refresh_tooltip)
        self.registry.job_removed.connect(self._on_job_removed)

        self.tray.show()
        self.installed = True
        self.window.hide_to_tray_enabled = True

    # ------------------------------------------------------------------ events

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.Context,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_popup()

    def show_popup(self) -> None:
        if self.popup is None or self.tray is None:
            return
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QRect(0, 0, 1440, 900)
        self.popup.show_at(self.tray.geometry(), available)

    def _refresh_tooltip(self, *_args) -> None:
        if self.tray is not None:
            self.tray.setToolTip(tray_tooltip(self.registry.jobs()))

    def _on_job_added(self, job) -> None:
        self._labels[job.id] = job.label  # the row is gone by the time it finishes
        self._refresh_tooltip()

    def _on_job_removed(self, job_id: int) -> None:
        label = self._labels.pop(job_id, "")
        self._refresh_tooltip()
        # Only when the window is hidden — with it open, the tab's own status line has
        # already said so, and a notification on top of that is noise.
        if label and not self.window.isVisible():
            # Since 050 the Dock tile is gone while hidden, so this badge normally lands
            # nowhere and the notification is the real signal. Kept for the case where
            # `hide_dock_icon()` declined (non-macOS, or an odd Objective-C runtime) and
            # the tile is still there.
            set_dock_badge(1)
            self.notify_finished(label)

    def notify_finished(self, label: str) -> None:
        """Say a job finished, for when the window is hidden and nobody can see the tab.

        The Dock badge is the reliable half: `showMessage` goes through the macOS
        notification centre, which can silently drop it for an unsigned bundle.
        """
        if self.tray is not None:
            self.tray.showMessage("NovelTrans", f"Đã xong: {label}")

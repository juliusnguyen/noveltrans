"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from noveltrans import __version__
from noveltrans.config import AppConfig
from noveltrans.gui import keep_awake
from noveltrans.gui.dock import POLICY_REGULAR, current_policy
from noveltrans.gui.icons import load_pixmap
from noveltrans.gui.main_window import MainWindow
from noveltrans.gui.style import apply_theme
from noveltrans.gui.tray import TrayController
from noveltrans.runtime_env import augment_tool_path, ensure_std_streams


class DockActivateFilter(QObject):
    """Clicking the Dock icon with the window hidden brings it back.

    A free-standing filter rather than a QApplication subclass, so the tests' shared
    QApplication keeps working and the filter can be handed a synthetic event.

    **Only when the Dock icon is actually there.** `ApplicationActivate` fires on *any*
    activation, and clicking our own menu-bar item activates the app too — so without the
    policy check this filter reopened the window (Dock icon and all) the instant the user
    clicked the status item, and the progress panel never appeared. Since 050 the Dock
    tile is gone while hidden, so in the normal hidden state this correctly does nothing
    and the tray click gets to show the popup. The check keeps the Dock-click fallback
    alive for the case where `hide_dock_icon()` declined and the tile is still present.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window

    def eventFilter(self, obj, event) -> bool:
        if (
            event.type() == QEvent.Type.ApplicationActivate
            and not self.window.isVisible()
            and current_policy() == POLICY_REGULAR  # a Dock icon exists to have been clicked
        ):
            self.window.show_from_tray()
        return super().eventFilter(obj, event)


def main() -> int:
    # A console-less Windows build has no stdout/stderr/stdin at all (None, not just
    # redirected) — fix this first, before any dependency (e.g. VieNeu-TTS's first-run
    # model download progress bar) can crash trying to write to it.
    ensure_std_streams()
    # Finder-launched .apps inherit a minimal PATH without Homebrew / ~/.local/bin, so
    # ffmpeg would be invisible (the Tạo video button greys out). Fix PATH before any
    # ffmpeg_available() check or subprocess runs.
    augment_tool_path()

    app = QApplication(sys.argv)
    app.setApplicationName("NovelTrans")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("noveltrans")
    # On macOS the .app bundle's own icon owns the Dock, so this only shows up in a
    # `make run` dev session; on Windows and Linux it is also the window and taskbar
    # icon. A missing asset yields a null pixmap and simply leaves the app iconless.
    app.setWindowIcon(QIcon(load_pixmap("app-icon.png")))
    apply_theme(app)
    # The window may be hidden to the menu bar while jobs run, so hiding it must not be
    # read as "the last window closed, quit". TrayController owns the matching flag on
    # MainWindow, and only sets it once a tray icon actually installed.
    app.setQuitOnLastWindowClosed(False)

    config = AppConfig()
    keep_awake.set_enabled(config.keep_awake_enabled)

    window = MainWindow(config)
    # Kept in locals for the life of app.exec(): nothing else owns them, and a collected
    # tray icon would silently vanish from the menu bar.
    tray = TrayController(window)  # a no-op where there is no system tray
    dock_filter = DockActivateFilter(window)
    app.installEventFilter(dock_filter)

    # Order matters: joining the workers can take a while, and killing `caffeinate` first
    # would let the Mac sleep mid-teardown. Slots fire in connection order.
    app.aboutToQuit.connect(window.shutdown_all)
    app.aboutToQuit.connect(keep_awake.shutdown)  # never leave the Mac awake past quit

    window.show()
    exit_code = app.exec()
    del tray, dock_filter
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

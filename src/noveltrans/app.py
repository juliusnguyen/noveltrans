"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication

from noveltrans import __version__
from noveltrans.config import AppConfig
from noveltrans.gui import keep_awake
from noveltrans.gui.main_window import MainWindow
from noveltrans.gui.style import apply_theme
from noveltrans.gui.tray import TrayController
from noveltrans.runtime_env import augment_tool_path


class DockActivateFilter(QObject):
    """Clicking the Dock icon with the window hidden brings it back.

    A free-standing filter rather than a QApplication subclass, so the tests' shared
    QApplication keeps working and the filter can be handed a synthetic event.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.ApplicationActivate and not self.window.isVisible():
            self.window.show_from_tray()
        return super().eventFilter(obj, event)


def main() -> int:
    # Finder-launched .apps inherit a minimal PATH without Homebrew / ~/.local/bin, so
    # ffmpeg would be invisible (the Tạo video button greys out). Fix PATH before any
    # ffmpeg_available() check or subprocess runs.
    augment_tool_path()

    app = QApplication(sys.argv)
    app.setApplicationName("NovelTrans")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("noveltrans")
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

"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from noveltrans import __version__
from noveltrans.config import AppConfig
from noveltrans.gui import keep_awake
from noveltrans.gui.main_window import MainWindow
from noveltrans.gui.style import apply_theme
from noveltrans.runtime_env import augment_tool_path


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

    config = AppConfig()
    keep_awake.set_enabled(config.keep_awake_enabled)
    app.aboutToQuit.connect(keep_awake.shutdown)  # never leave the Mac awake past quit

    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

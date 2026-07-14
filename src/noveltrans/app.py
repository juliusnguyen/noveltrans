"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from noveltrans import __version__
from noveltrans.config import AppConfig
from noveltrans.gui.main_window import MainWindow
from noveltrans.gui.style import apply_theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NovelTrans")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("noveltrans")
    apply_theme(app)

    window = MainWindow(AppConfig())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

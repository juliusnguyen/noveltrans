"""MainWindow — QTabWidget with the three app tabs + Settings."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.gui.settings_dialog import SettingsDialog
from noveltrans.gui.tab_export import ExportTab
from noveltrans.gui.tab_scrape import ScrapeTab
from noveltrans.gui.tab_translate import TranslateTab
from noveltrans.storage import AppState


def _placeholder(text: str) -> QWidget:
    """Temporary tab body until the real tab widget lands (M3/M5/M6)."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel(text)
    label.setStyleSheet("color: gray; font-size: 14px;")
    layout.addStretch()
    layout.addWidget(label)
    layout.addStretch()
    label.setWordWrap(True)
    return widget


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setWindowTitle("NovelTrans")
        self.resize(1000, 700)

        self.tabs = QTabWidget()
        self.scrape_tab = ScrapeTab(config)
        self.translate_tab = TranslateTab(config)
        self.export_tab = ExportTab(config)
        self.tabs.addTab(self.scrape_tab, "1. Tải truyện")
        self.tabs.addTab(self.translate_tab, "2. Dịch")
        self.tabs.addTab(self.export_tab, "3. Xuất file")
        self.setCentralWidget(self.tabs)

        # cross-session state: reopen the novel you were working on
        self.state = AppState()
        self.scrape_tab.project_changed.connect(self._on_project_touched)
        self.translate_tab.picker.project_selected.connect(self.state.touch_project)
        self.export_tab.picker.project_selected.connect(self.state.touch_project)
        last = self.state.valid_last_project()
        self.scrape_tab.refresh_recent(select_path=last)
        if last:
            self.translate_tab.refresh_projects(select_path=last)
            self.export_tab.refresh_projects(select_path=last)

        settings_action = QAction("&Cài đặt…", self)
        settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        settings_action.triggered.connect(self._open_settings)
        library_action = QAction("&Mở thư mục thư viện", self)
        library_action.triggered.connect(self._open_library)
        menu = self.menuBar().addMenu("&App")
        menu.addAction(settings_action)
        menu.addAction(library_action)

        self.statusBar().showMessage("Sẵn sàng")

        # restore window geometry from the previous session
        geometry = self.config.window_geometry
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _on_project_touched(self, path: str) -> None:
        self.state.touch_project(path)
        self.translate_tab.refresh_projects(path)
        self.export_tab.refresh_projects(path)

    def _open_settings(self) -> None:
        SettingsDialog(self.config, self).exec()

    def _open_library(self) -> None:
        library_dir = self.config.library_dir
        library_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(library_dir)))

    def closeEvent(self, event) -> None:
        self.config.window_geometry = self.saveGeometry()
        for tab_index in range(self.tabs.count()):
            tab = self.tabs.widget(tab_index)
            if hasattr(tab, "shutdown"):
                tab.shutdown()
        super().closeEvent(event)

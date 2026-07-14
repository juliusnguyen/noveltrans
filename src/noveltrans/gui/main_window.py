"""MainWindow — hosts N independent Workspace tabs (each a full crawl/translate flow).

The four-tab stack itself lives in Workspace; MainWindow keeps only app-global
concerns: Settings, the library shortcut, window geometry, the Dock badge, the single
shared AppState, and the cross-workspace same-project guard.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabBar,
    QTabWidget,
    QToolButton,
)

from noveltrans.config import AppConfig
from noveltrans.gui.notify import clear_dock_badge
from noveltrans.gui.settings_dialog import SettingsDialog
from noveltrans.gui.workspace import Workspace
from noveltrans.storage import AppState

_MAX_TAB_LABEL = 24  # truncate a novel title to this before the outer tab shows it


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setWindowTitle("NovelTrans")
        self.resize(1000, 700)

        # one shared state file across all workspaces; a project path may be open in at
        # most one workspace at a time (the guard below enforces it)
        self.state = AppState()
        self._open_paths: dict[str, Workspace] = {}
        self._ws_counter = 0  # monotonic, so labels stay stable as middle tabs close

        # browser-style outer tab bar (distinct from the inner step-tabs): document
        # mode flattens it, tabs don't stretch, and it scrolls once there are many
        # our own ✕ button per tab (Qt's default close icon is invisible on this dark
        # theme); tabsClosable stays off so the two don't fight
        self.workspaces = QTabWidget()
        self.workspaces.setObjectName("workspaceTabs")
        self.workspaces.setDocumentMode(True)
        self.workspaces.setMovable(True)
        self.workspaces.setUsesScrollButtons(True)
        self.workspaces.tabBar().setExpanding(False)
        self.workspaces.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.setCentralWidget(self.workspaces)

        # flat icon-style corner buttons: Settings left, "＋ new workspace" right
        self.settings_button = QPushButton("⚙")
        self.settings_button.setObjectName("cornerButton")
        self.settings_button.setToolTip("Cài đặt (thư viện, engine dịch, cookie…)")
        self.settings_button.clicked.connect(self._open_settings)
        self.workspaces.setCornerWidget(self.settings_button, Qt.Corner.TopLeftCorner)

        self.new_button = QPushButton("＋")
        self.new_button.setObjectName("cornerButton")
        self.new_button.setToolTip("Mở truyện mới trong tab riêng (Cmd/Ctrl+T)")
        self.new_button.clicked.connect(lambda: self._add_workspace())
        self.workspaces.setCornerWidget(self.new_button, Qt.Corner.TopRightCorner)

        self._build_menu()

        # first workspace reopens the novel from the previous session (old behaviour)
        self._add_workspace(reopen_last=True)

        self.statusBar().showMessage("Sẵn sàng")

        geometry = self.config.window_geometry
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _build_menu(self) -> None:
        new_action = QAction("&Truyện mới (tab)", self)
        new_action.setShortcut(QKeySequence.StandardKey.AddTab)  # Cmd/Ctrl+T
        new_action.triggered.connect(lambda: self._add_workspace())
        close_action = QAction("Đón&g tab hiện tại", self)
        close_action.setShortcut(QKeySequence.StandardKey.Close)  # Cmd/Ctrl+W
        close_action.triggered.connect(
            lambda: self._close_workspace(self.workspaces.currentIndex())
        )
        settings_action = QAction("&Cài đặt…", self)
        settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        settings_action.triggered.connect(self._open_settings)
        library_action = QAction("&Mở thư mục thư viện", self)
        library_action.triggered.connect(self._open_library)

        menu = self.menuBar().addMenu("&App")
        menu.addAction(new_action)
        menu.addAction(close_action)
        menu.addSeparator()
        menu.addAction(settings_action)
        menu.addAction(library_action)

    # ----------------------------------------------------------- workspaces

    def _add_workspace(self, reopen_last: bool = False) -> Workspace:
        ws = Workspace(self.config, self.state)
        ws.set_open_guard(self._claim_project)
        ws.title_changed.connect(lambda title, w=ws: self._set_ws_title(w, title))
        # scan/download paths announce the project after opening; claim it (always free
        # for a freshly-scanned novel). The picker path vetoes *before* opening instead.
        ws.project_opened.connect(lambda path, w=ws: self._claim_project(w, path))

        self._ws_counter += 1
        index = self.workspaces.addTab(ws, f"Truyện {self._ws_counter}")
        close_button = QToolButton()
        close_button.setObjectName("tabCloseButton")
        close_button.setText("✕")
        close_button.setToolTip("Đóng tab (Cmd/Ctrl+W)")
        close_button.clicked.connect(
            lambda _=False, w=ws: self._close_workspace(self.workspaces.indexOf(w))
        )
        self.workspaces.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, close_button)
        self.workspaces.setCurrentIndex(index)
        if reopen_last:
            ws.reopen_last_project()
        return ws

    def _close_workspace(self, index: int) -> None:
        if index < 0:
            return
        ws = self.workspaces.widget(index)
        if ws is None:
            return
        if ws.has_running_workers():
            answer = QMessageBox.question(
                self,
                "Đang chạy",
                "Truyện này đang tải/dịch/tạo audio. Đóng tab và huỷ tiến trình?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        ws.shutdown()
        self._release_workspace(ws)
        self.workspaces.removeTab(index)
        ws.deleteLater()
        # never leave an empty window — reopen a fresh blank workspace, like a browser
        if self.workspaces.count() == 0:
            self._add_workspace()

    def _set_ws_title(self, ws: Workspace, title: str) -> None:
        index = self.workspaces.indexOf(ws)
        if index < 0 or not title:
            return  # keep the default "Truyện N" label until a novel actually loads
        if len(title) > _MAX_TAB_LABEL:
            title = title[: _MAX_TAB_LABEL - 1] + "…"
        self.workspaces.setTabText(index, title)

    # --------------------------------------------- same-project-open guard

    def _claim_project(self, ws: Workspace, path: str) -> bool:
        """Grant `ws` ownership of `path`, or refuse if another workspace owns it.

        Returns False (and focuses the owner) on conflict, so the scrape tab can veto
        the open before touching SQLite. Called post-open for scan/download too, where
        a freshly-scanned novel is always free.
        """
        if not path:
            return True
        owner = self._open_paths.get(path)
        if owner is not None and owner is not ws:
            QMessageBox.warning(
                self,
                "Truyện đang mở ở tab khác",
                "Truyện này đang mở ở một tab khác. Đang chuyển sang tab đó để tránh "
                "ghi trùng dữ liệu.",
            )
            self.workspaces.setCurrentWidget(owner)
            return False
        # a workspace holds one project at a time — drop its previous path first
        self._release_workspace(ws, keep=path)
        self._open_paths[path] = ws
        return True

    def _release_workspace(self, ws: Workspace, keep: str = "") -> None:
        for path in [p for p, owner in self._open_paths.items() if owner is ws and p != keep]:
            del self._open_paths[path]

    # ------------------------------------------------------------ app-global

    def changeEvent(self, event) -> None:
        # user brought the app to the front → they've seen any pending alert
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            clear_dock_badge()
        super().changeEvent(event)

    def _open_settings(self) -> None:
        SettingsDialog(self.config, self).exec()

    def _open_library(self) -> None:
        library_dir = self.config.library_dir
        library_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(library_dir)))

    def closeEvent(self, event) -> None:
        self.config.window_geometry = self.saveGeometry()
        for index in range(self.workspaces.count()):
            ws = self.workspaces.widget(index)
            if hasattr(ws, "shutdown"):
                ws.shutdown()
        super().closeEvent(event)

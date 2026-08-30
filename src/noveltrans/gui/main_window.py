"""MainWindow — hosts N independent Workspace tabs (each a full crawl/translate flow).

The four-tab stack itself lives in Workspace; MainWindow keeps only app-global
concerns: Settings, the library shortcut, window geometry, the Dock badge, the single
shared AppState, and the cross-workspace same-project guard.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.gui import shortcuts
from noveltrans.gui.dock import hide_dock_icon, show_dock_icon
from noveltrans.gui.notify import clear_dock_badge
from noveltrans.gui.settings_dialog import SettingsDialog
from noveltrans.gui.style import VERTICAL_WORKSPACE_TABS_QSS
from noveltrans.gui.workspace import Workspace
from noveltrans.gui.workspace_tab_bar import WorkspaceTabBar
from noveltrans.storage import AppState



class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.setWindowTitle("NovelTrans")
        self.resize(1000, 700)

        # Flipped on by TrayController once a menu-bar icon actually installs. Defaults
        # off so that with no system tray the close button still really quits — with
        # `setQuitOnLastWindowClosed(False)` in app.py, the other way round would leave
        # the app running with no window and no way to get back to it.
        self.hide_to_tray_enabled = False
        self._shut_down = False

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
        # BEFORE any addTab: setTabBar replaces the object that owns all tab metadata,
        # so swapping it later would empty the window. One bar serves both orientations.
        self.workspaces.setTabBar(WorkspaceTabBar())
        self.workspaces.setObjectName("workspaceTabs")
        self.workspaces.setDocumentMode(True)
        self.workspaces.setMovable(True)
        self.workspaces.setUsesScrollButtons(True)
        self.workspaces.tabBar().setExpanding(False)
        self.workspaces.tabBar().setElideMode(Qt.TextElideMode.ElideRight)

        # flat icon-style buttons: Settings, then "＋ new workspace".
        self.settings_button = QPushButton("⚙")
        self.settings_button.setObjectName("cornerButton")
        self.settings_button.setToolTip("Cài đặt (thư viện, engine dịch, cookie…)")
        self.settings_button.clicked.connect(self._open_settings)

        self.new_button = QPushButton("＋")
        self.new_button.setObjectName("cornerButton")
        self.new_button.setToolTip("Mở truyện mới trong tab riêng (Cmd/Ctrl+T)")
        self.new_button.clicked.connect(lambda: self._add_workspace())

        # Their home while the bar is vertical. Qt gives a QTabWidget's corner widgets
        # ZERO geometry for West/East — measured: QRect(0,0,0,0) while isVisible() stays
        # True — so on a vertical bar they would simply disappear. This row is where
        # `_apply_tab_orientation` puts them instead; it is hidden in horizontal mode,
        # where the real corners work.
        self.tab_toolbar = QWidget()
        self.tab_toolbar.setObjectName("workspaceToolbar")
        toolbar_row = QHBoxLayout(self.tab_toolbar)
        toolbar_row.setContentsMargins(0, 0, 0, 0)
        toolbar_row.setSpacing(0)
        toolbar_row.addWidget(self.settings_button)
        toolbar_row.addWidget(self.new_button)
        toolbar_row.addStretch(1)

        container = QWidget()
        container_column = QVBoxLayout(container)
        container_column.setContentsMargins(0, 0, 0, 0)
        container_column.setSpacing(0)
        container_column.addWidget(self.tab_toolbar)
        container_column.addWidget(self.workspaces, stretch=1)
        self.setCentralWidget(container)

        self._apply_tab_orientation()
        self._build_menu()

        # first workspace reopens the novel from the previous session (old behaviour)
        self._add_workspace(reopen_last=True)

        self.statusBar().showMessage("Sẵn sàng")

        geometry = self.config.window_geometry
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _apply_tab_orientation(self) -> None:
        """Move the novel bar between the left column and the top strip, live.

        Everything survives the flip — Qt keeps the tabs, their labels, their tooltips,
        their close buttons and the current index — because the QTabBar object never
        changes, only its paint/measure mode does.

        The ⚙/＋ buttons do have to move house. Qt gives a QTabWidget's corner widgets
        zero geometry for West/East, so in vertical mode they live in `tab_toolbar`
        above the bar; in horizontal mode they go back to the real corners, where they
        sit inline with the tabs and cost no vertical space.

        Idempotent, so callers need no before/after bookkeeping.
        """
        vertical = self.config.workspace_tabs_vertical
        bar = self.workspaces.tabBar()
        bar.set_vertical(vertical)
        self.workspaces.setTabPosition(
            QTabWidget.TabPosition.West if vertical else QTabWidget.TabPosition.North
        )
        # A widget-level sheet beats the app-wide one at equal specificity; "" restores it.
        self.workspaces.setStyleSheet(VERTICAL_WORKSPACE_TABS_QSS if vertical else "")

        if vertical:
            self.workspaces.setCornerWidget(None, Qt.Corner.TopLeftCorner)
            self.workspaces.setCornerWidget(None, Qt.Corner.TopRightCorner)
            row = self.tab_toolbar.layout()
            row.insertWidget(0, self.settings_button)
            row.insertWidget(1, self.new_button)
        else:
            self.workspaces.setCornerWidget(self.settings_button, Qt.Corner.TopLeftCorner)
            self.workspaces.setCornerWidget(self.new_button, Qt.Corner.TopRightCorner)
        # Reparenting hides a widget; Qt does not show it again on its own.
        self.settings_button.show()
        self.new_button.show()
        self.tab_toolbar.setVisible(vertical)

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
        # QuitRole makes THIS the macOS app-menu Quit item, so ⌘Q, the popup's "Thoát"
        # and app.quit() all land on the same path instead of three different ones.
        # Kept on self: macOS moves a QuitRole action into the native application menu,
        # so the menu bar is not a reliable place to find it again.
        self.quit_action = QAction("&Thoát", self)
        self.quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.quit_action.setShortcut(QKeySequence("Ctrl+Q"))  # Qt maps Ctrl→⌘ on macOS
        self.quit_action.triggered.connect(self.quit_app)

        menu = self.menuBar().addMenu("&App")
        menu.addAction(new_action)
        menu.addAction(close_action)
        menu.addSeparator()
        menu.addAction(settings_action)
        menu.addAction(library_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)

        # Kept on self so tests can reach it. Must come with the removal of the per-table
        # QShortcut in widgets.enable_cell_copy: two owners of ⌘C in one window is an
        # ambiguous overload, and Qt then fires neither.
        self.edit_menu = shortcuts.build_edit_menu(self)

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
                "Truyện này đang tải/dịch/tạo audio (kể cả khi đang tạm dừng). "
                "Đóng tab và huỷ tiến trình?",
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

    def _set_ws_title(self, ws: Workspace, label: str) -> None:
        """Name a novel's tab, and make the part that doesn't fit reachable on hover.

        No hand truncation: the tab elides to whatever width it has (in either
        orientation), and the tooltip is what makes the cut-off remainder readable — so
        the two must always be set together. A tab with no novel loaded keeps its
        "Truyện N" label and gets NO tooltip: that label is fully visible, and a tooltip
        repeating it would be noise.
        """
        index = self.workspaces.indexOf(ws)
        if index < 0 or not label:
            return  # keep the default "Truyện N" label until a novel actually loads
        self.workspaces.setTabText(index, label)
        self.workspaces.setTabToolTip(index, label)

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
        before = self.config.library_dir
        dialog = SettingsDialog(self.config, self)
        dialog.exec()
        if self.config.library_dir != before:
            self._reload_library()
        self._apply_tab_orientation()  # idempotent — cheaper than tracking the old value
        # A multi-novel OneDrive sync is started from Settings but must not run inside it:
        # Settings is modal and a sync takes hours. It hands the chosen novels over here,
        # where the run gets its own modeless window and the app stays usable.
        if getattr(dialog, "sync_requests", None):
            self._start_onedrive_sync(dialog.sync_requests)

    def _start_onedrive_sync(self, requests: list) -> None:
        from noveltrans.gui.onedrive_sync_dialog import OneDriveSyncWindow

        # Kept on self so the window is not garbage-collected the moment this returns —
        # it owns the worker, and losing it mid-sync would abandon a live browser.
        self._onedrive_sync_window = OneDriveSyncWindow(requests, self)
        self._onedrive_sync_window.show()

    def _reload_library(self) -> None:
        """Re-list every workspace's project pickers after the library folder changed.

        The pickers are otherwise only filled when a workspace is built, so switching
        library kept showing the old folder's novels until the app was restarted.

        Each picker keeps its selection if that project exists in the new library and
        drops it otherwise (`refresh` re-emits `project_selected`, so the tabs close a
        novel that is no longer there). Open workers are unaffected — they hold absolute
        project paths and finish against the folder they started in.
        """
        library_dir = self.config.library_dir
        # The user may have typed a folder that doesn't exist yet; listing it would raise.
        library_dir.mkdir(parents=True, exist_ok=True)
        for index in range(self.workspaces.count()):
            self.workspaces.widget(index).populate_lists()

    def _open_library(self) -> None:
        library_dir = self.config.library_dir
        library_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(library_dir)))

    # -------------------------------------------------------- hide vs. quit

    def show_from_tray(self) -> None:
        """Bring the window back from the menu bar, Dock icon and all.

        The Dock tile goes back FIRST: switching out of Accessory is what gives the app
        an application menu again, and a window raised before the switch can end up
        behind whatever the user was looking at.
        """
        show_dock_icon()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        clear_dock_badge()

    def quit_app(self) -> None:
        """The one way out — ⌘Q, the App menu, and the popup's Thoát all come here."""
        app = QApplication.instance()
        if app is not None:
            app.quit()  # aboutToQuit runs shutdown_all

    def shutdown_all(self) -> None:
        """Cancel and join every workspace's workers. Idempotent.

        Runs on `aboutToQuit` (the normal path) and from `closeEvent` when there is no
        tray to hide into. Cancelling resumes a paused worker's gate, so a paused job
        cannot hold the join open — see `PausableWorker.cancel`.
        """
        if self._shut_down:
            return
        self._shut_down = True
        if self.isVisible():
            # A hidden window has a meaningless geometry; the hide already saved the
            # real one.
            self.config.window_geometry = self.saveGeometry()
        for index in range(self.workspaces.count()):
            ws = self.workspaces.widget(index)
            if hasattr(ws, "shutdown"):
                ws.shutdown()

    def closeEvent(self, event) -> None:
        self.config.window_geometry = self.saveGeometry()
        if self.hide_to_tray_enabled:
            # Hide, never shut down: `ws.shutdown()` cancels and joins every worker,
            # which is the exact opposite of "keep running in the menu bar".
            event.ignore()
            self.hide()
            # Only reached when a tray icon actually installed, so there is always a
            # menu-bar item left to get back in through. Dropping the Dock tile without
            # one would leave the app running with no way to reach it at all.
            hide_dock_icon()
            return
        self.shutdown_all()
        super().closeEvent(event)

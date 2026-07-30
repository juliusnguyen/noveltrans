"""MainWindow workspace-host tests (offscreen Qt).

Cover the structural guarantees of the multi-workspace refactor: one workspace on
launch, independent workspaces, a shared AppState, isolated shutdown, the
last-workspace refill, and the same-project-open guard. Real parallel downloads and
worker lifecycles are verified manually (see 007.02-INITIAL-PLAN.md §Manual).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QCloseEvent

from noveltrans.app import DockActivateFilter
from noveltrans.config import AppConfig
from noveltrans.gui import main_window as mw
from noveltrans.gui.workspace import Workspace
from noveltrans.storage.state import AppState


@pytest.fixture
def main(qapp, tmp_path, monkeypatch):
    # isolate session state so tests never read/write the real ~/.noveltrans, and so
    # "reopen last project" starts empty (no real SQLite project gets opened)
    monkeypatch.setattr(mw, "AppState", lambda: AppState(state_dir=tmp_path))
    window = mw.MainWindow(AppConfig())
    yield window
    window.close()


class TestWorkspaceLifecycle:
    def test_starts_with_one_workspace(self, main):
        assert main.workspaces.count() == 1
        assert isinstance(main.workspaces.widget(0), Workspace)

    def test_new_workspace_is_independent(self, main):
        ws1 = main.workspaces.widget(0)
        ws2 = main._add_workspace()
        assert main.workspaces.count() == 2
        assert ws1 is not ws2
        assert ws1.scrape_tab is not ws2.scrape_tab
        assert ws1.audio_tab is not ws2.audio_tab

    def test_all_workspaces_share_one_appstate(self, main):
        ws1 = main.workspaces.widget(0)
        ws2 = main._add_workspace()
        assert ws1.state is main.state
        assert ws2.state is main.state

    def test_close_shuts_down_only_that_workspace(self, main):
        ws1 = main.workspaces.widget(0)
        ws2 = main._add_workspace()
        closed = []

        def spy(ws, name):
            original = ws.shutdown

            def wrapped():
                closed.append(name)
                original()  # still join real worker threads

            return wrapped

        ws1.shutdown = spy(ws1, "ws1")  # type: ignore[method-assign]
        ws2.shutdown = spy(ws2, "ws2")  # type: ignore[method-assign]
        main._close_workspace(main.workspaces.indexOf(ws2))
        assert closed == ["ws2"]  # only the closed workspace was shut down
        assert main.workspaces.count() == 1

    def test_closing_last_workspace_refills(self, main):
        assert main.workspaces.count() == 1
        main._close_workspace(0)
        # never leaves an empty window
        assert main.workspaces.count() == 1
        assert isinstance(main.workspaces.widget(0), Workspace)

    def test_running_worker_close_is_confirmed(self, main, monkeypatch):
        ws2 = main._add_workspace()
        ws2.has_running_workers = lambda: True  # type: ignore[method-assign]
        monkeypatch.setattr(
            mw.QMessageBox,
            "question",
            lambda *a, **k: mw.QMessageBox.StandardButton.No,
        )
        main._close_workspace(main.workspaces.indexOf(ws2))
        # user declined → tab stays
        assert main.workspaces.count() == 2


class TestSameProjectGuard:
    def test_duplicate_open_is_refused_and_focuses_owner(self, main, monkeypatch):
        ws1 = main.workspaces.widget(0)
        ws2 = main._add_workspace()
        warned = []
        monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: warned.append(a))
        assert main._claim_project(ws1, "/lib/novel-x") is True
        assert main._claim_project(ws2, "/lib/novel-x") is False  # refused
        assert main._open_paths["/lib/novel-x"] is ws1  # original stays the owner
        assert warned  # user was warned
        assert main.workspaces.currentWidget() is ws1  # focused the owner

    def test_switching_project_releases_old_path(self, main):
        ws1 = main.workspaces.widget(0)
        main._claim_project(ws1, "/lib/novel-a")
        main._claim_project(ws1, "/lib/novel-b")
        assert "/lib/novel-a" not in main._open_paths  # released
        assert main._open_paths["/lib/novel-b"] is ws1

    def test_claiming_same_project_twice_from_owner_is_allowed(self, main):
        ws1 = main.workspaces.widget(0)
        assert main._claim_project(ws1, "/lib/novel-a") is True
        assert main._claim_project(ws1, "/lib/novel-a") is True  # idempotent for owner

    def test_closing_workspace_releases_its_project(self, main):
        ws2 = main._add_workspace()
        main._claim_project(ws2, "/lib/novel-c")
        main._close_workspace(main.workspaces.indexOf(ws2))
        assert "/lib/novel-c" not in main._open_paths


class TestSettingsDialog:
    def _config(self, tmp_path):
        from PySide6.QtCore import QSettings

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        return config

    def test_tts_workers_persists(self, qapp, tmp_path):
        from noveltrans.gui.settings_dialog import SettingsDialog

        config = self._config(tmp_path)
        dialog = SettingsDialog(config)
        assert dialog.tts_workers_spin.value() == 1  # default reflected
        dialog.tts_workers_spin.setValue(3)
        dialog.accept()
        assert config.tts_workers == 3


class TestLibraryFolderChange:
    """Switching “Thư mục thư viện” must take effect immediately, not on next launch."""

    def _config(self, tmp_path, library_dir):
        from PySide6.QtCore import QSettings

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        config.library_dir = library_dir
        return config

    def _library_with(self, root, title):
        from noveltrans.models import ChapterRef, NovelMeta
        from noveltrans.storage import NovelProject

        root.mkdir(parents=True, exist_ok=True)
        meta = NovelMeta(url=f"https://example.com/{title}", site="example", title=title)
        refs = [ChapterRef(index=0, title="第1章", url="https://example.com/1")]
        NovelProject.create(root, meta, refs).close()
        return root

    def _titles(self, ws):
        picker = ws.scrape_tab.picker
        return [picker.itemText(i) for i in range(picker.count())]

    def _window(self, tmp_path, monkeypatch, library_dir):
        monkeypatch.setattr(mw, "AppState", lambda: AppState(state_dir=tmp_path))
        return mw.MainWindow(self._config(tmp_path, library_dir))

    def _settings_that_switches_to(self, monkeypatch, window, new_dir):
        """Stand in for the dialog: accepting it is what writes the new library path."""

        class _FakeDialog:
            def __init__(self, config, parent=None):
                self.config = config

            def exec(self):
                self.config.library_dir = new_dir

        monkeypatch.setattr(mw, "SettingsDialog", _FakeDialog)

    def test_new_library_is_listed_without_restarting(self, qapp, tmp_path, monkeypatch):
        old = self._library_with(tmp_path / "old", "Truyện Cũ")
        new = self._library_with(tmp_path / "new", "Truyện Mới")
        window = self._window(tmp_path, monkeypatch, old)
        try:
            ws = window.workspaces.widget(0)
            assert self._titles(ws) == ["Truyện Cũ"]
            self._settings_that_switches_to(monkeypatch, window, new)
            window._open_settings()
            assert self._titles(ws) == ["Truyện Mới"]
        finally:
            window.close()

    def test_every_workspace_is_relisted(self, qapp, tmp_path, monkeypatch):
        old = self._library_with(tmp_path / "old", "Truyện Cũ")
        new = self._library_with(tmp_path / "new", "Truyện Mới")
        window = self._window(tmp_path, monkeypatch, old)
        try:
            ws2 = window._add_workspace()
            self._settings_that_switches_to(monkeypatch, window, new)
            window._open_settings()
            for index in range(window.workspaces.count()):
                assert self._titles(window.workspaces.widget(index)) == ["Truyện Mới"]
            assert ws2 is window.workspaces.widget(1)
        finally:
            window.close()

    def test_all_tabs_are_relisted_not_just_the_first(self, qapp, tmp_path, monkeypatch):
        old = self._library_with(tmp_path / "old", "Truyện Cũ")
        new = self._library_with(tmp_path / "new", "Truyện Mới")
        window = self._window(tmp_path, monkeypatch, old)
        try:
            ws = window.workspaces.widget(0)
            self._settings_that_switches_to(monkeypatch, window, new)
            window._open_settings()
            for tab in (ws.scrape_tab, ws.translate_tab, ws.export_tab,
                        ws.audio_tab, ws.video_tab):
                titles = [tab.picker.itemText(i) for i in range(tab.picker.count())]
                assert titles == ["Truyện Mới"]
        finally:
            window.close()

    def test_a_folder_that_does_not_exist_yet_is_created_not_crashed_on(
        self, qapp, tmp_path, monkeypatch
    ):
        """The path can be typed by hand, and listing a missing folder would raise."""
        old = self._library_with(tmp_path / "old", "Truyện Cũ")
        fresh = tmp_path / "brand-new"
        window = self._window(tmp_path, monkeypatch, old)
        try:
            self._settings_that_switches_to(monkeypatch, window, fresh)
            window._open_settings()
            assert fresh.is_dir()
            assert self._titles(window.workspaces.widget(0)) == []
        finally:
            window.close()

    def test_unchanged_library_does_not_relist(self, qapp, tmp_path, monkeypatch):
        """Only react to an actual change — Cancel and unrelated edits must be no-ops."""
        old = self._library_with(tmp_path / "old", "Truyện Cũ")
        window = self._window(tmp_path, monkeypatch, old)
        try:
            self._settings_that_switches_to(monkeypatch, window, old)
            reloaded = []
            monkeypatch.setattr(window, "_reload_library", lambda: reloaded.append(True))
            window._open_settings()
            assert reloaded == []
        finally:
            window.close()


class TestHideToMenuBar:
    """Closing the window hides it once a tray is installed — and must NOT shut down."""

    def _spy_shutdowns(self, main) -> list:
        closed: list = []
        for index in range(main.workspaces.count()):
            ws = main.workspaces.widget(index)
            original = ws.shutdown

            def wrapped(_ws=ws, _original=original):
                closed.append(_ws)
                _original()

            ws.shutdown = wrapped  # type: ignore[method-assign]
        return closed

    def test_close_hides_instead_of_quitting(self, main):
        main.show()
        main.hide_to_tray_enabled = True
        closed = self._spy_shutdowns(main)

        event = QCloseEvent()
        main.closeEvent(event)

        assert not event.isAccepted()  # ignored → Qt does not destroy the window
        assert not main.isVisible()
        # The whole feature: a hide must not cancel and join the running workers.
        assert closed == []

    def test_close_still_saves_geometry(self, main):
        main.show()
        main.hide_to_tray_enabled = True
        main.config.window_geometry = None
        main.closeEvent(QCloseEvent())
        assert main.config.window_geometry is not None

    def test_without_a_tray_close_still_shuts_down(self, main):
        # The degrade path. If this regressed, a machine with no system tray would hide
        # its only window with no way to bring it back or quit.
        main.hide_to_tray_enabled = False
        closed = self._spy_shutdowns(main)
        main.closeEvent(QCloseEvent())
        assert len(closed) == main.workspaces.count()

    def test_show_from_tray_restores_and_clears_the_badge(self, main, monkeypatch):
        cleared: list = []
        monkeypatch.setattr(mw, "clear_dock_badge", lambda: cleared.append(True))
        main.hide()
        main.show_from_tray()
        assert main.isVisible()
        assert cleared == [True]


class TestQuit:
    def test_quit_app_asks_the_application_to_quit(self, main, monkeypatch):
        calls: list = []
        monkeypatch.setattr(mw.QApplication.instance(), "quit", lambda: calls.append(True))
        main.quit_app()
        assert calls == [True]

    def test_shutdown_all_shuts_every_workspace(self, main):
        main._add_workspace()
        closed = TestHideToMenuBar()._spy_shutdowns(main)
        main.shutdown_all()
        assert len(closed) == 2

    def test_shutdown_all_is_idempotent(self, main):
        closed = TestHideToMenuBar()._spy_shutdowns(main)
        main.shutdown_all()
        main.shutdown_all()  # aboutToQuit and closeEvent can both reach it
        assert len(closed) == 1

    def test_a_quit_action_exists_with_the_macos_quit_role(self, main):
        # QuitRole is what makes this THE macOS Quit item, so ⌘Q, the App menu and the
        # popup's "Thoát" all converge on one path. Asserted on the action rather than
        # the menu bar: macOS relocates the action into the native application menu.
        action = main.quit_action
        assert action.text() == "&Thoát"
        assert action.menuRole() == mw.QAction.MenuRole.QuitRole

    def test_triggering_the_quit_action_quits(self, main, monkeypatch):
        calls: list = []
        monkeypatch.setattr(main, "quit_app", lambda: calls.append(True))
        main.quit_action.triggered.disconnect()
        main.quit_action.triggered.connect(main.quit_app)
        main.quit_action.trigger()
        assert calls == [True]


class TestDockActivate:
    def test_clicking_the_dock_reshows_a_hidden_window(self, main):
        main.hide()
        f = DockActivateFilter(main)
        f.eventFilter(main, QEvent(QEvent.Type.ApplicationActivate))
        assert main.isVisible()

    def test_it_leaves_a_visible_window_alone(self, main, monkeypatch):
        main.show()
        calls: list = []
        monkeypatch.setattr(main, "show_from_tray", lambda: calls.append(True))
        DockActivateFilter(main).eventFilter(main, QEvent(QEvent.Type.ApplicationActivate))
        assert calls == []

    def test_other_events_are_ignored(self, main):
        main.hide()
        DockActivateFilter(main).eventFilter(main, QEvent(QEvent.Type.WindowActivate))
        assert not main.isVisible()

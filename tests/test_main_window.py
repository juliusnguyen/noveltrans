"""MainWindow workspace-host tests (offscreen Qt).

Cover the structural guarantees of the multi-workspace refactor: one workspace on
launch, independent workspaces, a shared AppState, isolated shutdown, the
last-workspace refill, and the same-project-open guard. Real parallel downloads and
worker lifecycles are verified manually (see 007.02-INITIAL-PLAN.md §Manual).
"""

from __future__ import annotations

import pytest

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

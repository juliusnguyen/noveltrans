"""Workspace — one independent crawl/translate/TTS flow (the inner four-tab stack).

MainWindow hosts N of these in an outer tab bar so several novels can be scanned,
downloaded, translated and voiced in parallel. Each workspace owns its own project
selection and its own QThread workers; the shared AppState (recent/last project) is
injected so all workspaces agree on one on-disk state file.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from noveltrans.config import AppConfig
from noveltrans.gui.tab_audio import AudioTab
from noveltrans.gui.tab_export import ExportTab
from noveltrans.gui.tab_scrape import ScrapeTab
from noveltrans.gui.tab_translate import TranslateTab
from noveltrans.storage import AppState


class Workspace(QWidget):
    # loaded novel title → the host updates this workspace's outer tab label
    title_changed = Signal(str)
    # project path opened here → the host registers it for the same-project guard
    project_opened = Signal(str)

    def __init__(self, config: AppConfig, state: AppState, parent=None):
        super().__init__(parent)
        self.config = config
        self.state = state  # shared across all workspaces (one state.json)
        # host guard: (workspace, path) -> may this workspace open that project?
        self._open_guard: Callable[[Workspace, str], bool] | None = None

        self.tabs = QTabWidget()
        self.scrape_tab = ScrapeTab(config)
        self.translate_tab = TranslateTab(config)
        self.export_tab = ExportTab(config)
        self.audio_tab = AudioTab(config)
        self.tabs.addTab(self.scrape_tab, "1. Tải truyện")
        self.tabs.addTab(self.translate_tab, "2. Dịch")
        self.tabs.addTab(self.export_tab, "3. Xuất file")
        self.tabs.addTab(self.audio_tab, "4. Nghe audio")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

        # A scan/load/download in the scrape tab is the authoritative "project opened"
        # event: it fans out to the sibling pickers, the tab title, and the host guard.
        self.scrape_tab.project_changed.connect(self._on_scrape_project)
        # The other pickers only record recency. They must NOT re-refresh here: a
        # picker's refresh() re-emits project_selected, so refreshing from this handler
        # would recurse forever.
        self.translate_tab.picker.project_selected.connect(self.state.touch_project)
        self.export_tab.picker.project_selected.connect(self.state.touch_project)
        self.audio_tab.picker.project_selected.connect(self.state.touch_project)

        # the scrape tab consults the host before opening a project (same-project guard)
        self.scrape_tab.can_open_project = self._can_open_project

        # Every workspace lists the whole library in its pickers so novels opened in
        # other tabs are choosable here too — but with no selection, so a fresh tab
        # doesn't auto-open a novel. Workspace #1 additionally reopens the last project.
        self.populate_lists()

    def set_open_guard(self, guard: "Callable[[Workspace, str], bool]") -> None:
        self._open_guard = guard

    def _can_open_project(self, path: str) -> bool:
        return self._open_guard is None or self._open_guard(self, path)

    def populate_lists(self) -> None:
        library_dir = self.config.library_dir
        for tab in (self.scrape_tab, self.translate_tab, self.export_tab, self.audio_tab):
            tab.picker.refresh(library_dir, default_to_first=False)

    def _on_scrape_project(self, path: str) -> None:
        self.state.touch_project(path)
        # refresh the *other* pickers so a scan in the scrape tab shows up in the rest
        self.translate_tab.refresh_projects(path)
        self.export_tab.refresh_projects(path)
        self.audio_tab.refresh_projects(path)
        self.title_changed.emit(self.scrape_tab.current_title())
        self.project_opened.emit(path)

    def reopen_last_project(self) -> None:
        """Reopen the novel from the previous session (first workspace only)."""
        last = self.state.valid_last_project()
        self.scrape_tab.refresh_recent(select_path=last)
        if last:
            self.translate_tab.refresh_projects(select_path=last)
            self.export_tab.refresh_projects(select_path=last)
            self.audio_tab.refresh_projects(select_path=last)

    def current_project_path(self) -> str:
        project = self.scrape_tab.project
        return str(project.path) if project is not None else ""

    def current_title(self) -> str:
        return self.scrape_tab.current_title()

    def has_running_workers(self) -> bool:
        return any(
            tab.has_running_workers()
            for tab in (self.scrape_tab, self.translate_tab, self.export_tab, self.audio_tab)
        )

    def shutdown(self) -> None:
        for tab in (self.scrape_tab, self.translate_tab, self.export_tab, self.audio_tab):
            tab.shutdown()

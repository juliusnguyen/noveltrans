"""Tab 1 (Tải truyện) widget behaviour."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu

from noveltrans.config import AppConfig
from noveltrans.gui.tab_scrape import ScrapeTab
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject


@pytest.mark.parametrize(
    "label_name", ["title_label", "author_label", "count_label", "desc_label"]
)
def test_novel_info_labels_are_selectable(qapp, label_name):
    # "Thông tin truyện" values must be selectable so the user can copy the title,
    # author, or description (e.g. for a video title/description).
    tab = ScrapeTab(AppConfig())
    flags = getattr(tab, label_name).textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse


def _tab_with_project(qapp, library_dir, n=5) -> ScrapeTab:
    config = AppConfig()
    config.library_dir = library_dir
    meta = NovelMeta(url="https://fake.test/book/1", site="fake", title="T")
    refs = [ChapterRef(index=i, title=f"C{i + 1}", url=f"https://fake.test/{i}") for i in range(n)]
    project = NovelProject.create(library_dir, meta, refs)
    project.close()
    tab = ScrapeTab(config)
    tab._load_project(project.path)
    return tab


def test_range_controls_start_disabled(qapp):
    tab = ScrapeTab(AppConfig())
    assert not tab.range_button.isEnabled()


def test_loading_a_project_sets_range_bounds(qapp, library_dir):
    tab = _tab_with_project(qapp, library_dir, n=5)
    assert tab.range_from.maximum() == 5
    assert tab.range_to.maximum() == 5
    assert tab.range_to.value() == 5  # defaults to the last chapter
    assert tab.range_button.isEnabled()


def test_download_all_uses_the_whole_novel(qapp, library_dir, monkeypatch):
    tab = _tab_with_project(qapp, library_dir)
    scope = _capture_scope(tab, monkeypatch)
    tab._download_all()
    assert scope == [(0, None, False)]


def test_download_range_maps_1based_numbers_to_0based_idx(qapp, library_dir, monkeypatch):
    tab = _tab_with_project(qapp, library_dir)
    tab.range_from.setValue(2)
    tab.range_to.setValue(4)
    scope = _capture_scope(tab, monkeypatch)
    tab._download_range()
    assert scope == [(1, 3, False)]  # chapters 2..4 → idx 1..3, not forced


def test_download_range_tolerates_a_reversed_span(qapp, library_dir, monkeypatch):
    tab = _tab_with_project(qapp, library_dir)
    tab.range_from.setValue(4)
    tab.range_to.setValue(2)
    scope = _capture_scope(tab, monkeypatch)
    tab._download_range()
    assert scope == [(1, 3, False)]


def test_context_menu_offers_from_here_and_only_this(qapp, library_dir, monkeypatch):
    tab = _tab_with_project(qapp, library_dir)
    scope = _capture_scope(tab, monkeypatch)
    menu = QMenu()
    tab._add_download_actions(menu, tab.model.index(2, 0))  # right-click chapter 3
    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Tải từ chương 3" in labels
    assert "Chỉ tải lại chương 3" in labels

    _trigger(menu, "Tải từ chương 3")
    assert scope[-1] == (2, None, False)  # from idx 2 to the end, not forced
    _trigger(menu, "Chỉ tải lại chương 3")
    assert scope[-1] == (2, 2, True)  # only idx 2, forced re-fetch


def _capture_scope(tab, monkeypatch) -> list:
    """Replace _launch_download so scope is recorded instead of starting a worker."""
    scope: list = []
    monkeypatch.setattr(
        tab, "_launch_download", lambda: scope.append((tab._dl_start, tab._dl_end, tab._dl_force))
    )
    return scope


def _trigger(menu: QMenu, text: str) -> None:
    for action in menu.actions():
        if action.text() == text:
            action.trigger()
            return
    raise AssertionError(f"menu action not found: {text}")

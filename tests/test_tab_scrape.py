"""Tab 1 (Tải truyện) widget behaviour."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMenu, QMessageBox

from noveltrans.config import AppConfig
from noveltrans.gui.tab_scrape import ScrapeTab
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import Library, NovelProject


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


# ------------------------------------------------ novels the user writes themselves


def _tab_with_local_project(qapp, library_dir, titles=("Chương 1", "Chương 2")) -> ScrapeTab:
    config = AppConfig()
    config.library_dir = library_dir
    project = Library(library_dir).create_local_project(
        NovelMeta(url="", site="", title="Truyện của tôi", source_lang="vi")
    )
    project.add_chapters(list(titles))
    path = str(project.path)
    project.close()
    tab = ScrapeTab(config)
    tab._load_project(path)
    return tab


def test_local_project_hides_the_download_controls(qapp, library_dir):
    tab = _tab_with_local_project(qapp, library_dir)
    assert not tab.download_button.isEnabled()
    assert not tab.range_button.isEnabled()
    # isHidden, not isVisible: the tab itself is never shown in these tests, so every
    # child reports invisible regardless of what we set.
    assert not tab.add_chapter_button.isHidden()


def test_local_project_leaves_the_url_box_blank(qapp, library_dir):
    # Showing local://3f9c… would only invite someone to press Quét on it.
    tab = _tab_with_local_project(qapp, library_dir)
    assert tab.url_edit.text() == ""


def test_scraped_project_keeps_download_and_hides_add_chapter(qapp, library_dir):
    tab = _tab_with_project(qapp, library_dir)
    assert tab.download_button.isEnabled()
    assert tab.range_button.isEnabled()
    assert tab.add_chapter_button.isHidden()
    assert tab.url_edit.text() == "https://fake.test/book/1"


def test_local_context_menu_offers_delete_not_download(qapp, library_dir):
    tab = _tab_with_local_project(qapp, library_dir)
    menu = QMenu()
    tab._add_download_actions(menu, tab.model.index(1, 0))
    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Xoá chương 2" in labels
    assert "Tải từ chương 2" not in labels
    assert "Chỉ tải lại chương 2" not in labels
    assert "Sửa tên chương" in labels  # renaming still applies


def test_scraped_context_menu_offers_no_delete(qapp, library_dir):
    tab = _tab_with_project(qapp, library_dir)
    menu = QMenu()
    tab._add_download_actions(menu, tab.model.index(1, 0))
    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Xoá chương 2" not in labels


def test_begin_download_is_a_no_op_on_a_local_project(qapp, library_dir, monkeypatch):
    tab = _tab_with_local_project(qapp, library_dir)
    scope = _capture_scope(tab, monkeypatch)
    tab._begin_download(0, None, False)
    assert scope == []


def test_scan_refuses_a_local_url(qapp, library_dir, monkeypatch):
    tab = _tab_with_local_project(qapp, library_dir)
    started: list = []
    monkeypatch.setattr(
        "noveltrans.gui.tab_scrape.ScanWorker", lambda *a, **k: started.append(a) or None
    )
    tab.url_edit.setText("local://deadbeef")
    tab._start_scan()
    assert started == []


def test_add_chapters_appends_and_refreshes_the_table(qapp, library_dir, monkeypatch):
    tab = _tab_with_local_project(qapp, library_dir)

    class _Dialog:
        DialogCode = QDialog.DialogCode

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def titles(self):
            return ["Chương 3"]

    monkeypatch.setattr("noveltrans.gui.tab_scrape.AddChaptersDialog", _Dialog)
    tab._add_chapters()
    assert tab.model.rowCount() == 3
    assert tab.model.chapter_at(2).title == "Chương 3"
    assert tab.count_label.text() == "3"


def test_delete_chapter_removes_the_row_and_its_audio(qapp, library_dir, monkeypatch):
    tab = _tab_with_local_project(qapp, library_dir)
    audio = tab.project.path / "exports" / "audio" / "0002-c2.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"x")
    tab.project.save_audio(1, "exports/audio/0002-c2.mp3", "v1", 1.0, source="original")
    tab.model.set_chapters(tab.project.chapters())

    monkeypatch.setattr(
        "noveltrans.gui.tab_scrape.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )
    tab._delete_chapter(1)
    assert tab.model.rowCount() == 1
    assert not audio.exists()


def test_delete_chapter_respects_a_cancelled_confirm(qapp, library_dir, monkeypatch):
    tab = _tab_with_local_project(qapp, library_dir)
    monkeypatch.setattr(
        "noveltrans.gui.tab_scrape.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )
    tab._delete_chapter(1)
    assert tab.model.rowCount() == 2


def test_delete_chapter_is_refused_on_a_scraped_project(qapp, library_dir, monkeypatch):
    tab = _tab_with_project(qapp, library_dir)
    monkeypatch.setattr(
        "noveltrans.gui.tab_scrape.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )
    tab._delete_chapter(1)
    assert tab.model.rowCount() == 5  # replace_toc would restore it anyway


# ------------------------------------------------ menu-bar job registration (049)


def test_launching_a_download_registers_a_job(qapp, library_dir, monkeypatch):
    from noveltrans.gui.jobs import job_registry

    job_registry.reset()
    tab = _tab_with_project(qapp, library_dir)

    class _Sig:
        def connect(self, *_a):
            pass

    class _FakeWorker:
        progress = _Sig()
        finished = _Sig()

        def __init__(self, *a, **kw):
            self.finished_ok = self.chapter_done = self.chapter_error = _Sig()
            self.daily_limit_hit = _Sig()

        def start(self):
            pass

        def isRunning(self):
            return False

        def isFinished(self):
            return False

    monkeypatch.setattr("noveltrans.gui.tab_scrape.DownloadWorker", _FakeWorker)
    monkeypatch.setattr("noveltrans.gui.tab_scrape.track_worker", lambda *_a: None)
    tab._begin_download(0, None, False)

    jobs = job_registry.jobs()
    assert [j.kind for j in jobs] == ["Tải truyện"]
    assert jobs[0].novel == "T"  # this tab's own novel, not the workspace's
    assert tab.pause_button.isEnabled()
    job_registry.reset()


def test_the_pause_button_starts_disabled(qapp, library_dir):
    tab = _tab_with_project(qapp, library_dir)
    assert not tab.pause_button.isEnabled()  # nothing running yet

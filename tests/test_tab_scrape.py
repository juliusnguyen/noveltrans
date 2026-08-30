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


def _timotxt_tab(qapp, library_dir, n=4) -> ScrapeTab:
    """A tab on a timotxt project whose chapter 1 was stored with an undecoded glyph."""
    config = AppConfig()
    config.library_dir = library_dir
    meta = NovelMeta(
        url="https://www.timotxt.com/2608569069/", site="timotxt", title="T"
    )
    refs = [
        ChapterRef(index=i, title=f"C{i + 1}", url=f"https://www.timotxt.com/2608569069/{i + 1}.html")
        for i in range(n)
    ]
    project = NovelProject.create(library_dir, meta, refs)
    project.save_content(0, "뷁走進院子。")          # residue — needs repair
    project.save_translation(0, "C1", "bản dịch hỏng", "vi")
    project.save_audio(0, "exports/audio/0.wav", "Ngọc Lan", 9.0)
    project.save_content(1, "他走進院子。")           # clean — must be left alone
    project.save_translation(1, "C2", "bản dịch tốt", "vi")
    project.close()
    tab = ScrapeTab(config)
    tab._load_project(project.path)
    return tab


class TestResidueRepair:
    """Feature 071 — repairing chapters stored before the adapter learned to re-fetch."""

    def test_the_button_is_only_shown_for_timotxt(self, qapp, library_dir):
        assert not _tab_with_project(qapp, library_dir).repair_button.isVisible()

    def test_the_button_is_shown_for_a_timotxt_project(self, qapp, library_dir):
        tab = _timotxt_tab(qapp, library_dir)
        assert tab._is_timotxt()
        assert tab.repair_button.isVisibleTo(tab)

    def test_the_detector_finds_only_damaged_chapters(self, qapp, library_dir):
        tab = _timotxt_tab(qapp, library_dir)
        assert [c.index for c in tab._residue_chapters()] == [0]

    def test_a_non_timotxt_project_detects_nothing(self, qapp, library_dir):
        """Hangul in a chapter body only means damage on timotxt. Anywhere else it is
        just text, and offering to re-download would destroy a good translation."""
        config = AppConfig()
        config.library_dir = library_dir
        meta = NovelMeta(url="https://fake.test/book/9", site="fake", title="T")
        refs = [ChapterRef(index=0, title="C1", url="https://fake.test/9")]
        project = NovelProject.create(library_dir, meta, refs)
        project.save_content(0, "뷁走進院子。")
        project.close()
        tab = ScrapeTab(config)
        tab._load_project(project.path)

        assert tab._residue_chapters() == []

    def test_confirming_clears_the_translation_and_audio_and_scopes_the_download(
        self, qapp, library_dir, monkeypatch
    ):
        """The test that fails if someone re-fetches the source without invalidating what
        was derived from it. The damage is not visible in the translation, so a repair that
        keeps it is silently wrong."""
        tab = _timotxt_tab(qapp, library_dir)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        launched: list = []
        monkeypatch.setattr(tab, "_launch_download", lambda: launched.append(tab._dl_indices))

        tab._repair_residue()

        assert launched == [[0]], "the download is scoped to the damaged chapter only"
        assert tab.project.chapter(0).translated == ""
        assert not tab.project.chapter(0).has_audio
        assert tab.project.chapter(1).translated == "bản dịch tốt", "untouched"

    def test_cancelling_changes_nothing(self, qapp, library_dir, monkeypatch):
        tab = _timotxt_tab(qapp, library_dir)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        launched: list = []
        monkeypatch.setattr(tab, "_launch_download", lambda: launched.append(1))

        tab._repair_residue()

        assert launched == []
        assert tab.project.chapter(0).translated == "bản dịch hỏng"
        assert tab.project.chapter(0).has_audio

    def test_the_dialog_names_what_will_be_lost(self, qapp, library_dir, monkeypatch):
        """Manual edits and rewrites go too — the user must see that before agreeing."""
        tab = _timotxt_tab(qapp, library_dir)
        asked: list = []
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.No)[1],
        )

        tab._repair_residue()

        text = asked[0][2]
        assert "1 chương" in text
        assert "sửa tay" in text
        assert "audio" in text

    def test_nothing_to_repair_says_so_and_starts_no_worker(
        self, qapp, library_dir, monkeypatch
    ):
        tab = _timotxt_tab(qapp, library_dir)
        tab.project.save_content(0, "我走進院子。")  # fix it by hand first
        shown: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        launched: list = []
        monkeypatch.setattr(tab, "_launch_download", lambda: launched.append(1))

        tab._repair_residue()

        assert launched == []
        assert shown and "Không có gì cần sửa" in shown[0][1]


class TestRenameNovel:
    """The ✏️ button on the "Thông tin truyện" panel (074).

    QInputDialog is patched out in every case — a real one blocks the suite forever.
    """

    def _tab(self, qapp, library_dir):
        return _tab_with_project(qapp, library_dir)

    def test_the_button_is_disabled_until_a_novel_is_open(self, qapp):
        assert not ScrapeTab(AppConfig()).rename_button.isEnabled()

    def test_it_is_enabled_once_a_novel_is_open(self, qapp, library_dir):
        assert self._tab(qapp, library_dir).rename_button.isEnabled()

    def test_renaming_updates_the_panel_and_the_tab_label(
        self, qapp, library_dir, monkeypatch
    ):
        tab = self._tab(qapp, library_dir)
        titles = []
        tab.title_changed.connect(titles.append)
        monkeypatch.setattr(
            "noveltrans.gui.tab_scrape.QInputDialog.getText",
            lambda *a, **k: ("Trọng Sinh", True),
        )
        tab._rename_novel()
        assert tab.project.meta.display_name() == "Trọng Sinh"
        assert "Trọng Sinh" in tab.title_label.text()
        assert titles and "Trọng Sinh" in titles[-1]

    def test_cancelling_the_prompt_changes_nothing(self, qapp, library_dir, monkeypatch):
        tab = self._tab(qapp, library_dir)
        monkeypatch.setattr(
            "noveltrans.gui.tab_scrape.QInputDialog.getText",
            lambda *a, **k: ("Trọng Sinh", False),
        )
        tab._rename_novel()
        assert tab.project.meta.display_title == ""

    def test_it_tells_the_other_tabs_to_refresh(self, qapp, library_dir, monkeypatch):
        # Workspace._on_scrape_project is what re-labels the pickers and makes the video
        # tab rebuild its parts table under the new name.
        tab = self._tab(qapp, library_dir)
        changed = []
        tab.project_changed.connect(changed.append)
        monkeypatch.setattr(
            "noveltrans.gui.tab_scrape.QInputDialog.getText",
            lambda *a, **k: ("Trọng Sinh", True),
        )
        tab._rename_novel()
        assert changed == [str(tab.project.path)]

    def test_a_rendered_part_keeps_its_old_title_sidecar_in_step(
        self, qapp, library_dir, monkeypatch
    ):
        """The flag-6 fix, reached through the button a user actually presses."""
        tab = self._tab(qapp, library_dir)
        stem = f"{tab.project.meta.slug_name()}-0001-0005"
        part = tab.project.video_dir / stem
        part.mkdir(parents=True)
        (part / f"{stem}.mp4").write_bytes(b"mp4")
        (part / f"{stem}.title.txt").write_text("T - Phần 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "noveltrans.gui.tab_scrape.QInputDialog.getText",
            lambda *a, **k: ("Trọng Sinh", True),
        )
        # A rendered part means the consequences dialog opens; answer "keep the files".
        monkeypatch.setattr(
            "noveltrans.gui.rename_novel._ask", lambda parent, plan, busy: ("keep", False)
        )
        tab._rename_novel()
        assert (part / f"{stem}.title.txt").read_text(
            encoding="utf-8"
        ).strip() == "Trọng Sinh - Phần 1"
        assert (part / f"{stem}.mp4").is_file()  # kept, as chosen

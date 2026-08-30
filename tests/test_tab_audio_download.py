"""AudioTab: narration downloaded from the source site is protected, not re-voiced.

Audio fetched from tieuthuyetmang.com is not something this app can recreate — the user
may not even be entitled to fetch it again — so every path that would overwrite or delete
it has to opt out. These tests pin those opt-outs at the UI layer: the per-row 🔊 button
disappears, the batch regenerate skips the row and says why, and the merge step can
finally see the audio at all.

The storage-level guards (pending_audio, clear_audio) are pinned in test_storage.py.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QMenu

from noveltrans.config import AppConfig
from noveltrans.gui.tab_audio import SOURCE_AUDIO_KEY, AudioTab
from noveltrans.gui.widgets import AudioSourceTableModel
from noveltrans.models import AUDIO_SOURCE_DOWNLOADED, Chapter, SourceAudio


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.tts_use_translation = True
    config.tts_clean_text = False
    return config


def _chapters(count: int = 4) -> list[Chapter]:
    return [
        Chapter(
            index=i,
            title=f"Chương {i + 1}",
            url=f"https://x/{i}",
            content=f"Bản gốc {i}",
            translated=f"Bản dịch {i}",
        )
        for i in range(count)
    ]


def _make_downloaded(chapter: Chapter) -> Chapter:
    """Mark a chapter as carrying narration fetched from the site."""
    chapter.audio_path = f"exports/audio/{chapter.index:04d}-tieuthuyetmang.m4a"
    chapter.audio_voice = "tieuthuyetmang"
    chapter.audio_source = AUDIO_SOURCE_DOWNLOADED
    chapter.audio_seconds = 900.0
    return chapter


TTM_URL = "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem"


def _release(number: int, *, path: str = "", error: str = "") -> SourceAudio:
    return SourceAudio(
        number=number, title=f"[ YTB TẬP {number} ]", ord=number,
        path=path, seconds=120.0 if path else 0.0, error=error,
    )


class _FakeMeta:
    def __init__(self, url: str):
        self.url = url

    def display_name(self) -> str:
        return "Truyện Thử Nghiệm"


class _FakeProject:
    """Just enough NovelProject for the guards — no DB, no filesystem."""

    # Defaults to a site with no downloadable audio, so the pre-existing tests below
    # describe the ordinary case and the download wiring has to opt in explicitly.
    def __init__(self, chapters, url: str = "https://ixdzs.com/read/1/", releases=None):
        self._chapters = chapters
        self.path = "/nowhere"
        self.meta = _FakeMeta(url)
        self._releases = list(releases or [])

    def source_audio(self):
        return list(self._releases)

    def sync_source_audio(self, entries):
        return list(self._releases)

    def source_audio_at(self, number):
        return next((r for r in self._releases if r.number == number), None)

    def chapters(self):
        return self._chapters

    def counts(self):
        audio = sum(1 for c in self._chapters if c.has_audio)
        downloaded_audio = sum(
            1 for c in self._chapters if c.has_audio and c.audio_source == AUDIO_SOURCE_DOWNLOADED
        )
        return {
            "total": len(self._chapters),
            "translated": len(self._chapters),
            "downloaded": len(self._chapters),
            "errors": 0,
            "audio": audio,
            "downloaded_audio": downloaded_audio,
        }


def _tab(qapp, tmp_path, chapters, url: str = "https://ixdzs.com/read/1/", releases=None):
    tab = AudioTab(_config(tmp_path))
    tab.project = _FakeProject(chapters, url, releases)
    tab.model.set_chapters(chapters)
    return tab


def _select_rows(tab: AudioTab, rows: list[int]) -> None:
    selection = tab.table.selectionModel()
    selection.clearSelection()
    for row in rows:
        selection.select(
            tab.table.model().index(row, 0),
            selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
        )


class TestRegenerateSkipsDownloaded:
    def test_regenerable_indices_counts_the_two_skips_apart(self, qapp, tmp_path):
        # row 1 downloaded, row 2 has no source text, rows 0 and 3 are ordinary
        chapters = _chapters()
        _make_downloaded(chapters[1])
        chapters[2].translated = ""
        tab = _tab(qapp, tmp_path, chapters)
        indices, no_text, downloaded = tab._regenerable_indices([0, 1, 2, 3])
        assert indices == [0, 3]
        assert (no_text, downloaded) == (1, 1)

    def test_downloaded_row_is_skipped_even_though_it_has_text(self, qapp, tmp_path):
        # The row has a translation, so the old "empty source" rule would have offered
        # it — re-voicing would replace narration with TTS.
        chapters = _chapters(1)
        _make_downloaded(chapters[0])
        tab = _tab(qapp, tmp_path, chapters)
        assert chapters[0].translated  # precondition: not skipped for lack of text
        assert tab._regenerable_indices([0]) == ([], 0, 1)

    def test_menu_offers_nothing_when_every_selected_row_is_downloaded(self, qapp, tmp_path):
        chapters = [_make_downloaded(c) for c in _chapters(3)]
        tab = _tab(qapp, tmp_path, chapters)
        _select_rows(tab, [0, 1, 2])
        menu = QMenu()
        tab._add_regenerate_actions(menu, tab.table.model().index(0, 0))
        assert menu.actions() == []

    def test_menu_explains_both_skip_reasons(self, qapp, tmp_path):
        chapters = _chapters()
        _make_downloaded(chapters[1])
        chapters[2].translated = ""
        tab = _tab(qapp, tmp_path, chapters)
        _select_rows(tab, [0, 1, 2, 3])
        menu = QMenu()
        tab._add_regenerate_actions(menu, tab.table.model().index(0, 0))
        tip = [a for a in menu.actions() if a.text().startswith("🔊")][0].toolTip()
        assert "chưa có nội dung" in tip
        assert "audio tải về" in tip  # the protected rows get their own wording

    def test_status_line_explains_a_fully_downloaded_selection(self, qapp, tmp_path):
        chapters = [_make_downloaded(c) for c in _chapters(2)]
        tab = _tab(qapp, tmp_path, chapters)
        tab._regenerate_rows([0, 1])
        # not the generic "chưa có bản dịch" message — that would be a lie here
        assert "audio tải về" in tab.status_label.text()


class TestTableRendering:
    def test_downloaded_row_shows_its_own_status(self, qapp, tmp_path):
        chapters = _chapters(2)
        _make_downloaded(chapters[0])
        chapters[1].audio_path = "exports/audio/0002-ngoc-lan.wav"
        chapters[1].audio_voice = "Ngọc Lan"
        tab = _tab(qapp, tmp_path, chapters)
        status = tab.model.index(0, tab.model.STATUS_COLUMN).data()
        assert status == "Đã tải"
        assert tab.model.index(1, tab.model.STATUS_COLUMN).data() == "Đã tạo"

    def test_no_regenerate_button_on_a_downloaded_row(self, qapp, tmp_path):
        # Both the delegate's painter and its click handler gate on UserRole, so a falsy
        # value removes the button rather than merely disabling it.
        chapters = _chapters(2)
        _make_downloaded(chapters[0])
        tab = _tab(qapp, tmp_path, chapters)
        column = tab.model.REGENERATE_COLUMN
        assert not tab.model.index(0, column).data(Qt.ItemDataRole.UserRole)
        assert tab.model.index(1, column).data(Qt.ItemDataRole.UserRole)

    def test_no_regenerate_tooltip_on_a_downloaded_row(self, qapp, tmp_path):
        chapters = _chapters(1)
        _make_downloaded(chapters[0])
        tab = _tab(qapp, tmp_path, chapters)
        cell = tab.model.index(0, tab.model.REGENERATE_COLUMN)
        assert cell.data(Qt.ItemDataRole.ToolTipRole) is None


class TestMergeSource:
    def test_lists_the_tts_voices_the_project_actually_has(self, qapp, tmp_path):
        chapters = _chapters(3)
        chapters[1].audio_path = "exports/audio/0002-ngoc-lan.wav"
        chapters[1].audio_voice = "Ngọc Lan"
        # chapters[0] and [2] have no audio and must not contribute empty entries
        tab = _tab(qapp, tmp_path, chapters)
        tab._refresh_merge_sources()
        voices = [tab.merge_source.itemData(i) for i in range(tab.merge_source.count())]
        assert voices == ["Ngọc Lan"]

    def test_site_audio_is_offered_as_its_own_edition(self, qapp, tmp_path):
        """Not as a voice: it lives in `source_audio`, not on any chapter row."""
        tab = _tab(qapp, tmp_path, _chapters(2), TTM_URL, releases=[_release(1, path="a.mp3")])
        tab._refresh_merge_sources()
        assert tab.merge_source.itemData(0) == SOURCE_AUDIO_KEY
        assert "Audio từ nguồn" in tab.merge_source.itemText(0)

    def test_both_editions_can_be_offered_at_once(self, qapp, tmp_path):
        chapters = _chapters(2)
        chapters[0].audio_path = "exports/audio/0001-ngoc-lan.wav"
        chapters[0].audio_voice = "Ngọc Lan"
        tab = _tab(qapp, tmp_path, chapters, TTM_URL, releases=[_release(1, path="a.mp3")])
        tab._refresh_merge_sources()
        data = [tab.merge_source.itemData(i) for i in range(tab.merge_source.count())]
        assert data == [SOURCE_AUDIO_KEY, "Ngọc Lan"]

    def test_a_release_with_no_file_yet_is_not_offered(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(1), TTM_URL, releases=[_release(1)])
        tab._refresh_merge_sources()
        assert tab.merge_source.itemData(0) != SOURCE_AUDIO_KEY

    def test_falls_back_to_the_synthesis_voice_when_nothing_has_audio(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(2))
        tab.voice_combo.addItem("Ngọc Lan", "Ngọc Lan")
        tab._refresh_merge_sources()
        # never empty: _start_merge still needs something to report "no audio" about
        assert tab.merge_source.currentData() == "Ngọc Lan"


class TestMergingTheSourceEdition:
    """Feature 066: `_start_merge` previewed with `plan_merge_windows` over chapter rows
    while already passing `source_audio=True` to the worker, so "Ghép audio" on the site's
    edition died at the tab's own "Chưa có audio" dialog and MergeWorker — which orders its
    branches correctly — was never started."""

    def _merge(self, qapp, tmp_path, monkeypatch, releases, *, mode="all", rng=None):
        from PySide6.QtWidgets import QMessageBox

        tab = _tab(qapp, tmp_path, _chapters(2), TTM_URL, releases=releases)
        tab._refresh_merge_sources()
        tab.merge_source.setCurrentIndex(tab.merge_source.findData(SOURCE_AUDIO_KEY))
        tab.merge_mode.setCurrentIndex(tab.merge_mode.findData(mode))
        if rng is not None:
            tab.range_from.setValue(rng[0])
            tab.range_to.setValue(rng[1])

        shown, asked, built = [], [], []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.MergeWorker",
            lambda *a, **kw: _StubWorker(built, *a, **kw),
        )
        monkeypatch.setattr("noveltrans.gui.tab_audio.track_worker", lambda *a, **k: None)
        tab._start_merge()
        return tab, shown, asked, built

    def test_it_starts_the_worker_instead_of_reporting_no_audio(
        self, qapp, tmp_path, monkeypatch
    ):
        releases = [_release(1, path="a.mp3"), _release(2, path="b.mp3")]
        tab, shown, _asked, built = self._merge(qapp, tmp_path, monkeypatch, releases)

        assert shown == [], "the releases are downloaded — nothing to report"
        assert len(built) == 1 and built[0]["source_audio"] is True
        tab.shutdown()

    def test_the_confirm_counts_releases_not_chapters(self, qapp, tmp_path, monkeypatch):
        """A source window groups releases; there are 2 of them and 2 unrelated chapters."""
        releases = [_release(1, path="a.mp3"), _release(2, path="b.mp3")]
        tab, _shown, asked, _built = self._merge(qapp, tmp_path, monkeypatch, releases)

        assert "2 mục" in asked[0][2]
        assert "audio từ nguồn" in asked[0][2]
        assert "__source_audio__" not in asked[0][2], "the sentinel is not a voice name"
        tab.shutdown()

    def test_an_empty_range_reports_the_source_specific_advice(
        self, qapp, tmp_path, monkeypatch
    ):
        """The empty case must not tell the user to go voice some chapters — there is no
        voice involved, and "giọng __source_audio__" is not advice either."""
        releases = [_release(1, path="a.mp3"), _release(2, path="b.mp3")]
        tab, shown, _asked, built = self._merge(
            qapp, tmp_path, monkeypatch, releases, mode="range", rng=(5, 6)
        )

        assert built == [], "nothing in range — the worker must not start"
        assert shown and "Chưa tải mục audio nào từ trang nguồn" in shown[0][2]
        tab.shutdown()


class TestSourceAudioModel:
    """Rows are releases. There is deliberately no chapter column."""

    def test_rows_are_releases(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(6), TTM_URL,
                   releases=[_release(1), _release(11)])
        tab._rebuild_source_rows()
        assert tab.source_model.rowCount() == 2, "two releases, not six chapters"

    def test_the_table_has_no_chapter_column(self, qapp, tmp_path):
        assert "Chương" not in AudioSourceTableModel.COLUMNS

    def test_status_separates_downloaded_pending_and_error(self, qapp, tmp_path):
        rels = [_release(1, path="a.mp3"), _release(2), _release(3, error="hỏng")]
        tab = _tab(qapp, tmp_path, _chapters(3), TTM_URL, releases=rels)
        tab._rebuild_source_rows()
        labels = [
            tab.source_model.index(r, AudioSourceTableModel.STATUS_COLUMN).data()
            for r in range(3)
        ]
        assert labels == ["Đã tải", "Chưa tải", "Lỗi"]

    def test_a_finished_download_updates_the_row_in_place(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(2), TTM_URL, releases=[_release(1)])
        tab._rebuild_source_rows()
        status = AudioSourceTableModel.STATUS_COLUMN
        assert tab.source_model.index(0, status).data() == "Chưa tải"
        tab.source_model.update_item(_release(1, path="a.mp3"))
        assert tab.source_model.index(0, status).data() == "Đã tải"


class TestChaptersStayClean:
    """The 059.07 ask: site audio must not show up in the chapter list."""

    def test_a_downloaded_release_leaves_every_chapter_row_untouched(self, qapp, tmp_path):
        chapters = _chapters(4)
        tab = _tab(qapp, tmp_path, chapters, TTM_URL,
                   releases=[_release(1, path="a.mp3"), _release(2, path="b.mp3")])
        tab._rebuild_source_rows()
        statuses = [
            tab.model.index(r, tab.model.STATUS_COLUMN).data() for r in range(len(chapters))
        ]
        assert set(statuses) == {"Chưa tạo"}, "no chapter may report site audio"

    def test_the_chapter_view_still_offers_tts_on_every_row(self, qapp, tmp_path):
        """Previously a downloaded row lost its 🔊 button. With the editions separated
        there is nothing on the chapter row to protect, so TTS is available again."""
        tab = _tab(qapp, tmp_path, _chapters(2), TTM_URL,
                   releases=[_release(1, path="a.mp3")])
        column = tab.model.REGENERATE_COLUMN
        assert all(
            tab.model.index(r, column).data(Qt.ItemDataRole.UserRole) for r in range(2)
        )


class TestViewToggle:
    def test_the_toggle_is_hidden_for_a_source_with_no_audio(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters())
        tab.show()
        tab._sync_download_button()
        assert not tab.view_combo.isVisible()

    def test_the_toggle_is_shown_for_tieuthuyetmang(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL)
        tab.show()
        tab._sync_download_button()
        assert tab.view_combo.isVisible()

    def test_switching_swaps_the_table_model(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL, releases=[_release(1)])
        assert tab.table.model().sourceModel() is tab.model
        tab.view_combo.setCurrentIndex(1)
        assert tab.table.model().sourceModel() is tab.source_model
        tab.view_combo.setCurrentIndex(0)
        assert tab.table.model().sourceModel() is tab.model

    def test_the_tts_buttons_are_off_while_the_audio_list_shows(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL, releases=[_release(1)])
        tab.view_combo.setCurrentIndex(1)
        assert not tab.generate_button.isEnabled()
        tab.view_combo.setCurrentIndex(0)
        assert tab.generate_button.isEnabled()

    def test_reset_buttons_does_not_re_enable_tts_in_the_audio_view(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL, releases=[_release(1)])
        tab.view_combo.setCurrentIndex(1)
        tab._reset_buttons()  # what _on_finished calls
        assert not tab.generate_button.isEnabled()

    def test_regenerate_context_action_is_absent_in_the_audio_view(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL, releases=[_release(1)])
        tab.view_combo.setCurrentIndex(1)
        menu = QMenu()
        tab._add_regenerate_actions(menu, tab.table.model().index(0, 0))
        assert not [a for a in menu.actions() if a.text()]

    def test_leaving_an_unsupported_source_returns_to_the_chapter_view(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL, releases=[_release(1)])
        tab.view_combo.setCurrentIndex(1)
        tab.project = _FakeProject(_chapters())
        tab._sync_download_button()
        assert not tab._in_source_view()
        assert tab.table.model().sourceModel() is tab.model


class TestDownloadWiring:
    def test_refuses_without_a_stored_cookie(self, qapp, tmp_path, monkeypatch):
        tab = _tab(qapp, tmp_path, _chapters(), TTM_URL)
        shown: list[tuple] = []
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.QMessageBox.information",
            lambda *a, **k: shown.append(a),
        )
        tab._start_audio_download()
        assert shown and "cookie" in shown[0][1].lower()
        assert tab._worker is None

    def test_the_selection_is_read_as_release_numbers(self, qapp, tmp_path, monkeypatch):
        tab = _tab(qapp, tmp_path, _chapters(6), TTM_URL,
                   releases=[_release(1), _release(11), _release(21)])
        tab.config.tieuthuyetmang_cookies = "session=abc"
        tab.view_combo.setCurrentIndex(1)
        _select_rows(tab, [1])  # the SECOND release, number 11
        started: list = []
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.AudioDownloadWorker",
            lambda *a, **kw: _StubWorker(started, *a, **kw),
        )
        tab._start_audio_download()
        assert started[0]["numbers"] == [11]

    def test_no_selection_means_everything_the_source_offers(self, qapp, tmp_path, monkeypatch):
        tab = _tab(qapp, tmp_path, _chapters(6), TTM_URL, releases=[_release(1), _release(11)])
        tab.config.tieuthuyetmang_cookies = "session=abc"
        tab.view_combo.setCurrentIndex(1)
        started: list = []
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.AudioDownloadWorker",
            lambda *a, **kw: _StubWorker(started, *a, **kw),
        )
        tab._start_audio_download()
        assert started[0]["numbers"] is None

    def test_the_batch_button_does_not_force(self, qapp, tmp_path, monkeypatch):
        tab = _tab(qapp, tmp_path, _chapters(6), TTM_URL, releases=[_release(1)])
        tab.config.tieuthuyetmang_cookies = "session=abc"
        tab.view_combo.setCurrentIndex(1)
        started: list = []
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.AudioDownloadWorker",
            lambda *a, **kw: _StubWorker(started, *a, **kw),
        )
        tab._start_audio_download()
        assert started[0]["skip_downloaded"] is True


class TestPerRowRedownloadButton:
    def test_offered_on_every_release(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(3), TTM_URL,
                   releases=[_release(1), _release(11, path="b.mp3")])
        tab._rebuild_source_rows()
        column = AudioSourceTableModel.REDOWNLOAD_COLUMN
        assert all(
            tab.source_model.index(r, column).data(Qt.ItemDataRole.UserRole) for r in range(2)
        )

    def test_clicking_it_forces_a_re_fetch_of_that_release_only(
        self, qapp, tmp_path, monkeypatch
    ):
        tab = _tab(qapp, tmp_path, _chapters(6), TTM_URL,
                   releases=[_release(1), _release(11, path="b.mp3")])
        tab.config.tieuthuyetmang_cookies = "session=abc"
        tab.view_combo.setCurrentIndex(1)
        started: list = []
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.AudioDownloadWorker",
            lambda *a, **kw: _StubWorker(started, *a, **kw),
        )
        tab._redownload_row(tab.table.model().index(1, 0))
        assert started[0]["numbers"] == [11]
        assert started[0]["skip_downloaded"] is False

    def test_the_context_action_forces_and_is_absent_in_the_chapter_view(
        self, qapp, tmp_path, monkeypatch
    ):
        tab = _tab(qapp, tmp_path, _chapters(3), TTM_URL, releases=[_release(1)])
        chapter_menu = QMenu()
        tab._add_download_actions(chapter_menu, tab.table.model().index(0, 0))
        assert not [a for a in chapter_menu.actions() if a.text()], "no release behind a chapter"

        tab.view_combo.setCurrentIndex(1)
        menu = QMenu()
        tab._add_download_actions(menu, tab.table.model().index(0, 0))
        started: list = []
        monkeypatch.setattr(
            "noveltrans.gui.tab_audio.AudioDownloadWorker",
            lambda *a, **kw: _StubWorker(started, *a, **kw),
        )
        tab.config.tieuthuyetmang_cookies = "session=abc"
        next(a for a in menu.actions() if "Tải lại" in a.text()).trigger()
        assert started[0]["skip_downloaded"] is False

    def test_the_button_delegate_is_removed_in_the_chapter_view(self, qapp, tmp_path):
        """RowButtonDelegate.paint draws nothing when UserRole is falsy and does not chain
        to the default painter, so leaving it on would blank a chapter-view cell."""
        tab = _tab(qapp, tmp_path, _chapters(2), TTM_URL, releases=[_release(1)])
        tab.view_combo.setCurrentIndex(1)
        column = AudioSourceTableModel.REDOWNLOAD_COLUMN
        assert tab.table.itemDelegateForColumn(column) is tab._redownload_delegate
        tab.view_combo.setCurrentIndex(0)
        assert tab.table.itemDelegateForColumn(column) is tab._plain_delegate


class _StubWorker:
    """Captures the constructor kwargs; never starts a thread."""

    def __init__(self, sink, path, **kwargs):
        sink.append({"path": path, **kwargs})

    def __getattr__(self, name):
        return _Noop()

    def isRunning(self):
        return False

    def start(self):
        pass


class _Noop:
    def connect(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return None

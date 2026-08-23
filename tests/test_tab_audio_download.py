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
from noveltrans.gui.tab_audio import AudioTab
from noveltrans.models import AUDIO_SOURCE_DOWNLOADED, Chapter


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


class _FakeProject:
    """Just enough NovelProject for the guards — no DB, no filesystem."""

    def __init__(self, chapters):
        self._chapters = chapters
        self.path = "/nowhere"

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


def _tab(qapp, tmp_path, chapters) -> AudioTab:
    tab = AudioTab(_config(tmp_path))
    tab.project = _FakeProject(chapters)
    tab.model.set_chapters(chapters)
    return tab


def _select_rows(tab: AudioTab, rows: list[int]) -> None:
    selection = tab.table.selectionModel()
    selection.clearSelection()
    for row in rows:
        selection.select(
            tab.model.index(row, 0),
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
        tab._add_regenerate_actions(menu, tab.model.index(0, 0))
        assert menu.actions() == []

    def test_menu_explains_both_skip_reasons(self, qapp, tmp_path):
        chapters = _chapters()
        _make_downloaded(chapters[1])
        chapters[2].translated = ""
        tab = _tab(qapp, tmp_path, chapters)
        _select_rows(tab, [0, 1, 2, 3])
        menu = QMenu()
        tab._add_regenerate_actions(menu, tab.model.index(0, 0))
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
    def test_lists_the_voices_the_project_actually_has(self, qapp, tmp_path):
        chapters = _chapters(3)
        _make_downloaded(chapters[0])
        chapters[1].audio_path = "exports/audio/0002-ngoc-lan.wav"
        chapters[1].audio_voice = "Ngọc Lan"
        # chapters[2] has no audio and must not contribute an empty entry
        tab = _tab(qapp, tmp_path, chapters)
        tab._refresh_merge_sources()
        voices = [tab.merge_source.itemData(i) for i in range(tab.merge_source.count())]
        assert voices == ["tieuthuyetmang", "Ngọc Lan"]

    def test_downloaded_voice_is_labelled_for_the_user(self, qapp, tmp_path):
        chapters = [_make_downloaded(c) for c in _chapters(1)]
        tab = _tab(qapp, tmp_path, chapters)
        tab._refresh_merge_sources()
        assert "tải từ trang" in tab.merge_source.itemText(0)

    def test_keeps_the_current_pick_across_a_refresh(self, qapp, tmp_path):
        chapters = _chapters(2)
        _make_downloaded(chapters[0])
        chapters[1].audio_path = "exports/audio/0002-ngoc-lan.wav"
        chapters[1].audio_voice = "Ngọc Lan"
        tab = _tab(qapp, tmp_path, chapters)
        tab._refresh_merge_sources()
        tab.merge_source.setCurrentIndex(1)
        assert tab.merge_source.currentData() == "Ngọc Lan"
        tab._refresh_merge_sources()
        assert tab.merge_source.currentData() == "Ngọc Lan"

    def test_falls_back_to_the_synthesis_voice_when_nothing_has_audio(self, qapp, tmp_path):
        tab = _tab(qapp, tmp_path, _chapters(2))
        tab.voice_combo.addItem("Ngọc Lan", "Ngọc Lan")
        tab._refresh_merge_sources()
        # never empty: _start_merge still needs something to report "no audio" about
        assert tab.merge_source.currentData() == "Ngọc Lan"

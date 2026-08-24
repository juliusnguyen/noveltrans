"""Feature 059 — making video from the site's own audio edition.

After 059.07 a downloaded release lives in `source_audio`, not on a chapter row, so the
video tab cannot reach it through `plan_merge_windows` (which filters chapter rows by
voice) at all. It offers the edition as its own entry instead, and plans parts with
`plan_source_windows`, where "phần 1..N" counts RELEASES.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import AppConfig
from noveltrans.gui.tab_video import SOURCE_AUDIO_KEY, VideoTab
from noveltrans.gui.widgets import audio_source_label
from noveltrans.storage import NovelProject
from noveltrans.tts.merge import plan_merge_windows, plan_source_windows

TTS_VOICES = [("Giọng Nữ Miền Nam", "vi-VN-female"), ("Giọng Nam", "vi-VN-male")]


def _config(tmp_path):
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.library_dir = tmp_path / "library"
    return config


def _manifest(*numbers: int) -> list[dict]:
    return [
        {"chapterNumber": n, "title": f"[ YTB TẬP {i + 1} ] Chương {n}-{n + 4}"}
        for i, n in enumerate(numbers)
    ]


def _project_with_releases(library_dir, meta, refs, *numbers, downloaded=True):
    project = NovelProject.create(library_dir, meta, refs)
    project.sync_source_audio(_manifest(*numbers))
    if downloaded:
        for n in numbers:
            rel = f"exports/audio/nguon-{n:04d}.mp3"
            target = project.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00" * 16)
            project.save_source_audio(n, rel, 7200.0)
    path = project.path
    project.close()
    return path


def _project_with_tts(library_dir, meta, refs, voice="vi-VN-female", count=3):
    project = NovelProject.create(library_dir, meta, refs)
    for idx in range(count):
        rel = f"exports/audio/{idx + 1:04d}-x.wav"
        target = project.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 16)
        project.save_audio(idx, rel, voice, 60.0)
    path = project.path
    project.close()
    return path


class TestAudioSourceLabel:
    def test_a_downloaded_adapter_name_says_where_it_came_from(self):
        assert audio_source_label("tieuthuyetmang") == "tieuthuyetmang (tải từ trang)"

    def test_a_tts_voice_keeps_its_pretty_label(self):
        assert audio_source_label("vi-VN-female", {"vi-VN-female": "Giọng Nữ"}) == "Giọng Nữ"

    def test_an_unknown_voice_passes_through_unchanged(self):
        assert audio_source_label("mystery") == "mystery"


class TestVideoTabOffersTheSourceEdition:
    def test_it_is_offered_when_releases_are_downloaded(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        tab = VideoTab(_config(tmp_path))
        tab._on_voices_listed(TTS_VOICES)
        tab._on_project_selected(str(path))
        assert tab.voice_combo.findData(SOURCE_AUDIO_KEY) >= 0
        tab.shutdown()

    def test_the_entry_counts_the_releases(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        row = tab.voice_combo.findData(SOURCE_AUDIO_KEY)
        assert "3 mục" in tab.voice_combo.itemText(row)
        tab.shutdown()

    def test_it_is_not_offered_when_nothing_is_downloaded_yet(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """A listed-but-unfetched release would plan zero parts."""
        path = _project_with_releases(
            library_dir, sample_meta, sample_refs, 1, 11, downloaded=False
        )
        tab = VideoTab(_config(tmp_path))
        tab._on_voices_listed(TTS_VOICES)
        tab._on_project_selected(str(path))
        assert tab.voice_combo.findData(SOURCE_AUDIO_KEY) < 0
        tab.shutdown()

    def test_tts_audio_is_still_offered(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = _project_with_tts(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))
        tab._on_voices_listed(TTS_VOICES)
        tab._on_project_selected(str(path))
        assert tab.voice_combo.findData("vi-VN-female") >= 0
        tab.shutdown()

    def test_both_editions_can_be_offered_at_once(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = _project_with_tts(library_dir, sample_meta, sample_refs)
        project = NovelProject.open(path)
        try:
            project.sync_source_audio(_manifest(1))
            rel = "exports/audio/nguon-0001.mp3"
            (project.path / rel).write_bytes(b"\x00" * 16)
            project.save_source_audio(1, rel, 7200.0)
        finally:
            project.close()
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        assert tab.voice_combo.findData(SOURCE_AUDIO_KEY) >= 0
        assert tab.voice_combo.findData("vi-VN-female") >= 0
        tab.shutdown()

    def test_a_project_with_no_audio_falls_back_to_the_engine_catalogue(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        path = project.path
        project.close()
        tab = VideoTab(_config(tmp_path))
        tab._on_voices_listed(TTS_VOICES)
        tab._on_project_selected(str(path))
        assert tab.voice_combo.findData("vi-VN-female") >= 0
        tab.shutdown()

    def test_a_late_voice_list_does_not_overwrite_the_projects_own(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """TtsVoicesWorker finishes asynchronously; whichever landed last used to win."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab._on_voices_listed(TTS_VOICES)
        assert tab.voice_combo.findData(SOURCE_AUDIO_KEY) >= 0
        tab.shutdown()

    def test_the_selection_plans_parts_from_the_releases(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("all"))
        windows = tab._windows_for_current_selection()
        assert windows and sum(len(w.chapters) for w in windows) == 3
        tab.shutdown()


class TestPlanSourceWindows:
    def test_each_release_can_be_its_own_part(self, library_dir, sample_meta, sample_refs):
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        project = NovelProject.open(path)
        try:
            windows = plan_source_windows(project.source_audio(), "batch", batch=1)
        finally:
            project.close()
        assert len(windows) == 3, "21 releases would be 21 parts"
        assert [w.first_num for w in windows] == [1, 2, 3], "numbered by release, not chapter"

    def test_a_release_with_no_file_is_left_out(self, library_dir, sample_meta, sample_refs):
        path = _project_with_releases(
            library_dir, sample_meta, sample_refs, 1, 11, downloaded=False
        )
        project = NovelProject.open(path)
        try:
            assert plan_source_windows(project.source_audio(), "all") == []
        finally:
            project.close()

    def test_chapter_planning_cannot_see_the_source_edition(
        self, library_dir, sample_meta, sample_refs
    ):
        """The separation, stated as a test: releases are simply not chapter audio."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        project = NovelProject.open(path)
        try:
            assert plan_merge_windows(project.chapters(), "tieuthuyetmang", "all") == []
            assert plan_source_windows(project.source_audio(), "all")
        finally:
            project.close()

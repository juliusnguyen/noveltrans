"""Feature 059.07 — `source_audio`: the site's audio edition, stored apart from chapters."""

from __future__ import annotations

import sqlite3

from noveltrans.models import AUDIO_SOURCE_DOWNLOADED, ChapterRef, NovelMeta
from noveltrans.storage import NovelProject

URL = "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem"


def _project(library_dir, n: int = 6):
    meta = NovelMeta(url=URL, site="tieuthuyetmang", title="Truyện Thử Nghiệm")
    refs = [
        ChapterRef(index=i, title=f"Chương {i + 1}", url=f"{URL}/doc/{i + 1}") for i in range(n)
    ]
    return NovelProject.create(library_dir, meta, refs)


def _manifest(*numbers: int) -> list[dict]:
    return [{"chapterNumber": n, "title": f"[ YTB TẬP {i + 1} ]"} for i, n in enumerate(numbers)]


class TestSyncSourceAudio:
    def test_records_the_release_list_in_reading_order(self, library_dir):
        project = _project(library_dir)
        try:
            releases = project.sync_source_audio(_manifest(1, 11, 21))
            assert [r.number for r in releases] == [1, 11, 21]
            assert [r.ord for r in releases] == [1, 2, 3]
        finally:
            project.close()

    def test_re_listing_keeps_what_is_already_downloaded(self, library_dir):
        """A reworded volume title must not cost the user a 200 MB re-download."""
        project = _project(library_dir)
        try:
            project.sync_source_audio(_manifest(1, 11))
            project.save_source_audio(1, "exports/audio/a.mp3", 7200.0)
            project.sync_source_audio(
                [{"chapterNumber": 1, "title": "Tên mới"}, {"chapterNumber": 11, "title": "B"}]
            )
            first = project.source_audio_at(1)
            assert first.path == "exports/audio/a.mp3" and first.seconds == 7200.0
            assert first.title == "Tên mới"
        finally:
            project.close()

    def test_saving_clears_a_previous_error(self, library_dir):
        project = _project(library_dir)
        try:
            project.sync_source_audio(_manifest(1))
            project.mark_source_audio_error(1, "hỏng")
            assert project.source_audio_at(1).error
            project.save_source_audio(1, "exports/audio/a.mp3", 1.0)
            assert not project.source_audio_at(1).error
        finally:
            project.close()

    def test_an_entry_with_no_number_is_ignored(self, library_dir):
        project = _project(library_dir)
        try:
            project.sync_source_audio([{"title": "no number"}, {"chapterNumber": 3}])
            assert [r.number for r in project.source_audio()] == [3]
        finally:
            project.close()


class TestSeparation:
    def test_releases_are_not_chapter_audio(self, library_dir):
        project = _project(library_dir)
        try:
            project.sync_source_audio(_manifest(1, 11))
            project.save_source_audio(1, "exports/audio/a.mp3", 7200.0)
            assert all(not c.audio_path for c in project.chapters())
            assert project.counts()["audio"] == 0
        finally:
            project.close()


class TestMigrationOffChapterRows:
    """059 first stored downloads on `chapters`. The files are 50-200 MB and may no
    longer be re-fetchable, so the upgrade MOVES them rather than dropping them."""

    def _legacy(self, library_dir):
        project = _project(library_dir)
        path = project.path
        project.save_audio(0, "exports/audio/0001-old.mp3", "tieuthuyetmang", 7200.0,
                           source=AUDIO_SOURCE_DOWNLOADED)
        project.save_audio(1, "exports/audio/0002-tts.wav", "vi-VN", 60.0)
        project.close()
        # drop the new table so reopening runs the migration from scratch
        db = sqlite3.connect(path / "chapters.db")
        db.execute("DROP TABLE source_audio")
        db.commit()
        db.close()
        return path

    def test_the_download_moves_into_source_audio(self, library_dir):
        path = self._legacy(library_dir)
        project = NovelProject.open(path)
        try:
            moved = project.source_audio()
            assert [r.number for r in moved] == [1], "keyed by the number in the chapter URL"
            assert moved[0].path == "exports/audio/0001-old.mp3"
            assert moved[0].seconds == 7200.0
        finally:
            project.close()

    def test_the_chapter_row_is_cleared_so_it_stops_showing_site_audio(self, library_dir):
        path = self._legacy(library_dir)
        project = NovelProject.open(path)
        try:
            assert not project.chapter(0).audio_path
            assert project.chapter(0).audio_source != AUDIO_SOURCE_DOWNLOADED
        finally:
            project.close()

    def test_tts_audio_is_left_exactly_where_it_is(self, library_dir):
        path = self._legacy(library_dir)
        project = NovelProject.open(path)
        try:
            assert project.chapter(1).audio_path == "exports/audio/0002-tts.wav"
            assert project.chapter(1).audio_voice == "vi-VN"
        finally:
            project.close()

    def test_running_twice_is_harmless(self, library_dir):
        path = self._legacy(library_dir)
        NovelProject.open(path).close()
        project = NovelProject.open(path)
        try:
            assert len(project.source_audio()) == 1
        finally:
            project.close()

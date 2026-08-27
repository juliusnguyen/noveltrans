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


def _source_video_worker(tmp_path, path, monkeypatch, **kwargs):
    """A source-edition VideoWorker with ffmpeg stubbed out, plus the list it renders into.

    Returns `(worker, rendered)`; call `worker.run()` synchronously — no QThread is started
    anywhere in this suite.
    """
    from pathlib import Path

    from noveltrans.gui.workers import VideoWorker

    rendered = []

    def _fake_render_video(segments, image_path, out_path, *a, **k):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake mp4")
        rendered.append(out_path)

    monkeypatch.setattr("noveltrans.tts.video.render_video", _fake_render_video)
    image = tmp_path / "bg.png"
    image.write_bytes(b"fake")
    kwargs.setdefault("mode", "batch")
    worker = VideoWorker(
        path, voice=SOURCE_AUDIO_KEY, image_path=str(image), source_audio=True, **kwargs
    )
    return worker, rendered


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


class TestSourceAudioBatchVideo:
    """Feature 066 — "Tạo video" (batch, the tab's default mode) on the source edition.

    `VideoWorker.run()` used to test `mode == "batch"` before `source_audio`, so the
    everyday press planned over chapter rows filtered by voice `__source_audio__`, found
    nothing, and died with "Chưa tải mục audio nào từ trang nguồn…". Only the per-row
    "Tạo" (mode="range") and the multi-select (explicit_windows) dodged that branch.
    """

    def _worker(self, tmp_path, path, monkeypatch, **kwargs):
        return _source_video_worker(tmp_path, path, monkeypatch, **kwargs)

    def test_batch_mode_renders_the_source_edition_instead_of_failing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The reported bug, as a test: the default mode must not report "chưa tải"."""
        from pathlib import Path

        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        worker, rendered = self._worker(tmp_path, path, monkeypatch, batch=1)
        failures, finished = [], []
        worker.failed.connect(failures.append)
        worker.finished_ok.connect(finished.append)
        worker.run()  # synchronous

        assert failures == [], "batch mode planned over chapters and found no audio"
        slug = slugify(sample_meta.translated_title or sample_meta.title)
        expected = [
            NovelProject.open(path).video_dir / Path(n).stem / n
            for n in (
                video_part_name(slug, i, i, whole_novel=False, source_audio=True)
                for i in (1, 2, 3)
            )
        ]
        assert rendered == expected, "one part per release, numbered by release ordinal"
        assert finished == [3]

    def test_batch_windows_match_what_the_table_shows(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The table is the contract: whatever it previews is what the render produces."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        windows = tab._windows_for_current_selection()
        previewed = [tab._part_output_path(w, whole_novel=False) for w in windows]
        tab.shutdown()

        worker, rendered = self._worker(tmp_path, path, monkeypatch, batch=2)
        worker.run()
        assert rendered == previewed

    def test_part_titles_count_releases_not_chapters(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """"Phần N" numbers releases — the worker's own fallback must agree with the tab."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        worker, rendered = self._worker(tmp_path, path, monkeypatch, batch=2)
        worker.run()

        assert len(rendered) == 2, "3 releases at batch 2 → phần 1 (1-2), phần 2 (3-3)"
        titles = [
            (out.parent / (out.stem + ".title.txt")).read_text(encoding="utf-8")
            for out in rendered
        ]
        assert "Phần 1" in titles[0]
        assert "Phần 2" in titles[1]

    def test_a_manual_chapter_split_does_not_reshape_the_source_plan(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """`video_manual_windows.json` is keyed by CHAPTER number. Applying it to a plan
        keyed by release ordinal would silently reshape a different edition's parts."""
        from noveltrans.video_windows import split_window

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21, 31)
        split_window(path, 1, 4, 1)  # a chapter-space split of "1-4"
        worker, rendered = self._worker(tmp_path, path, monkeypatch, batch=4)
        worker.run()

        assert len(rendered) == 1, "the 4 releases stay one part — the split is not theirs"

    def test_a_rendered_chapter_part_does_not_freeze_a_source_window(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Committed chapter parts must not be discovered as locks on the release grid.

        Since 067 this holds twice over: the source planner ignores commit discovery
        entirely, AND the two editions no longer share a filename namespace. Kept as a
        behaviour test of the plan, not of the naming — if either guarantee is ever
        removed, the surviving one should still make this pass."""
        from pathlib import Path

        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21, 31)
        slug = slugify(sample_meta.translated_title or sample_meta.title)
        project = NovelProject.open(path)
        name = video_part_name(slug, 1, 2, whole_novel=False)  # a chapter part 1-2
        committed = project.video_dir / Path(name).stem / name
        committed.parent.mkdir(parents=True, exist_ok=True)
        committed.write_bytes(b"fake mp4")
        project.close()

        worker, rendered = self._worker(
            tmp_path, path, monkeypatch, batch=4, skip_existing=True
        )
        worker.run()

        full = video_part_name(slug, 1, 4, whole_novel=False, source_audio=True)
        assert rendered == [project.video_dir / Path(full).stem / full]

    def test_skip_existing_still_skips_a_rendered_source_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Per-window skipping is a file check, not plan-level locking — it must survive."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        worker, rendered = self._worker(
            tmp_path, path, monkeypatch, batch=1, skip_existing=True
        )
        worker.run()
        assert len(rendered) == 3

        again, re_rendered = self._worker(
            tmp_path, path, monkeypatch, batch=1, skip_existing=True
        )
        finished = []
        again.finished_ok.connect(finished.append)
        again.run()
        assert re_rendered == [] and finished == [0]

    def test_whole_novel_mode_is_namespaced_too(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """"Toàn bộ" already worked before 066, and 067 namespaces it like every other part.

        `{slug}.mp4` carries no range suffix, so it collided with the chapter edition's
        whole-novel render just as squarely as a windowed part did."""
        from noveltrans.storage.project import slugify

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        worker, rendered = self._worker(tmp_path, path, monkeypatch, mode="all")
        worker.run()

        slug = slugify(sample_meta.translated_title or sample_meta.title)
        assert [p.name for p in rendered] == [f"{slug}-nguon.mp4"]

    def test_switching_to_the_source_edition_clears_the_locked_caches(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The `_locked_*` caches are per-plan. A chapter-voice plan's entries left behind
        would hand a release window a chapter part number and paint a phantom "bị khoá"."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.sync_source_audio(_manifest(1, 11, 21))
        for n in (1, 11, 21):
            rel = f"exports/audio/nguon-{n:04d}.mp3"
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"\x00" * 16)
            project.save_source_audio(n, rel, 7200.0)
        for i in range(5):
            rel = f"exports/audio/{i}.mp3"
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"\x00" * 16)
            project.save_audio(i, rel, "vi-VN-female", 60.0)
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab._on_voices_listed(TTS_VOICES)
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("vi-VN-female"))
        assert tab._windows_for_current_selection(), "the TTS plan populates the caches"
        assert tab._locked_part_numbers

        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        windows = tab._windows_for_current_selection()
        assert tab._locked_part_numbers == {}
        assert tab._locked_committed == {}
        assert tab._locked_manual == {}
        assert tab._part_number(windows[0]) == 1
        tab.shutdown()


class TestRedoAllForSourceAudio:
    """"Tạo lại tất cả video" planned over chapters directly, so it never reached the
    worker for the source edition — it stopped at its own "Chưa có audio" dialog."""

    def _tab(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.video_image_edit.setText(str(path))  # any existing file passes the image check
        tab._on_project_selected(str(path))
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        return tab

    def test_redo_all_launches_the_worker_for_the_source_edition(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        tab = self._tab(tmp_path, path)
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))
        tab._redo_all_videos()

        assert shown == [], "no \"Chưa có audio\" — the releases are right there"
        assert captured == {"skip_existing": False}
        tab.shutdown()

    def test_redo_all_counts_the_same_parts_as_the_table(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        tab = self._tab(tmp_path, path)
        asked = []
        # `information` too, not just `question`: a regression here takes the "Chưa có
        # audio" path, and an unpatched modal blocks the run forever instead of failing.
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: None)
        expected = len(tab._windows_for_current_selection())
        tab._redo_all_videos()

        assert f"toàn bộ {expected} phần" in asked[0][2]
        assert "mục" in asked[0][2], "a source window groups releases, not chương"
        assert "__source_audio__" not in asked[0][2], "the sentinel is not a voice name"
        tab.shutdown()


class TestSourcePartNamespace:
    """Feature 067 — the two editions no longer share a part-folder namespace.

    Both used to render through `video_part_name(slug, first, last)`, which knows only a
    slug and two integers, into the same `video_dir`. So "chương 1-10" and "phần 1-10 của
    bản nguồn" were the same folder, the same `.mp4`, and the same sidecar family —
    including `<stem>.upload.json`, which carries a live YouTube video id.
    """

    def _worker(self, tmp_path, path, monkeypatch, **kwargs):
        return _source_video_worker(tmp_path, path, monkeypatch, **kwargs)

    def _chapter_part(self, path, first, last, *, body=b"chapter render"):
        """A part folder under the CHAPTER name — what both editions used to produce."""
        from pathlib import Path

        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        project = NovelProject.open(path)
        try:
            slug = slugify(project.meta.translated_title or project.meta.title)
            name = video_part_name(slug, first, last, whole_novel=False)
            out = project.video_dir / Path(name).stem / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            return out
        finally:
            project.close()

    def _source_tab(self, tmp_path, path, *, batch=2):
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(batch)
        return tab

    def test_a_source_render_does_not_overwrite_a_chapter_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Consequence 1: the render used to truncate the other edition's .mp4 outright."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        chapter = self._chapter_part(path, 1, 2)

        worker, rendered = self._worker(tmp_path, path, monkeypatch, batch=2)
        worker.run()

        assert chapter.read_bytes() == b"chapter render", "the chapter part was overwritten"
        assert len(rendered) == 1
        assert rendered[0] != chapter
        assert rendered[0].name.endswith("-nguon-0001-0002.mp4")

    def test_a_rendered_chapter_part_does_not_mark_a_source_part_as_created(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Consequence 2: `effective_created` is a path check, so a chapter render used to
        make `skip_existing` skip a source part that had never been rendered."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21, 31)
        self._chapter_part(path, 1, 2)

        worker, rendered = self._worker(
            tmp_path, path, monkeypatch, batch=2, skip_existing=True
        )
        worker.run()

        assert len(rendered) == 2, "the source part 1-2 was skipped as already made"

    def test_an_uploaded_chapter_part_does_not_look_published_on_the_source_edition(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Consequence 3, the worst one: `<stem>.upload.json` holds a real YouTube video id,
        so a thumbnail or subtitle push could resolve — and overwrite — the OTHER edition's
        published video."""
        import json

        from noveltrans.youtube_upload import is_published, uploaded_video_id

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        chapter = self._chapter_part(path, 1, 2)
        (chapter.parent / (chapter.stem + ".upload.json")).write_text(
            json.dumps({"status": "published", "video_id": "ABC123"}), encoding="utf-8"
        )
        assert is_published(chapter), "fixture check: the chapter part IS published"

        tab = self._source_tab(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        source_path = tab._part_output_path(window, whole_novel=False)

        assert tab._part_uploaded(window, False) is False
        assert uploaded_video_id(source_path) == ""
        tab.shutdown()

    def test_a_manual_created_tick_on_one_edition_does_not_tick_the_other(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.video_state import effective_created, set_created_override

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        chapter = self._chapter_part(path, 1, 2)
        set_created_override(chapter, True, file_exists=True)

        tab = self._source_tab(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        assert effective_created(tab._part_output_path(window, whole_novel=False)) is False
        tab.shutdown()

    def test_the_description_resync_leaves_the_source_edition_alone(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Consequence 5: the resync rebuilds a part's `.txt` by selecting CHAPTERS in its
        number range. A source part's numbers are releases, so running it over one would
        describe a completely different set of chapters."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        tab = self._source_tab(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        sidecar = out.parent / (out.stem + ".txt")
        sidecar.write_text("mô tả của bản nguồn\n", encoding="utf-8")

        assert tab._resync_description_sidecars() == (0, 0)
        assert sidecar.read_text(encoding="utf-8") == "mô tả của bản nguồn\n"
        assert out.parent.name not in tab._stale_descriptions
        tab.shutdown()

    def test_the_whole_novel_renders_of_the_two_editions_are_different_files(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """`{slug}.mp4` carries no range suffix — it collided just as squarely as a part."""
        from noveltrans.storage.project import slugify

        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        slug = slugify(sample_meta.translated_title or sample_meta.title)
        worker, rendered = self._worker(tmp_path, path, monkeypatch, mode="all")
        worker.run()

        assert [p.name for p in rendered] == [f"{slug}-nguon.mp4"]
        assert rendered[0].name != f"{slug}.mp4"

    def test_the_tab_and_the_worker_agree_on_the_source_stem(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """`_part_output_path` and `VideoWorker` derive the name independently, so they must
        agree about the edition too — in whole-novel mode as well as batch."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11, 21)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("all"))
        windows = tab._windows_for_current_selection()
        previewed = [tab._part_output_path(w, whole_novel=True) for w in windows]
        tab.shutdown()

        worker, rendered = self._worker(tmp_path, path, monkeypatch, mode="all")
        worker.run()
        assert rendered == previewed

    def test_a_chapter_render_is_still_reachable_while_the_source_edition_is_selected(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The "no migration" guarantee from the tab's side: switching editions must not
        make an existing chapter part unreachable — select its voice and it resolves again."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.sync_source_audio(_manifest(1, 11))
        for n in (1, 11):
            rel = f"exports/audio/nguon-{n:04d}.mp3"
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"\x00" * 16)
            project.save_source_audio(n, rel, 7200.0)
        for i in range(2):
            rel = f"exports/audio/{i}.mp3"
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"\x00" * 16)
            project.save_audio(i, rel, "vi-VN-female", 60.0)
        path = project.path
        project.close()
        chapter = self._chapter_part(path, 1, 2)

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab._on_voices_listed(TTS_VOICES)
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)

        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        source_window = tab._windows_for_current_selection()[0]
        assert tab._part_output_path(source_window, whole_novel=False) != chapter

        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("vi-VN-female"))
        chapter_window = tab._windows_for_current_selection()[0]
        assert tab._part_output_path(chapter_window, whole_novel=False) == chapter
        tab.shutdown()


class TestLegacySourcePartWarning:
    """Feature 067's whole migration story: a source part rendered BEFORE the split kept the
    chapter name, and nothing in a folder records which planner made it. So it is reported,
    never renamed — an automatic move would be a coin flip with a live YouTube id at stake.
    """

    def _tab(self, tmp_path, path, *, batch=2):
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData(SOURCE_AUDIO_KEY))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(batch)
        return tab

    def _legacy_part(self, path, first, last):
        from pathlib import Path

        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        project = NovelProject.open(path)
        try:
            slug = slugify(project.meta.translated_title or project.meta.title)
            name = video_part_name(slug, first, last, whole_novel=False)
            out = project.video_dir / Path(name).stem / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake mp4")
            return out
        finally:
            project.close()

    def test_a_chapter_named_folder_matching_a_source_window_is_reported(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        legacy = self._legacy_part(path, 1, 2)

        tab = self._tab(tmp_path, path)
        assert [d.name for d in tab._legacy_source_part_dirs()] == [legacy.parent.name]
        tab._warn_about_legacy_source_parts()
        assert "tên cũ" in tab.status_label.text()
        tab.shutdown()

    def test_nothing_is_reported_once_the_source_part_exists(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Once the user has re-rendered under the new name there is nothing to act on."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        self._legacy_part(path, 1, 2)

        tab = self._tab(tmp_path, path)
        out = tab._part_output_path(
            tab._windows_for_current_selection()[0], whole_novel=False
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        assert tab._legacy_source_part_dirs() == []
        tab.shutdown()

    def test_a_folder_whose_span_is_not_a_source_window_is_not_reported(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Only a span the source edition is actually planning is suspicious. Anything else
        is just an ordinary chapter part and must not be flagged."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        self._legacy_part(path, 41, 60)

        tab = self._tab(tmp_path, path)
        assert tab._legacy_source_part_dirs() == []
        tab.shutdown()

    def test_the_chapter_edition_is_never_warned_about(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The note is only meaningful while the source edition is on screen."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        self._legacy_part(path, 1, 2)

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab._on_voices_listed(TTS_VOICES)
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("vi-VN-female"))
        assert tab._legacy_source_part_dirs() == []
        tab.shutdown()

    def test_it_never_touches_a_single_file(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The load-bearing property: it reports, it does not migrate."""
        path = _project_with_releases(library_dir, sample_meta, sample_refs, 1, 11)
        legacy = self._legacy_part(path, 1, 2)
        before = sorted(p.name for p in legacy.parent.parent.rglob("*"))

        tab = self._tab(tmp_path, path)
        tab._warn_about_legacy_source_parts()
        assert sorted(p.name for p in legacy.parent.parent.rglob("*")) == before
        assert legacy.read_bytes() == b"fake mp4"
        tab.shutdown()

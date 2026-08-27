"""Feature 025 — the split-out VideoTab + its registration as "5. Video" (offscreen Qt)."""

from __future__ import annotations

from PySide6.QtCore import QDate, QDateTime, QObject, QSettings, QTime, Signal

from noveltrans.config import AppConfig
from noveltrans.gui.tab_video import VideoTab
from noveltrans.gui.workspace import Workspace
from noveltrans.storage import NovelProject
from noveltrans.storage.state import AppState


def _config(tmp_path):
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.library_dir = tmp_path / "library"
    return config


class TestVideoTab:
    def test_constructs_and_exposes_the_tab_contract(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        assert hasattr(tab, "picker")
        assert callable(tab.refresh_projects)
        assert tab.has_running_workers() is False
        tab.shutdown()

    def test_loads_saved_tags_and_prompt_on_project_select(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_tags("người hầu, truyện audio, review truyện")
        project.save_thumbnail_prompt("a cinematic xianxia scene, 16:9")
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        assert tab.tags_edit.toPlainText() == "người hầu, truyện audio, review truyện"
        assert tab.image_prompt_edit.toPlainText() == "a cinematic xianxia scene, 16:9"
        tab.shutdown()

    def test_loads_saved_image_and_playlist_on_project_select(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_video_image_path("C:/covers/bg.png")
        project.save_upload_playlist("Truyện A")
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        assert tab.video_image_edit.text() == "C:/covers/bg.png"
        assert tab.upload_playlist.currentText() == "Truyện A"
        tab.shutdown()

    def test_switching_novels_does_not_leak_image_or_playlist(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The reported bug: picking a background image / playlist for one novel, then
        opening another, used to keep showing the first novel's choices on the second's
        video tab — because neither was ever persisted per-novel."""
        from dataclasses import replace

        project_a = NovelProject.create(library_dir, sample_meta, sample_refs)
        project_a.save_video_image_path("C:/covers/a.png")
        project_a.save_upload_playlist("Playlist A")
        path_a = project_a.path
        project_a.close()

        # `sample_meta` is mutated in place by `save_video_image_path`/`save_upload_playlist`
        # above (NovelProject keeps the same object, doesn't copy it) — reset both explicitly
        # so novel B starts with neither set, rather than inheriting A's just-saved values.
        meta_b = replace(
            sample_meta,
            url="https://example.com/novel/456",
            title="Novel B",
            video_image_path="",
            upload_playlist="",
        )
        project_b = NovelProject.create(library_dir, meta_b, sample_refs)
        path_b = project_b.path
        project_b.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path_a))
        assert tab.video_image_edit.text() == "C:/covers/a.png"
        assert tab.upload_playlist.currentText() == "Playlist A"

        tab._on_project_selected(str(path_b))
        assert tab.video_image_edit.text() == ""  # no leak from novel A
        assert tab.upload_playlist.currentText() == ""

        tab._on_project_selected(str(path_a))  # switching back still remembers A
        assert tab.video_image_edit.text() == "C:/covers/a.png"
        assert tab.upload_playlist.currentText() == "Playlist A"
        tab.shutdown()

    def test_picking_playlist_text_persists_to_the_open_novel(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.upload_playlist.setCurrentText("Danh sách mới")

        reopened = NovelProject.open(path)
        assert reopened.meta.upload_playlist == "Danh sách mới"
        reopened.close()
        tab.shutdown()

    def test_loads_saved_visibility_on_project_select(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_upload_visibility("public")
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        assert tab.upload_visibility.currentData() == "public"
        tab.shutdown()

    def test_unset_visibility_defaults_to_private_not_another_novels_choice(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The one thing worth being paranoid about here: a novel that never had a
        visibility chosen must land on Riêng tư (private), never on whatever a
        DIFFERENT novel — possibly Public or Schedule — had selected last."""
        from dataclasses import replace

        project_a = NovelProject.create(library_dir, sample_meta, sample_refs)
        project_a.save_upload_visibility("public")
        path_a = project_a.path
        project_a.close()

        meta_b = replace(
            sample_meta,
            url="https://example.com/novel/456",
            title="Novel B",
            upload_visibility="",
        )
        project_b = NovelProject.create(library_dir, meta_b, sample_refs)
        path_b = project_b.path
        project_b.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path_a))
        assert tab.upload_visibility.currentData() == "public"

        tab._on_project_selected(str(path_b))
        assert tab.upload_visibility.currentData() == "private"  # safe default, no leak
        tab.shutdown()

    def test_changing_visibility_persists_to_the_open_novel(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab.upload_visibility.setCurrentIndex(tab.upload_visibility.findData("schedule"))

        reopened = NovelProject.open(path)
        assert reopened.meta.upload_visibility == "schedule"
        reopened.close()
        tab.shutdown()

    def test_shared_ai_engine_combo_excludes_google(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        keys = {tab.ai_engine_combo.itemData(i) for i in range(tab.ai_engine_combo.count())}
        assert "google" not in keys
        assert "lmstudio" in keys
        tab.shutdown()

    def test_has_image_prompt_controls(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        assert hasattr(tab, "image_prompt_button")
        assert hasattr(tab, "image_prompt_edit")
        tab.shutdown()

    def test_preview_ready_builds_a_live_dialog_with_color_controls(self, qapp, tmp_path):
        from PIL import Image

        png = tmp_path / "prev.png"
        Image.new("RGB", (640, 360), (120, 90, 160)).save(png)
        tab = VideoTab(_config(tmp_path))
        tab._on_preview_ready(str(png))
        assert tab._preview_dialog is not None
        assert tab._preview_dialog.isVisible()
        assert tab._preview_color_button is not None
        assert not tab._preview_label.pixmap().isNull()
        tab.shutdown()  # closes the dialog
        assert tab._preview_dialog is None  # refs cleared on close

    def test_changing_color_refreshes_an_open_preview(self, qapp, tmp_path, monkeypatch):
        tab = VideoTab(_config(tmp_path))
        calls = []
        monkeypatch.setattr(tab, "_start_preview", lambda: calls.append(1))
        # no preview open → changing color does not trigger a re-render
        tab._reset_bg_color()
        assert calls == []
        # open a preview window → changing color now re-renders it in place
        tab._build_preview_dialog()
        tab._preview_dialog.show()
        tab._reset_bg_color()
        assert calls == [1]
        tab._preview_dialog.close()

    def test_bg_color_starts_at_the_default_with_no_novel_open(self, qapp, tmp_path):
        """màu nền is per-novel: with nothing open there is no novel to take it from, so
        the box must NOT show whatever colour was last used elsewhere."""
        config = _config(tmp_path)
        config.video_bg_color = "#1e785a"
        tab = VideoTab(config)
        assert tab.bg_color == ""
        assert tab.bg_color_button.text() == "Chọn màu…"
        tab.shutdown()

    def test_bg_color_loads_from_the_open_novel_and_resets(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_video_settings({"video_bg_color": "#1e785a"})
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        assert tab.bg_color == "#1e785a"
        assert "#1e785a" in tab.bg_color_button.styleSheet()
        tab._reset_bg_color()
        assert tab.bg_color == ""
        assert tab.bg_color_button.text() == "Chọn màu…"
        # and the reset is remembered against that novel, not globally
        reopened = NovelProject.open(str(path))
        assert reopened.meta.video_settings["video_bg_color"] == ""
        reopened.close()
        tab.shutdown()

    def test_mode_and_batch_size_persist_to_config(self, qapp, tmp_path):
        config = _config(tmp_path)
        config.video_mode = "range"
        config.video_batch_size = 25
        tab = VideoTab(config)
        # the remembered choices are restored…
        assert tab.video_mode.currentData() == "range"
        assert tab.video_batch_size.value() == 25
        # …and a change writes straight back to config
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(7)
        assert config.video_mode == "batch"
        assert config.video_batch_size == 7
        tab.shutdown()

    def test_thumbnail_font_loads_from_the_open_novel_and_persists(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The cover font is part of how a novel looks, so it is stored on the novel."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_video_settings({"video_thumbnail_font": "be_vietnam"})
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        assert tab.thumb_font.currentData() == "be_vietnam"
        tab.thumb_font.setCurrentIndex(tab.thumb_font.findData("montserrat"))
        reopened = NovelProject.open(str(path))
        assert reopened.meta.video_settings["video_thumbnail_font"] == "montserrat"
        reopened.close()
        tab.shutdown()


class TestVideoPartsList:
    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")  # deterministic voice (skip async load)
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _make_button(self, tab, row):
        from PySide6.QtWidgets import QPushButton

        return tab.video_list.cellWidget(row, 6).findChildren(QPushButton)[0]

    def test_lists_one_row_per_part_all_pending(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        # 5 chapters, batch of 2 → parts (1-2), (3-4), (5)
        assert tab.video_list.rowCount() == 3
        assert tab.video_list.item(0, 4).text() == "⬜ Chưa tạo"
        assert self._make_button(tab, 0).text() == "Tạo"
        tab.shutdown()

    def test_title_column_shows_part_title(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        assert tab.video_list.item(0, 3).text().endswith("- Phần 1")
        assert tab.video_list.item(2, 3).text().endswith("- Phần 3")
        tab.shutdown()

    def test_duration_column_sums_part_audio(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        # each chapter = 60s; a batch of 2 → 120s = "2:00"
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        assert tab.video_list.item(0, 2).text() == "2:00"

    def test_duration_over_12h_is_flagged(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 5 * 3600.0)  # 5h each
        path = project.path
        project.close()
        tab = self._tab_on_project(tmp_path, path)  # batch 2 → 10h and 10h and 5h
        # a bigger batch pushes a part past 12h
        tab.video_batch_size.setValue(3)  # 3 × 5h = 15h > 12h
        tab._refresh_video_list()
        assert "⚠️" in tab.video_list.item(0, 2).text()
        tab.shutdown()

    def test_existing_file_shows_done_and_recreate(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        out = tab._part_output_path(windows[0], whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        tab._refresh_video_list()
        assert tab.video_list.item(0, 4).text() == "✅ Đã tạo"
        assert self._make_button(tab, 0).text() == "Tạo lại"
        assert tab.video_list.item(1, 4).text() == "⬜ Chưa tạo"  # the others still pending
        tab.shutdown()

    def test_each_part_renders_into_its_own_subfolder(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        out = tab._part_output_path(windows[0], whole_novel=False)
        # video lives in a folder named after itself, inside video_dir
        assert out.parent.name == out.stem
        assert out.parent.parent == tab.project.video_dir
        tab.shutdown()

    def test_the_chapter_edition_keeps_its_pre_067_name(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Feature 067's "no migration" guarantee, stated where it is easiest to break.

        Only the SOURCE edition was given a namespace; chapter audio's folder, `.mp4` and
        every sidecar beside it — including the `.upload.json` holding a published video id
        — keep the exact names they already have on disk. If `_novel_slug` or
        `_part_output_path` ever picks up the edition marker for chapter audio, every
        rendered part on every existing install is stranded, and this fails.
        """
        from noveltrans.storage.project import slugify

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        slug = slugify(sample_meta.translated_title or sample_meta.title)
        out = tab._part_output_path(tab._windows_for_current_selection()[0], whole_novel=False)

        assert out.name == f"{slug}-0001-0002.mp4"
        assert out.parent.name == f"{slug}-0001-0002"
        assert "nguon" not in out.name
        tab.shutdown()

    def test_legacy_flat_render_is_still_recognised(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        out = tab._part_output_path(windows[0], whole_novel=False)
        # simulate a pre-existing flat file directly under video_dir (old layout)
        legacy = tab.project.video_dir / out.name
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"fake mp4")
        # the exists check resolves to the legacy file, so the part shows as done
        assert tab._part_output_path(windows[0], whole_novel=False) == legacy
        tab._refresh_video_list()
        assert tab.video_list.item(0, 4).text() == "✅ Đã tạo"
        tab.shutdown()

    def test_part_metadata_reads_sidecars_then_falls_back(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        # not rendered yet → computed title/description
        title, desc, _tags = tab._part_metadata(windows[0], 1, False)
        assert title.endswith("- Phần 1")
        assert "Mục lục chương:" in desc
        # write sidecars → they win
        base = tab._part_output_path(windows[0], whole_novel=False)
        base.parent.mkdir(parents=True, exist_ok=True)
        (base.parent / (base.stem + ".title.txt")).write_text("TITLE FROM FILE\n", encoding="utf-8")
        (base.parent / (base.stem + ".txt")).write_text("DESC FROM FILE\n", encoding="utf-8")
        title2, desc2, _ = tab._part_metadata(windows[0], 1, False)
        assert title2 == "TITLE FROM FILE"
        assert desc2.strip() == "DESC FROM FILE"
        tab.shutdown()

    def test_thumbnail_button_enabled_only_when_jpg_exists(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from PySide6.QtWidgets import QPushButton

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        thumb_btn = tab.video_list.cellWidget(0, 6).findChildren(QPushButton)[2]
        assert thumb_btn.text() == "Ảnh bìa"
        assert not thumb_btn.isEnabled()  # no thumbnail yet
        windows = tab._windows_for_current_selection()
        jpg = tab._part_sidecar(windows[0], False, ".jpg")
        jpg.parent.mkdir(parents=True, exist_ok=True)
        jpg.write_bytes(b"fake jpg")
        tab._refresh_video_list()
        thumb_btn = tab.video_list.cellWidget(0, 6).findChildren(QPushButton)[2]
        assert thumb_btn.isEnabled()
        tab.shutdown()

    def test_regen_part_thumbnail_writes_jpg_without_a_render(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from PIL import Image

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        # a real base image so render_thumbnail has something to cover-fit
        base = tmp_path / "cover.png"
        Image.new("RGB", (640, 360), (40, 60, 90)).save(base)
        tab.thumb_image_edit.setText(str(base))
        windows = tab._windows_for_current_selection()
        jpg = tab._part_sidecar(windows[0], False, ".jpg")
        assert not jpg.is_file()
        assert tab._regen_part_thumbnail(windows[0], 1, False) is True
        assert jpg.is_file()  # cover written even though no video was rendered
        tab.shutdown()

    def test_regen_all_thumbnails_covers_every_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from PIL import Image

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)  # 5 chapters, batch 2 → 3 parts
        base = tmp_path / "cover.png"
        Image.new("RGB", (640, 360), (40, 60, 90)).save(base)
        tab.thumb_image_edit.setText(str(base))
        windows = tab._windows_for_current_selection()
        assert len(windows) == 3
        tab._regen_all_thumbnails()
        for w in windows:
            assert tab._part_sidecar(w, False, ".jpg").is_file()
        tab.shutdown()

    def test_regen_without_a_base_image_is_a_no_op(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        tab.thumb_image_edit.setText("")
        tab.video_image_edit.setText("")
        # suppress the warning dialog so the test stays headless
        monkeypatch.setattr(
            "noveltrans.gui.tab_video.QMessageBox.warning", lambda *a, **k: None
        )
        windows = tab._windows_for_current_selection()
        assert tab._regen_part_thumbnail(windows[0], 1, False) is False
        assert not tab._part_sidecar(windows[0], False, ".jpg").is_file()
        tab.shutdown()

    def test_render_one_uses_range_mode_for_that_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))
        tab._render_one(windows[1])
        assert captured == {
            "mode": "range",
            "start": windows[1].first_num,
            "end": windows[1].last_num,
            "skip_existing": False,
            # the explicit part number a range-mode worker has no batch grid to derive on
            # its own — windows[1] is chapters 3-4, part 2 (batch size 2)
            "part_num": tab._part_number(windows[1]),
        }
        tab.shutdown()


class TestLockedBatchWindows:
    """Feature 058 follow-up: a batch part committed with fewer than a full batch of
    chapters must stay that size in the table too — new chapters get their own part."""

    def _project(self, library_dir, sample_meta, n_chapters):
        from noveltrans.models import ChapterRef

        refs = [
            ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
            for i in range(n_chapters)
        ]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(n_chapters):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path, batch=2):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(batch)
        tab._on_project_selected(str(path))
        return tab

    def _add_chapters(self, path, start, n):
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject

        project = NovelProject.open(path)
        existing = project.chapters()
        new_refs = [
            ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
            for i in range(start, start + n)
        ]
        project.replace_toc(
            [ChapterRef(index=c.index, title=c.title, url=c.url) for c in existing]
            + new_refs
        )
        for i in range(start, start + n):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        project.close()

    def test_an_uncommitted_short_tail_is_not_flagged(
        self, qapp, tmp_path, library_dir, sample_meta
    ):
        """3 chapters, batch 2 → part 2 (chương 3) is short but simply waiting for more
        chapters — nothing has locked it yet, so no warning."""
        path = self._project(library_dir, sample_meta, 3)
        tab = self._tab_on_project(tmp_path, path)
        assert tab.video_list.rowCount() == 2
        assert "⚠️" not in tab.video_list.item(1, 1).text()
        tab.shutdown()

    def test_a_committed_short_part_is_flagged(
        self, qapp, tmp_path, library_dir, sample_meta
    ):
        from noveltrans.video_state import set_created_override

        path = self._project(library_dir, sample_meta, 3)
        tab = self._tab_on_project(tmp_path, path)
        window2 = tab._windows_for_current_selection()[1]
        out2 = tab._part_output_path(window2, whole_novel=False)
        set_created_override(out2, True, file_exists=False)
        tab._refresh_video_list()

        item = tab.video_list.item(1, 1)
        assert "⚠️" in item.text()
        assert "1/2" in item.toolTip()
        tab.shutdown()

    def test_new_chapters_get_their_own_part_not_absorbed_into_the_locked_one(
        self, qapp, tmp_path, library_dir, sample_meta
    ):
        from noveltrans.video_state import set_created_override

        path = self._project(library_dir, sample_meta, 3)
        tab = self._tab_on_project(tmp_path, path)
        window2 = tab._windows_for_current_selection()[1]
        out2 = tab._part_output_path(window2, whole_novel=False)
        set_created_override(out2, True, file_exists=False)  # part 2 (chương 3) committed

        self._add_chapters(path, 3, 1)  # chương 4 arrives
        tab._on_project_selected(str(path))  # reload to pick up the new chapter

        windows = tab._windows_for_current_selection()
        assert len(windows) == 3  # part 1 (1-2), part 2 (3, locked), part 3 (4)
        assert (windows[1].first_num, windows[1].last_num) == (3, 3)
        assert (windows[2].first_num, windows[2].last_num) == (4, 4)
        assert tab._part_number(windows[1]) == 2
        assert tab._part_number(windows[2]) == 3
        assert "⚠️" in tab.video_list.item(1, 1).text()  # still locked/flagged
        assert "⚠️" not in tab.video_list.item(2, 1).text()  # the new part is not
        tab.shutdown()


class TestSplitMergeParts:
    """Feature 058 follow-up: right-click a part to split it (e.g. to stay under
    YouTube's 12h cap) or merge two adjacent parts back together."""

    def _project(self, library_dir, sample_meta, n_chapters):
        from noveltrans.models import ChapterRef

        refs = [
            ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
            for i in range(n_chapters)
        ]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(n_chapters):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path, batch=10):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(batch)
        tab._on_project_selected(str(path))
        return tab

    def _yes(self, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        return asked

    def _row_pos(self, tab, row):
        from PySide6.QtCore import QPoint

        return QPoint(10, tab.video_list.rowViewportPosition(row) + 5)

    class _FakeAction:
        def __init__(self, text):
            self.text = text
            self.enabled = True
            self.tooltip = ""
            self._callback = None

        def setEnabled(self, value):
            self.enabled = value

        def setToolTip(self, text):
            self.tooltip = text

        @property
        def triggered(self):
            return self

        def connect(self, callback):
            self._callback = callback

        def trigger(self):
            self._callback()

    class _FakeMenu:
        instances: list = []

        def __init__(self, parent=None):
            self.actions = []
            TestSplitMergeParts._FakeMenu.instances.append(self)

        def addAction(self, text):
            action = TestSplitMergeParts._FakeAction(text)
            self.actions.append(action)
            return action

        def addSeparator(self):
            pass

        def exec(self, pos):
            pass

    def _menu_for(self, tab, monkeypatch, row):
        self._FakeMenu.instances.clear()
        monkeypatch.setattr("noveltrans.gui.tab_video.QMenu", self._FakeMenu)
        tab._on_video_list_context_menu(self._row_pos(tab, row))
        return self._FakeMenu.instances[-1] if self._FakeMenu.instances else None

    def test_right_clicking_an_unselected_row_selects_just_that_row(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        path = self._project(library_dir, sample_meta, 20)  # batch 10 → 2 parts
        tab = self._tab_on_project(tmp_path, path)
        self._menu_for(tab, monkeypatch, 1)
        selected = {idx.row() for idx in tab.video_list.selectionModel().selectedRows()}
        assert selected == {1}
        tab.shutdown()

    def test_single_row_offers_split_plus_bulk_status_actions(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        path = self._project(library_dir, sample_meta, 20)
        tab = self._tab_on_project(tmp_path, path)
        menu = self._menu_for(tab, monkeypatch, 0)
        assert [a.text for a in menu.actions] == [
            "Tạo video", "Tách phần…",
            "Đánh dấu \"Đã tạo\"", "Đánh dấu \"Chưa tạo\"",
            "Đánh dấu \"Đã tải lên\"", "Đánh dấu \"Chưa tải lên\"",
        ]
        assert menu.actions[0].enabled and menu.actions[1].enabled
        tab.shutdown()

    def test_two_adjacent_rows_offer_an_enabled_merge(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtCore import QItemSelectionModel

        path = self._project(library_dir, sample_meta, 30)  # batch 10 → 3 parts
        tab = self._tab_on_project(tmp_path, path)
        tab.video_list.selectRow(0)
        tab.video_list.selectionModel().select(
            tab.video_list.model().index(1, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        menu = self._menu_for(tab, monkeypatch, 0)
        assert [a.text for a in menu.actions] == [
            "Tạo video", "Gộp 2 phần liền kề",
            "Đánh dấu \"Đã tạo\"", "Đánh dấu \"Chưa tạo\"",
            "Đánh dấu \"Đã tải lên\"", "Đánh dấu \"Chưa tải lên\"",
        ]
        assert menu.actions[0].enabled and menu.actions[1].enabled
        tab.shutdown()

    def test_split_off_the_last_n_chapters(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QInputDialog

        from noveltrans.video_windows import read_manual_windows

        path = self._project(library_dir, sample_meta, 10)  # batch 10 → 1 part
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, True))
        self._yes(monkeypatch)

        tab._split_part(window)

        assert read_manual_windows(path) == {1: 7, 8: 10}
        windows = tab._windows_for_current_selection()
        assert [(w.first_num, w.last_num) for w in windows] == [(1, 7), (8, 10)]
        tab.shutdown()

    def test_declining_the_split_confirmation_changes_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        from noveltrans.video_windows import read_manual_windows

        path = self._project(library_dir, sample_meta, 10)
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, True))
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )

        tab._split_part(window)

        assert read_manual_windows(path) == {}
        tab.shutdown()

    def test_cancelling_the_split_dialog_changes_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QInputDialog

        from noveltrans.video_windows import read_manual_windows

        path = self._project(library_dir, sample_meta, 10)
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, False))

        tab._split_part(window)

        assert read_manual_windows(path) == {}
        tab.shutdown()

    def test_split_deletes_the_old_rendered_folder(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QInputDialog

        path = self._project(library_dir, sample_meta, 10)
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"already rendered")
        (out.parent / (out.stem + ".title.txt")).write_text("x", encoding="utf-8")

        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, True))
        asked = self._yes(monkeypatch)

        tab._split_part(window)

        assert not out.parent.exists()  # whole per-part folder removed
        assert "đã có video" in asked[0][2]  # confirmation warned about the deletion
        tab.shutdown()

    def test_split_warns_when_the_old_part_was_uploaded(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QInputDialog

        from noveltrans.youtube_upload import mark_uploaded_by_hand

        path = self._project(library_dir, sample_meta, 10)
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"already rendered")
        mark_uploaded_by_hand(out)

        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, True))
        asked = self._yes(monkeypatch)

        tab._split_part(window)

        assert "YouTube" in asked[0][2]
        tab.shutdown()

    def test_merge_two_adjacent_parts(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from noveltrans.video_windows import read_manual_windows

        path = self._project(library_dir, sample_meta, 20)  # batch 10 → 2 parts
        tab = self._tab_on_project(tmp_path, path)
        window_a, window_b = tab._windows_for_current_selection()
        self._yes(monkeypatch)

        tab._merge_parts(window_a, window_b)

        assert read_manual_windows(path) == {1: 20}
        windows = tab._windows_for_current_selection()
        assert len(windows) == 1
        assert (windows[0].first_num, windows[0].last_num) == (1, 20)
        tab.shutdown()

    def test_merging_the_two_halves_of_a_split_undoes_it(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QInputDialog

        from noveltrans.video_windows import read_manual_windows

        path = self._project(library_dir, sample_meta, 10)
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (3, True))
        self._yes(monkeypatch)
        tab._split_part(window)
        assert read_manual_windows(path) == {1: 7, 8: 10}

        window_a, window_b = tab._windows_for_current_selection()
        tab._merge_parts(window_a, window_b)

        assert read_manual_windows(path) == {1: 10}
        tab.shutdown()

    def test_bulk_mark_created_for_multiple_selected_rows(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from noveltrans.video_state import created_override

        path = self._project(library_dir, sample_meta, 30)  # batch 10 → 3 parts
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        paths = [tab._part_output_path(w, whole_novel=False) for w in windows[:2]]
        asked = self._yes(monkeypatch)

        tab._bulk_set_created(paths, True)

        assert all(created_override(p) is True for p in paths)
        assert created_override(tab._part_output_path(windows[2], whole_novel=False)) is None
        assert "2 phần" in asked[0][2]
        tab.shutdown()

    def test_bulk_mark_created_skips_rows_already_correct_and_says_so(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        path = self._project(library_dir, sample_meta, 20)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        paths = [tab._part_output_path(w, whole_novel=False) for w in windows]
        informed = []
        monkeypatch.setattr(
            QMessageBox, "information", lambda *a, **k: informed.append(a)
        )
        asked = self._yes(monkeypatch)

        tab._bulk_set_created(paths, False)  # already "chưa tạo" — nothing to do

        assert asked == []  # no confirmation needed, nothing would change
        assert informed  # but the user is told so
        tab.shutdown()

    def test_bulk_mark_uploaded_for_multiple_selected_rows(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from noveltrans.youtube_upload import is_published

        path = self._project(library_dir, sample_meta, 30)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        paths = [tab._part_output_path(w, whole_novel=False) for w in windows[:2]]
        asked = self._yes(monkeypatch)

        tab._bulk_set_uploaded(paths, True)

        assert all(is_published(p) for p in paths)
        assert not is_published(tab._part_output_path(windows[2], whole_novel=False))
        assert "2 phần" in asked[0][2]
        tab.shutdown()

    def test_declining_a_bulk_action_changes_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        path = self._project(library_dir, sample_meta, 10)
        tab = self._tab_on_project(tmp_path, path)
        windows = tab._windows_for_current_selection()
        paths = [tab._part_output_path(w, whole_novel=False) for w in windows]
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )

        tab._bulk_set_created(paths, True)

        assert all(created_override(p) is None for p in paths)
        tab.shutdown()

    def test_context_menu_bulk_created_action_targets_the_selected_rows(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """The menu action itself (not just the underlying method) hits exactly the
        selected rows' paths — multi-select is the whole point of this feature."""
        from PySide6.QtCore import QItemSelectionModel

        from noveltrans.video_state import created_override

        path = self._project(library_dir, sample_meta, 30)  # batch 10 → 3 parts
        tab = self._tab_on_project(tmp_path, path)
        tab.video_list.selectRow(0)
        tab.video_list.selectionModel().select(
            tab.video_list.model().index(2, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._yes(monkeypatch)
        menu = self._menu_for(tab, monkeypatch, 0)  # rows 0 and 2 selected, not adjacent

        created_on = next(a for a in menu.actions if a.text == "Đánh dấu \"Đã tạo\"")
        created_on.trigger()

        windows = tab._windows_for_current_selection()
        assert created_override(tab._part_output_path(windows[0], whole_novel=False)) is True
        assert created_override(tab._part_output_path(windows[1], whole_novel=False)) is None
        assert created_override(tab._part_output_path(windows[2], whole_novel=False)) is True
        tab.shutdown()

    def test_render_selected_parts_skips_already_created_ones(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        path = self._project(library_dir, sample_meta, 30)  # batch 10 → 3 parts
        tab = self._tab_on_project(tmp_path, path)
        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        tab.video_image_edit.setText(str(image))
        windows = tab._windows_for_current_selection()
        out0 = tab._part_output_path(windows[0], whole_novel=False)
        out0.parent.mkdir(parents=True, exist_ok=True)
        out0.write_bytes(b"already rendered")  # part 1 already done

        asked = self._yes(monkeypatch)
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))

        tab._render_selected_parts(windows)  # all 3 rows "selected"

        assert captured["explicit_windows"] == [windows[1], windows[2]]
        assert captured["explicit_part_numbers"] == {
            windows[1].first_num: tab._part_number(windows[1]),
            windows[2].first_num: tab._part_number(windows[2]),
        }
        assert captured["mode"] == "batch" and captured["skip_existing"] is True
        assert "bỏ qua 1 phần" in asked[0][2]
        tab.shutdown()

    def test_render_selected_parts_with_nothing_pending_says_so(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from noveltrans.video_state import set_created_override

        path = self._project(library_dir, sample_meta, 10)  # batch 10 → 1 part
        tab = self._tab_on_project(tmp_path, path)
        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        tab.video_image_edit.setText(str(image))
        windows = tab._windows_for_current_selection()
        set_created_override(
            tab._part_output_path(windows[0], whole_novel=False), True, file_exists=False
        )

        launched = []
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: launched.append(kw))
        from PySide6.QtWidgets import QMessageBox
        informed = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: informed.append(a))

        tab._render_selected_parts(windows)

        assert launched == []
        assert informed
        tab.shutdown()

    def test_context_menu_render_action_targets_the_selected_rows(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        from PySide6.QtCore import QItemSelectionModel

        path = self._project(library_dir, sample_meta, 30)  # batch 10 → 3 parts
        tab = self._tab_on_project(tmp_path, path)
        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        tab.video_image_edit.setText(str(image))
        tab.video_list.selectRow(0)
        tab.video_list.selectionModel().select(
            tab.video_list.model().index(2, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._yes(monkeypatch)
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))
        menu = self._menu_for(tab, monkeypatch, 0)  # rows 0 and 2 selected, not adjacent

        render_action = next(a for a in menu.actions if a.text == "Tạo video")
        render_action.trigger()

        windows = tab._windows_for_current_selection()
        assert captured["explicit_windows"] == [windows[0], windows[2]]  # not windows[1]
        tab.shutdown()


class TestCreatedStatusToggle:
    """Feature 058 — manually ticking/unticking the "Trạng thái" (đã tạo) column."""

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _render_part(self, tab, index):
        """Fake a rendered part: create its .mp4 so it exists on disk."""
        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        return window, out

    def _answer(self, monkeypatch, button):
        from PySide6.QtWidgets import QMessageBox

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: (asked.append(a), button)[1]
        )
        return asked

    def test_column_is_checkable(self, qapp, tmp_path, library_dir, sample_meta, sample_refs):
        from PySide6.QtCore import Qt

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        item = tab.video_list.item(0, 4)
        assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert item.checkState() == Qt.CheckState.Unchecked
        tab.shutdown()

    def test_ticking_a_missing_part_marks_it_created(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Checked)

        assert created_override(out) is True
        assert tab.video_list.item(0, 4).text() == "✅ Đã tạo"
        tab.shutdown()

    def test_declining_the_tick_leaves_it_unmarked(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        self._answer(monkeypatch, QMessageBox.StandardButton.No)

        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Checked)

        assert created_override(out) is None
        assert tab.video_list.item(0, 4).checkState() == Qt.CheckState.Unchecked
        tab.shutdown()

    def test_unticking_a_rendered_part_marks_it_not_created(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Unchecked)

        assert created_override(out) is False
        assert tab.video_list.item(0, 4).text() == "⬜ Chưa tạo"
        # the .mp4 itself is untouched — only the tick changed
        assert out.is_file()
        tab.shutdown()

    def test_toggling_back_into_agreement_clears_the_override_without_asking(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override, state_path

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Checked)  # tick, file missing
        assert created_override(out) is True

        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Unchecked)  # back to reality

        assert created_override(out) is None
        assert not state_path(out).is_file()
        assert asked == []  # no confirmation for the safe/undo direction
        tab.shutdown()

    def test_a_real_render_clears_a_stale_not_created_override(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Untick a rendered part, then re-render it — the override must not survive.

        `_on_video_file_done` fires once per part the render worker actually finishes —
        this is the real "a render happened" signal, distinct from just refreshing the
        table (which must NOT clear the override on its own).
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Unchecked)
        assert created_override(out) is False

        # an innocuous refresh alone must not clear the override
        tab._refresh_video_list()
        assert created_override(out) is False

        out.unlink()
        out.write_bytes(b"freshly re-rendered")  # simulate a new render finishing
        tab._on_video_file_done(str(out))
        tab._refresh_video_list()

        assert created_override(out) is None
        assert tab.video_list.item(0, 4).text() == "✅ Đã tạo"
        tab.shutdown()

    def test_created_true_override_survives_a_refresh_while_file_still_missing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Checked)

        tab._refresh_video_list()  # e.g. triggered by an unrelated selection change

        assert created_override(out) is True
        assert tab.video_list.item(0, 4).text() == "✅ Đã tạo"
        assert not out.is_file()  # still nothing rendered
        tab.shutdown()

    def test_repopulating_the_table_does_not_fire_the_toggle_handler(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab._refresh_video_list()  # rebuilds every row, setting check states

        assert asked == []
        tab.shutdown()

    def test_start_video_skips_a_manually_marked_created_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """"Tạo video" only fills in what's missing — a part manually ticked "đã tạo"
        must be skipped too, not just one whose .mp4 actually exists on disk (matches
        VideoWorker's own skip_existing check, see test_video.py::TestVideoWorker)."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.video_state import created_override

        tab = self._tab_on_project(
            tmp_path, self._project_with_audio(library_dir, sample_meta, sample_refs)
        )
        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        tab.video_image_edit.setText(str(image))
        tab.tags_edit.setPlainText("a, b")  # skip the auto-tag-generation branch
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        tab.video_list.item(0, 4).setCheckState(Qt.CheckState.Checked)  # tick, no file
        assert created_override(out) is True

        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))

        tab._start_video()

        assert captured == {"skip_existing": True}
        assert "bỏ qua 1 phần đã có" in asked[0][2]
        tab.shutdown()


class TestRedoAllVideos:
    """“Tạo lại tất cả video” — re-render everything, overwriting what's already there."""

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab.video_image_edit.setText(str(path))  # any existing file passes the image check
        tab._on_project_selected(str(path))
        return tab

    def _yes(self, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        return asked

    def test_button_exists_next_to_create(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        assert tab.redo_all_button.text() == "Tạo lại tất cả video"
        tab.shutdown()

    def test_renders_every_part_without_skipping(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The whole point: unlike “Tạo video”, it must NOT skip parts that already exist."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"already rendered")

        self._yes(monkeypatch)
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))
        tab._redo_all_videos()
        assert captured == {"skip_existing": False}
        tab.shutdown()

    def test_confirm_dialog_mentions_the_overwrite_count(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for i in range(2):
            w = tab._windows_for_current_selection()[i]
            out = tab._part_output_path(w, whole_novel=False)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")

        asked = self._yes(monkeypatch)
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: None)
        tab._redo_all_videos()
        assert "Ghi đè 2 video đã có" in asked[0][2]
        tab.shutdown()

    def test_warns_when_parts_are_already_on_youtube(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Re-rendering does not change a published video — say so before an hours-long job."""
        from noveltrans.youtube_upload import STATE_PUBLISHED, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        w = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(w, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        write_upload_state(out, status=STATE_PUBLISHED)

        asked = self._yes(monkeypatch)
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: None)
        tab._redo_all_videos()
        assert "đã tải lên YouTube" in asked[0][2]
        tab.shutdown()

    def test_declining_the_confirm_renders_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        launched = []
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: launched.append(kw))
        tab._redo_all_videos()
        assert launched == []
        tab.shutdown()

    def test_no_project_starts_nothing(self, qapp, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        tab = VideoTab(_config(tmp_path))
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        launched = []
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: launched.append(kw))
        tab._redo_all_videos()
        assert shown and launched == []
        tab.shutdown()

    def test_redo_all_ignores_locked_windows_and_uses_the_fresh_grid(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """Feature 058 follow-up: unlike "Tạo video", "Tạo lại tất cả video" is an explicit
        full rebuild — it must use the current, full chapter grid (part 2 gets all 2
        chapters if now available), not stay pinned to an earlier partial commit."""
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.video_state import set_created_override

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(3)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(3):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()

        tab = self._tab_on_project(tmp_path, path)  # batch 2 → part 1 (1-2), part 2 (3)
        window2 = tab._windows_for_current_selection()[1]
        out2 = tab._part_output_path(window2, whole_novel=False)
        set_created_override(out2, True, file_exists=False)  # part 2 locked at chương 3

        project = NovelProject.open(path)
        project.replace_toc(refs + [ChapterRef(index=3, title="第4章", url="https://x/4")])
        project.save_audio(3, "exports/audio/3.mp3", "V", 60.0)
        project.close()
        tab._on_project_selected(str(path))  # reload to pick up chương 4

        self._yes(monkeypatch)
        captured = {}
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: captured.update(kw))
        tab._redo_all_videos()

        assert captured == {"skip_existing": False}  # redo-all never locks anything
        tab.shutdown()

    def test_redo_all_still_ignores_committed_windows_in_its_count(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """Feature 066: the confirm dialog now plans through the shared helper, but with
        `honor_committed=False` — it must still count the FRESH grid, matching the worker
        it launches with `skip_existing=False`, not the incremental run's locked plan."""
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.video_state import set_created_override

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(3)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(3):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()

        tab = self._tab_on_project(tmp_path, path)  # batch 2 → part 1 (1-2), part 2 (3)
        window2 = tab._windows_for_current_selection()[1]
        set_created_override(
            tab._part_output_path(window2, whole_novel=False), True, file_exists=False
        )

        project = NovelProject.open(path)
        project.replace_toc(refs + [ChapterRef(index=3, title="第4章", url="https://x/4")])
        project.save_audio(3, "exports/audio/3.mp3", "V", 60.0)
        project.close()
        tab._on_project_selected(str(path))  # chương 4 arrives

        asked = self._yes(monkeypatch)
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: None)
        tab._redo_all_videos()

        # fresh grid: (1-2) and (3-4). The locked plan would say 3 — (1-2), (3-3), (4-4).
        assert "toàn bộ 2 phần" in asked[0][2]
        tab.shutdown()

    def test_redo_all_count_honors_a_manual_split(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """The other half of the asymmetry, and a pre-existing mismatch this fixes: the
        redo-all worker has always honored a manual split (test_video.py), but the dialog
        planned with a bare `plan_merge_windows` and under-counted the parts."""
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.video_windows import split_window

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(4)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(4):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        split_window(path, 1, 4, 1)  # batch=4 would be one part; split off the last chương

        tab = self._tab_on_project(tmp_path, path)
        tab.video_batch_size.setValue(4)

        asked = self._yes(monkeypatch)
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: None)
        tab._redo_all_videos()

        assert "toàn bộ 2 phần" in asked[0][2], "the split the render honors, counted"
        tab.shutdown()

    def test_redo_all_does_not_disturb_the_tables_part_numbers(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """Redo-all's committed-blind plan must not be written into the `_locked_*` caches:
        a per-row "Tạo lại" clicked afterwards reads `_part_number` without re-planning."""
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.video_state import set_created_override

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(3)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(3):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()

        tab = self._tab_on_project(tmp_path, path)
        window2 = tab._windows_for_current_selection()[1]
        set_created_override(
            tab._part_output_path(window2, whole_novel=False), True, file_exists=False
        )

        project = NovelProject.open(path)
        project.replace_toc(refs + [ChapterRef(index=3, title="第4章", url="https://x/4")])
        project.save_audio(3, "exports/audio/3.mp3", "V", 60.0)
        project.close()
        tab._on_project_selected(str(path))

        windows = tab._windows_for_current_selection()  # locked: (1-2), (3-3), (4-4)
        before = {w.first_num: tab._part_number(w) for w in windows}

        self._yes(monkeypatch)
        monkeypatch.setattr(tab, "_launch_video", lambda **kw: None)
        tab._redo_all_videos()

        assert {w.first_num: tab._part_number(w) for w in windows} == before
        tab.shutdown()

    def test_render_and_upload_buttons_lock_each_other_out(self, qapp, tmp_path):
        """Rendering overwrites the very files an upload reads — they must not overlap."""
        tab = VideoTab(_config(tmp_path))
        tab._reset_video_ui()
        assert tab.upload_button.isEnabled() and tab.redo_all_button.isEnabled()
        tab._reset_upload_ui()
        assert tab.video_button.isEnabled() and tab.redo_all_button.isEnabled()
        tab.shutdown()


class TestYouTubeUploadUi:
    """The upload box, the "Đã tải lên" column, and the queue-selection rule."""

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _render_part(self, tab, index):
        """Fake a rendered part: create its .mp4 so it becomes upload-eligible."""
        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        return window, out

    def test_parts_table_has_an_upload_column(self, qapp, tmp_path, library_dir,
                                              sample_meta, sample_refs):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        assert tab.video_list.columnCount() == 7
        assert tab.video_list.horizontalHeaderItem(5).text() == "Đã tải lên"
        tab.shutdown()

    def test_upload_cell_reflects_each_state(self, qapp, tmp_path, library_dir,
                                             sample_meta, sample_refs):
        from noveltrans.youtube_upload import (
            STATE_COMMITTED,
            STATE_PUBLISHED,
            write_upload_state,
        )

        from PySide6.QtCore import Qt

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        cell = tab.video_list.item(0, 5)
        assert cell.checkState() == Qt.CheckState.Unchecked
        assert cell.text() == "Chưa tải"
        assert cell.flags() & Qt.ItemFlag.ItemIsUserCheckable  # the tick is the control

        write_upload_state(out, status=STATE_PUBLISHED, published_at="2026-07-27T20:00:00",
                           url="https://youtu.be/dQw4w9WgXcQ")
        tab._refresh_video_list()
        assert tab.video_list.item(0, 5).checkState() == Qt.CheckState.Checked

        # An interrupted attempt must be visibly different from "chưa tải" — showing it
        # as not-uploaded is what would invite the duplicate.
        write_upload_state(out, status=STATE_COMMITTED)
        tab._refresh_video_list()
        cell = tab.video_list.item(0, 5)
        assert "Dở dang" in cell.text()
        assert cell.checkState() == Qt.CheckState.Unchecked
        tab.shutdown()

    def test_repopulating_the_table_does_not_fire_the_toggle_handler(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Setting check states while rebuilding would pop a confirmation nobody asked for."""
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import STATE_PUBLISHED, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_PUBLISHED)
        asked = []
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a))
        tab._refresh_video_list()
        tab._refresh_video_list()
        assert asked == []
        tab.shutdown()

    def test_pending_rows_skip_unrendered_published_and_interrupted(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.youtube_upload import STATE_PUBLISHED, STATE_STARTED, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        # 3 parts; render all three, then mark one published and one interrupted.
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        write_upload_state(outs[0], status=STATE_PUBLISHED)
        write_upload_state(outs[1], status=STATE_STARTED)
        pending = tab._pending_upload_rows()
        assert [label for _, label, _, _ in pending] == ["Phần 3"]
        tab.shutdown()

    def test_pending_rows_ignores_parts_with_no_video(self, qapp, tmp_path, library_dir,
                                                     sample_meta, sample_refs):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        assert tab._pending_upload_rows() == []  # nothing rendered yet
        self._render_part(tab, 1)
        assert [label for _, label, _, _ in tab._pending_upload_rows()] == ["Phần 2"]
        tab.shutdown()

    def test_upload_request_reads_the_sidecars(self, qapp, tmp_path, library_dir,
                                               sample_meta, sample_refs):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window, out = self._render_part(tab, 0)
        (out.parent / (out.stem + ".title.txt")).write_text("Tiêu đề riêng", encoding="utf-8")
        (out.parent / (out.stem + ".tags.txt")).write_text("a, b, c", encoding="utf-8")
        (out.parent / (out.stem + ".jpg")).write_bytes(b"jpeg")
        tab.upload_playlist.setCurrentText("Danh sách của tôi")  # editable combo since 039

        request = tab._upload_request(window, "Phần 1", 1, False, publish_at=None)
        assert request.title == "Tiêu đề riêng"
        assert request.tags == "a, b, c"
        assert request.thumbnail == out.parent / (out.stem + ".jpg")
        assert request.playlist == "Danh sách của tôi"
        tab.shutdown()

    def test_upload_request_falls_back_when_sidecars_are_missing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Parts rendered before the sidecars existed must still be uploadable."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window, _ = self._render_part(tab, 0)
        request = tab._upload_request(window, "Phần 1", 1, False, publish_at=None)
        assert request.title.endswith("- Phần 1")  # computed, not read
        assert request.description  # the timestamp table, computed on the fly
        assert request.thumbnail is None
        tab.shutdown()

    def test_saving_tags_resyncs_already_rendered_parts(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Regenerating/editing tags used to leave every already-rendered part's
        `.tags.txt` frozen at whatever it was written at render time — stale both for
        "Chi tiết phần" and for that part's next upload (`_upload_request` reads the
        sidecar directly). Saving tags must re-stamp every rendered part's sidecar."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out0 = self._render_part(tab, 0)
        _, out1 = self._render_part(tab, 1)
        sidecar0 = out0.parent / (out0.stem + ".tags.txt")
        sidecar1 = out1.parent / (out1.stem + ".tags.txt")
        sidecar0.write_text("old, tags", encoding="utf-8")
        sidecar1.write_text("old, tags", encoding="utf-8")

        tab.tags_edit.setPlainText("new, tags")
        tab._save_tags()

        assert sidecar0.read_text(encoding="utf-8").strip() == "new, tags"
        assert sidecar1.read_text(encoding="utf-8").strip() == "new, tags"
        tab.shutdown()

    def test_resync_skips_parts_with_no_rendered_video(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """A part that hasn't been rendered yet has nowhere to write a sidecar into —
        `_resync_tags_sidecars` must not create one out of thin air."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)  # part 1 rendered, part 2 (index 1) is not

        updated = tab._resync_tags_sidecars("new, tags")

        assert updated == 1
        tab.shutdown()

    def test_generating_tags_resyncs_already_rendered_parts(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Same as saving, but via the "Tạo tags" auto-generate path (`_on_tags_ready`),
        which is the more common way tags actually get (re)created."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out0 = self._render_part(tab, 0)
        sidecar0 = out0.parent / (out0.stem + ".tags.txt")
        sidecar0.write_text("old, tags", encoding="utf-8")

        tab._on_tags_ready("fresh, tags")

        assert sidecar0.read_text(encoding="utf-8").strip() == "fresh, tags"
        tab.shutdown()

    def test_schedule_controls_only_show_for_scheduling(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        tab.upload_visibility.setCurrentIndex(tab.upload_visibility.findData("private"))
        assert not tab.upload_start.isVisibleTo(tab)
        tab.upload_visibility.setCurrentIndex(tab.upload_visibility.findData("schedule"))
        assert tab.upload_start.isVisibleTo(tab)
        tab.shutdown()

    def test_default_visibility_is_private(self, qapp, tmp_path):
        """The safe failure mode for a feature whose worst bug is publishing something."""
        tab = VideoTab(_config(tmp_path))
        assert tab.upload_visibility.currentData() == "private"
        tab.shutdown()

    def test_schedule_preview_lists_the_pending_parts(self, qapp, tmp_path, library_dir,
                                                     sample_meta, sample_refs):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for i in range(3):
            self._render_part(tab, i)
        tab.upload_visibility.setCurrentIndex(tab.upload_visibility.findData("schedule"))
        tab.upload_start.setDateTime(QDateTime(QDate(2026, 8, 1), QTime(20, 0)))
        tab.upload_spacing.setValue(2)
        tab._refresh_schedule_preview()
        text = tab.schedule_preview.text()
        assert "Phần 1: 01/08 20:00" in text
        assert "Phần 2: 03/08 20:00" in text
        assert "Phần 3: 05/08 20:00" in text
        tab.shutdown()

    def test_start_upload_with_nothing_eligible_starts_no_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab._start_upload()  # nothing rendered
        assert shown
        assert tab._upload_worker is None
        tab.shutdown()

    def test_launch_upload_builds_and_starts_a_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Drive `_launch_upload` all the way to `worker.start()`.

        Regression: `track_worker` was called with an extra argument, which only blew up
        once a real upload was attempted — every earlier test stopped at the "nothing
        eligible" guard and never reached this path. The wake-lock *manager* is stubbed
        here, but `track_worker` itself is the real one, so its call signature is
        exercised.
        """
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import YouTubeUploadWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        started = []
        monkeypatch.setattr(YouTubeUploadWorker, "start", lambda self: started.append(self))

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for i in range(2):
            self._render_part(tab, i)

        tab._start_upload()
        assert len(started) == 1
        assert [r.label for r in tab._upload_worker.requests] == ["Phần 1", "Phần 2"]
        # the run locks out the render buttons that would overwrite the files being sent
        assert not tab.upload_button.isEnabled()
        assert not tab.video_button.isEnabled()
        assert not tab.redo_all_button.isEnabled()
        assert tab.upload_cancel_button.isEnabled()
        tab._upload_worker = None  # never really started; don't let shutdown() wait on it
        tab.shutdown()

    def test_launch_upload_attaches_the_computed_schedule(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import YouTubeUploadWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        monkeypatch.setattr(YouTubeUploadWorker, "start", lambda self: None)

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for i in range(3):
            self._render_part(tab, i)
        tab.upload_visibility.setCurrentIndex(tab.upload_visibility.findData("schedule"))
        start = QDateTime.currentDateTime().addDays(1)
        tab.upload_start.setDateTime(start)
        tab.upload_spacing.setValue(2)

        tab._start_upload()
        times = [r.publish_at for r in tab._upload_worker.requests]
        assert [t.date() for t in times] == [
            start.addDays(d).toPython().date() for d in (0, 2, 4)
        ]
        assert all(r.visibility == "schedule" for r in tab._upload_worker.requests)
        tab._upload_worker = None
        tab.shutdown()

    def test_stuck_row_offers_reset_instead_of_a_dead_button(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """A “dở dang” part must have a way forward in the UI, not just a disabled button."""
        from PySide6.QtWidgets import QPushButton

        from noveltrans.youtube_upload import STATE_STARTED, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        assert tab.video_list.cellWidget(0, 6).findChildren(QPushButton)[3].text() == "Tải lên"

        write_upload_state(out, status=STATE_STARTED)
        tab._refresh_video_list()
        btn = tab.video_list.cellWidget(0, 6).findChildren(QPushButton)[3]
        assert btn.text() == "Đặt lại"
        assert btn.isEnabled()
        tab.shutdown()

    def test_reset_clears_the_record_and_requeues_the_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import (
            STATE_STARTED,
            read_upload_state,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_STARTED)
        assert tab._pending_upload_rows() == []  # stuck: excluded from the queue

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        tab._reset_upload_state(window, False)
        assert read_upload_state(out) == {}
        assert [label for _, label, _, _ in tab._pending_upload_rows()] == ["Phần 1"]
        # no video id was recorded, so the warning must say nothing reached YouTube
        assert "KHÔNG có" in asked[0][2]
        tab.shutdown()

    def test_reset_warns_differently_when_a_draft_exists_on_the_channel(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """With a video id recorded, clearing can create a duplicate — say so."""
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import STATE_DRAFT, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_DRAFT, video_id="dQw4w9WgXcQ",
                           url="https://youtu.be/dQw4w9WgXcQ")
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.No)[1],
        )
        tab._reset_upload_state(window, False)
        assert "HAI bản" in asked[0][2]
        assert "youtu.be/dQw4w9WgXcQ" in asked[0][2]
        tab.shutdown()

    def test_declining_the_reset_keeps_the_record(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import (
            STATE_STARTED,
            read_upload_state,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_STARTED)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        tab._reset_upload_state(window, False)
        assert read_upload_state(out).get("status") == STATE_STARTED
        tab.shutdown()

    def _accept_reset_dialog(self, monkeypatch, *, stuck=True, published=False):
        """Drive the reset dialog without showing it: tick boxes, then accept.

        `exec()` is modal and would block the offscreen test run forever, so it is
        replaced by a function that sets the checkboxes the way a user would and returns
        Accepted.
        """
        from PySide6.QtWidgets import QCheckBox, QDialog

        captured = {}

        def _exec(dialog):
            boxes = dialog.findChildren(QCheckBox)
            captured["labels"] = [b.text() for b in boxes]
            captured["defaults"] = [b.isChecked() for b in boxes]
            for box in boxes:
                if "dở dang" in box.text():
                    box.setChecked(stuck and box.isEnabled())
                elif "đã tải lên" in box.text():
                    box.setChecked(published and box.isEnabled())
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(QDialog, "exec", _exec)
        return captured

    def test_batch_reset_clears_every_stuck_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """A failed batch strands every part at once — one click has to fix all of them."""
        from noveltrans.youtube_upload import (
            STATE_COMMITTED,
            STATE_STARTED,
            is_uploadable,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        write_upload_state(outs[0], status=STATE_STARTED)
        write_upload_state(outs[1], status=STATE_COMMITTED, video_id="dQw4w9WgXcQ")
        self._accept_reset_dialog(monkeypatch, stuck=True)
        tab._reset_all_upload_states()
        assert all(is_uploadable(p) for p in outs)
        assert len(tab._pending_upload_rows()) == 3
        tab.shutdown()

    def test_published_parts_are_not_cleared_unless_explicitly_chosen(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The dangerous category must never ride along with the safe one."""
        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            STATE_STARTED,
            is_published,
            is_uploadable,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        write_upload_state(outs[0], status=STATE_STARTED)
        write_upload_state(outs[1], status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        captured = self._accept_reset_dialog(monkeypatch, stuck=True, published=False)
        tab._reset_all_upload_states()
        assert is_uploadable(outs[0])  # the stuck one went
        assert is_published(outs[1])  # the published one stayed
        # and the dangerous box is never pre-ticked
        assert captured["defaults"] == [True, False]
        tab.shutdown()

    def test_unmarking_published_parts_when_chosen(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The 'published but the transfer never landed' case this exists for."""
        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            is_uploadable,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        for out in outs:
            write_upload_state(out, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        self._accept_reset_dialog(monkeypatch, stuck=False, published=True)
        tab._reset_all_upload_states()
        assert all(is_uploadable(p) for p in outs)
        assert len(tab._pending_upload_rows()) == 3
        tab.shutdown()

    def test_cancelling_the_reset_dialog_changes_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QDialog

        from noveltrans.youtube_upload import STATE_STARTED, read_upload_state, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_STARTED)
        monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
        tab._reset_all_upload_states()
        assert read_upload_state(out).get("status") == STATE_STARTED
        tab.shutdown()

    def _answer(self, monkeypatch, button):
        from PySide6.QtWidgets import QMessageBox

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: (asked.append(a), button)[1]
        )
        return asked

    def test_unticking_a_published_part_marks_it_not_uploaded(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The tick is the control — this is the action the user asked for."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            is_uploadable,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ",
                           url="https://youtu.be/dQw4w9WgXcQ")
        tab._refresh_video_list()
        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab.video_list.item(0, 5).setCheckState(Qt.CheckState.Unchecked)

        assert is_uploadable(out)
        assert [label for _, label, _, _ in tab._pending_upload_rows()] == ["Phần 1"]
        # the strongest warning: names the duplicate risk and the link
        assert "HAI bản" in asked[0][2]
        assert "youtu.be/dQw4w9WgXcQ" in asked[0][2]
        tab.shutdown()

    def test_declining_the_untick_restores_the_tick_and_the_record(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            is_published,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        write_upload_state(out, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        tab._refresh_video_list()
        self._answer(monkeypatch, QMessageBox.StandardButton.No)

        tab.video_list.item(0, 5).setCheckState(Qt.CheckState.Unchecked)

        assert is_published(out)  # record untouched
        assert tab.video_list.item(0, 5).checkState() == Qt.CheckState.Checked  # tick back
        tab.shutdown()

    def test_ticking_an_unuploaded_part_marks_it_uploaded_by_hand(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """For a part the user uploaded themselves — batches must then skip it."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import is_published, read_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        assert [label for _, label, _, _ in tab._pending_upload_rows()] == ["Phần 1"]
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab.video_list.item(0, 5).setCheckState(Qt.CheckState.Checked)

        assert is_published(out)
        # no video id, and flagged as hand-marked so nothing treats it as a real link
        assert read_upload_state(out).get("marked_by_hand") is True
        assert read_upload_state(out).get("video_id", "") == ""
        assert tab._pending_upload_rows() == []
        tab.shutdown()

    def test_declining_the_tick_leaves_the_part_unmarked(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import is_uploadable

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        self._answer(monkeypatch, QMessageBox.StandardButton.No)

        tab.video_list.item(0, 5).setCheckState(Qt.CheckState.Checked)

        assert is_uploadable(out)
        assert tab.video_list.item(0, 5).checkState() == Qt.CheckState.Unchecked
        tab.shutdown()

    def test_batch_reset_with_nothing_stuck_says_so(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab._reset_all_upload_states()
        assert shown
        tab.shutdown()

    def test_header_indicator_tracks_all_none_some(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from PySide6.QtCore import Qt

        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            clear_upload_state,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        tab._refresh_video_list()
        assert tab.upload_header.check_state() == Qt.CheckState.Unchecked

        write_upload_state(outs[0], status=STATE_PUBLISHED)
        tab._refresh_video_list()
        assert tab.upload_header.check_state() == Qt.CheckState.PartiallyChecked

        for out in outs:
            write_upload_state(out, status=STATE_PUBLISHED)
        tab._refresh_video_list()
        assert tab.upload_header.check_state() == Qt.CheckState.Checked

        for out in outs:
            clear_upload_state(out)
        tab._refresh_video_list()
        assert tab.upload_header.check_state() == Qt.CheckState.Unchecked
        tab.shutdown()

    def test_header_check_all_marks_every_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import is_published, read_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        tab._refresh_video_list()
        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab._on_upload_header_toggled(True)

        assert all(is_published(p) for p in outs)
        assert all(read_upload_state(p).get("marked_by_hand") for p in outs)
        assert tab._pending_upload_rows() == []
        assert "3 phần" in asked[0][2]
        tab.shutdown()

    def test_header_uncheck_all_clears_every_record(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            is_uploadable,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        for out in outs:
            write_upload_state(out, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        tab._refresh_video_list()
        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab._on_upload_header_toggled(False)

        assert all(is_uploadable(p) for p in outs)
        assert len(tab._pending_upload_rows()) == 3
        assert "HAI bản" in asked[0][2]  # the bulk warning still names the duplicate risk
        tab.shutdown()

    def test_header_toggle_only_touches_rows_that_would_change(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import (
            STATE_PUBLISHED,
            is_published,
            read_upload_state,
            write_upload_state,
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        # one is already published by a real upload — its record must survive untouched
        write_upload_state(outs[0], status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        tab._refresh_video_list()
        asked = self._answer(monkeypatch, QMessageBox.StandardButton.Yes)

        tab._on_upload_header_toggled(True)

        assert all(is_published(p) for p in outs)
        assert read_upload_state(outs[0])["video_id"] == "dQw4w9WgXcQ"
        assert not read_upload_state(outs[0]).get("marked_by_hand")
        assert "2 phần" in asked[0][2]  # only the two that actually changed
        tab.shutdown()

    def test_header_toggle_declined_changes_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.youtube_upload import is_uploadable

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        outs = [self._render_part(tab, i)[1] for i in range(3)]
        tab._refresh_video_list()
        self._answer(monkeypatch, QMessageBox.StandardButton.No)

        tab._on_upload_header_toggled(True)

        assert all(is_uploadable(p) for p in outs)
        tab.shutdown()

    def test_header_toggle_with_nothing_to_do_says_so(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        tab._refresh_video_list()  # nothing rendered → no rows to toggle
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab._on_upload_header_toggled(True)
        assert shown
        tab.shutdown()

    def test_row_upload_button_disabled_once_published(self, qapp, tmp_path, library_dir,
                                                      sample_meta, sample_refs):
        from PySide6.QtWidgets import QPushButton

        from noveltrans.youtube_upload import STATE_PUBLISHED, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        tab._refresh_video_list()
        btn = tab.video_list.cellWidget(0, 6).findChildren(QPushButton)[3]
        assert btn.text() == "Tải lên" and btn.isEnabled()

        write_upload_state(out, status=STATE_PUBLISHED)
        tab._refresh_video_list()
        # No direct re-upload path for a published part — un-marking is the tick in the
        # "Đã tải lên" column, so this button just goes dead.
        assert not tab.video_list.cellWidget(0, 6).findChildren(QPushButton)[3].isEnabled()
        tab.shutdown()

    def test_shutdown_and_running_check_account_for_the_upload_worker(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        assert tab.has_running_workers() is False
        tab.shutdown()  # must not raise with no upload worker


class TestWorkspaceRegistration:
    def test_workspace_has_five_tabs_with_video_last(self, qapp, tmp_path):
        ws = Workspace(_config(tmp_path), AppState(state_dir=tmp_path))
        assert ws.tabs.count() == 5
        assert ws.tabs.tabText(4) == "5. Video"
        assert hasattr(ws, "video_tab")
        ws.shutdown()

    def test_audio_tab_no_longer_owns_video_controls(self, qapp, tmp_path):
        ws = Workspace(_config(tmp_path), AppState(state_dir=tmp_path))
        assert not hasattr(ws.audio_tab, "video_button")
        assert hasattr(ws.video_tab, "video_button")
        ws.shutdown()


class TestThumbnailUpdateUi:
    """The two "cập nhật ảnh bìa" buttons: eligibility, interlocks, and the confirm.

    Same helpers as `TestYouTubeUploadUi`, plus a cover writer — the whole feature is
    about a part having BOTH a video on the channel and a .jpg on disk.
    """

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _render_part(self, tab, index):
        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        return window, out

    def _cover(self, tab, index):
        """Write the .jpg sidecar the cover editor would produce for this part."""
        window = tab._windows_for_current_selection()[index]
        jpg = tab._part_sidecar(window, False, ".jpg")
        jpg.parent.mkdir(parents=True, exist_ok=True)
        jpg.write_bytes(b"\xff\xd8\xff" + b"jpeg-ish" * 64)
        return jpg

    def _publish(self, out, video_id="dQw4w9WgXcQ"):
        from noveltrans.youtube_upload import STATE_PUBLISHED, write_upload_state

        write_upload_state(out, status=STATE_PUBLISHED, video_id=video_id)

    def _cover_button(self, tab, row=0):
        """The row's "Cập nhật bìa" — appended last, after Tạo/Chi tiết/Ảnh bìa/Tải lên."""
        from PySide6.QtWidgets import QPushButton

        return tab.video_list.cellWidget(row, 6).findChildren(QPushButton)[4]

    def test_row_button_is_last_so_existing_positions_do_not_shift(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)
        tab._refresh_video_list()
        assert self._cover_button(tab).text() == "Cập nhật bìa"
        tab.shutdown()

    def test_row_button_needs_a_video_on_youtube(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)
        self._cover(tab, 0)
        tab._refresh_video_list()
        button = self._cover_button(tab)
        assert not button.isEnabled()
        assert "chưa có video" in button.toolTip()
        tab.shutdown()

    def test_row_button_needs_a_cover_on_disk(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._publish(out)
        tab._refresh_video_list()
        button = self._cover_button(tab)
        assert not button.isEnabled()
        assert "Chưa có ảnh bìa" in button.toolTip()
        tab.shutdown()

    def test_row_button_enabled_when_both_are_there(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._cover(tab, 0)
        self._publish(out)
        tab._refresh_video_list()
        assert self._cover_button(tab).isEnabled()
        tab.shutdown()

    def test_row_button_enabled_for_a_scheduled_draft_not_only_published(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The eligibility decision, pinned at the UI level: a scheduled or private video
        still has an editable thumbnail, and a push can't duplicate anything."""
        from noveltrans.youtube_upload import STATE_DRAFT, write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._cover(tab, 0)
        write_upload_state(out, status=STATE_DRAFT, video_id="dQw4w9WgXcQ")
        tab._refresh_video_list()
        assert self._cover_button(tab).isEnabled()
        tab.shutdown()

    def test_rows_skip_parts_with_no_video_or_no_cover(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.youtube_upload import mark_uploaded_by_hand

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out0 = self._render_part(tab, 0)
        self._cover(tab, 0)
        self._publish(out0)
        _, out1 = self._render_part(tab, 1)  # hand-marked: no video id to aim at
        self._cover(tab, 1)
        mark_uploaded_by_hand(out1)
        assert [label for _w, label, _wn in tab._thumbnail_update_rows()] == ["Phần 1"]
        tab.shutdown()

    def test_rows_include_a_part_whose_mp4_was_deleted(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The video lives on YouTube now; a cleaned-up local render must not block the push."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._cover(tab, 0)
        self._publish(out)
        out.unlink()
        assert [label for _w, label, _wn in tab._thumbnail_update_rows()] == ["Phần 1"]
        tab.shutdown()

    def test_request_points_at_the_jpg_sidecar_and_the_recorded_id(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Pins "the source is the sidecar" — there is no file picker in this flow."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        window, out = self._render_part(tab, 0)
        jpg = self._cover(tab, 0)
        self._publish(out)
        request = tab._thumbnail_request(window, "Phần 1", False)
        assert request.thumbnail == jpg
        assert request.video_id == "dQw4w9WgXcQ"
        tab.shutdown()

    def test_nothing_eligible_starts_no_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)
        tab._start_thumbnail_update()
        assert shown and tab._thumbnail_worker is None
        tab.shutdown()

    def _drive(self, tab, monkeypatch, answer=True):
        """Run `_start_thumbnail_update` to `worker.start()` with the real `track_worker`."""
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import YouTubeThumbnailWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        asked = []

        def question(_self, title, text, *a, **k):
            asked.append(text)
            return (
                QMessageBox.StandardButton.Yes if answer else QMessageBox.StandardButton.No
            )

        monkeypatch.setattr(QMessageBox, "question", question)
        started = []
        monkeypatch.setattr(
            YouTubeThumbnailWorker, "start", lambda self: started.append(self)
        )
        tab._start_thumbnail_update()
        return started, asked

    def test_launch_builds_and_starts_a_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The `_launch_upload` regression test's twin: `track_worker` itself is the real
        one here, so its call signature is exercised — that is the bug 033 shipped."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for i in range(2):
            _, out = self._render_part(tab, i)
            self._cover(tab, i)
            self._publish(out, video_id=f"vid{i}xxxxxxx")

        started, _asked = self._drive(tab, monkeypatch)
        assert len(started) == 1
        assert [r.label for r in tab._thumbnail_worker.requests] == ["Phần 1", "Phần 2"]
        # a render would rewrite the very .jpg being pushed; an upload shares the profile
        assert not tab.thumbnail_update_button.isEnabled()
        assert not tab.upload_button.isEnabled()
        assert not tab.video_button.isEnabled()
        assert not tab.redo_all_button.isEnabled()
        assert not tab.thumb_regen_button.isEnabled()
        assert tab.upload_cancel_button.isEnabled()
        tab._thumbnail_worker = None  # never really started; don't let shutdown() wait
        tab.shutdown()

    def test_confirm_warns_when_a_target_is_live_on_the_channel(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._cover(tab, 0)
        self._publish(out)
        _started, asked = self._drive(tab, monkeypatch)
        assert asked and "đang hiển thị trên kênh" in asked[0]
        tab._thumbnail_worker = None
        tab.shutdown()

    def test_declining_the_confirm_starts_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._cover(tab, 0)
        self._publish(out)
        started, _asked = self._drive(tab, monkeypatch, answer=False)
        assert started == [] and tab._thumbnail_worker is None
        tab.shutdown()

    def test_finished_handler_restores_every_button(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for b in (
            tab.thumbnail_update_button, tab.upload_button, tab.video_button,
            tab.redo_all_button, tab.thumb_regen_button,
        ):
            b.setEnabled(False)
        tab._on_thumbnail_finished(2, 0)
        assert tab.thumbnail_update_button.isEnabled()
        assert tab.upload_button.isEnabled()
        assert tab.video_button.isEnabled()
        assert tab.redo_all_button.isEnabled()
        assert tab.thumb_regen_button.isEnabled()
        assert not tab.upload_cancel_button.isEnabled()
        tab.shutdown()

    def test_an_upload_run_locks_out_the_thumbnail_button(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Both runs open the same persistent profile, so they can never overlap."""
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import YouTubeUploadWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        monkeypatch.setattr(YouTubeUploadWorker, "start", lambda self: None)

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)
        tab._start_upload()
        assert not tab.thumbnail_update_button.isEnabled()
        tab._upload_worker = None
        tab.shutdown()

    def test_cancel_button_stops_the_thumbnail_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))

        class _Fake:
            def __init__(self):
                self.cancelled = False

            def isRunning(self):
                return True

            def cancel(self):
                self.cancelled = True

        worker = _Fake()
        tab._thumbnail_worker = worker
        tab._cancel_upload()
        assert worker.cancelled is True
        tab._thumbnail_worker = None
        tab.shutdown()

    def test_upload_cell_tooltip_reports_the_last_cover_push(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The only place a push leaves a visible trace — without it "did that do
        anything?" has no answer."""
        from noveltrans.youtube_upload import write_upload_state

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._publish(out)
        write_upload_state(out, thumbnail_updated_at="2026-07-28T10:00:00+07:00")
        tab._refresh_video_list()
        assert "2026-07-28" in tab.video_list.item(0, 5).toolTip()
        tab.shutdown()

    def test_regen_all_points_at_the_push_button_when_parts_are_live(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Re-rendering a cover changes nothing on YouTube — the same trap
        `_redo_all_videos` warns about, and the discovery path for the new button."""
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        _, out = self._render_part(tab, 0)
        self._cover(tab, 0)
        self._publish(out)
        monkeypatch.setattr(tab, "_thumbnail_base_image", lambda: "base.png")
        monkeypatch.setattr(tab, "_render_thumbnail_now", lambda *a, **k: None)
        tab._regen_all_thumbnails()
        assert "Cập nhật ảnh bìa" in tab.status_label.text()
        tab.shutdown()

    def test_regen_all_stays_quiet_when_nothing_is_on_youtube(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        self._render_part(tab, 0)
        monkeypatch.setattr(tab, "_thumbnail_base_image", lambda: "base.png")
        monkeypatch.setattr(tab, "_render_thumbnail_now", lambda *a, **k: None)
        tab._regen_all_thumbnails()
        assert "Cập nhật ảnh bìa" not in tab.status_label.text()
        tab.shutdown()

    def test_shutdown_accounts_for_the_thumbnail_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        tab.shutdown()  # no worker: must not raise

    def test_clicking_the_row_button_starts_a_run_for_that_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Regression: the row path first looked its part up in `_thumbnail_update_rows()`,
        which re-plans the windows on every call — so the fresh objects never matched the
        one the row had captured and the button always reported "chưa có video"."""
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import YouTubeThumbnailWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        started = []
        monkeypatch.setattr(
            YouTubeThumbnailWorker, "start", lambda self: started.append(self)
        )

        tab = self._tab_on_project(tmp_path, self._project_with_audio(
            library_dir, sample_meta, sample_refs))
        for i in range(2):
            _, out = self._render_part(tab, i)
            self._cover(tab, i)
            self._publish(out, video_id=f"vid{i}xxxxxxx")
        tab._refresh_video_list()

        self._cover_button(tab, row=1).click()
        assert len(started) == 1
        assert [r.label for r in tab._thumbnail_worker.requests] == ["Phần 2"]
        tab._thumbnail_worker = None
        tab.shutdown()


class TestDisplayTitleUi:
    """Feature 035 — the "Tên hiển thị" override, end to end through the Video tab."""

    def _project(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        project.save_meta_translation(
            "[ĐM/EDIT] CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG", "mô tả", "vi"
        )
        path = project.path
        project.close()
        return path

    def _tab(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def test_empty_box_shows_the_current_title_as_a_placeholder(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """An empty box has to read as "dùng tên đã dịch", not "there is no title"."""
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        assert tab.display_title_edit.text() == ""
        assert "[ĐM/EDIT]" in tab.display_title_edit.placeholderText()
        tab.shutdown()

    def test_setting_it_changes_the_video_title_and_the_parts_table(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.display_title_edit.setText("CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG")
        tab._save_display_title()

        assert tab.project.meta.display_name() == (
            "CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG"
        )
        # the video title ("... - Phần N") and the table's Tiêu đề column both follow
        assert tab._part_title(1) == (
            "CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG - Phần 1"
        )
        assert "[ĐM/EDIT]" not in tab.video_list.item(0, 3).text()
        tab.shutdown()

    def test_the_slug_and_therefore_every_rendered_part_stays_put(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """**The invariant this feature turns on.**

        Rendering a part, then editing the display title, must not move the .mp4 or its
        sidecars. If it did, the part would read as "chưa tạo" and its `.upload.json`
        from feature 034 would orphan — the app would offer to re-upload a video already
        live on the channel.
        """
        from noveltrans.youtube_upload import STATE_PUBLISHED, uploaded_video_id, write_upload_state

        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        window = tab._windows_for_current_selection()[0]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        write_upload_state(out, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")

        tab.display_title_edit.setText("CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG")
        tab._save_display_title()

        after = tab._part_output_path(
            tab._windows_for_current_selection()[0], whole_novel=False
        )
        assert after == out and after.is_file()
        assert uploaded_video_id(after) == "dQw4w9WgXcQ"  # the record still points at it
        assert tab.video_list.item(0, 4).text() == "✅ Đã tạo"
        tab.shutdown()

    def test_a_whitespace_only_entry_does_not_blank_the_title(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.display_title_edit.setText("   ")
        tab._save_display_title()
        assert tab.project.meta.display_name().startswith("[ĐM/EDIT]")
        tab.shutdown()

    def test_clearing_it_falls_back_to_the_translated_title(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.display_title_edit.setText("Tên ngắn")
        tab._save_display_title()
        tab.display_title_edit.setText("")
        tab._save_display_title()
        assert tab.project.meta.display_name().startswith("[ĐM/EDIT]")
        tab.shutdown()

    def test_the_status_line_says_covers_need_regenerating(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Editing the title doesn't redraw covers already on disk — say so, or it looks
        like nothing happened."""
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.display_title_edit.setText("Tên ngắn")
        tab._save_display_title()
        assert "Tạo lại tất cả ảnh bìa" in tab.status_label.text()
        tab.shutdown()

    def test_the_thumbnail_is_rendered_with_the_override(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from noveltrans.tts.video import font_dir_context

        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.display_title_edit.setText("Tên ngắn")
        tab._save_display_title()

        seen = {}
        monkeypatch.setattr(
            "noveltrans.tts.thumbnail.render_thumbnail",
            lambda *a, **k: seen.update(k) or a[1],
        )
        window = tab._windows_for_current_selection()[0]
        with font_dir_context() as font_dir:
            tab._render_thumbnail_now(
                window, 1, False, base="/no/such.png", font_dir=font_dir
            )
        assert seen["vn_title"] == "Tên ngắn"
        tab.shutdown()

    def test_render_passes_the_configured_text_scales(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from noveltrans.tts.video import font_dir_context

        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        # Per-novel now: the cover layout belongs to the novel, not the app config.
        tab._apply_video_settings(
            {
                **tab._video_settings,
                "video_thumbnail_title_scale": 1.5,
                "video_thumbnail_part_scale": 0.8,
                "video_thumbnail_tagline_scale": 1.2,
            }
        )

        seen = {}
        monkeypatch.setattr(
            "noveltrans.tts.thumbnail.render_thumbnail",
            lambda *a, **k: seen.update(k) or a[1],
        )
        window = tab._windows_for_current_selection()[0]
        with font_dir_context() as font_dir:
            tab._render_thumbnail_now(
                window, 1, False, base="/no/such.png", font_dir=font_dir
            )
        assert seen["title_scale"] == 1.5
        assert seen["part_scale"] == 0.8
        assert seen["tagline_scale"] == 1.2
        tab.shutdown()

    def test_the_render_worker_is_given_the_title_and_scales(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """The video render is the *other* path that writes a cover — it must carry the
        same settings, or a full render would silently undo an editor change."""
        from noveltrans.gui import tab_video as tab_module

        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        # Per-novel now: the cover layout belongs to the novel, not the app config.
        tab._apply_video_settings(
            {
                **tab._video_settings,
                "video_thumbnail_title_scale": 1.5,
                "video_thumbnail_part_scale": 0.8,
                "video_thumbnail_tagline_scale": 1.2,
            }
        )
        tab.video_image_edit.setText(str(tmp_path / "bg.png"))
        (tmp_path / "bg.png").write_bytes(b"x")

        built = {}

        class _FakeWorker:
            def __init__(self, *a, **kw):
                built.update(kw)
                self.progress = self.file_done = self.finished_ok = self.failed = _Sig()

            def start(self):
                pass

            def isRunning(self):
                return False

        class _Sig:
            def connect(self, *_a):
                pass

        monkeypatch.setattr(tab_module, "VideoWorker", _FakeWorker)
        monkeypatch.setattr(tab_module, "track_worker", lambda *_a: None)
        tab._launch_video()

        assert built["thumb_title_scale"] == 1.5
        assert built["thumb_part_scale"] == 0.8
        assert built["thumb_tagline_scale"] == 1.2
        tab._video_worker = None
        tab.shutdown()

    def test_both_cover_paths_carry_the_title_alignment(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Feature 036. Both writers must carry it, or a full video render would silently
        undo an alignment set in the cover editor."""
        from noveltrans.gui import tab_video as tab_module
        from noveltrans.tts.video import font_dir_context

        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        # Per-novel now: the cover layout belongs to the novel, not the app config.
        tab._apply_video_settings(
            {**tab._video_settings, "video_thumbnail_title_align": "right"}
        )

        # path 1: the cover-only render
        seen = {}
        monkeypatch.setattr(
            "noveltrans.tts.thumbnail.render_thumbnail",
            lambda *a, **k: seen.update(k) or a[1],
        )
        window = tab._windows_for_current_selection()[0]
        with font_dir_context() as font_dir:
            tab._render_thumbnail_now(
                window, 1, False, base="/no/such.png", font_dir=font_dir
            )
        assert seen["title_align"] == "right"

        # path 2: the full video render
        tab.video_image_edit.setText(str(tmp_path / "bg.png"))
        (tmp_path / "bg.png").write_bytes(b"x")
        built = {}

        class _Sig:
            def connect(self, *_a):
                pass

        class _FakeWorker:
            def __init__(self, *a, **kw):
                built.update(kw)
                self.progress = self.file_done = self.finished_ok = self.failed = _Sig()

            def start(self):
                pass

            def isRunning(self):
                return False

        monkeypatch.setattr(tab_module, "VideoWorker", _FakeWorker)
        monkeypatch.setattr(tab_module, "track_worker", lambda *_a: None)
        tab._launch_video()
        assert built["thumb_title_align"] == "right"
        tab._video_worker = None
        tab.shutdown()


class TestVideoTabScrolling:
    """Feature 037 — the tab scrolls, and the parts table stops collapsing.

    Reported against a 75-part project on a laptop window: the table was squeezed to a
    single vertically-clipped row and the boxes below it were sliced through, because six
    group boxes are taller than the window and there was nowhere for the overflow to go.
    """

    def _tab(self, tmp_path, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()

        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab._on_project_selected(str(path))
        return tab

    def _laid_out(self, qapp, tab, width, height):
        tab.resize(width, height)
        tab.show()
        qapp.processEvents()
        return tab

    def test_the_tab_content_lives_in_a_resizable_scroll_area(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from PySide6.QtWidgets import QScrollArea

        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        assert isinstance(tab.scroll, QScrollArea)
        # widgetResizable is the whole trick: the content fills the viewport when there is
        # room and grows past it when there isn't.
        assert tab.scroll.widgetResizable()
        assert tab.scroll.widget().isAncestorOf(tab.video_list)
        tab.shutdown()

    def test_progress_and_status_stay_out_of_the_scroll_area(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """A render or upload batch runs for minutes to hours. Feedback you have to scroll
        to find is feedback you don't see — least of all while reading the parts table."""
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        content = tab.scroll.widget()
        assert not content.isAncestorOf(tab.progress)
        assert not content.isAncestorOf(tab.status_label)
        assert tab.isAncestorOf(tab.progress)
        tab.shutdown()

    def test_the_parts_table_survives_a_short_window(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """**The regression.** It used to collapse to one clipped row here."""
        tab = self._laid_out(
            qapp, self._tab(tmp_path, library_dir, sample_meta, sample_refs), 1400, 420
        )
        row_h = tab.video_list.verticalHeader().defaultSectionSize()
        assert tab.video_list.height() >= 240
        assert tab.video_list.height() // row_h >= 5  # several rows, not one sliced one
        tab.hide()
        tab.shutdown()

    def test_a_short_window_has_something_to_scroll_to(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The content keeps its natural height instead of being squeezed into the
        viewport — which is what makes the boxes below the table reachable at all."""
        tab = self._laid_out(
            qapp, self._tab(tmp_path, library_dir, sample_meta, sample_refs), 1400, 420
        )
        assert tab.scroll.widget().height() > tab.scroll.viewport().height()
        assert tab.scroll.verticalScrollBar().maximum() > 0
        tab.hide()
        tab.shutdown()

    def test_a_tall_window_still_grows_the_table(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The minimum is a floor, not a fixed size: spare vertical space still goes to
        the table via its stretch, so a big screen gets a big table."""
        tab = self._laid_out(
            qapp, self._tab(tmp_path, library_dir, sample_meta, sample_refs), 1400, 1600
        )
        assert tab.video_list.height() > 240
        tab.hide()
        tab.shutdown()

    def test_the_horizontal_policy_is_as_needed_never_off(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Off would clip silently if content turned out wider than the viewport — the
        exact failure this change exists to end."""
        from PySide6.QtCore import Qt

        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        assert (
            tab.scroll.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        tab.shutdown()


class TestPlaylistPickerUi:
    """Feature 039 — the playlist combo, the fetch, and the ordered add-all."""

    def _project(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _uploaded_part(self, tab, index, video_id):
        from noveltrans.youtube_upload import STATE_PUBLISHED, write_upload_state

        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        write_upload_state(out, status=STATE_PUBLISHED, video_id=video_id)
        return out

    def test_the_picker_is_editable_so_a_new_name_still_works(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The list is an aid, not a gate — a name that isn't on the channel yet must
        still reach the upload, exactly as it did before this feature."""
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        assert tab.upload_playlist.isEditable()
        self._uploaded_part(tab, 0, "vid0xxxxxxx")
        tab.upload_playlist.setCurrentText("Danh sách chưa tồn tại")
        w = tab._windows_for_current_selection()[0]
        request = tab._upload_request(w, "Phần 1", 1, False, publish_at=None)
        assert request.playlist == "Danh sách chưa tồn tại"
        tab.shutdown()

    def test_fetching_repopulates_without_losing_what_was_typed(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Losing a half-typed name to a background fetch would be its own small
        betrayal, and the combo is editable precisely so an unlisted name stays usable."""
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.upload_playlist.setCurrentText("Đang gõ dở")
        tab._on_playlists_fetched(["Truyện A", "Truyện B"])
        assert tab.upload_playlist.currentText() == "Đang gõ dở"
        assert [
            tab.upload_playlist.itemText(i) for i in range(tab.upload_playlist.count())
        ] == ["Truyện A", "Truyện B"]
        tab.shutdown()

    def test_an_empty_channel_says_so_instead_of_looking_broken(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab._on_playlists_fetched([])
        assert "chưa có danh sách phát" in tab.status_label.text().lower()
        tab.shutdown()

    def test_sync_rows_are_uploaded_parts_in_order(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        self._uploaded_part(tab, 0, "vid0xxxxxxx")
        self._uploaded_part(tab, 2, "vid2xxxxxxx")  # part 2 not uploaded
        assert [label for _p, label in tab._playlist_sync_rows()] == ["Phần 1", "Phần 3"]
        tab.shutdown()

    def test_sync_without_a_playlist_name_starts_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        shown: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        self._uploaded_part(tab, 0, "vid0xxxxxxx")
        tab.upload_playlist.setCurrentText("")
        tab._start_playlist_sync()
        assert shown and tab._playlist_worker is None
        tab.shutdown()

    def test_sync_with_nothing_uploaded_starts_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        shown: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        tab.upload_playlist.setCurrentText("Truyện A")
        tab._start_playlist_sync()
        assert shown and tab._playlist_worker is None
        tab.shutdown()

    def _drive(self, tab, monkeypatch, answer=True):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import PlaylistSyncWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        asked: list = []

        def question(_self, title, text, *a, **k):
            asked.append(text)
            return (
                QMessageBox.StandardButton.Yes if answer else QMessageBox.StandardButton.No
            )

        monkeypatch.setattr(QMessageBox, "question", question)
        started: list = []
        monkeypatch.setattr(PlaylistSyncWorker, "start", lambda self: started.append(self))
        tab._start_playlist_sync()
        return started, asked

    def test_the_confirm_states_that_it_empties_the_playlist(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """It empties something viewers may be watching. Agreeing to that once when the
        feature was designed is not the same as remembering it at the moment of clicking."""
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        self._uploaded_part(tab, 0, "vid0xxxxxxx")
        tab.upload_playlist.setCurrentText("Truyện A")
        _started, asked = self._drive(tab, monkeypatch)
        assert asked
        assert "XOÁ HẾT" in asked[0]
        assert "Truyện A" in asked[0]
        tab._playlist_worker = None
        tab.shutdown()

    def test_declining_the_confirm_starts_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        self._uploaded_part(tab, 0, "vid0xxxxxxx")
        tab.upload_playlist.setCurrentText("Truyện A")
        started, _asked = self._drive(tab, monkeypatch, answer=False)
        assert started == [] and tab._playlist_worker is None
        tab.shutdown()

    def test_launching_builds_a_worker_and_locks_the_other_browser_runs_out(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """`track_worker` is the real one here, so its call signature is exercised — the
        bug 033 shipped."""
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        for i in range(2):
            self._uploaded_part(tab, i, f"vid{i}xxxxxxx")
        tab.upload_playlist.setCurrentText("Truyện A")
        started, _asked = self._drive(tab, monkeypatch)

        assert len(started) == 1
        assert [r.label for r in tab._playlist_worker.requests] == ["Phần 1", "Phần 2"]
        assert tab._playlist_worker.playlist == "Truyện A"
        assert not tab.upload_button.isEnabled()
        assert not tab.thumbnail_update_button.isEnabled()
        assert not tab.video_button.isEnabled()
        assert not tab.playlist_sync_button.isEnabled()
        assert tab.upload_cancel_button.isEnabled()
        tab._playlist_worker = None
        tab.shutdown()

    def test_the_finish_handler_restores_every_button(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))
        for b in (
            tab.playlist_sync_button, tab.playlist_fetch_button, tab.upload_button,
            tab.thumbnail_update_button, tab.video_button, tab.redo_all_button,
        ):
            b.setEnabled(False)
        tab._on_playlist_finished(3, 3, 0)
        assert tab.playlist_sync_button.isEnabled()
        assert tab.upload_button.isEnabled()
        assert not tab.upload_cancel_button.isEnabled()
        assert "gỡ 3" in tab.status_label.text()
        tab.shutdown()

    def test_the_cancel_button_stops_the_playlist_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, self._project(library_dir, sample_meta, sample_refs))

        class _Fake:
            def __init__(self):
                self.cancelled = False

            def isRunning(self):
                return True

            def cancel(self):
                self.cancelled = True

        worker = _Fake()
        tab._playlist_worker = worker
        tab._cancel_upload()
        assert worker.cancelled is True
        tab._playlist_worker = None
        tab.shutdown()


class TestSubtitleCoverageUi:
    """Feature 040 — the render's report about which parts got an .srt."""

    def _tab(self, tmp_path, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _render(self, tab, index, *, with_srt):
        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        if with_srt:
            out.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
        return out

    def test_full_coverage_reports_no_warning(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._render(tab, 0, with_srt=True)
        assert tab._subtitle_coverage() == (1, 1)
        tab._on_video_finished(1)
        assert "Phụ đề:" not in tab.status_label.text()
        assert "phụ đề .srt" in tab.status_label.text()
        tab.shutdown()

    def test_partial_coverage_says_the_audio_predates_the_feature(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Silence here would read as a broken feature rather than as old audio."""
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._render(tab, 0, with_srt=True)
        self._render(tab, 1, with_srt=False)
        assert tab._subtitle_coverage() == (1, 2)
        tab._on_video_finished(2)
        text = tab.status_label.text()
        assert "Phụ đề: 1/2" in text
        assert "tạo lại audio" in text
        tab.shutdown()

    def test_unrendered_parts_are_not_counted_against_coverage(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Only parts that actually produced a video can be expected to have an .srt."""
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._render(tab, 0, with_srt=True)  # parts 2 and 3 never rendered
        assert tab._subtitle_coverage() == (1, 1)
        tab.shutdown()


class TestBurnSubtitlesOption:
    """Feature 041 — the "Chèn phụ đề" checkbox."""

    def _tab(self, tmp_path, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def test_it_is_off_by_default(self, qapp, tmp_path, library_dir, sample_meta, sample_refs):
        """It changes what every future render looks like, and the current look is what
        users have already published."""
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        assert not tab.burn_subs_check.isChecked()
        assert not tab.config.video_burn_subtitles
        tab.shutdown()

    def test_ticking_it_persists_to_config(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        tab.burn_subs_check.setChecked(True)
        assert tab.config.video_burn_subtitles is True
        tab.shutdown()

    def test_the_saved_setting_seeds_the_checkbox(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        tab.config.video_burn_subtitles = True
        tab2 = VideoTab(tab.config)
        assert tab2.burn_subs_check.isChecked()
        tab.shutdown()
        tab2.shutdown()

    def test_the_flag_reaches_the_render_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from noveltrans.gui import tab_video as tab_module

        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        tab.burn_subs_check.setChecked(True)
        tab.video_image_edit.setText(str(tmp_path / "bg.png"))
        (tmp_path / "bg.png").write_bytes(b"x")

        built = {}

        class _Sig:
            def connect(self, *_a):
                pass

        class _FakeWorker:
            def __init__(self, *a, **kw):
                built.update(kw)
                self.progress = self.file_done = self.finished_ok = self.failed = _Sig()

            def start(self):
                pass

            def isRunning(self):
                return False

        monkeypatch.setattr(tab_module, "VideoWorker", _FakeWorker)
        monkeypatch.setattr(tab_module, "track_worker", lambda *_a: None)
        tab._launch_video()
        assert built["burn_subtitles"] is True
        tab._video_worker = None
        tab.shutdown()


class TestSubtitleWorker:
    """Feature 042 — writing .srt without a render, and where the file lands.

    The worker owns the risky parts: planning the same windows the render would, and
    building the same output path. A sidecar written here and one written by a render must
    be the same file in the same place.
    """

    def _project(self, library_dir, sample_meta, sample_refs, tmp_path):
        from noveltrans.tts.subtitles import Cue, write_cues

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_meta_translation("Tên Truyện", "mô tả", "vi")
        audio_dir = project.path / "exports" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            wav = audio_dir / f"{i:04d}.wav"
            wav.write_bytes(b"RIFF" + b"\0" * 64)
            project.save_audio(i, str(wav.relative_to(project.path)), "V", 60.0)
            write_cues(wav, [Cue(0, 5, f"Câu chương {i + 1}")], seconds=60.0)
        path = project.path
        project.close()
        return path

    def _run(self, path, **kw):
        from noveltrans.gui.workers import SubtitleWorker

        worker = SubtitleWorker(path, "V", "batch", batch=2, **kw)
        got = {}
        worker.finished_ok.connect(lambda w, b, s: got.update(written=w, backfilled=b, skipped=s))
        worker.failed.connect(lambda m: got.update(error=m))
        worker.run()  # synchronous: this is the logic, not the threading
        return got

    def test_it_writes_one_srt_per_part_without_rendering(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project(library_dir, sample_meta, sample_refs, tmp_path)
        got = self._run(path)
        assert "error" not in got, got.get("error")
        assert got["written"] == 3  # 5 chapters, batch of 2 -> 3 parts
        srts = sorted((path / "exports" / "video").rglob("*.srt"))
        assert len(srts) == 3
        assert not list((path / "exports" / "video").rglob("*.mp4"))  # nothing rendered

    def test_the_srt_lands_beside_where_the_mp4_would_go(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Same folder and stem as the render's output, or the pair would never meet."""
        from pathlib import Path

        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        path = self._project(library_dir, sample_meta, sample_refs, tmp_path)
        self._run(path)
        name = video_part_name(slugify("Tên Truyện"), 1, 2, whole_novel=False)
        expected = path / "exports" / "video" / Path(name).stem / (Path(name).stem + ".srt")
        assert expected.is_file()

    def test_the_written_srt_has_the_chapter_offsets(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project(library_dir, sample_meta, sample_refs, tmp_path)
        self._run(path)
        srt = sorted((path / "exports" / "video").rglob("*.srt"))[0].read_text(encoding="utf-8")
        assert "Câu chương 1" in srt and "Câu chương 2" in srt
        assert srt.count("-->") == 2  # one cue per chapter in the part

    def test_a_project_with_no_audio_for_the_voice_reports_it(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project(library_dir, sample_meta, sample_refs, tmp_path)
        from noveltrans.gui.workers import SubtitleWorker

        worker = SubtitleWorker(path, "GiọngKhác", "batch", batch=2)
        got = {}
        worker.failed.connect(lambda m: got.update(error=m))
        worker.run()
        assert "Không có chương nào" in got.get("error", "")

    def test_chapters_that_already_have_cues_are_not_backfilled(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Backfill runs ffmpeg per chapter; re-running the button must not redo work that
        real synthesis already did — and must never overwrite real cues with recovered ones."""
        path = self._project(library_dir, sample_meta, sample_refs, tmp_path)
        got = self._run(path)
        assert got["backfilled"] == 0

    def test_start_subtitles_builds_a_worker_from_real_config_attributes(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Regression: `_start_subtitles` read `config.tts_gap`, which does not exist —
        the property is `tts_gap_seconds`. It raised AttributeError the moment the button
        was clicked.

        042's tests drove `SubtitleWorker` directly, so the *worker* was covered and the
        code that constructs it never ran. Same shape as 040's bug: testing the piece
        instead of the call.
        """
        from noveltrans.gui import tab_video as tab_module
        from noveltrans.gui.workers import SubtitleWorker

        path = self._project(library_dir, sample_meta, sample_refs, tmp_path)
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))

        tab.config.tts_gap_seconds = 0.55
        tab.config.tts_speed = 1.15
        monkeypatch.setattr(SubtitleWorker, "start", lambda self: None)
        monkeypatch.setattr(tab_module, "track_worker", lambda *_a: None)

        tab._start_subtitles()  # would have raised AttributeError

        worker = tab._subtitle_worker
        assert worker is not None
        assert worker.gap_seconds == 0.55
        assert worker.speed == 1.15
        assert worker.voice == "V"
        assert worker.batch_size == 2
        tab._subtitle_worker = None
        tab.shutdown()

    def test_every_config_attribute_the_tab_reads_exists(self):
        """A structural guard for the whole class of bug: any `self.config.X` in the Video
        tab must be a real AppConfig attribute. Catches a typo at test time rather than on
        the click that needs it."""
        import inspect
        import re

        from noveltrans.config import AppConfig
        from noveltrans.gui import tab_video as tab_module

        used = set(re.findall(r"self\.config\.([a-z_][a-z0-9_]*)", inspect.getsource(tab_module)))
        missing = sorted(n for n in used if not hasattr(AppConfig, n))
        assert not missing, f"tab_video reads AppConfig attributes that don't exist: {missing}"


class TestSubtitleUploadUi:
    """Feature 044 — the "💬 Tải phụ đề lên" button."""

    def _tab(self, tmp_path, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(2)
        tab._on_project_selected(str(path))
        return tab

    def _part(self, tab, index, *, uploaded=True, srt=True):
        from noveltrans.youtube_upload import STATE_PUBLISHED, write_upload_state

        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        if uploaded:
            write_upload_state(out, status=STATE_PUBLISHED, video_id=f"vid{index}xxxxxxx")
        if srt:
            out.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
        return out

    def test_a_part_needs_both_a_video_and_an_srt(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._part(tab, 0, uploaded=True, srt=True)
        self._part(tab, 1, uploaded=True, srt=False)   # no subtitle file
        self._part(tab, 2, uploaded=False, srt=True)   # not on YouTube
        assert [label for _p, _s, label in tab._subtitle_upload_rows()] == ["Phần 1"]
        tab.shutdown()

    def test_nothing_eligible_starts_no_worker(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        shown: list = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._part(tab, 0, uploaded=True, srt=False)
        tab._start_subtitle_upload()
        assert shown and tab._subtitle_upload_worker is None
        tab.shutdown()

    def test_launching_builds_a_worker_and_locks_the_other_browser_runs_out(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """`track_worker` is the real one here — the 033 signature bug, guarded."""
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui import keep_awake
        from noveltrans.gui.workers import SubtitleUploadWorker

        class _DummyManager:
            def acquire(self):
                pass

            def release(self):
                pass

        monkeypatch.setattr(keep_awake, "_manager", _DummyManager())
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        started: list = []
        monkeypatch.setattr(
            SubtitleUploadWorker, "start", lambda self: started.append(self)
        )

        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        for i in range(2):
            self._part(tab, i)
        tab._start_subtitle_upload()

        assert len(started) == 1
        assert [r.label for r in tab._subtitle_upload_worker.requests] == ["Phần 1", "Phần 2"]
        assert not tab.upload_button.isEnabled()
        assert not tab.thumbnail_update_button.isEnabled()
        assert not tab.playlist_sync_button.isEnabled()
        assert not tab.subtitle_upload_button.isEnabled()
        assert tab.upload_cancel_button.isEnabled()
        tab._subtitle_upload_worker = None
        tab.shutdown()

    def test_declining_the_confirm_starts_nothing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui.workers import SubtitleUploadWorker

        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        started: list = []
        monkeypatch.setattr(
            SubtitleUploadWorker, "start", lambda self: started.append(self)
        )
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._part(tab, 0)
        tab._start_subtitle_upload()
        assert started == [] and tab._subtitle_upload_worker is None
        tab.shutdown()

    def test_the_cancel_button_stops_it(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)

        class _Fake:
            def __init__(self):
                self.cancelled = False

            def isRunning(self):
                return True

            def cancel(self):
                self.cancelled = True

        worker = _Fake()
        tab._subtitle_upload_worker = worker
        tab._cancel_upload()
        assert worker.cancelled is True
        tab._subtitle_upload_worker = None
        tab.shutdown()

    def test_the_finish_handler_restores_every_button(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        tab = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        for b in (
            tab.subtitle_upload_button, tab.upload_button, tab.thumbnail_update_button,
            tab.playlist_sync_button, tab.video_button,
        ):
            b.setEnabled(False)
        tab._on_subtitle_upload_finished(2, 0)
        assert tab.subtitle_upload_button.isEnabled()
        assert tab.upload_button.isEnabled()
        assert not tab.upload_cancel_button.isEnabled()
        tab.shutdown()


class TestPauseButtonRouting:
    """Two Dừng buttons in this tab, so two pause buttons — one per section (049).

    A single shared pause button sat in the render row while four of the six jobs live
    in the upload row, and starting an upload rebound it away from a running render.
    """

    def _tab(self, tmp_path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        return tab

    def test_each_section_has_its_own_pause_button(self, qapp, tmp_path):
        tab = self._tab(tmp_path)
        assert tab.pause_button is not tab.upload_pause_button

    def test_both_start_disabled(self, qapp, tmp_path):
        tab = self._tab(tmp_path)
        assert not tab.pause_button.isEnabled()
        assert not tab.upload_pause_button.isEnabled()

    def test_the_upload_button_warns_about_the_browser_session(self, qapp, tmp_path):
        tab = self._tab(tmp_path)
        assert "Chrome" in tab.upload_pause_button.toolTip()
        assert "Chrome" not in tab.pause_button.toolTip()

    def test_an_upload_never_steals_the_render_pause(self, qapp, tmp_path):
        from noveltrans.gui.jobs import job_registry

        job_registry.reset()
        tab = self._tab(tmp_path)
        try:
            render = job_registry.register(_StubWorker(), kind="Tạo video", novel="A")
            tab.pause_button.set_job(render.id)
            upload = job_registry.register(_StubWorker(), kind="Tải video lên", novel="A")
            tab.upload_pause_button.set_job(upload.id)

            # The render must still be pausable from its own row.
            assert tab.pause_button._job_id == render.id
            assert tab.upload_pause_button._job_id == upload.id
        finally:
            job_registry.reset()

    def test_every_registration_targets_the_button_beside_its_own_dung(self):
        # Source-level, because driving all six launches needs a browser and ffmpeg.
        # `_cancel` stops the render worker; `_cancel_upload` stops the four browser ones.
        import inspect
        import re

        from noveltrans.gui import tab_video

        source = inspect.getsource(tab_video)
        upload_kinds = {"Tải video lên", "Tải phụ đề lên", "Danh sách phát", "Đổi ảnh bìa"}
        pairs = re.findall(
            r'kind="([^"]+)", novel=self\._job_novel\(\)\s*\)\s*self\.(\w*pause_button)', source
        )
        assert len(pairs) == 6
        for kind, button in pairs:
            expected = "upload_pause_button" if kind in upload_kinds else "pause_button"
            assert button == expected, f"{kind} binds {button}, expected {expected}"


class _StubWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal()

    def isFinished(self) -> bool:  # noqa: N802 — mirrors QThread's Qt-cased API
        return False

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass


class TestPerNovelVideoSettings:
    """The reported bug: picking `ảnh nền` for one novel put it on the next novel's video.

    The settings split in two (see noveltrans.video_settings) — what a novel *looks like*
    is never inherited, how the *user* likes to work still is — so both halves are pinned
    down here.
    """

    def _project(self, library_dir, meta, refs):
        project = NovelProject.create(library_dir, meta, refs)
        path = project.path
        project.close()
        return path

    def _two_projects(self, library_dir, sample_meta, sample_refs):
        from dataclasses import replace

        first = self._project(library_dir, sample_meta, sample_refs)
        other = replace(sample_meta, url=sample_meta.url + "-two", title="Truyện Hai")
        second = self._project(library_dir, other, sample_refs)
        return first, second

    def test_an_image_picked_for_one_novel_does_not_reach_another(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        first, second = self._two_projects(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))

        tab._on_project_selected(str(first))
        tab._save_video_setting("video_image_path", "/covers/first.png")

        tab._on_project_selected(str(second))
        assert tab._video_settings["video_image_path"] == ""
        assert tab.video_image_edit.text() == ""

        # and going back shows the first novel's image again
        tab._on_project_selected(str(first))
        assert tab._video_settings["video_image_path"] == "/covers/first.png"
        assert tab.video_image_edit.text() == "/covers/first.png"
        tab.shutdown()

    def test_bg_color_is_likewise_not_inherited(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        first, second = self._two_projects(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(first))
        tab._save_video_setting("video_bg_color", "#1e785a")

        tab._on_project_selected(str(second))
        assert tab.bg_color == ""  # the default gradient, not the first novel's colour
        tab.shutdown()

    def test_workflow_choices_still_carry_to_a_new_novel(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The other half of the split: quality is a habit, not a property of a novel."""
        first, second = self._two_projects(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(first))
        tab._save_video_setting("video_quality", "fastest")
        assert tab.config.video_quality == "fastest"  # mirrored as the user's habit

        tab._on_project_selected(str(second))
        assert tab._video_settings["video_quality"] == "fastest"
        tab.shutdown()

    def test_a_novel_that_diverges_keeps_its_own_workflow_choice(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        first, second = self._two_projects(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(first))
        tab._save_video_setting("video_quality", "fast")
        tab._on_project_selected(str(second))
        tab._save_video_setting("video_quality", "high")

        tab._on_project_selected(str(first))
        assert tab._video_settings["video_quality"] == "fast"  # not the newer choice
        tab.shutdown()

    def test_settings_survive_reopening_the_app(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(path))
        tab._save_video_setting("video_image_path", "/covers/a.png")
        tab._save_video_setting("video_tagline", "một dòng")
        tab.shutdown()

        fresh = VideoTab(_config(tmp_path))
        fresh._on_project_selected(str(path))
        assert fresh._video_settings["video_image_path"] == "/covers/a.png"
        assert fresh.tagline_edit.text() == "một dòng"
        fresh.shutdown()

    def test_an_existing_novel_adopts_todays_globals_once(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Migration: a novel set up before settings went per-novel must keep rendering
        the same, so on first open it takes a snapshot of the current globals."""
        path = self._project(library_dir, sample_meta, sample_refs)
        config = _config(tmp_path)
        config.video_bg_color = "#123456"
        config.video_tagline = "cũ"

        tab = VideoTab(config)
        tab._on_project_selected(str(path))
        assert tab.bg_color == "#123456"  # unchanged output, now owned by this novel

        stored = NovelProject.open(str(path))
        assert stored.meta.video_settings["video_bg_color"] == "#123456"
        assert stored.meta.video_settings["video_tagline"] == "cũ"
        stored.close()
        tab.shutdown()

    def test_a_novels_own_image_survives_the_migration(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The pre-existing `video_image_path` field wins over the adopted globals — a
        novel that already had its own image must not take the global one instead."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_video_image_path("/covers/mine.png")
        path = project.path
        project.close()

        config = _config(tmp_path)
        config.video_image_path = "/covers/someone-elses.png"
        tab = VideoTab(config)
        tab._on_project_selected(str(path))
        assert tab._video_settings["video_image_path"] == "/covers/mine.png"
        tab.shutdown()

    def test_nothing_is_saved_onto_a_novel_while_settings_are_loading(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Applying novel B's values fires the widgets' change handlers. If those wrote
        through, switching novels would rewrite whichever novel was open."""
        first, second = self._two_projects(library_dir, sample_meta, sample_refs)
        tab = VideoTab(_config(tmp_path))
        tab._on_project_selected(str(first))
        tab._save_video_setting("video_tagline", "của truyện một")

        tab._on_project_selected(str(second))  # applying B must not touch A
        stored = NovelProject.open(str(first))
        assert stored.meta.video_settings["video_tagline"] == "của truyện một"
        stored.close()
        tab.shutdown()


class TestDescriptionCapWarning:
    """Feature 065 — the parts table flags a part whose chapter index won't fit."""

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path, batch=5):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(batch)
        tab._on_project_selected(str(path))
        return tab

    def _shrink(self, monkeypatch, limit=120):
        """The cap is resolved at call time precisely so this works — see tts/description."""
        monkeypatch.setattr(
            "noveltrans.tts.description.YOUTUBE_DESCRIPTION_CHAR_LIMIT", limit
        )

    def test_chapter_cell_warns_when_the_description_would_be_truncated(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._shrink(monkeypatch)
        tab._refresh_video_list()
        assert "⚠️" in tab.video_list.item(0, 1).text()
        assert "mục lục" in tab.video_list.item(0, 1).toolTip()
        tab.shutdown()

    def test_no_truncation_warning_at_the_real_limit(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        # The regression guard: a normal 5-chapter part must not sprout a warning
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        assert "⚠️" not in tab.video_list.item(0, 1).text()
        tab.shutdown()

    def test_computed_part_description_is_capped(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.tts.description import description_length

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._shrink(monkeypatch)
        window = tab._windows_for_current_selection()[0]
        assert description_length(tab._compute_part_description(window, "Tựa")) <= 120
        tab.shutdown()

    def test_description_label_shows_the_character_count(self, qapp, tmp_path):
        tab = VideoTab(_config(tmp_path))
        assert "/5000 ký tự" in tab._description_label_text("abc\n")
        tab.shutdown()

    def test_description_label_warns_when_truncated(self, qapp, tmp_path):
        from noveltrans.tts.description import truncation_line

        tab = VideoTab(_config(tmp_path))
        text = "Mục lục chương:\n0:00 A\n" + truncation_line(9, 5000) + "\n"
        assert "⚠️" in tab._description_label_text(text)
        tab.shutdown()


class TestShortenByAI:
    """Feature 065 — the "Shorten by AI" button's orchestration in "Chi tiết phần"."""

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(5)
        tab._on_project_selected(str(path))
        return tab

    def _render_part(self, tab, index=0):
        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        return window, out

    def _stub_worker(self, monkeypatch, titles=None, fell_back=0):
        """Replace ShortenTitlesWorker with one that emits finished_ok on start()."""
        from PySide6.QtCore import QObject, Signal

        class _Stub(QObject):
            progress = Signal(int, int)
            finished_ok = Signal(list, int)
            failed = Signal(str)

            def __init__(self, given, engine_name, **kw):
                super().__init__()
                self.given = given

            def isRunning(self):
                return False

            def start(self):
                out = titles if titles is not None else [f"ngắn {t}" for t in self.given]
                self.finished_ok.emit(out, fell_back)

        monkeypatch.setattr("noveltrans.gui.tab_video.ShortenTitlesWorker", _Stub)
        monkeypatch.setattr("noveltrans.gui.tab_video.track_worker", lambda w: None)

    def _widgets(self):
        from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton

        return QPlainTextEdit(), QPushButton(), QLabel()

    def test_shorten_replaces_the_description_with_the_short_form(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._stub_worker(monkeypatch)
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False, False)

        text = edit.toPlainText()
        assert "Tên truyện" not in text
        assert "Tác giả" not in text
        assert "Tạo bởi" not in text
        assert "Mục lục chương:" in text
        assert "0:00 C.1 " in text
        tab.shutdown()

    def test_extras_come_back_when_there_is_room(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        """Five short chapters leave thousands of characters spare, so the header and the
        credit line cost nothing and go back on."""
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._stub_worker(monkeypatch)
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False, True)

        text = edit.toPlainText()
        assert text.startswith("Tên truyện: ")
        assert "Tác giả: " in text
        assert text.rstrip().endswith("Tạo bởi: Fox Novel")
        assert "0:00 C.1 " in text  # ...and the shortened index is still the point
        assert "Còn chỗ nên giữ lại" in status.text()
        tab.shutdown()

    def test_extras_are_skipped_when_they_would_cost_a_chapter(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        """The index wins: shortening exists to fit more chapters, so the header is never
        bought at the price of one."""
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._stub_worker(monkeypatch)
        monkeypatch.setattr(
            "noveltrans.tts.description.YOUTUBE_DESCRIPTION_CHAR_LIMIT", 150
        )
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False, True)

        text = edit.toPlainText()
        assert "Tên truyện" not in text
        assert "Mục lục chương:" in text
        assert "Không đủ chỗ" in status.text()
        tab.shutdown()

    def test_the_chapter_number_never_comes_from_the_model(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        """The model is only ever sent the descriptive half, and `C.N` is reassembled
        locally — a renumbered index would be a far worse bug than a long description."""
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        # a model that returns garbage numbering must not move any chapter number
        self._stub_worker(monkeypatch, titles=[f"999. rác {i}" for i in range(5)])
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False)

        lines = [ln for ln in edit.toPlainText().splitlines() if ln.startswith("0:")]
        assert lines and lines[0].split()[1] == "C.1"
        tab.shutdown()

    def test_shorten_writes_the_sidecar_when_the_part_is_rendered(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        window, _out = self._render_part(tab)
        self._stub_worker(monkeypatch)
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False)

        sidecar = tab._part_sidecar(window, False, ".txt")
        assert sidecar.is_file()
        assert "0:00 C.1 " in sidecar.read_text(encoding="utf-8")
        assert "Đã lưu" in status.text()
        tab.shutdown()

    def test_shorten_does_not_write_a_sidecar_for_an_unrendered_part(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._stub_worker(monkeypatch)
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False)

        assert not tab._part_sidecar(window, False, ".txt").is_file()
        assert "chưa lưu" in status.text().lower()
        tab.shutdown()

    def test_shorten_reports_partial_fallbacks_in_the_status(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._stub_worker(monkeypatch, fell_back=2)
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        tab._shorten_description(window, edit, button, status, False)
        assert "2 nhóm giữ nguyên" in status.text()
        tab.shutdown()

    def test_a_mismatched_reply_leaves_the_description_alone(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        from PySide6.QtWidgets import QMessageBox

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._stub_worker(monkeypatch, titles=["chỉ một"])  # 1 title for 5 chapters
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        window = tab._windows_for_current_selection()[0]
        edit, button, status = self._widgets()
        edit.setPlainText("giữ nguyên")
        tab._shorten_description(window, edit, button, status, False)
        assert edit.toPlainText() == "giữ nguyên"
        tab.shutdown()


class TestDescriptionResync:
    """Feature 065 — an already-rendered part's `.txt` follows a chapter rename."""

    def _project_with_audio(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(5):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        path = project.path
        project.close()
        return path

    def _tab_on_project(self, tmp_path, path):
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab.video_mode.setCurrentIndex(tab.video_mode.findData("batch"))
        tab.video_batch_size.setValue(5)
        tab._on_project_selected(str(path))
        return tab

    def _render_with_description(self, tab, index=0, text=None):
        """Fake a rendered part plus its description sidecar, as a real render would."""
        window = tab._windows_for_current_selection()[index]
        out = tab._part_output_path(window, whole_novel=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        sidecar = tab._part_sidecar(window, False, ".txt")
        if text is None:
            text = tab._compute_part_description(window, tab.project.meta.display_name())
        sidecar.write_text(text, encoding="utf-8")
        return window, sidecar

    def _rename(self, path, idx, title):
        project = NovelProject.open(path)
        project.edit_title(idx, title)
        project.close()

    def test_renaming_a_chapter_rewrites_the_sidecar(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        _window, sidecar = self._render_with_description(tab)
        self._rename(path, 0, "Chương 1: Tên hoàn toàn mới")

        tab._on_project_selected(str(path))  # what returning to the tab does
        assert "Tên hoàn toàn mới" in sidecar.read_text(encoding="utf-8")
        tab.shutdown()

    def test_editing_the_translated_title_rewrites_the_sidecar(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        # chapter_marker_title reads `translated_title or title`, so this counts too
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        _window, sidecar = self._render_with_description(tab)
        project = NovelProject.open(path)
        project.edit_translation(0, title="Bản dịch mới")
        project.close()

        tab._on_project_selected(str(path))
        assert "Bản dịch mới" in sidecar.read_text(encoding="utf-8")
        tab.shutdown()

    def test_a_fresh_sidecar_is_not_rewritten(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._render_with_description(tab)
        assert tab._resync_description_sidecars() == (0, 0)
        tab.shutdown()

    def test_an_unrendered_part_has_nothing_to_resync(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._rename(path, 0, "Chương 1: Tên mới")
        tab._on_project_selected(str(path))
        assert tab._resync_description_sidecars() == (0, 0)
        tab.shutdown()

    def test_a_legacy_simple_description_is_upgraded(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.tts.video import build_youtube_description

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        window = tab._windows_for_current_selection()[0]
        legacy = build_youtube_description(
            tab._part_segments(window), tab.project.meta.display_name()
        )
        _w, sidecar = self._render_with_description(tab, text=legacy)

        rewritten, customised = tab._resync_description_sidecars()
        assert (rewritten, customised) == (1, 0)
        assert "Tạo bởi:" in sidecar.read_text(encoding="utf-8")
        tab.shutdown()

    def test_an_ai_shortened_sidecar_is_never_overwritten(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The data-loss guard: shortened titles cannot be rebuilt from the database, so
        overwriting one is not a regeneration."""
        from noveltrans.tts.description import build_short_description

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        short, _d, _e = build_short_description(
            [(f"{i}:00", f"C.{i}", f"ngắn {i}") for i in range(1, 6)], total_chapters=5
        )
        _w, sidecar = self._render_with_description(tab, text=short)
        self._rename(path, 0, "Chương 1: Tên mới")

        tab._on_project_selected(str(path))
        assert sidecar.read_text(encoding="utf-8") == short
        tab.shutdown()

    def test_an_ai_shortened_stale_sidecar_is_flagged(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.tts.description import build_short_description

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        short, _d, _e = build_short_description(
            [(f"{i}:00", f"C.{i}", f"ngắn {i}") for i in range(1, 6)], total_chapters=5
        )
        window, _sidecar = self._render_with_description(tab, text=short)
        self._rename(path, 0, "Chương 1: Tên mới")

        tab._on_project_selected(str(path))
        assert tab._part_dir_name(window) in tab._stale_descriptions
        assert "⚠️" in tab.video_list.item(0, 1).text()
        assert "rút gọn bằng AI" in tab.video_list.item(0, 1).toolTip()
        tab.shutdown()

    def test_a_part_that_lost_chapters_is_flagged_not_rewritten(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """Deleting a chapter leaves the .mp4's audio unchanged, so rebuilding the index
        around fewer chapters would produce timestamps that point nowhere."""
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        window, sidecar = self._render_with_description(tab)
        before = sidecar.read_text(encoding="utf-8")
        project = NovelProject.open(path)
        project.delete_chapter(4)  # the 5th chapter is gone; the .mp4 still narrates it
        project.close()

        tab._on_project_selected(str(path))
        assert tab._part_dir_name(window) in tab._stale_descriptions
        assert sidecar.read_text(encoding="utf-8") == before
        tab.shutdown()

    def test_resync_respects_the_char_limit(
        self, qapp, tmp_path, monkeypatch, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.tts.description import description_length

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        _w, sidecar = self._render_with_description(tab)
        self._rename(path, 0, "Chương 1: " + "ả" * 300)
        monkeypatch.setattr(
            "noveltrans.tts.description.YOUTUBE_DESCRIPTION_CHAR_LIMIT", 400
        )
        tab._on_project_selected(str(path))
        assert description_length(sidecar.read_text(encoding="utf-8")) <= 400
        tab.shutdown()

    def test_restore_button_overwrites_a_customised_sidecar(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton
        from noveltrans.tts.description import build_short_description

        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        short, _d, _e = build_short_description(
            [(f"{i}:00", f"C.{i}", f"ngắn {i}") for i in range(1, 6)], total_chapters=5
        )
        window, sidecar = self._render_with_description(tab, text=short)
        self._rename(path, 0, "Chương 1: Tên mới")
        tab._on_project_selected(str(path))
        assert tab._part_dir_name(window) in tab._stale_descriptions

        window = tab._windows_for_current_selection()[0]
        edit, button, status = QPlainTextEdit(), QPushButton(), QLabel()
        tab._restore_generated_description(window, edit, button, status, False)
        text = sidecar.read_text(encoding="utf-8")
        assert "Tạo bởi:" in text and "Tên mới" in text
        assert tab._part_dir_name(window) not in tab._stale_descriptions
        tab.shutdown()

    def test_upload_request_picks_up_the_resynced_description(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        """The bug that actually matters: without the resync, the OLD chapter name is what
        gets typed into Studio."""
        path = self._project_with_audio(library_dir, sample_meta, sample_refs)
        tab = self._tab_on_project(tmp_path, path)
        self._render_with_description(tab)
        self._rename(path, 0, "Chương 1: Tên hoàn toàn mới")

        tab._on_project_selected(str(path))
        window = tab._windows_for_current_selection()[0]
        request = tab._upload_request(window, "Phần 1", 1, False, publish_at=None)
        assert "Tên hoàn toàn mới" in request.description
        tab.shutdown()

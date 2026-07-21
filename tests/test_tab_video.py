"""Feature 025 — the split-out VideoTab + its registration as "5. Video" (offscreen Qt)."""

from __future__ import annotations

from PySide6.QtCore import QSettings

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

    def test_bg_color_loads_from_config_and_resets(self, qapp, tmp_path):
        config = _config(tmp_path)
        config.video_bg_color = "#1e785a"
        tab = VideoTab(config)
        assert tab.bg_color == "#1e785a"
        assert "#1e785a" in tab.bg_color_button.styleSheet()
        tab._reset_bg_color()
        assert tab.bg_color == ""
        assert config.video_bg_color == ""
        assert tab.bg_color_button.text() == "Chọn màu…"
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

    def test_thumbnail_font_loads_from_config_and_persists(self, qapp, tmp_path):
        config = _config(tmp_path)
        config.video_thumbnail_font = "be_vietnam"
        tab = VideoTab(config)
        assert tab.thumb_font.currentData() == "be_vietnam"
        tab.thumb_font.setCurrentIndex(tab.thumb_font.findData("montserrat"))
        assert config.video_thumbnail_font == "montserrat"
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

        return tab.video_list.cellWidget(row, 5).findChildren(QPushButton)[0]

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
        thumb_btn = tab.video_list.cellWidget(0, 5).findChildren(QPushButton)[2]
        assert thumb_btn.text() == "Ảnh bìa"
        assert not thumb_btn.isEnabled()  # no thumbnail yet
        windows = tab._windows_for_current_selection()
        jpg = tab._part_sidecar(windows[0], False, ".jpg")
        jpg.parent.mkdir(parents=True, exist_ok=True)
        jpg.write_bytes(b"fake jpg")
        tab._refresh_video_list()
        thumb_btn = tab.video_list.cellWidget(0, 5).findChildren(QPushButton)[2]
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
        }
        tab.shutdown()


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

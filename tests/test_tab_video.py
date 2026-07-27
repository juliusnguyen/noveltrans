"""Feature 025 — the split-out VideoTab + its registration as "5. Video" (offscreen Qt)."""

from __future__ import annotations

from PySide6.QtCore import QDate, QDateTime, QSettings, QTime

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
        }
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
        tab.upload_playlist.setText("Danh sách của tôi")

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

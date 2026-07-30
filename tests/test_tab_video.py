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
        tab.config.video_thumbnail_title_scale = 1.5
        tab.config.video_thumbnail_part_scale = 0.8
        tab.config.video_thumbnail_tagline_scale = 1.2

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
        tab.config.video_thumbnail_title_scale = 1.5
        tab.config.video_thumbnail_part_scale = 0.8
        tab.config.video_thumbnail_tagline_scale = 1.2
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
        tab.config.video_thumbnail_title_align = "right"

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

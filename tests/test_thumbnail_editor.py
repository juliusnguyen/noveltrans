"""The live cover editor dialog + the config round-trip for its saved settings."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import AppConfig
from noveltrans.gui.thumbnail_editor import ThumbnailEditorDialog


def _config(tmp_path):
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    return config


class TestThumbnailPositionConfig:
    def test_defaults_match_the_renderer(self, tmp_path):
        from noveltrans.tts.thumbnail import DEFAULT_PART_POS, DEFAULT_TITLE_POS

        config = _config(tmp_path)
        assert config.video_thumbnail_title_pos == DEFAULT_TITLE_POS
        assert config.video_thumbnail_part_pos == DEFAULT_PART_POS

    def test_round_trips_and_clamps(self, tmp_path):
        config = _config(tmp_path)
        config.video_thumbnail_title_pos = (0.25, 0.4)
        config.video_thumbnail_part_pos = (1.8, -0.3)  # out of range → clamped to [0, 1]
        assert config.video_thumbnail_title_pos == (0.25, 0.4)
        assert config.video_thumbnail_part_pos == (1.0, 0.0)


class TestThumbnailEditorDialog:
    def _dialog(self, tmp_path, **kw):
        config = _config(tmp_path)
        return ThumbnailEditorDialog(
            config, base_image="", novel_title="Tụ Bảo Tiên Bồn", part_num=1, **kw
        )

    def test_constructs_and_renders_a_preview(self, qapp, tmp_path):
        dlg = self._dialog(tmp_path)
        assert dlg.preview.pixmap() is not None
        assert not dlg.preview.pixmap().isNull()

    def test_dragging_moves_the_active_block(self, qapp, tmp_path):
        dlg = self._dialog(tmp_path)
        dlg.pick_title.setChecked(True)  # title is active
        dlg._on_preview_dragged(0.42, 0.30)
        assert dlg.title_pos == [0.42, 0.30]
        dlg.pick_part.setChecked(True)  # now the PHẦN block is active
        dlg._on_preview_dragged(0.6, 0.8)
        assert dlg.part_pos == [0.6, 0.8]
        # the title stayed where it was
        assert dlg.title_pos == [0.42, 0.30]

    def test_sliders_reflect_and_update_the_active_block(self, qapp, tmp_path):
        dlg = self._dialog(tmp_path)
        dlg.pick_part.setChecked(True)
        dlg.slider_x.setValue(750)
        dlg.slider_y.setValue(250)
        assert dlg.part_pos == [0.75, 0.25]

    def test_save_persists_positions_and_font(self, qapp, tmp_path):
        dlg = self._dialog(tmp_path)
        dlg.title_pos = [0.1, 0.2]
        dlg.part_pos = [0.5, 0.9]
        dlg.font_key = "nunito"
        dlg._save()
        assert dlg.config.video_thumbnail_title_pos == (0.1, 0.2)
        assert dlg.config.video_thumbnail_part_pos == (0.5, 0.9)
        assert dlg.config.video_thumbnail_font == "nunito"

    def test_save_and_apply_calls_the_callback(self, qapp, tmp_path):
        called = []
        dlg = self._dialog(tmp_path, on_apply_all=lambda: called.append(True))
        dlg._save_and_apply()
        assert called == [True]
        # config was written before the callback ran
        assert dlg.config.video_thumbnail_font == dlg.font_key

    def test_reset_restores_the_default_positions(self, qapp, tmp_path):
        from noveltrans.tts.thumbnail import DEFAULT_PART_POS, DEFAULT_TITLE_POS

        dlg = self._dialog(tmp_path)
        dlg.title_pos = [0.9, 0.9]
        dlg.part_pos = [0.1, 0.1]
        dlg._reset_positions()
        assert tuple(dlg.title_pos) == DEFAULT_TITLE_POS
        assert tuple(dlg.part_pos) == DEFAULT_PART_POS

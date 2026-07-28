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


class TestThumbnailScaleConfig:
    """Feature 035 — the three text-size multipliers."""

    def test_defaults_are_the_original_layout(self, tmp_path):
        from noveltrans.tts.thumbnail import DEFAULT_TEXT_SCALE

        config = _config(tmp_path)
        assert config.video_thumbnail_title_scale == DEFAULT_TEXT_SCALE
        assert config.video_thumbnail_part_scale == DEFAULT_TEXT_SCALE
        assert config.video_thumbnail_tagline_scale == DEFAULT_TEXT_SCALE

    def test_round_trips_and_clamps(self, tmp_path):
        from noveltrans.tts.thumbnail import MAX_TEXT_SCALE, MIN_TEXT_SCALE

        config = _config(tmp_path)
        config.video_thumbnail_title_scale = 1.4
        config.video_thumbnail_part_scale = 99.0  # out of range → clamped
        config.video_thumbnail_tagline_scale = 0.01
        assert config.video_thumbnail_title_scale == 1.4
        assert config.video_thumbnail_part_scale == MAX_TEXT_SCALE
        assert config.video_thumbnail_tagline_scale == MIN_TEXT_SCALE

    def test_a_hand_edited_settings_file_is_clamped_on_read(self, tmp_path):
        """Clamped on read too, so a value written by hand — or by a future build with
        wider bounds — can't produce an unreadable cover."""
        from noveltrans.tts.thumbnail import MAX_TEXT_SCALE

        config = _config(tmp_path)
        config._s.setValue("video_thumbnail_title_scale", 12.0)
        assert config.video_thumbnail_title_scale == MAX_TEXT_SCALE


class TestThumbnailEditorSizeSliders:
    def _dialog(self, tmp_path, **kw):
        config = _config(tmp_path)
        return ThumbnailEditorDialog(
            config, base_image="", novel_title="Tụ Bảo Tiên Bồn", part_num=1, **kw
        )

    def test_sliders_seed_from_config_and_show_percent(self, qapp, tmp_path):
        dialog = self._dialog(tmp_path)
        dialog.config.video_thumbnail_title_scale = 1.5
        dialog = ThumbnailEditorDialog(
            dialog.config, base_image="", novel_title="X", part_num=1
        )
        assert dialog.size_title.value() == 150
        assert dialog.size_title_label.text() == "150%"

    def test_slider_range_matches_the_renderer_clamp(self, qapp, tmp_path):
        """What you drag to is what gets rendered — the slider can't ask for a size
        `compose_thumbnail` would refuse."""
        from noveltrans.tts.thumbnail import MAX_TEXT_SCALE, MIN_TEXT_SCALE

        dialog = self._dialog(tmp_path)
        assert dialog.size_title.minimum() == round(MIN_TEXT_SCALE * 100)
        assert dialog.size_title.maximum() == round(MAX_TEXT_SCALE * 100)

    def test_moving_a_size_slider_updates_state_and_readout(self, qapp, tmp_path):
        dialog = self._dialog(tmp_path)
        dialog.size_part.setValue(180)
        assert dialog.part_scale == 1.8
        assert dialog.size_part_label.text() == "180%"

    def test_save_persists_all_three_scales(self, qapp, tmp_path):
        dialog = self._dialog(tmp_path)
        dialog.size_title.setValue(140)
        dialog.size_part.setValue(60)
        dialog.size_tagline.setValue(120)
        dialog._save_to_config()
        assert dialog.config.video_thumbnail_title_scale == 1.4
        assert dialog.config.video_thumbnail_part_scale == 0.6
        assert dialog.config.video_thumbnail_tagline_scale == 1.2

    def test_reset_restores_sizes_as_well_as_positions(self, qapp, tmp_path):
        """A "đặt lại" that left the text at twice its size would read as broken."""
        from noveltrans.tts.thumbnail import DEFAULT_TEXT_SCALE, DEFAULT_TITLE_POS

        dialog = self._dialog(tmp_path)
        dialog.size_title.setValue(200)
        dialog.title_pos = [0.9, 0.9]
        dialog._reset_positions()
        assert dialog.title_scale == DEFAULT_TEXT_SCALE
        assert dialog.size_title.value() == round(DEFAULT_TEXT_SCALE * 100)
        assert tuple(dialog.title_pos) == DEFAULT_TITLE_POS

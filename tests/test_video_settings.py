"""Tests for the identity/workflow split that keeps one novel's look off another's video.

The GUI wiring is covered in test_tab_video.py; this pins the resolution rules themselves,
which are pure and need no Qt.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans import video_settings
from noveltrans.config import AppConfig


def _config(tmp_path):
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    return config


class TestKeySplit:
    def test_the_two_kinds_do_not_overlap(self):
        assert not set(video_settings.IDENTITY_KEYS) & set(video_settings.WORKFLOW_KEYS)

    def test_every_key_is_a_real_config_property(self):
        # A typo here would silently resolve to a default forever, so check the names
        # against AppConfig itself rather than a second hand-written list.
        for key in video_settings.VIDEO_SETTING_KEYS:
            assert isinstance(getattr(AppConfig, key, None), property), key

    def test_identity_defaults_cover_every_identity_key(self):
        assert set(video_settings.identity_defaults()) == set(video_settings.IDENTITY_KEYS)


class TestEffective:
    def test_identity_ignores_the_global_value(self, tmp_path):
        """The bug in one line: a novel with no image of its own must not get the last
        one picked for some other novel."""
        config = _config(tmp_path)
        config.video_image_path = "/covers/someone-elses.png"
        config.video_bg_color = "#1e785a"

        resolved = video_settings.effective({}, config)
        assert resolved["video_image_path"] == ""
        assert resolved["video_bg_color"] == ""

    def test_identity_uses_the_novels_own_value_when_it_has_one(self, tmp_path):
        config = _config(tmp_path)
        config.video_image_path = "/covers/someone-elses.png"
        resolved = video_settings.effective({"video_image_path": "/covers/mine.png"}, config)
        assert resolved["video_image_path"] == "/covers/mine.png"

    def test_workflow_inherits_the_users_last_used_value(self, tmp_path):
        config = _config(tmp_path)
        config.video_quality = "fastest"
        assert video_settings.effective({}, config)["video_quality"] == "fastest"

    def test_a_novels_workflow_choice_overrides_the_inherited_one(self, tmp_path):
        config = _config(tmp_path)
        config.video_quality = "fastest"
        resolved = video_settings.effective({"video_quality": "high"}, config)
        assert resolved["video_quality"] == "high"

    def test_resolves_every_key_so_callers_can_index_it(self, tmp_path):
        resolved = video_settings.effective(None, _config(tmp_path))
        assert set(resolved) == set(video_settings.VIDEO_SETTING_KEYS)

    def test_an_empty_string_is_a_real_choice_not_a_missing_one(self, tmp_path):
        """Resetting the colour to "default gradient" stores "". If that read as unset,
        the reset would be undone by the fallback on the next open."""
        config = _config(tmp_path)
        config.video_bg_color = "#1e785a"
        resolved = video_settings.effective({"video_bg_color": ""}, config)
        assert resolved["video_bg_color"] == ""

    def test_pair_settings_come_back_as_tuples_after_a_json_round_trip(self, tmp_path):
        # meta.json turns tuples into lists; the renderer compares against tuple defaults.
        resolved = video_settings.effective(
            {"video_thumbnail_title_pos": [0.25, 0.75]}, _config(tmp_path)
        )
        assert resolved["video_thumbnail_title_pos"] == (0.25, 0.75)


class TestSnapshot:
    def test_captures_every_key(self, tmp_path):
        assert set(video_settings.snapshot(_config(tmp_path))) == set(
            video_settings.VIDEO_SETTING_KEYS
        )

    def test_takes_the_current_global_values(self, tmp_path):
        """Migration: an existing novel adopts today's globals so its output is unchanged."""
        config = _config(tmp_path)
        config.video_bg_color = "#123456"
        config.video_quality = "fast"
        captured = video_settings.snapshot(config)
        assert captured["video_bg_color"] == "#123456"
        assert captured["video_quality"] == "fast"

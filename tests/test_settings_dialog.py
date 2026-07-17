"""SettingsDialog — the pre-TTS clean-text checkbox round-trips through config.

Uses an isolated QSettings (tmp .ini) so the test never touches the real user store,
following the pattern in test_main_window.py.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import AppConfig
from noveltrans.gui.settings_dialog import SettingsDialog


def _isolated_config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return config


def test_clean_checkbox_loads_the_saved_value(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    config.tts_clean_text = False
    dialog = SettingsDialog(config)
    assert dialog.tts_clean_check.isChecked() is False


def test_clean_checkbox_defaults_to_on(qapp, tmp_path):
    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert dialog.tts_clean_check.isChecked() is True  # DEFAULT_TTS_CLEAN_TEXT


def test_unticking_and_accepting_persists_off(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    dialog = SettingsDialog(config)
    dialog.tts_clean_check.setChecked(False)
    dialog.accept()
    assert config.tts_clean_text is False
    # a freshly opened dialog reflects the saved value
    assert SettingsDialog(config).tts_clean_check.isChecked() is False


def test_extra_remove_field_round_trips(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    dialog = SettingsDialog(config)
    dialog.tts_extra_remove_edit.setText("()“”")
    dialog.accept()
    assert config.tts_clean_extra_remove == "()“”"
    assert SettingsDialog(config).tts_extra_remove_edit.text() == "()“”"


def test_extra_remove_field_disabled_when_cleaning_is_off(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    config.tts_clean_text = False
    dialog = SettingsDialog(config)
    assert dialog.tts_extra_remove_edit.isEnabled() is False
    # re-enables live when the checkbox is ticked
    dialog.tts_clean_check.setChecked(True)
    assert dialog.tts_extra_remove_edit.isEnabled() is True

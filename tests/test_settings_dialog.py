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


def test_tts_adjust_controls_load_and_save(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    config.tts_gap_seconds, config.tts_speed = 0.7, 1.2
    config.tts_volume, config.tts_temperature = 1.5, 0.6
    dialog = SettingsDialog(config)
    assert dialog.tts_gap_spin.value() == 0.7
    assert dialog.tts_speed_spin.value() == 1.2
    assert dialog.tts_volume_spin.value() == 1.5
    assert dialog.tts_temperature_spin.value() == 0.6

    dialog.tts_gap_spin.setValue(0.2)
    dialog.tts_temperature_spin.setValue(0.0)  # "Mặc định"
    dialog.accept()
    assert config.tts_gap_seconds == 0.2
    assert config.tts_temperature == 0.0


def test_temperature_zero_shows_as_default(qapp, tmp_path):
    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert dialog.tts_temperature_spin.specialValueText() == "Mặc định"
    assert dialog.tts_temperature_spin.minimum() == 0.0  # sentinel maps to the minimum


def test_speed_control_disabled_without_ffmpeg(qapp, tmp_path, monkeypatch):
    # WAV needs no ffmpeg, but atempo does — so the speed control gates on it.
    monkeypatch.setattr("noveltrans.gui.settings_dialog.ffmpeg_available", lambda: False)
    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert dialog.tts_speed_spin.isEnabled() is False
    assert "ffmpeg" in dialog.tts_speed_spin.toolTip()


def test_precision_dropdown_loads_and_saves(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    config.tts_precision = "fp32"
    dialog = SettingsDialog(config)
    assert dialog.tts_precision_combo.currentData() == "fp32"

    dialog.tts_precision_combo.setCurrentIndex(dialog.tts_precision_combo.findData("int8"))
    dialog.accept()
    assert config.tts_precision == "int8"
    assert SettingsDialog(config).tts_precision_combo.currentData() == "int8"


class TestLibraryDirHistory:
    """Feature 045 — remember previously-used library folders so they can be switched to."""

    def _config(self, tmp_path):
        from PySide6.QtCore import QSettings

        from noveltrans.config import AppConfig

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        return config

    def test_the_current_library_is_always_in_the_history(self, tmp_path):
        """Even on a fresh install with nothing recorded — otherwise the dropdown would
        open empty while a library is plainly in use."""
        config = self._config(tmp_path)
        assert str(config.library_dir) in config.library_dir_history

    def test_setting_a_library_records_it(self, tmp_path):
        config = self._config(tmp_path)
        config.library_dir = tmp_path / "A"
        config.library_dir = tmp_path / "B"
        assert config.library_dir_history[:2] == [str(tmp_path / "B"), str(tmp_path / "A")]

    def test_reusing_a_library_moves_it_to_the_front_without_duplicating(self, tmp_path):
        config = self._config(tmp_path)
        for name in ("A", "B", "A"):
            config.library_dir = tmp_path / name
        history = config.library_dir_history
        assert history[0] == str(tmp_path / "A")
        assert history.count(str(tmp_path / "A")) == 1

    def test_the_history_is_capped(self, tmp_path):
        from noveltrans.config import MAX_LIBRARY_HISTORY

        config = self._config(tmp_path)
        for i in range(MAX_LIBRARY_HISTORY + 5):
            config.library_dir = tmp_path / f"lib{i}"
        assert len(config.library_dir_history) == MAX_LIBRARY_HISTORY
        assert config.library_dir_history[0] == str(tmp_path / f"lib{MAX_LIBRARY_HISTORY + 4}")

    def test_an_empty_value_neither_blanks_the_library_nor_enters_the_history(self, tmp_path):
        """An empty box on save must not strand the user with no library at all."""
        config = self._config(tmp_path)
        config.library_dir = tmp_path / "A"
        config.library_dir = "   "
        assert config.library_dir == tmp_path / "A"
        assert "" not in config.library_dir_history

    def test_a_settings_file_holding_one_entry_still_reads_as_a_list(self, tmp_path):
        """QSettings collapses a one-item list to a bare string — read back as a string it
        would iterate into single characters."""
        config = self._config(tmp_path)
        config._s.setValue("library_dir_history", "/only/one")
        assert "/only/one" in config.library_dir_history
        assert "/" not in config.library_dir_history  # not exploded into characters

    def test_forgetting_removes_an_entry_but_never_the_active_one(self, tmp_path):
        config = self._config(tmp_path)
        config.library_dir = tmp_path / "A"
        config.library_dir = tmp_path / "B"
        config.forget_library_dir(str(tmp_path / "A"))
        assert str(tmp_path / "A") not in config.library_dir_history
        config.forget_library_dir(str(tmp_path / "B"))  # the active one
        assert str(tmp_path / "B") in config.library_dir_history


class TestLibraryDirDropdown:
    def _dialog(self, qapp, tmp_path):
        from PySide6.QtCore import QSettings

        from noveltrans.config import AppConfig
        from noveltrans.gui.settings_dialog import SettingsDialog

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        config.library_dir = tmp_path / "A"
        config.library_dir = tmp_path / "B"
        return SettingsDialog(config), config

    def test_it_offers_the_history_with_the_current_one_selected(self, qapp, tmp_path):
        dialog, config = self._dialog(qapp, tmp_path)
        items = [dialog.library_edit.itemText(i) for i in range(dialog.library_edit.count())]
        assert items[:2] == [str(tmp_path / "B"), str(tmp_path / "A")]
        assert dialog.library_edit.currentText() == str(config.library_dir)

    def test_a_path_not_in_the_list_can_still_be_typed(self, qapp, tmp_path):
        """The list is an aid, not a gate — the same call made for the playlist picker."""
        dialog, config = self._dialog(qapp, tmp_path)
        assert dialog.library_edit.isEditable()
        dialog.library_edit.setCurrentText(str(tmp_path / "brand-new"))
        dialog.accept()
        assert config.library_dir == tmp_path / "brand-new"
        assert str(tmp_path / "brand-new") == config.library_dir_history[0]

    def test_choosing_an_older_entry_switches_to_it(self, qapp, tmp_path):
        dialog, config = self._dialog(qapp, tmp_path)
        dialog.library_edit.setCurrentText(str(tmp_path / "A"))
        dialog.accept()
        assert config.library_dir == tmp_path / "A"


class TestSiteCookies:
    """Feature 046 — a second cookie-gated site, and the special case it deleted.

    Both workers used to carry `if adapter.name == "medoctruyen": client.set_cookies(...)`,
    so every new gated site meant a second branch in two places. The mapping now lives in
    `AppConfig`, where the cookies already are, and the workers are unconditional.
    """

    def test_the_tieuthuyetmang_cookie_round_trips(self, tmp_path):
        config = _isolated_config(tmp_path)
        config.tieuthuyetmang_cookies = "session=abc"
        assert AppConfig.tieuthuyetmang_cookies.fget(config) == "session=abc"

    def test_it_defaults_to_empty(self, tmp_path):
        assert _isolated_config(tmp_path).tieuthuyetmang_cookies == ""

    def test_cookies_for_url_picks_the_cookie_of_the_site_in_the_url(self, tmp_path):
        config = _isolated_config(tmp_path)
        config.medoctruyen_cookies = "med=1"
        config.tieuthuyetmang_cookies = "ttm=1"
        assert config.cookies_for_url("https://medoctruyen.vn/abc") == "med=1"
        assert (
            config.cookies_for_url("https://tieuthuyetmang.com/truyen/abc/doc/1") == "ttm=1"
        )

    def test_a_site_with_no_stored_cookie_gets_a_blank_one(self, tmp_path):
        """`HttpClient.set_cookies` ignores a blank string, which is what lets the workers
        call it unconditionally."""
        config = _isolated_config(tmp_path)
        assert config.cookies_for_url("https://ixdzs.com/read/123") == ""

    def test_the_dialog_loads_and_saves_the_cookie(self, qapp, tmp_path):
        config = _isolated_config(tmp_path)
        config.tieuthuyetmang_cookies = "session=old"
        dialog = SettingsDialog(config)
        assert dialog.tieuthuyetmang_cookie_edit.text() == "session=old"
        dialog.tieuthuyetmang_cookie_edit.setText("  session=new  ")
        dialog.accept()
        assert config.tieuthuyetmang_cookies == "session=new"  # whitespace stripped

    def test_neither_worker_still_special_cases_one_site_by_name(self):
        """The point of `cookies_for_url`: a name check in the worker would have to grow
        a branch per site, and silently drop the cookie for any site not listed."""
        import inspect

        from noveltrans.gui import workers

        source = inspect.getsource(workers)
        assert 'adapter.name == "medoctruyen"' not in source

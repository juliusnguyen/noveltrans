"""SettingsDialog — the pre-TTS clean-text checkbox round-trips through config.

Uses an isolated QSettings (tmp .ini) so the test never touches the real user store,
following the pattern in test_main_window.py.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QLabel

from noveltrans.config import AppConfig
from noveltrans.gui.settings_dialog import SettingsDialog


class _FakeLoginWorker:
    """Stands in for OneDriveLoginWorker: records how it was built, never opens Chrome.

    `fire_done` / `fire_failed` replay what the real worker emits, so a test can watch
    what the dialog does with each outcome.
    """

    def __init__(self, record):
        self._record = record
        self._slots: dict[str, list] = {"done": [], "failed": []}

    def __call__(self, parent=None, *, switch=False):
        self._record["switch"] = switch
        return self

    def start(self):
        self._record["started"] = True

    @property
    def done(self):
        return _FakeWorkerSignal(self._slots["done"])

    @property
    def failed(self):
        return _FakeWorkerSignal(self._slots["failed"])

    def fire_done(self, account):
        for slot in self._slots["done"]:
            slot(account)

    def fire_failed(self, message):
        for slot in self._slots["failed"]:
            slot(message)


class _FakeWorkerSignal:
    def __init__(self, slots):
        self._slots = slots

    def connect(self, slot):
        self._slots.append(slot)


def _isolated_config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return config


def test_form_is_wrapped_in_a_scroll_area(qapp, tmp_path):
    """The form has too many rows to fit a small screen — it must scroll instead of
    pushing the OK/Cancel buttons off-screen."""
    from PySide6.QtWidgets import QScrollArea

    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert isinstance(dialog.scroll, QScrollArea)
    assert dialog.scroll.widgetResizable() is True
    # a control deep in the form is reachable through the scroll area's inner widget
    assert dialog.scroll.widget().isAncestorOf(dialog.tts_clean_check)


def test_dialog_height_is_capped_to_the_screen(qapp, tmp_path):
    from PySide6.QtWidgets import QApplication

    dialog = SettingsDialog(_isolated_config(tmp_path))
    screen = QApplication.primaryScreen()
    if screen is None:
        pytest.skip("no primary screen in this environment")
    assert dialog.height() <= int(screen.availableGeometry().height() * 0.85) + 1


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


# -- OneDrive sign-in row (051) -----------------------------------------------


def test_onedrive_status_says_not_connected_before_any_login(qapp, tmp_path):
    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert "Chưa đăng nhập" in dialog.onedrive_status.text()


def test_onedrive_status_names_the_connected_account(qapp, tmp_path):
    """The whole failure this exists for is signing into the wrong account and not
    finding out until a novel is sitting in someone else's OneDrive — so it names it."""
    config = _isolated_config(tmp_path)
    config.onedrive_account = "ai-do@example.com"
    dialog = SettingsDialog(config)
    assert "ai-do@example.com" in dialog.onedrive_status.text()


def test_a_signed_in_account_with_no_readable_name_still_reads_as_connected(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    config.onedrive_account = "?"
    dialog = SettingsDialog(config)
    assert "Đã kết nối" in dialog.onedrive_status.text()
    assert "Chưa đăng nhập" not in dialog.onedrive_status.text()


def test_an_existing_profile_folder_is_not_treated_as_a_login(qapp, tmp_path, monkeypatch):
    """MEASURED, and it caught this out: Playwright creates the profile folder before the
    user types anything, so a probe run or an abandoned sign-in leaves one that looks
    exactly like a good session. Reading the folder told the user “✅ đã kết nối” over a
    profile that had never been logged into."""
    import noveltrans.onedrive_upload as od

    profile = tmp_path / ".onedrive-profile"
    profile.mkdir()
    monkeypatch.setattr(od, "profile_dir", lambda: profile)
    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert "Chưa đăng nhập" in dialog.onedrive_status.text()


def test_the_status_refreshes_rather_than_being_read_once(qapp, tmp_path):
    config = _isolated_config(tmp_path)
    dialog = SettingsDialog(config)
    assert "Chưa đăng nhập" in dialog.onedrive_status.text()
    config.onedrive_account = "ai-do@example.com"
    dialog._refresh_onedrive_status()
    assert "ai-do@example.com" in dialog.onedrive_status.text()


def test_a_successful_login_records_the_account(qapp, tmp_path, monkeypatch):
    import noveltrans.gui.settings_dialog as sd

    started = {}
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    worker = _FakeLoginWorker(started)
    monkeypatch.setattr(sd, "OneDriveLoginWorker", worker)
    config = _isolated_config(tmp_path)
    dialog = SettingsDialog(config)
    dialog._onedrive_login()
    worker.fire_done("ai-do@example.com")
    assert config.onedrive_account == "ai-do@example.com"
    assert "ai-do@example.com" in dialog.onedrive_status.text()


def test_a_failed_login_forgets_the_previous_account(qapp, tmp_path, monkeypatch):
    """A switch drops the old session on its way in. Claiming a connection we no longer
    have is the exact failure this indicator exists to prevent."""
    import noveltrans.gui.settings_dialog as sd

    started = {}
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **k: None)
    worker = _FakeLoginWorker(started)
    monkeypatch.setattr(sd, "OneDriveLoginWorker", worker)
    config = _isolated_config(tmp_path)
    config.onedrive_account = "cu@example.com"
    dialog = SettingsDialog(config)
    dialog._onedrive_login()
    worker.fire_failed("Hết thời gian chờ đăng nhập OneDrive.")
    assert config.onedrive_account == ""
    assert "Chưa đăng nhập" in dialog.onedrive_status.text()


def test_switching_account_asks_first_and_a_no_stops_it(qapp, tmp_path, monkeypatch):
    """Switching drops the saved session. A misclick would sign the user out for nothing."""
    from PySide6.QtWidgets import QMessageBox

    import noveltrans.gui.settings_dialog as sd

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    monkeypatch.setattr(
        sd, "OneDriveLoginWorker", lambda *a, **k: pytest.fail("started a login anyway")
    )
    SettingsDialog(_isolated_config(tmp_path))._onedrive_switch()


def test_signing_in_starts_a_worker_with_switch_off(qapp, tmp_path, monkeypatch):
    import noveltrans.gui.settings_dialog as sd

    started = {}
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sd, "OneDriveLoginWorker", _FakeLoginWorker(started))
    SettingsDialog(_isolated_config(tmp_path))._onedrive_login()
    assert started == {"switch": False, "started": True}


def test_confirming_a_switch_starts_a_worker_with_switch_on(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    import noveltrans.gui.settings_dialog as sd

    started = {}
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sd, "OneDriveLoginWorker", _FakeLoginWorker(started))
    SettingsDialog(_isolated_config(tmp_path))._onedrive_switch()
    assert started == {"switch": True, "started": True}


def test_the_hint_states_the_destination_and_the_overwrite(qapp, tmp_path):
    """There is no destination field, so the hint is the only place the user learns
    where their files go — and that a same-named file up there gets replaced."""
    dialog = SettingsDialog(_isolated_config(tmp_path))
    labels = [
        w.text()
        for w in dialog.findChildren(QLabel)
        if "OneDrive" in w.text() and len(w.text()) > 80
    ]
    assert labels, "the OneDrive hint went missing"
    hint = labels[0]
    assert "<thư mục đích>/<tên truyện>/" in hint
    assert "GHI ĐÈ" in hint
    assert "playwright install chromium" in hint


def test_the_destination_folder_round_trips(qapp, tmp_path):
    """One destination for the whole library; each novel is a subfolder of it."""
    config = _isolated_config(tmp_path)
    dialog = SettingsDialog(config)
    dialog.onedrive_root_edit.setText("/Fox Novel")
    dialog.accept()
    assert config.onedrive_root == "/Fox Novel"
    assert SettingsDialog(config).onedrive_root_edit.text() == "/Fox Novel"


def test_an_emptied_destination_falls_back_to_the_default(qapp, tmp_path):
    """A blank destination would put every novel in the OneDrive root, which is not a
    thing anyone means to ask for."""
    from noveltrans.config import DEFAULT_ONEDRIVE_ROOT

    config = _isolated_config(tmp_path)
    dialog = SettingsDialog(config)
    dialog.onedrive_root_edit.setText("   ")
    dialog.accept()
    assert config.onedrive_root == DEFAULT_ONEDRIVE_ROOT


def test_the_onedrive_row_does_not_disturb_the_youtube_one(qapp, tmp_path):
    """They are separate accounts and separate profiles; the two rows must both exist."""
    dialog = SettingsDialog(_isolated_config(tmp_path))
    assert dialog.youtube_status.text()
    assert dialog.onedrive_status.text()
    assert dialog.youtube_status is not dialog.onedrive_status

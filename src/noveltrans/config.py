"""AppConfig — typed wrapper around QSettings.

Note: the Claude API key and the medoctruyen.vn session cookie are stored
unencrypted in the native QSettings location (plist on macOS, registry on
Windows, ini on Linux).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from noveltrans.storage.library import DEFAULT_LIBRARY_DIR

DEFAULT_REQUEST_DELAY = 1.5
DEFAULT_TARGET_LANG = "vi"
DEFAULT_TRANSLATOR = "google"
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"

DEFAULT_CLI_COMMAND = "agy -p"
DEFAULT_CLAUDE_CLI_COMMAND = "claude -p"
DEFAULT_LMSTUDIO_URL = "http://127.0.0.1:1234"
DEFAULT_TTS_VOICE = "Ngọc Lan"
DEFAULT_TTS_FORMAT = "mp3"  # falls back to wav when ffmpeg is missing

TARGET_LANGS = {"vi": "Tiếng Việt", "en": "English"}
TRANSLATORS = {
    "google": "Google Translate (miễn phí)",
    "claude": "Claude API",
    "cli": "CLI Agent",
    "claude_cli": "Claude CLI",
    "lmstudio": "LM Studio (local)",
}


def translator_labels(config: "AppConfig | None" = None) -> dict[str, str]:
    """Engine labels for combo boxes; CLI entries show their actual command."""
    labels = dict(TRANSLATORS)
    if config is not None:
        for key, command in (
            ("cli", config.cli_command),
            ("claude_cli", config.claude_cli_command),
        ):
            parts = (command or "").strip().split()
            if parts:
                labels[key] = f"CLI Agent ({parts[0]})"
    return labels


class AppConfig:
    def __init__(self) -> None:
        self._s = QSettings("noveltrans", "noveltrans")

    # ------------------------------------------------------------ properties

    @property
    def library_dir(self) -> Path:
        return Path(self._s.value("library_dir", str(DEFAULT_LIBRARY_DIR)))

    @library_dir.setter
    def library_dir(self, value: Path) -> None:
        self._s.setValue("library_dir", str(value))

    @property
    def request_delay(self) -> float:
        return float(self._s.value("request_delay", DEFAULT_REQUEST_DELAY))

    @request_delay.setter
    def request_delay(self, value: float) -> None:
        self._s.setValue("request_delay", float(value))

    @property
    def target_lang(self) -> str:
        return str(self._s.value("target_lang", DEFAULT_TARGET_LANG))

    @target_lang.setter
    def target_lang(self, value: str) -> None:
        self._s.setValue("target_lang", value)

    @property
    def translator(self) -> str:
        return str(self._s.value("translator", DEFAULT_TRANSLATOR))

    @translator.setter
    def translator(self, value: str) -> None:
        self._s.setValue("translator", value)

    @property
    def claude_api_key(self) -> str:
        return str(self._s.value("claude_api_key", ""))

    @claude_api_key.setter
    def claude_api_key(self, value: str) -> None:
        self._s.setValue("claude_api_key", value)

    @property
    def medoctruyen_cookies(self) -> str:
        return str(self._s.value("medoctruyen_cookies", ""))

    @medoctruyen_cookies.setter
    def medoctruyen_cookies(self, value: str) -> None:
        self._s.setValue("medoctruyen_cookies", value)

    @property
    def discord_autounlock_enabled(self) -> bool:
        return self._s.value("discord_autounlock_enabled", False, type=bool)

    @discord_autounlock_enabled.setter
    def discord_autounlock_enabled(self, value: bool) -> None:
        self._s.setValue("discord_autounlock_enabled", bool(value))

    @property
    def discord_channel_url(self) -> str:
        """The #mở-khoá channel link (https://discord.com/channels/<guild>/<chan>)
        the throwaway account uses to run /mochuong. Empty until the user sets it."""
        return str(self._s.value("discord_channel_url", ""))

    @discord_channel_url.setter
    def discord_channel_url(self, value: str) -> None:
        self._s.setValue("discord_channel_url", value.strip())

    @property
    def claude_model(self) -> str:
        return str(self._s.value("claude_model", DEFAULT_CLAUDE_MODEL))

    @claude_model.setter
    def claude_model(self, value: str) -> None:
        self._s.setValue("claude_model", value)

    @property
    def cli_command(self) -> str:
        return str(self._s.value("cli_command", DEFAULT_CLI_COMMAND))

    @cli_command.setter
    def cli_command(self, value: str) -> None:
        self._s.setValue("cli_command", value)

    @property
    def claude_cli_command(self) -> str:
        return str(self._s.value("claude_cli_command", DEFAULT_CLAUDE_CLI_COMMAND))

    @claude_cli_command.setter
    def claude_cli_command(self, value: str) -> None:
        self._s.setValue("claude_cli_command", value)

    def cli_command_for(self, engine: str) -> str:
        """The shell command backing a CLI-based engine."""
        return self.claude_cli_command if engine == "claude_cli" else self.cli_command

    @property
    def lmstudio_url(self) -> str:
        return str(self._s.value("lmstudio_url", DEFAULT_LMSTUDIO_URL))

    @lmstudio_url.setter
    def lmstudio_url(self, value: str) -> None:
        self._s.setValue("lmstudio_url", value.strip() or DEFAULT_LMSTUDIO_URL)

    @property
    def tts_voice(self) -> str:
        return str(self._s.value("tts_voice", DEFAULT_TTS_VOICE))

    @tts_voice.setter
    def tts_voice(self, value: str) -> None:
        self._s.setValue("tts_voice", value.strip() or DEFAULT_TTS_VOICE)

    @property
    def tts_format(self) -> str:
        return str(self._s.value("tts_format", DEFAULT_TTS_FORMAT))

    @tts_format.setter
    def tts_format(self, value: str) -> None:
        self._s.setValue("tts_format", value)

    def cli_model_for(self, engine: str) -> str:
        """Model override for a CLI-based engine ("" = the CLI's own default)."""
        return str(self._s.value(f"{engine}_model", ""))

    def set_cli_model_for(self, engine: str, value: str) -> None:
        self._s.setValue(f"{engine}_model", value.strip())

    @property
    def window_geometry(self):
        return self._s.value("window_geometry")

    @window_geometry.setter
    def window_geometry(self, value) -> None:
        self._s.setValue("window_geometry", value)

    def sync(self) -> None:
        self._s.sync()

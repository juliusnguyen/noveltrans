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
DEFAULT_TTS_VOICE = "Ngọc Linh"
DEFAULT_TTS_FORMAT = "mp3"  # falls back to wav when ffmpeg is missing
DEFAULT_TTS_WORKERS = 1  # sequential; >1 loads one ~334MB engine per worker
DEFAULT_TTS_CLEAN_TEXT = True  # strip special chars before TTS for smoother audio
# Adjustable TTS output. Defaults reproduce the pre-018 behaviour exactly:
DEFAULT_TTS_GAP = 0.4  # seconds of silence between chunks
DEFAULT_TTS_SPEED = 1.0  # playback tempo (ffmpeg atempo); 1.0 = unchanged
DEFAULT_TTS_VOLUME = 1.0  # linear gain; 1.0 = unchanged
DEFAULT_TTS_TEMPERATURE = 0.0  # 0.0 = unset (pass nothing → the model's own default)
DEFAULT_TTS_PRECISION = "int8"  # VieNeu ONNX/CPU graph: "int8" (fast) or "fp32" (accurate)
TTS_PRECISIONS = ("int8", "fp32")
# Reading style, independent of voice. Default "tu_nhien" reproduces today's output
# (the engine's own default). Ordered (id, label) for the audio-tab dropdown.
DEFAULT_TTS_STYLE = "tu_nhien"
TTS_STYLES = (
    ("tu_nhien", "Tự nhiên"),
    ("doc_truyen", "Kể chuyện"),
    ("tin_tuc", "Tin tức"),
)

TARGET_LANGS = {"vi": "Tiếng Việt", "en": "English"}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


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

    @property
    def tts_use_translation(self) -> bool:
        """Voice the translation (True) or the original text (False)."""
        return self._s.value("tts_use_translation", True, type=bool)

    @tts_use_translation.setter
    def tts_use_translation(self, value: bool) -> None:
        self._s.setValue("tts_use_translation", bool(value))

    @property
    def tts_clean_text(self) -> bool:
        """Strip special characters (emoji, decorative symbols, stray CJK, markdown)
        from chapter text before TTS so the audio reads smoothly. Vietnamese is kept.
        Only the copy fed to the engine is cleaned — stored text is untouched."""
        return self._s.value("tts_clean_text", DEFAULT_TTS_CLEAN_TEXT, type=bool)

    @tts_clean_text.setter
    def tts_clean_text(self, value: bool) -> None:
        self._s.setValue("tts_clean_text", bool(value))

    @property
    def tts_clean_extra_remove(self) -> str:
        """Extra characters to strip before TTS, on top of the automatic cleaning.
        Only affects characters that would otherwise be kept (e.g. "()" so parentheses
        aren't voiced) — anything already stripped is unaffected."""
        return str(self._s.value("tts_clean_extra_remove", ""))

    @tts_clean_extra_remove.setter
    def tts_clean_extra_remove(self, value: str) -> None:
        self._s.setValue("tts_clean_extra_remove", value)

    @property
    def tts_workers(self) -> int:
        """Parallel TTS synthesis workers. Each worker loads its own ~334MB
        VieNeu engine, so N workers ≈ N×334MB RAM. 1 = sequential (default)."""
        return max(1, self._s.value("tts_workers", DEFAULT_TTS_WORKERS, type=int))

    @tts_workers.setter
    def tts_workers(self, value: int) -> None:
        self._s.setValue("tts_workers", max(1, int(value)))

    @property
    def tts_gap_seconds(self) -> float:
        """Silence between chunks/paragraphs in the audio (seconds). Default 0.4."""
        return _clamp(self._s.value("tts_gap_seconds", DEFAULT_TTS_GAP, type=float), 0.0, 2.0)

    @tts_gap_seconds.setter
    def tts_gap_seconds(self, value: float) -> None:
        self._s.setValue("tts_gap_seconds", _clamp(float(value), 0.0, 2.0))

    @property
    def tts_speed(self) -> float:
        """Playback tempo, applied via ffmpeg atempo (pitch-preserving). 1.0 = normal."""
        return _clamp(self._s.value("tts_speed", DEFAULT_TTS_SPEED, type=float), 0.5, 2.0)

    @tts_speed.setter
    def tts_speed(self, value: float) -> None:
        self._s.setValue("tts_speed", _clamp(float(value), 0.5, 2.0))

    @property
    def tts_volume(self) -> float:
        """Linear gain on the rendered audio. 1.0 = unchanged; >1.0 may clip."""
        return _clamp(self._s.value("tts_volume", DEFAULT_TTS_VOLUME, type=float), 0.1, 3.0)

    @tts_volume.setter
    def tts_volume(self, value: float) -> None:
        self._s.setValue("tts_volume", _clamp(float(value), 0.1, 3.0))

    @property
    def tts_temperature(self) -> float:
        """VieNeu expressiveness. 0.0 = unset (pass nothing → the model's own default);
        higher = more varied delivery, lower = steadier."""
        return _clamp(self._s.value("tts_temperature", DEFAULT_TTS_TEMPERATURE, type=float), 0.0, 1.5)

    @tts_temperature.setter
    def tts_temperature(self, value: float) -> None:
        self._s.setValue("tts_temperature", _clamp(float(value), 0.0, 1.5))

    @property
    def tts_precision(self) -> str:
        """VieNeu model precision on the CPU/ONNX path. "int8" = fast default,
        "fp32" = higher quality (slower, larger download). Unknown values → int8."""
        value = str(self._s.value("tts_precision", DEFAULT_TTS_PRECISION))
        return value if value in TTS_PRECISIONS else DEFAULT_TTS_PRECISION

    @tts_precision.setter
    def tts_precision(self, value: str) -> None:
        self._s.setValue(
            "tts_precision", value if value in TTS_PRECISIONS else DEFAULT_TTS_PRECISION
        )

    @property
    def tts_style(self) -> str:
        """Reading style, independent of voice: tu_nhien / doc_truyen / tin_tuc.
        Unknown values fall back to the default (tu_nhien)."""
        value = str(self._s.value("tts_style", DEFAULT_TTS_STYLE))
        valid = {sid for sid, _ in TTS_STYLES}
        return value if value in valid else DEFAULT_TTS_STYLE

    @tts_style.setter
    def tts_style(self, value: str) -> None:
        valid = {sid for sid, _ in TTS_STYLES}
        self._s.setValue("tts_style", value if value in valid else DEFAULT_TTS_STYLE)

    @property
    def keep_awake_enabled(self) -> bool:
        """Keep the Mac awake while a download/translate/TTS/merge job is running."""
        return self._s.value("keep_awake_enabled", True, type=bool)

    @keep_awake_enabled.setter
    def keep_awake_enabled(self, value: bool) -> None:
        self._s.setValue("keep_awake_enabled", bool(value))

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

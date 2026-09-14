"""Feature 088 — the Codex CLI engine's settings: its command, labels and QC chain entry."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import (
    CLI_ENGINES,
    CLI_MODEL_SUGGESTIONS,
    DEFAULT_CODEX_CLI_COMMAND,
    LLM_ENGINES,
    TRANSLATORS,
    AppConfig,
    translator_labels,
)


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    return config


def test_codex_cli_is_a_selectable_llm_engine():
    assert TRANSLATORS["codex_cli"] == "Codex CLI"
    assert "codex_cli" in LLM_ENGINES
    assert "codex_cli" in CLI_ENGINES


def test_the_default_command_runs_headless_outside_a_repo_without_writes():
    args = DEFAULT_CODEX_CLI_COMMAND.split()
    assert args[:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in args
    assert args[args.index("--sandbox") + 1] == "read-only"


def test_the_command_round_trips(tmp_path):
    config = _config(tmp_path)
    assert config.codex_cli_command == DEFAULT_CODEX_CLI_COMMAND
    config.codex_cli_command = "codex exec --skip-git-repo-check -c model_reasoning_effort=low"
    assert _config(tmp_path).codex_cli_command.endswith("model_reasoning_effort=low")


def test_each_cli_engine_gets_its_own_command(tmp_path):
    config = _config(tmp_path)
    config.cli_command = "agy -p"
    config.claude_cli_command = "claude -p"
    config.codex_cli_command = "codex exec"
    assert config.cli_command_for("cli") == "agy -p"
    assert config.cli_command_for("claude_cli") == "claude -p"
    assert config.cli_command_for("codex_cli") == "codex exec"
    assert config.cli_command_for("anything-else") == "agy -p"


def test_the_combo_label_shows_the_binary(tmp_path):
    assert translator_labels(_config(tmp_path))["codex_cli"] == "CLI Agent (codex)"


def test_the_model_is_remembered_per_engine(tmp_path):
    config = _config(tmp_path)
    config.set_cli_model_for("codex_cli", " gpt-5.6-luna ")
    config.set_cli_model_for("claude_cli", "sonnet")
    assert config.cli_model_for("codex_cli") == "gpt-5.6-luna"
    assert config.cli_model_for("claude_cli") == "sonnet"


def test_codex_can_be_a_qc_fallback(tmp_path):
    config = _config(tmp_path)
    config.qc_engine_chain = [("cli", "", 2), ("codex_cli", "gpt-5.5", 3)]
    assert _config(tmp_path).qc_engine_chain == [("cli", "", 2), ("codex_cli", "gpt-5.5", 3)]


def test_codex_has_model_suggestions_so_the_tab_never_runs_codex_models():
    assert CLI_MODEL_SUGGESTIONS["codex"]
    assert CLI_MODEL_SUGGESTIONS["claude"] == ["haiku", "sonnet", "opus"]

"""Feature 088 — the Translate tab's model box for the Codex CLI engine."""

from __future__ import annotations

import noveltrans.gui.tab_translate as tt
from noveltrans.config import CLI_MODEL_SUGGESTIONS, AppConfig


class _RecordingWorker:
    started: list[str] = []

    def __init__(self, source):
        _RecordingWorker.started.append(source)

    def isRunning(self):
        return False

    @property
    def models_listed(self):
        class _Signal:
            def connect(self, slot):
                pass

        return _Signal()

    def start(self):
        pass


def _tab_on(engine, qapp, monkeypatch, *, command=None):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _RecordingWorker.started = []
    monkeypatch.setattr(tt, "CliModelsWorker", _RecordingWorker)
    config = AppConfig()
    if command is not None:
        config.codex_cli_command = command
    config.set_cli_model_for("codex_cli", "gpt-5.5")
    tab = tt.TranslateTab(config)
    tab.engine_combo.setCurrentIndex(tab.engine_combo.findData(engine))
    tab._on_engine_changed()
    return tab


def _items(tab):
    return [tab.model_combo.itemText(i) for i in range(tab.model_combo.count())]


def test_codex_is_offered_in_the_engine_box(qapp, monkeypatch):
    tab = _tab_on("codex_cli", qapp, monkeypatch)
    assert tab.engine_combo.currentData() == "codex_cli"


def test_the_model_box_shows_codex_models_and_the_remembered_one(qapp, monkeypatch):
    tab = _tab_on("codex_cli", qapp, monkeypatch)
    assert not tab.model_combo.isHidden()
    assert _items(tab)[1:] == CLI_MODEL_SUGGESTIONS["codex"]
    assert tab.model_combo.currentText() == "gpt-5.5"


def test_it_never_runs_codex_models(qapp, monkeypatch):
    # `codex models` is not a subcommand: codex would start a session with "models" as
    # the prompt
    _tab_on("codex_cli", qapp, monkeypatch)
    _tab_on("codex_cli", qapp, monkeypatch, command="/opt/homebrew/bin/codex exec")
    assert _RecordingWorker.started == []

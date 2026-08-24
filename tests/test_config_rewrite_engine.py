"""Feature 060 — `rewrite_ai_engine` / `rewrite_ai_model`, and the shared LLM_ENGINES list."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import LLM_ENGINES, AppConfig


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    return config


class TestLlmEngines:
    def test_google_can_never_be_chosen_for_an_llm_only_feature(self):
        assert "google" not in LLM_ENGINES

    def test_the_video_tab_shares_the_same_list(self):
        # One list, so the tags combo and the rewrite combo cannot drift apart.
        from noveltrans.gui.tab_video import _TAG_ENGINES

        assert _TAG_ENGINES is LLM_ENGINES


class TestRewriteEngineDefault:
    def test_it_follows_the_translator_when_that_is_an_llm(self, tmp_path):
        config = _config(tmp_path)
        config.translator = "claude"
        assert config.rewrite_ai_engine == "claude"

    def test_it_falls_back_to_the_cli_agent_when_the_translator_is_google(self, tmp_path):
        # Google is translate-only, so preselecting it would put an error dialog in
        # front of anyone using the app's default engine.
        config = _config(tmp_path)
        config.translator = "google"
        assert config.rewrite_ai_engine == "cli"

    def test_an_explicit_choice_wins_over_the_default(self, tmp_path):
        config = _config(tmp_path)
        config.translator = "claude"
        config.rewrite_ai_engine = "lmstudio"
        assert config.rewrite_ai_engine == "lmstudio"

    def test_it_is_independent_of_the_video_tabs_engine(self, tmp_path):
        # Translating on the free engine and rewriting on a metered one is the whole
        # reason this is a separate setting.
        config = _config(tmp_path)
        config.rewrite_ai_engine = "claude"
        config.video_ai_engine = "lmstudio"
        assert config.rewrite_ai_engine == "claude"
        assert config.video_ai_engine == "lmstudio"


class TestRewriteModel:
    def test_it_defaults_to_the_engines_own_model(self, tmp_path):
        assert _config(tmp_path).rewrite_ai_model == ""

    def test_it_is_stored_trimmed(self, tmp_path):
        config = _config(tmp_path)
        config.rewrite_ai_model = "  Gemini 3.1 Pro  "
        assert config.rewrite_ai_model == "Gemini 3.1 Pro"

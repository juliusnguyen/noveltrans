"""Feature 084 — the translation-QC settings, and what a bad settings file may do.

`qc_engine_chain` is the interesting one: it is a list, and QSettings mangles those in a
specific documented way, so the getter has to be defensive on read rather than trusting
what it stored.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import DEFAULT_QC_ATTEMPTS, MAX_QC_ATTEMPTS, AppConfig


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    return config


class TestDefaults:
    def test_qc_is_off_until_the_user_turns_it_on(self, tmp_path):
        """The backward-compatibility guarantee: nothing changes for anyone who does not
        ask for it, because QC costs quota and can mark chapters."""
        assert _config(tmp_path).qc_enabled is False

    def test_the_style_judge_is_on_once_qc_is(self, tmp_path):
        # Dense Hán-Việt is the failure that prompted the feature and the only one no
        # cheap check can catch, so a QC run without the judge would miss the point.
        assert _config(tmp_path).qc_use_llm_judge is True

    def test_two_tries_per_engine(self, tmp_path):
        # A retry re-translates a WHOLE chapter; an engine that failed twice with the
        # reason spelled out is unlikely to be talked round on a third go.
        assert DEFAULT_QC_ATTEMPTS == 2

    def test_the_judge_follows_the_translator_when_that_is_an_llm(self, tmp_path):
        config = _config(tmp_path)
        config.translator = "claude"
        assert config.qc_ai_engine == "claude"

    def test_the_judge_falls_back_when_the_translator_is_google(self, tmp_path):
        config = _config(tmp_path)
        config.translator = "google"
        assert config.qc_ai_engine == "cli"  # Google can translate, not judge

    def test_an_unconfigured_chain_is_empty(self, tmp_path):
        # Empty means "the engine the Translate tab is set to", which is the obvious
        # reading of "QC on, chain not configured".
        assert _config(tmp_path).qc_engine_chain == []


class TestEngineChain:
    def test_it_round_trips_engine_model_and_attempts(self, tmp_path):
        config = _config(tmp_path)
        config.qc_engine_chain = [("cli", "", 2), ("claude_cli", "sonnet", 3)]
        assert config.qc_engine_chain == [("cli", "", 2), ("claude_cli", "sonnet", 3)]

    def test_a_one_entry_chain_survives_qsettings_collapsing_it_to_a_string(self, tmp_path):
        """QSettings turns a one-item list into a bare string on read. A single-engine
        chain is the COMMON case, so this would bite immediately."""
        config = _config(tmp_path)
        config.qc_engine_chain = [("cli", "", 2)]
        config._s.sync()
        reopened = AppConfig()
        reopened._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        assert reopened.qc_engine_chain == [("cli", "", 2)]

    def test_an_unknown_engine_is_dropped_not_fatal(self, tmp_path):
        config = _config(tmp_path)
        config._s.setValue("qc_engine_chain", ["cli||2", "gemini_cli||2", "claude||1"])
        assert config.qc_engine_chain == [("cli", "", 2), ("claude", "", 1)]

    def test_attempts_are_clamped_and_bad_ones_default(self, tmp_path):
        config = _config(tmp_path)
        config._s.setValue("qc_engine_chain", ["cli||99", "claude||0", "lmstudio||abc"])
        assert config.qc_engine_chain == [
            ("cli", "", MAX_QC_ATTEMPTS),
            ("claude", "", 1),
            ("lmstudio", "", DEFAULT_QC_ATTEMPTS),
        ]

    def test_a_bare_engine_name_gets_the_defaults(self, tmp_path):
        config = _config(tmp_path)
        config._s.setValue("qc_engine_chain", ["cli"])
        assert config.qc_engine_chain == [("cli", "", DEFAULT_QC_ATTEMPTS)]

    def test_it_is_independent_of_the_other_ai_features(self, tmp_path):
        # Translating on one engine, judging on a second and rewriting on a third is the
        # whole reason these are separate settings.
        config = _config(tmp_path)
        config.qc_ai_engine = "cli"
        config.rewrite_ai_engine = "claude"
        config.video_ai_engine = "lmstudio"
        assert (config.qc_ai_engine, config.rewrite_ai_engine, config.video_ai_engine) == (
            "cli", "claude", "lmstudio",
        )

    def test_the_judge_model_is_stored_trimmed(self, tmp_path):
        config = _config(tmp_path)
        config.qc_ai_model = "  sonnet  "
        assert config.qc_ai_model == "sonnet"

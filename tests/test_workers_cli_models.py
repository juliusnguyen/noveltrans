"""Feature 079 — CliModelsWorker spawns `<binary> models` with the console-flash guard."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from noveltrans import runtime_env
from noveltrans.gui.workers import CliModelsWorker


def _result(stdout="", returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


class TestCliModelsWorker:
    def test_passes_no_console_kwargs_on_windows(self, monkeypatch):
        # CliModelsWorker.run() imports subprocess locally, so the module-level
        # `subprocess.run` (the same cached sys.modules object) is what needs patching —
        # not `noveltrans.gui.workers.subprocess.run`, which doesn't exist as an attribute.
        monkeypatch.setattr(runtime_env.sys, "platform", "win32")
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _result(stdout="model-a\nmodel-b\n")
            CliModelsWorker("agy").run()
            assert mock_run.call_args.kwargs["creationflags"] == create_no_window

    def test_no_console_kwargs_empty_on_other_platforms(self, monkeypatch):
        monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _result(stdout="model-a\n")
            CliModelsWorker("agy").run()
            assert "creationflags" not in mock_run.call_args.kwargs

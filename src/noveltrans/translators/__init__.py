"""Translator registry."""

from __future__ import annotations

from noveltrans.errors import TranslateError
from noveltrans.translators.base import Translator


def get_translator(
    name: str,
    *,
    api_key: str = "",
    model: str = "",
    request_delay: float = 1.0,
    cli_command: str = "",
    base_url: str = "",
) -> Translator:
    """Build a translator by name. Imports lazily so one engine's missing
    dependency doesn't break the other."""
    if name == "google":
        from noveltrans.translators.google_free import GoogleFreeTranslator

        return GoogleFreeTranslator(request_delay=request_delay)
    if name == "claude":
        from noveltrans.translators.claude import ClaudeTranslator

        kwargs = {"model": model} if model else {}
        return ClaudeTranslator(api_key=api_key, **kwargs)
    if name in ("cli", "claude_cli"):
        from noveltrans.translators.cli_agent import CliAgentTranslator

        return CliAgentTranslator(command=cli_command, model=model)
    if name == "lmstudio":
        from noveltrans.translators.lmstudio import LmStudioTranslator

        return LmStudioTranslator(base_url=base_url, model=model)
    raise TranslateError(f"Unknown translator: {name!r}")

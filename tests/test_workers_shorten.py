"""Feature 065 — ShortenTitlesWorker: what comes back when the model misbehaves.

The load-bearing rule is that the returned list always has exactly one entry per input
title. A list short by one would attach every chapter title to the wrong timestamp, which
is a worse bug than not shortening at all.
"""

from __future__ import annotations

import pytest

from noveltrans.errors import NovelTransError
from noveltrans.gui.workers import ShortenTitlesWorker
from noveltrans.translators.base import Translator


class _FakeEngine(Translator):
    """An LLM engine whose reply is a pure function of the chunk it was sent."""

    name = "fake"
    display_name = "Fake"
    max_chunk_chars = 4000
    supports_completion = True

    def __init__(self, reply=None, supports: bool = True):
        self.reply = reply or self._numbered
        self.supports_completion = supports
        self.prompts: list[str] = []

    @staticmethod
    def _titles(prompt: str) -> list[str]:
        """Pull the numbered input list back out of the prompt."""
        body = prompt.split("\n\n")[1]
        return [line.split(". ", 1)[1] for line in body.splitlines()]

    def _numbered(self, prompt: str) -> str:
        titles = self._titles(prompt)
        return "\n".join(f"{i}. ngắn {t}" for i, t in enumerate(titles, 1))

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        raise AssertionError("shortening must never call translate()")

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply(prompt)


def _run(monkeypatch, titles, engine) -> tuple[list, list[str], int, list[tuple[int, int]]]:
    """Run the worker synchronously; returns (failures, titles, fell_back, progress)."""
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)
    worker = ShortenTitlesWorker(titles, "fake")
    failures: list[str] = []
    results: list[tuple[list, int]] = []
    progress: list[tuple[int, int]] = []
    worker.failed.connect(failures.append)
    worker.finished_ok.connect(lambda t, f: results.append((t, f)))
    worker.progress.connect(lambda d, t: progress.append((d, t)))
    worker.run()  # not start() — no thread, so the signals land before we assert
    out, fell_back = results[0] if results else ([], 0)
    return failures, out, fell_back, progress


class TestShortenTitlesWorker:
    def test_returns_one_title_per_input_in_order(self, qapp, monkeypatch):
        failures, out, fell_back, _ = _run(
            monkeypatch, ["Mở đầu", "Cao trào", "Kết"], _FakeEngine()
        )
        assert not failures
        assert out == ["ngắn Mở đầu", "ngắn Cao trào", "ngắn Kết"]
        assert fell_back == 0

    def test_no_titles_finishes_without_calling_the_engine(self, qapp, monkeypatch):
        engine = _FakeEngine()
        failures, out, _, _ = _run(monkeypatch, [], engine)
        assert not failures
        assert out == []
        assert engine.prompts == []

    def test_chunks_requests_above_the_chunk_size(self, qapp, monkeypatch):
        engine = _FakeEngine()
        titles = [f"Tên {i}" for i in range(130)]
        _failures, out, _, _ = _run(monkeypatch, titles, engine)
        assert len(engine.prompts) == 3  # 60 + 60 + 10
        assert len(out) == 130

    def test_progress_is_emitted_per_chunk(self, qapp, monkeypatch):
        titles = [f"Tên {i}" for i in range(130)]
        _f, _o, _b, progress = _run(monkeypatch, titles, _FakeEngine())
        assert progress == [(60, 130), (120, 130), (130, 130)]

    def test_a_chunk_that_misparses_falls_back_to_its_originals(self, qapp, monkeypatch):
        """Losing 60 good titles because the model miscounted one chunk is worse than
        keeping that chunk long — and returning fewer lines than chapters would misalign
        every timestamp after the gap."""
        engine = _FakeEngine()
        calls = {"n": 0}

        def flaky(prompt: str) -> str:
            calls["n"] += 1
            if calls["n"] == 2:
                return "1. chỉ một dòng"  # too few lines for a 60-title chunk
            return engine._numbered(prompt)

        engine.reply = flaky
        titles = [f"Tên {i}" for i in range(130)]
        failures, out, fell_back, _ = _run(monkeypatch, titles, engine)
        assert not failures
        assert len(out) == 130
        assert out[0] == "ngắn Tên 0"  # chunk 1 shortened
        assert out[60:120] == titles[60:120]  # chunk 2 verbatim
        assert out[120] == "ngắn Tên 120"  # chunk 3 shortened
        assert fell_back == 1

    def test_an_engine_error_in_one_chunk_falls_back_rather_than_failing(
        self, qapp, monkeypatch
    ):
        engine = _FakeEngine()
        calls = {"n": 0}

        def flaky(prompt: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise NovelTransError("engine hiccup")
            return engine._numbered(prompt)

        engine.reply = flaky
        titles = [f"Tên {i}" for i in range(70)]
        failures, out, fell_back, _ = _run(monkeypatch, titles, engine)
        assert not failures
        assert out[:60] == titles[:60]
        assert fell_back == 1

    def test_engine_without_completion_support_fails_loudly(self, qapp, monkeypatch):
        failures, out, _, _ = _run(
            monkeypatch, ["Mở đầu"], _FakeEngine(supports=False)
        )
        assert failures and "CLI Agent" in failures[0]
        assert out == []

    def test_an_unbuildable_engine_fails(self, qapp, monkeypatch):
        def boom(*a, **k):
            raise NovelTransError("thiếu API key")

        monkeypatch.setattr("noveltrans.translators.get_translator", boom)
        worker = ShortenTitlesWorker(["Mở đầu"], "fake")
        failures: list[str] = []
        worker.failed.connect(failures.append)
        worker.run()
        assert failures == ["thiếu API key"]

    def test_every_chunk_failing_reports_failure(self, qapp, monkeypatch):
        engine = _FakeEngine(reply=lambda prompt: "rác")
        failures, out, _, _ = _run(monkeypatch, ["Mở đầu", "Kết"], engine)
        assert failures
        assert out == []


@pytest.fixture(autouse=True)
def _no_thread_start(monkeypatch):
    """Guard: these tests call run() directly, so nothing should spin a real thread."""
    monkeypatch.setattr(
        ShortenTitlesWorker, "start", lambda self: pytest.fail("start() in a unit test")
    )

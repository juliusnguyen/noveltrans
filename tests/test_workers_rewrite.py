"""Feature 060 — RewriteWorker: what reaches the database, and what must not.

The load-bearing test here is `test_a_summarising_engine_leaves_the_translation_intact`.
Everything else is plumbing; that one is the feature's hard rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.errors import NovelTransError
from noveltrans.gui.workers import RewriteWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject
from noveltrans.translators.base import Translator

CONVERT = (
    "Phó Thanh Từ đứng ở cửa, trong lòng hắn có một loại không cách nào nói nói tư vị.\n\n"
    "Giang Dư nhìn hắn thật lâu, sau đó mới chậm rãi mở miệng nói ra một câu."
)


def _polish(text: str) -> str:
    """A well-behaved engine: fixes the word order, keeps everything else."""
    return text.replace("không cách nào nói nói tư vị", "tư vị khó nói")


def _summarise(text: str) -> str:
    """A misbehaving engine: drops the last paragraph of a multi-paragraph chunk.

    One-line text (a chapter title) passes through, so the body is what fails — which is
    the realistic shape of the failure and keeps the title out of the way.
    """
    parts = text.split("\n\n")
    return "\n\n".join(parts[:-1]) if len(parts) > 1 else text


class _FakeEngine(Translator):
    """An LLM engine whose reply is a pure function of the text it was sent.

    `complete` takes exactly ONE positional parameter, so a worker that ever passed
    `system=` would raise TypeError here — which is the point. That is the signature the
    CLI agent physically cannot widen.
    """

    name = "fake"
    display_name = "Fake"
    max_chunk_chars = 4000
    supports_completion = True

    def __init__(self, transform):
        self.transform = transform
        self.prompts: list[str] = []

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        raise AssertionError("the rewrite pass must never call translate()")

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.transform(prompt.split("\n---\n", 1)[1])


def _project(library_dir: Path, translated: int = 2) -> Path:
    """Three chapters, the first `translated` of them carrying a convert-style body."""
    meta = NovelMeta(url="https://x/truyen", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}") for i in range(3)]
    project = NovelProject.create(library_dir, meta, refs)
    for idx in range(translated):
        project.save_content(idx, f"原文{idx}")
        project.save_translation(
            idx, f"Chương {idx + 1}", CONVERT, "vi", translator="Google Translate"
        )
    path = project.path
    project.close()
    return path


def _use(monkeypatch, engine) -> None:
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)


@pytest.fixture
def opened():
    """Open a project for assertions and close it afterwards."""
    handles: list[NovelProject] = []

    def _open(path: Path) -> NovelProject:
        project = NovelProject.open(path)
        handles.append(project)
        return project

    yield _open
    for project in handles:
        project.close()


class TestEngineGuard:
    def test_google_is_refused_and_nothing_is_written(self, qapp, library_dir, opened):
        path = _project(library_dir)
        failures: list[str] = []
        worker = RewriteWorker(path, engine_name="google")
        worker.failed.connect(failures.append)
        worker.run()

        assert len(failures) == 1
        for engine in ("CLI Agent", "Claude", "LM Studio"):
            assert engine in failures[0]
        assert not opened(path).chapter(0).is_rewritten

    def test_an_engine_that_cannot_be_built_reports_and_stops(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        monkeypatch.setattr(
            "noveltrans.translators.get_translator",
            lambda *a, **k: (_ for _ in ()).throw(NovelTransError("chưa có API key")),
        )
        failures: list[str] = []
        worker = RewriteWorker(path, engine_name="claude")
        worker.failed.connect(failures.append)
        worker.run()

        assert failures == ["chưa có API key"]
        assert not opened(path).chapter(0).is_rewritten

    def test_the_project_is_closed_on_the_failure_path(
        self, qapp, library_dir, monkeypatch
    ):
        path = _project(library_dir)
        real_close = NovelProject.close
        closes: list[bool] = []

        def spy(self):
            closes.append(True)
            real_close(self)

        monkeypatch.setattr(NovelProject, "close", spy)
        monkeypatch.setattr(
            "noveltrans.translators.get_translator",
            lambda *a, **k: (_ for _ in ()).throw(NovelTransError("boom")),
        )
        RewriteWorker(path, engine_name="claude").run()
        assert closes == [True]


class TestHappyPath:
    def test_every_translated_chapter_is_rewritten_and_backed_up(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        done: list[int] = []
        finished: list[tuple[int, int]] = []
        worker = RewriteWorker(path, engine_name="cli")
        worker.chapter_done.connect(done.append)
        worker.finished_ok.connect(lambda ok, bad: finished.append((ok, bad)))
        worker.run()

        assert done == [0, 1]
        assert finished == [(2, 0)]  # a `system=` kwarg or a translate() call would error
        project = opened(path)
        for idx in (0, 1):
            chapter = project.chapter(idx)
            assert chapter.is_rewritten
            assert "tư vị khó nói" in chapter.translated
            assert chapter.translated_raw == CONVERT
            # restyled, not re-translated: engine and language are untouched
            assert chapter.translator == "Google Translate"
            assert chapter.target_lang == "vi"
        assert not project.chapter(2).is_rewritten  # never translated, never eligible

    def test_a_second_run_skips_what_it_already_did(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        RewriteWorker(path, engine_name="cli").run()

        again: list[int] = []
        worker = RewriteWorker(path, engine_name="cli")
        worker.chapter_done.connect(again.append)
        worker.run()
        assert again == []

    def test_force_re_rewrites_but_keeps_the_first_backup(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        RewriteWorker(path, engine_name="cli").run()
        RewriteWorker(path, engine_name="cli", force=True, end_idx=0).run()

        chapter = opened(path).chapter(0)
        assert chapter.translated_raw == CONVERT  # still the pre-rewrite text

    def test_explicit_indices_select_exactly_those_chapters(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        RewriteWorker(path, engine_name="cli", indices=[1]).run()

        project = opened(path)
        assert not project.chapter(0).is_rewritten
        assert project.chapter(1).is_rewritten

    def test_a_chapter_range_is_honoured(self, qapp, library_dir, monkeypatch, opened):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        RewriteWorker(path, engine_name="cli", start_idx=1).run()

        project = opened(path)
        assert not project.chapter(0).is_rewritten
        assert project.chapter(1).is_rewritten


class TestFailureIsNeverWritten:
    def test_a_summarising_engine_leaves_the_translation_intact(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """The feature's hard rule, end to end.

        A rewrite that loses a paragraph must not reach the database: the alternative to
        failing is the perfectly good translation already there.
        """
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_summarise))
        errors: list[tuple[int, str]] = []
        done: list[int] = []
        finished: list[tuple[int, int]] = []
        worker = RewriteWorker(path, engine_name="cli")
        worker.chapter_error.connect(lambda idx, msg: errors.append((idx, msg)))
        worker.chapter_done.connect(done.append)
        worker.finished_ok.connect(lambda ok, bad: finished.append((ok, bad)))
        worker.run()

        assert done == []
        assert finished == [(0, 2)]
        # the batch kept going after the first failure
        assert [idx for idx, _ in errors] == [0, 1]
        assert "số đoạn" in errors[0][1]

        project = opened(path)
        for idx in (0, 1):
            chapter = project.chapter(idx)
            assert chapter.translated == CONVERT  # byte for byte
            assert chapter.translated_title == f"Chương {idx + 1}"
            assert not chapter.is_rewritten
            assert chapter.status == "error"
            assert "số đoạn" in chapter.error

    def test_an_unexpected_engine_crash_is_contained_to_one_chapter(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)

        def explode(text: str) -> str:
            if "Phó Thanh Từ" in text:
                raise RuntimeError("engine đứt")
            return text

        _use(monkeypatch, _FakeEngine(explode))
        finished: list[tuple[int, int]] = []
        worker = RewriteWorker(path, engine_name="cli")
        worker.finished_ok.connect(lambda ok, bad: finished.append((ok, bad)))
        worker.run()

        assert finished == [(0, 2)]
        assert opened(path).chapter(0).translated == CONVERT


class TestPreview:
    def test_dry_run_emits_the_result_and_writes_nothing(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        previews: list[tuple[int, str, str]] = []
        worker = RewriteWorker(path, engine_name="cli", indices=[0], dry_run=True)
        worker.preview_ready.connect(lambda i, t, b: previews.append((i, t, b)))
        worker.run()

        assert len(previews) == 1
        index, title, body = previews[0]
        assert index == 0
        assert title == "Chương 1"
        assert "tư vị khó nói" in body

        chapter = opened(path).chapter(0)
        assert chapter.translated == CONVERT
        assert not chapter.is_rewritten

    def test_a_failed_preview_does_not_mark_the_chapter_errored(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_summarise))
        errors: list[int] = []
        worker = RewriteWorker(path, engine_name="cli", indices=[0], dry_run=True)
        worker.chapter_error.connect(lambda idx, msg: errors.append(idx))
        worker.run()

        assert errors == [0]  # the user is told
        chapter = opened(path).chapter(0)
        assert chapter.status != "error"  # …but nothing is written, not even the error
        assert chapter.error == ""


class TestCancel:
    def test_cancel_stops_after_the_chapter_in_flight(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(_polish))
        worker = RewriteWorker(path, engine_name="cli")
        worker.chapter_done.connect(lambda _: worker.cancel())
        worker.run()

        project = opened(path)
        assert project.chapter(0).is_rewritten
        assert not project.chapter(1).is_rewritten

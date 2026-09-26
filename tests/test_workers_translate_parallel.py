"""Feature 097 — translating 2+ chapters at once.

`translate_workers` gates which loop `TranslateWorker.run()` takes: `_run_sequential`
(== 1, a literal move of the pre-097 loop, covered by `test_workers_qc.py`) or
`_run_parallel` (> 1, new). These tests only exercise what `_run_parallel` adds: a
thread pool, one engine instance per pool thread (`_ctx_for_thread`), and the
per-chapter `Đang dịch` signal — not the QC/name-fix/title-only logic itself, which is
identical code shared with the sequential path and already covered there.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noveltrans.gui.workers import TranslateWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject
from noveltrans.translators.base import Translator

SOURCE = "他看了她一眼然后转身离开，心中涌起一阵难以言喻的悲伤。" * 12
GOOD_VI = (
    "Hắn nhìn nàng một cái rồi quay đầu bỏ đi, trong lòng dâng lên một nỗi buồn khó tả. "
) * 12
CHINESE_TITLE = "第1章 夜雨孤灯"
GOOD_TITLE = "Chương 1: Đèn cô độc đêm mưa"


class _ThreadTrackingEngine(Translator):
    """Records which thread each `translate()` call ran on. A short sleep gives a
    sibling pool thread time to start before this one loops back for more work — without
    it, nothing stops one fast thread from draining the whole queue by itself."""

    name = "fake"
    display_name = "Fake"
    max_chunk_chars = 100_000

    def __init__(self, calls: list[int], lock: threading.Lock):
        self._calls = calls
        self._lock = lock

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        if text == CHINESE_TITLE:
            return GOOD_TITLE
        if not text.startswith(SOURCE[:20]):
            return text  # short, non-chapter text (a title, the meta title/desc, …)
        # Only a real chapter BODY is timed and recorded: the novel's own meta
        # title/description is translated up front, on the orchestrator thread, before
        # any pool thread exists, and would otherwise masquerade as a third "worker".
        time.sleep(0.03)
        with self._lock:
            self._calls.append(threading.get_ident())
        return GOOD_VI


def _tracking_get_translator(calls: list[int], builds: list[int], build_lock: threading.Lock):
    def factory(*_args, **_kwargs):
        with build_lock:
            builds.append(threading.get_ident())
        return _ThreadTrackingEngine(calls, build_lock)

    return factory


def _project(library_dir: Path, n: int) -> Path:
    meta = NovelMeta(url="https://x/truyen", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}") for i in range(n)]
    project = NovelProject.create(library_dir, meta, refs)
    for idx in range(n):
        project.save_content(idx, SOURCE)
    path = project.path
    project.close()
    return path


def _title_failed_project(library_dir: Path, extra_pending: int) -> Path:
    """One chapter flagged for a title-only retranslation, plus N plain-pending ones."""
    meta = NovelMeta(url="https://x/t", site="x", title="Truyện", source_lang="zh")
    refs = [
        ChapterRef(index=0, title=CHINESE_TITLE, url="https://x/1"),
        *(
            ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
            for i in range(1, extra_pending + 1)
        ),
    ]
    project = NovelProject.create(library_dir, meta, refs)
    for ref in refs:
        project.save_content(ref.index, SOURCE)
    project.save_translation(0, CHINESE_TITLE, GOOD_VI, "vi", "CLI (agy)")
    project.mark_qc_failed(
        0, "title_untranslated", "tiêu đề còn chữ Hán", project.chapter(0).qc_fingerprint()
    )
    path = project.path
    project.close()
    return path


def _opened(path: Path) -> NovelProject:
    return NovelProject.open(path)


class TestTranslateWorkersConcurrency:
    def test_one_worker_never_leaves_the_calling_thread(self, qapp, library_dir, monkeypatch):
        """`translate_workers=1` must take the old, single-threaded path — no pool, no
        extra engine build."""
        path = _project(library_dir, 4)
        calls: list[int] = []
        builds: list[int] = []
        monkeypatch.setattr(
            "noveltrans.translators.get_translator",
            _tracking_get_translator(calls, builds, threading.Lock()),
        )

        worker = TranslateWorker(path, "fake", "vi", translate_workers=1)
        worker.run()

        project = _opened(path)
        try:
            assert all(project.chapter(i).is_translated for i in range(4))
        finally:
            project.close()
        assert len(builds) == 1  # only the eager, up-front build
        assert set(calls) == {threading.get_ident()}  # every call ran on this thread

    def test_two_workers_use_two_threads_and_two_engines(self, qapp, library_dir, monkeypatch):
        """`translate_workers=2` must actually overlap: two pool threads, each with its
        own engine instance (see `_ctx_for_thread` — a shared `GoogleFreeTranslator`
        would otherwise race on its own request-delay throttle)."""
        path = _project(library_dir, 4)
        calls: list[int] = []
        builds: list[int] = []
        monkeypatch.setattr(
            "noveltrans.translators.get_translator",
            _tracking_get_translator(calls, builds, threading.Lock()),
        )
        started: list[int] = []

        worker = TranslateWorker(path, "fake", "vi", translate_workers=2)
        worker.chapter_started.connect(started.append)
        worker.run()

        project = _opened(path)
        try:
            assert all(project.chapter(i).is_translated for i in range(4))
        finally:
            project.close()
        assert sorted(started) == [0, 1, 2, 3]  # every chapter reported "Đang dịch"
        assert len(builds) == 2  # one eager + one more, for the second pool thread
        assert len(set(calls)) == 2  # both threads actually translated a chapter

    def test_two_workers_still_resolve_a_title_only_fix_correctly(
        self, qapp, library_dir, monkeypatch
    ):
        """The per-thread fallback title chain (`_ctx_for_thread`, no QC configured) is
        new code, exercised only under `_run_parallel` — this is its only test."""
        path = _title_failed_project(library_dir, extra_pending=2)
        calls: list[int] = []
        builds: list[int] = []
        monkeypatch.setattr(
            "noveltrans.translators.get_translator",
            _tracking_get_translator(calls, builds, threading.Lock()),
        )

        worker = TranslateWorker(
            path, "fake", "vi", indices=[0, 1, 2], title_only={0}, translate_workers=2,
        )
        worker.run()

        project = _opened(path)
        try:
            chapter = project.chapter(0)
            assert chapter.translated_title == GOOD_TITLE
            assert chapter.translated == GOOD_VI  # body untouched — no engine call for it
            assert chapter.qc_ok
            assert project.chapter(1).is_translated
            assert project.chapter(2).is_translated
        finally:
            project.close()

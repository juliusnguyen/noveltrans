"""Feature 077 — AudioWorker records a fingerprint of the text it actually voiced.

The integration point where staleness could silently start comparing the wrong thing: if
what gets SPOKEN and what gets HASHED are derived separately, every chapter is either
permanently stale or permanently fresh and no test of the predicate alone would notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.gui.workers import AudioWorker
from noveltrans.models import AUDIO_SOURCE_ORIGINAL, Chapter, ChapterRef, NovelMeta
from noveltrans.storage.project import NovelProject

VOICE = "Ngọc Lan"


class FakeEngine:
    """Writes a file and records the (title, text) it was asked to speak."""

    def __init__(self):
        self.spoken: list[tuple[str, str]] = []

    def synthesize_chapter(self, title, text, out_path, **kwargs):
        self.spoken.append((title, text))
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"RIFF")
        return 12.0


@pytest.fixture
def project(library_dir) -> NovelProject:
    meta = NovelMeta(url="https://example.com/n/1", site="example", title="测试小说")
    refs = [ChapterRef(index=0, title="第1章 标题", url="https://example.com/n/1/0")]
    project = NovelProject.create(library_dir, meta, refs)
    project.save_content(0, "原文。")
    project.save_translation(0, "Chương 1", "Bản dịch.", "vi", "Google")
    return project


def _run(project, use_translation=True) -> tuple[AudioWorker, FakeEngine]:
    worker = AudioWorker(project.path, voice=VOICE, use_translation=use_translation)
    engine = FakeEngine()
    source = "translated" if use_translation else AUDIO_SOURCE_ORIGINAL
    worker._run_sequential(project, engine, [project.chapter(0)], source)
    return worker, engine


class TestSequential:
    def test_the_stored_hash_matches_the_text_that_was_spoken(self, qapp, project):
        _, engine = _run(project)
        spoken_title, spoken_text = engine.spoken[0]
        # Rebuild the fingerprint from what the engine was actually handed, not from the
        # chapter — that is the comparison that catches the two sides drifting apart.
        voiced = Chapter(index=0, title="x", url="u",
                         translated_title=spoken_title, translated=spoken_text)
        assert project.chapter(0).audio_text_hash == voiced.audio_fingerprint(True)

    def test_the_chapter_is_not_stale_straight_after_generating(self, qapp, project):
        _run(project)
        assert not project.chapter(0).audio_is_stale

    def test_editing_the_text_afterwards_makes_it_stale(self, qapp, project):
        _run(project)
        project.edit_translation(0, text="Bản dịch đã sửa.")
        assert project.chapter(0).audio_is_stale

    def test_the_original_source_fingerprints_the_original(self, qapp, project):
        _, engine = _run(project, use_translation=False)
        assert engine.spoken[0] == ("第1章 标题", "原文。")
        assert not project.chapter(0).audio_is_stale


class TestNoDrift:
    @pytest.mark.parametrize("use_translation", [True, False])
    def test_the_voiced_pair_is_the_fingerprinted_pair(self, qapp, use_translation):
        # _title_text_for must stay a delegate. A second copy of the "which text?" rule in
        # the worker is exactly how this would rot.
        worker = AudioWorker(Path("x"), voice=VOICE, use_translation=use_translation)
        chapter = Chapter(
            index=0, title="第1章", url="u", content="原文。",
            translated="Bản dịch.", translated_title="Chương 1",
        )
        assert worker._title_text_for(chapter) == chapter.audio_source_text(use_translation)

"""Feature 077 — `pending_audio` re-queues a chapter whose text changed.

The model-level predicate is covered in `test_audio_staleness.py`. This is the half that
makes it act: a stale chapter has to come back out of `pending_audio`, or "Tạo audio tất
cả" keeps skipping it and the mark is decoration.
"""

from __future__ import annotations

import pytest

from noveltrans.models import (
    AUDIO_SOURCE_DOWNLOADED,
    AUDIO_SOURCE_ORIGINAL,
    ChapterRef,
    NovelMeta,
)
from noveltrans.storage.project import NovelProject

VOICE = "Ngọc Lan"


@pytest.fixture
def meta() -> NovelMeta:
    return NovelMeta(url="https://example.com/n/1", site="example", title="测试小说")


@pytest.fixture
def project(library_dir, meta) -> NovelProject:
    refs = [
        ChapterRef(index=i, title=f"第{i + 1}章 标题", url=f"https://example.com/n/1/{i}")
        for i in range(3)
    ]
    project = NovelProject.create(library_dir, meta, refs)
    for idx in range(3):
        project.save_content(idx, f"原文{idx}。")
        project.save_translation(idx, f"Chương {idx + 1}", f"Bản dịch {idx}.", "vi", "Google")
    return project


def _voice_it(project: NovelProject, idx: int, source: str = "translated") -> None:
    """Generate audio for one chapter the way AudioWorker does, fingerprint included."""
    chapter = project.chapter(idx)
    project.save_audio(
        idx,
        f"exports/audio/{idx + 1:04d}.wav",
        VOICE,
        12.0,
        source,
        text_hash=chapter.audio_fingerprint(source != AUDIO_SOURCE_ORIGINAL),
    )


def _pending(project: NovelProject, use_translation: bool = True) -> list[int]:
    return [c.index for c in project.pending_audio(VOICE, use_translation)]


class TestStaleReQueues:
    def test_a_freshly_voiced_chapter_is_not_pending(self, project):
        _voice_it(project, 0)
        assert 0 not in _pending(project)

    def test_editing_the_translation_puts_it_back(self, project):
        _voice_it(project, 0)
        project.edit_translation(0, text="Bản dịch đã sửa.")
        assert 0 in _pending(project)

    def test_editing_the_translated_title_puts_it_back(self, project):
        _voice_it(project, 0)
        project.edit_translation(0, title="Chương 1: Tên mới")
        assert 0 in _pending(project)

    def test_a_rewrite_puts_it_back(self, project):
        # The point of a fingerprint over a per-writer flag: save_rewrite never heard of
        # this feature and is covered anyway.
        _voice_it(project, 0)
        project.save_rewrite(0, "Chương 1", "Bản viết lại.")
        assert 0 in _pending(project)

    def test_a_find_replace_pass_puts_it_back(self, project):
        _voice_it(project, 0)
        project.apply_replacements({0: {"translated": "Bản dịch đã thay."}})
        assert 0 in _pending(project)

    def test_editing_a_different_chapter_does_not_disturb_this_one(self, project):
        _voice_it(project, 0)
        _voice_it(project, 1)
        project.edit_translation(1, text="Chỉ sửa chương này.")
        pending = _pending(project)
        assert 1 in pending and 0 not in pending

    def test_the_result_stays_in_chapter_order_without_duplicates(self, project):
        _voice_it(project, 0)
        _voice_it(project, 1)
        project.edit_translation(0, text="Sửa.")
        # 0 is stale, 1 is fresh, 2 was never voiced — one entry each, ascending, because
        # AudioWorker walks this list in order.
        assert _pending(project) == [0, 2]


class TestWhatMustNotGoStale:
    def test_audio_predating_fingerprints_stays_done(self, project):
        # The upgrade case: an existing library has no hashes, and re-queueing every
        # voiced chapter would look like the app had lost the whole novel's audio.
        project.save_audio(0, "exports/audio/0001.wav", VOICE, 12.0, "translated")
        project.edit_translation(0, text="Bản dịch đã sửa.")
        assert 0 not in _pending(project)

    def test_downloaded_narration_stays_done(self, project):
        project.save_audio(0, "exports/audio/nguon.mp3", VOICE, 12.0, AUDIO_SOURCE_DOWNLOADED)
        project.edit_translation(0, text="Bản dịch đã sửa.")
        assert 0 not in _pending(project)

    def test_editing_the_translation_leaves_original_voiced_audio_alone(self, project):
        _voice_it(project, 0, AUDIO_SOURCE_ORIGINAL)
        project.edit_translation(0, text="Bản dịch đã sửa.")
        assert 0 not in _pending(project, use_translation=False)

    def test_editing_the_original_re_queues_original_voiced_audio(self, project):
        _voice_it(project, 0, AUDIO_SOURCE_ORIGINAL)
        project.edit_content(0, "原文改。")
        assert 0 in _pending(project, use_translation=False)


class TestPersistence:
    def test_the_fingerprint_survives_a_reopen(self, project):
        _voice_it(project, 0)
        path = project.path
        project.close()
        reopened = NovelProject.open(path)
        assert reopened.chapter(0).audio_text_hash
        assert not reopened.chapter(0).audio_is_stale

    def test_clear_audio_drops_the_fingerprint_too(self, project):
        _voice_it(project, 0)
        project.clear_audio()
        chapter = project.chapter(0)
        assert chapter.audio_text_hash == ""
        assert not chapter.audio_is_stale  # no audio at all now, so nothing to be stale

    def test_a_db_written_before_this_feature_gains_the_column(self, project):
        # A fresh DB gets the column from _SCHEMA, which would pass with no migration at
        # all. So drop the column back out to make a genuine pre-077 file, then reopen.
        path = project.path
        project._db.execute("ALTER TABLE chapters DROP COLUMN audio_text_hash")
        project._db.commit()
        assert "audio_text_hash" not in {
            r[1] for r in project._db.execute("PRAGMA table_info(chapters)")
        }
        project.close()

        reopened = NovelProject.open(path)
        assert "audio_text_hash" in {
            r[1] for r in reopened._db.execute("PRAGMA table_info(chapters)")
        }
        # and the rows are intact, defaulting to "unknown, assume fresh"
        assert reopened.chapter(0).translated == "Bản dịch 0."
        assert reopened.chapter(0).audio_text_hash == ""

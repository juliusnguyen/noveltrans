
import json

import pytest

from noveltrans.models import (
    STATUS_DOWNLOADED,
    STATUS_PENDING,
    STATUS_TRANSLATED,
    ChapterRef,
)
from noveltrans.storage import Library, NovelProject
from noveltrans.storage.project import slugify


class TestSlugify:
    def test_ascii(self):
        assert slugify("My Great Novel!") == "my-great-novel"

    def test_cjk_falls_back(self):
        assert slugify("测试小说") == "novel"

    def test_mixed(self):
        assert slugify("测试 Test Novel 123") == "test-novel-123"

    def test_vietnamese_diacritics(self):
        assert slugify("Đấu Phá Thương Khung") == "dau-pha-thuong-khung"
        assert slugify("Truyện Thử") == "truyen-thu"


class TestProjectLifecycle:
    def test_create_seeds_chapters(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        chapters = project.chapters()
        assert len(chapters) == 5
        assert all(c.status == STATUS_PENDING for c in chapters)
        assert chapters[0].title == "第1章"
        assert project.exports_dir.is_dir()

    def test_create_writes_readable_meta(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        text = (project.path / "meta.json").read_text(encoding="utf-8")
        assert "测试小说" in text  # ensure_ascii=False keeps CJK readable

    def test_open_roundtrip(self, library_dir, sample_meta, sample_refs):
        created = NovelProject.create(library_dir, sample_meta, sample_refs)
        created.save_content(0, "原文内容")
        created.close()

        reopened = NovelProject.open(created.path)
        assert reopened.meta.title == sample_meta.title
        assert reopened.chapter(0).content == "原文内容"

    def test_rescan_preserves_content_and_adds_chapters(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "已下载")
        new_refs = sample_refs + [
            ChapterRef(index=5, title="第6章", url="https://example.com/novel/123/6")
        ]
        project.replace_toc(new_refs)
        chapters = project.chapters()
        assert len(chapters) == 6
        assert chapters[0].content == "已下载"


class TestResumeQueries:
    def test_pending_download_shrinks(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        assert len(project.pending_download()) == 5
        project.save_content(0, "text")
        project.save_content(2, "text")
        pending = project.pending_download()
        assert [c.index for c in pending] == [1, 3, 4]

    def test_pending_download_from_a_start_index(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        # "download from chapter 3" (idx 2) → only pending chapters at idx >= 2
        assert [c.index for c in project.pending_download(2)] == [2, 3, 4]

    def test_pending_download_within_a_range(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(2, "text")  # already downloaded → skipped even in-range
        assert [c.index for c in project.pending_download(1, 3)] == [1, 3]

    def test_chapters_in_range_ignores_status(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(2, "text")  # downloaded — still included (force path)
        assert [c.index for c in project.chapters_in_range(1, 3)] == [1, 2, 3]
        assert [c.index for c in project.chapters_in_range(3)] == [3, 4]

    def test_pending_translation_requires_content(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        assert project.pending_translation("vi") == []
        project.save_content(0, "原文")
        assert [c.index for c in project.pending_translation("vi")] == [0]

    def test_pending_translation_language_change(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "Chương 1", "bản dịch", "vi")
        assert project.pending_translation("vi") == []
        # switching target language re-pends the row
        assert [c.index for c in project.pending_translation("en")] == [0]

    def test_status_transitions(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        assert project.chapter(0).status == STATUS_DOWNLOADED
        project.save_translation(0, "t", "dịch", "vi")
        assert project.chapter(0).status == STATUS_TRANSLATED

    def test_save_translation_records_translator(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "t", "dịch", "vi", translator="CLI (agy)", seconds=12.5)
        assert project.chapter(0).translator == "CLI (agy)"
        assert project.chapter(0).translate_seconds == 12.5
        # re-translating with another engine overwrites the record
        project.save_translation(0, "t", "dịch 2", "vi", translator="Google Translate")
        assert project.chapter(0).translator == "Google Translate"

    def test_edit_translation_keeps_engine_metadata(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "Chương 1", "bản dịch", "vi", translator="CLI (agy)", seconds=3.0)

        project.edit_translation(0, title="Chương Một")
        chapter = project.chapter(0)
        assert chapter.translated_title == "Chương Một"
        assert chapter.translated == "bản dịch"  # text untouched

        project.edit_translation(0, text="bản dịch sửa tay")
        chapter = project.chapter(0)
        assert chapter.translated == "bản dịch sửa tay"
        assert chapter.translated_title == "Chương Một"  # title untouched
        # engine metadata and status survive manual edits
        assert chapter.translator == "CLI (agy)"
        assert chapter.translate_seconds == 3.0
        assert chapter.status == STATUS_TRANSLATED
        assert chapter.target_lang == "vi"

    def test_edit_translation_without_fields_is_noop(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "t", "dịch", "vi")
        before = project.chapter(0)
        project.edit_translation(0)
        after = project.chapter(0)
        assert after == before

    def test_edit_content_keeps_status_and_translation(
        self, library_dir, sample_meta, sample_refs
    ):
        # The key contrast with save_content, which would flip status back to
        # DOWNLOADED and re-queue the chapter for translation.
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文 Lâm Phong")
        project.save_translation(0, "Chương 1", "dịch", "vi", translator="CLI (agy)", seconds=3.0)

        project.edit_content(0, "原文 Diệp Vân")
        chapter = project.chapter(0)
        assert chapter.content == "原文 Diệp Vân"
        assert chapter.status == STATUS_TRANSLATED  # NOT reset to DOWNLOADED
        assert chapter.translated == "dịch"  # translation untouched
        assert chapter.translator == "CLI (agy)"

    def test_edit_content_bumps_updated_at(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        before = project.chapter(0).updated_at
        project.edit_content(0, "原文 sửa")
        after = project.chapter(0).updated_at
        assert after != "" and after >= before


class TestApplyReplacements:
    def test_writes_multiple_columns_across_chapters(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "Lâm Phong đến")
        project.save_translation(0, "Lâm Phong", "Lâm Phong tới", "vi")
        project.save_content(1, "Lâm Phong đi")

        project.apply_replacements(
            {
                0: {"content": "Diệp Vân đến", "translated": "Diệp Vân tới",
                    "translated_title": "Diệp Vân"},
                1: {"content": "Diệp Vân đi"},
            }
        )
        assert project.chapter(0).content == "Diệp Vân đến"
        assert project.chapter(0).translated == "Diệp Vân tới"
        assert project.chapter(0).translated_title == "Diệp Vân"
        assert project.chapter(1).content == "Diệp Vân đi"

    def test_leaves_status_untouched(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "Lâm Phong")
        project.save_translation(0, "t", "Lâm Phong", "vi")
        project.apply_replacements({0: {"translated": "Diệp Vân"}})
        assert project.chapter(0).status == STATUS_TRANSLATED

    def test_can_write_the_original_title(self, library_dir, sample_meta, sample_refs):
        # title is opted-in scope (the GUI warns it reverts on re-scan).
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.apply_replacements({0: {"title": "Tựa đề mới"}})
        assert project.chapter(0).title == "Tựa đề mới"

    def test_rejects_a_non_editable_column(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        # status is not in the whitelist — must not be writable via find/replace.
        with pytest.raises(ValueError, match="non-editable"):
            project.apply_replacements({0: {"status": "hacked"}})

    def test_empty_changes_is_a_noop(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        before = project.chapter(0)
        project.apply_replacements({})
        project.apply_replacements({0: {}})  # empty column dict skipped
        assert project.chapter(0) == before

    def test_a_bad_column_rolls_back_valid_writes_in_the_same_batch(
        self, library_dir, sample_meta, sample_refs
    ):
        # The whole point of one transaction: no half-applied replace across a novel.
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "orig0")
        with pytest.raises(ValueError):
            project.apply_replacements({0: {"content": "NEW0"}, 1: {"status": "bad"}})
        assert project.chapter(0).content == "orig0"  # ch.0's valid write rolled back

    def test_clear_translations_resets_translator(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "t", "dịch", "vi", translator="CLI (agy)", seconds=9.0)
        project.clear_translations()
        assert project.chapter(0).translator == ""
        assert project.chapter(0).translate_seconds == 0

    def test_clear_translations_can_be_narrowed_to_some_chapters(
        self, library_dir, sample_meta, sample_refs
    ):
        """Feature 071's repair drops only the chapters translated from bad source text —
        the rest of the novel, including any hand edits, must survive untouched."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for idx in (0, 1, 2):
            project.save_content(idx, "原文")
            project.save_translation(idx, "t", f"dịch {idx}", "vi")

        assert project.clear_translations([1]) == 1

        assert project.chapter(0).translated == "dịch 0"
        assert project.chapter(1).translated == ""
        assert project.chapter(2).translated == "dịch 2"

    def test_clear_translations_with_no_indices_still_clears_everything(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for idx in (0, 1):
            project.save_content(idx, "原文")
            project.save_translation(idx, "t", "dịch", "vi")

        project.clear_translations()

        assert all(project.chapter(i).translated == "" for i in (0, 1))

    def test_a_narrow_clear_drops_the_rewrite_backup_too(
        self, library_dir, sample_meta, sample_refs
    ):
        """A backup of a translation being discarded must go with it, or the chapter stays
        flagged as rewritten with an undo pointing at text that no longer exists."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "t", "dịch", "vi")
        project.save_rewrite(0, "t", "viết lại")

        project.clear_translations([0])

        assert project.chapter(0).translated_raw == ""

    def test_an_empty_index_list_clears_nothing(
        self, library_dir, sample_meta, sample_refs
    ):
        """`[]` means "no chapters", not "every chapter" — the difference is a whole novel."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "t", "dịch", "vi")

        assert project.clear_translations([]) == 0
        assert project.chapter(0).translated == "dịch"

    def test_counts(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "a")
        project.save_translation(0, "t", "d", "vi")
        project.save_content(1, "b")
        project.mark_error(2, "boom")
        assert project.counts() == {
            "total": 5,
            "downloaded": 2,
            "translated": 1,
            "rewritten": 0,
            "errors": 1,
            "audio": 0,
            "downloaded_audio": 0,
        }


class TestMigration:
    def test_open_pre_translator_db_adds_column(self, library_dir, sample_meta, sample_refs):
        import sqlite3

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        path = project.path
        project.close()

        # simulate a chapters.db created before the newer columns existed
        db = sqlite3.connect(path / "chapters.db")
        for column in (
            "translator",
            "translate_seconds",
            "translated_raw",
            "translated_title_raw",
            "audio_path",
            "audio_voice",
            "audio_seconds",
            "audio_error",
        ):
            db.execute(f"ALTER TABLE chapters DROP COLUMN {column}")
        db.commit()
        db.close()

        reopened = NovelProject.open(path)
        assert reopened.chapter(0).translator == ""
        assert reopened.chapter(0).translate_seconds == 0
        assert reopened.chapter(0).audio_path == ""
        assert reopened.chapter(0).translated_raw == ""
        assert reopened.chapter(0).translated_title_raw == ""
        assert not reopened.chapter(0).is_rewritten
        reopened.save_translation(0, "t", "dịch", "vi", translator="CLI (agy)", seconds=3.0)
        assert reopened.chapter(0).translator == "CLI (agy)"
        assert reopened.chapter(0).translate_seconds == 3.0
        # the rewrite pass must work on a migrated DB, not just a freshly created one
        reopened.save_rewrite(0, "t", "dịch lại")
        assert reopened.chapter(0).is_rewritten
        assert reopened.chapter(0).translated_raw == "dịch"


class TestRewriteState:
    """Feature 060 — the style-rewrite backup columns and the queries over them."""

    def _translated(self, library_dir, sample_meta, sample_refs, count: int = 3):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for idx in range(count):
            project.save_content(idx, f"原文{idx}")
            project.save_translation(
                idx, f"Chương {idx}", f"convert {idx}", "vi", translator="Google Translate"
            )
        return project

    def test_save_rewrite_backs_up_and_leaves_the_translation_metadata_alone(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "Chương 0 hay hơn", "văn phong tự nhiên")
        chapter = project.chapter(0)
        assert chapter.translated == "văn phong tự nhiên"
        assert chapter.translated_title == "Chương 0 hay hơn"
        assert chapter.translated_raw == "convert 0"
        assert chapter.translated_title_raw == "Chương 0"
        assert chapter.is_rewritten
        # the chapter is still the same translation, by the same engine, in the same
        # language — only its prose changed
        assert chapter.translator == "Google Translate"
        assert chapter.target_lang == "vi"

    def test_a_second_rewrite_keeps_the_original_backup(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "lần 1", "viết lại lần 1")
        project.save_rewrite(0, "lần 2", "viết lại lần 2")
        chapter = project.chapter(0)
        assert chapter.translated == "viết lại lần 2"
        assert chapter.translated_raw == "convert 0"  # NOT "viết lại lần 1"
        assert chapter.translated_title_raw == "Chương 0"

    def test_restore_translation_puts_the_original_back_and_clears_the_flag(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "Chương 0 hay hơn", "văn phong tự nhiên")
        assert project.restore_translation(0) == 1
        chapter = project.chapter(0)
        assert chapter.translated == "convert 0"
        assert chapter.translated_title == "Chương 0"
        assert chapter.translated_raw == ""
        assert chapter.translated_title_raw == ""
        assert not chapter.is_rewritten

    def test_restore_translation_whole_novel_reports_how_many_it_restored(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "a", "A")
        project.save_rewrite(2, "c", "C")
        assert project.restore_translation() == 2
        assert [c.translated for c in project.chapters()][:3] == [
            "convert 0",
            "convert 1",
            "convert 2",
        ]

    def test_restore_translation_is_a_no_op_on_a_chapter_never_rewritten(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        assert project.restore_translation(1) == 0
        assert project.chapter(1).translated == "convert 1"

    def test_undo_after_a_rewrite_makes_the_chapter_eligible_again(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "a", "A")
        assert 0 not in [c.index for c in project.pending_rewrite("vi")]
        project.restore_translation(0)
        assert 0 in [c.index for c in project.pending_rewrite("vi")]

    def test_pending_rewrite_skips_rewritten_and_untranslated_chapters(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(1, "b", "B")
        assert [c.index for c in project.pending_rewrite("vi")] == [0, 2]

    def test_pending_rewrite_honours_the_chapter_range(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        assert [c.index for c in project.pending_rewrite("vi", 1, 1)] == [1]
        assert [c.index for c in project.pending_rewrite("vi", 1)] == [1, 2]

    def test_pending_rewrite_skips_another_target_language(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        assert project.pending_rewrite("en") == []

    def test_pending_rewrite_accepts_a_legacy_row_with_no_target_lang(
        self, library_dir, sample_meta, sample_refs
    ):
        project = self._translated(library_dir, sample_meta, sample_refs)
        project._db.execute("UPDATE chapters SET target_lang = '' WHERE idx = 0")
        project._db.commit()
        assert 0 in [c.index for c in project.pending_rewrite("vi")]

    def test_a_rewritten_chapter_is_not_re_flagged_for_translation(
        self, library_dir, sample_meta, sample_refs
    ):
        # If save_rewrite ever touched target_lang, this would queue the whole novel for
        # a re-translation the user never asked for.
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "a", "A")
        assert 0 not in [c.index for c in project.pending_translation("vi")]

    def test_clear_translations_drops_the_backup_too(
        self, library_dir, sample_meta, sample_refs
    ):
        # Otherwise "Dịch lại từ đầu" leaves every chapter flagged as rewritten, with an
        # undo that restores text belonging to a translation that no longer exists.
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.save_rewrite(0, "a", "A")
        project.clear_translations()
        chapter = project.chapter(0)
        assert chapter.translated_raw == ""
        assert chapter.translated_title_raw == ""
        assert not chapter.is_rewritten

    def test_counts_reports_rewritten_chapters(self, library_dir, sample_meta, sample_refs):
        project = self._translated(library_dir, sample_meta, sample_refs)
        assert project.counts()["rewritten"] == 0
        project.save_rewrite(0, "a", "A")
        project.save_rewrite(2, "c", "C")
        counts = project.counts()
        assert counts["rewritten"] == 2
        assert counts["translated"] == 3  # a subset, not a sibling

    def test_a_failed_rewrite_leaves_the_translation_intact(
        self, library_dir, sample_meta, sample_refs
    ):
        # The worker calls mark_error instead of save_rewrite when validation fails; the
        # good translation must survive untouched.
        project = self._translated(library_dir, sample_meta, sample_refs)
        project.mark_error(0, "viết lại thất bại: số đoạn không khớp")
        chapter = project.chapter(0)
        assert chapter.translated == "convert 0"
        assert not chapter.is_rewritten
        assert "số đoạn" in chapter.error


class TestAudioState:
    def _translated_project(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "Chương 1", "bản dịch", "vi")
        return project

    def test_pending_audio_requires_translation(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        assert [c.index for c in project.pending_audio()] == [0]
        project.save_audio(0, "exports/audio/0001-chuong-1.wav", "Ngọc Lan", 123.4)
        assert project.pending_audio() == []

    def test_pending_audio_voice_change_repends(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_audio(0, "exports/audio/0001-chuong-1.wav", "Ngọc Lan", 123.4)
        assert project.pending_audio("Ngọc Lan") == []
        # switching voice re-pends the chapter (old audio gets replaced)
        assert [c.index for c in project.pending_audio("Gia Bảo")] == [0]
        # no voice given -> only missing audio counts
        assert project.pending_audio() == []

    def test_save_audio_roundtrip(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.mark_audio_error(0, "boom")
        assert project.chapter(0).audio_error == "boom"
        project.save_audio(0, "exports/audio/0001-chuong-1.wav", "Ngọc Lan", 123.4)
        chapter = project.chapter(0)
        assert chapter.audio_path == "exports/audio/0001-chuong-1.wav"
        assert chapter.audio_voice == "Ngọc Lan"
        assert chapter.audio_seconds == 123.4
        assert chapter.audio_error == ""  # save clears a previous error
        assert chapter.has_audio
        assert project.counts()["audio"] == 1

    def test_clear_audio_can_be_narrowed_to_some_chapters(
        self, library_dir, sample_meta, sample_refs
    ):
        """Feature 071: `pending_audio` re-queues on an empty path or a voice/source
        mismatch, never on the translation changing — so a repair that did not clear audio
        would leave the video pipeline consuming audio read from the bad translation."""
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        for idx in (0, 1):
            project.save_audio(idx, f"exports/audio/{idx}.wav", "Ngọc Lan", 9.0)

        assert project.clear_audio(indices=[0]) == 1

        assert not project.chapter(0).has_audio
        assert project.chapter(1).has_audio

    def test_clear_audio(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_audio(0, "exports/audio/0001-chuong-1.wav", "Ngọc Lan", 9.0)
        project.clear_audio()
        chapter = project.chapter(0)
        assert not chapter.has_audio
        assert chapter.audio_voice == "" and chapter.audio_seconds == 0
        # translation state untouched
        assert chapter.translated == "bản dịch"

    def test_pending_audio_excludes_downloaded(self, library_dir, sample_meta, sample_refs):
        """Downloaded narration must not be perpetually pending.

        audio_source = "downloaded" never equals the "translated"/"original" the
        voice-mismatch clause tests, so without the guard every bulk pass would re-voice
        it with TTS — and AudioWorker's stale-file cleanup would then delete the
        downloaded file. Regression-pins the guard, not just the query.
        """
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_audio(
            0, "exports/audio/0001-tieuthuyetmang.m4a", "tieuthuyetmang", 900.0, "downloaded"
        )
        assert project.pending_audio() == []
        assert project.pending_audio("Ngọc Lan") == []
        # ...and still excluded when the caller asks about the other text source
        assert project.pending_audio("Ngọc Lan", use_translation=False) == []
        # opt back in only when the caller explicitly means to overwrite narration
        assert [c.index for c in project.pending_audio("Ngọc Lan", include_downloaded=True)] == [0]

    def test_clear_audio_spares_downloaded(self, library_dir, sample_meta, sample_refs):
        """"Tạo lại từ đầu" must not forget where downloaded narration came from.

        Nothing re-fetches it, and the user may no longer be entitled to, so clearing the
        row would orphan the file with no record of its origin.
        """
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_content(1, "nội dung")
        project.save_translation(1, "Chương 2", "bản dịch 2", "vi")
        project.save_audio(0, "exports/audio/0001-tts.wav", "Ngọc Lan", 9.0)
        project.save_audio(
            1, "exports/audio/0002-tieuthuyetmang.m4a", "tieuthuyetmang", 900.0, "downloaded"
        )
        project.clear_audio()
        assert not project.chapter(0).has_audio  # synthesised: cleared as before
        kept = project.chapter(1)
        assert kept.has_audio and kept.audio_voice == "tieuthuyetmang"
        assert kept.audio_seconds == 900.0
        # a deliberate "forget the downloads too" still works
        project.clear_audio(include_downloaded=True)
        assert not project.chapter(1).has_audio

    def test_counts_reports_downloaded_audio(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_audio(0, "exports/audio/0001-tts.wav", "Ngọc Lan", 9.0)
        counts = project.counts()
        assert counts["audio"] == 1 and counts["downloaded_audio"] == 0
        project.save_audio(
            0, "exports/audio/0001-tieuthuyetmang.m4a", "tieuthuyetmang", 900.0, "downloaded"
        )
        counts = project.counts()
        # a subset of "audio", not a sibling of it
        assert counts["audio"] == 1 and counts["downloaded_audio"] == 1

    def test_audio_dir(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        assert project.audio_dir == project.exports_dir / "audio"

    def test_pending_audio_original_uses_content(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "nội dung gốc")  # downloaded, NOT translated
        # translation source sees nothing pending; original source sees the chapter
        assert project.pending_audio(use_translation=True) == []
        assert [c.index for c in project.pending_audio(use_translation=False)] == [0]

    def test_save_audio_records_source(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "nội dung gốc")
        project.save_audio(0, "exports/audio/0001.wav", "Ngọc Lan", 5.0, source="original")
        assert project.chapter(0).audio_source == "original"

    def test_switching_source_repends(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)  # has both
        project.save_audio(0, "exports/audio/0001.wav", "Ngọc Lan", 5.0, source="translated")
        # same source+voice → not pending; the other source re-pends the chapter
        assert project.pending_audio("Ngọc Lan", use_translation=True) == []
        assert [c.index for c in project.pending_audio("Ngọc Lan", use_translation=False)] == [0]

    def test_legacy_audio_defaults_to_translated(self, library_dir, sample_meta, sample_refs):
        # audio saved before the source column existed back-fills to 'translated', so
        # the translation query does NOT needlessly re-pend it
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_audio(0, "exports/audio/0001.wav", "Ngọc Lan", 5.0)  # no source arg
        assert project.chapter(0).audio_source == "translated"
        assert project.pending_audio("Ngọc Lan", use_translation=True) == []


class TestMetaTranslation:
    def test_save_and_reload(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_meta_translation("Truyện Thử", "Mô tả tiếng Việt.", "vi")
        assert project.meta.translated_title == "Truyện Thử"
        project.close()

        reopened = NovelProject.open(project.path)
        assert reopened.meta.translated_title == "Truyện Thử"
        assert reopened.meta.translated_description == "Mô tả tiếng Việt."
        assert reopened.meta.translated_lang == "vi"
        # original fields untouched
        assert reopened.meta.title == sample_meta.title


class TestReloadMeta:
    def test_picks_up_translation_from_another_instance(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        other = NovelProject.open(project.path)  # e.g. the translate tab's handle
        other.save_meta_translation("Truyện Thử", "Mô tả tiếng Việt.", "vi")
        other.close()

        assert project.meta.translated_title == ""  # stale in-memory copy
        reloaded = project.reload_meta()
        assert reloaded.translated_title == "Truyện Thử"
        assert project.meta.translated_description == "Mô tả tiếng Việt."


class TestErrorHandling:
    def test_mark_error_and_reset(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.mark_error(0, "translate failed")
        project.mark_error(1, "download failed")
        assert len(project.errored()) == 2

        project.reset_errors()
        assert project.errored() == []
        # chapter 0 has content -> back to downloaded; chapter 1 -> pending
        assert project.chapter(0).status == STATUS_DOWNLOADED
        assert project.chapter(1).status == STATUS_PENDING

    def test_save_content_clears_error(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.mark_error(0, "boom")
        project.save_content(0, "text")
        chapter = project.chapter(0)
        assert chapter.status == STATUS_DOWNLOADED
        assert chapter.error == ""


class TestLibrary:
    def test_create_and_list(self, library_dir, sample_meta, sample_refs):
        library = Library(library_dir)
        project = library.create_project(sample_meta, sample_refs)
        assert library.list_projects() == [project.path]
        assert library.project_meta(project.path).title == sample_meta.title

    def test_find_by_url(self, library_dir, sample_meta, sample_refs):
        library = Library(library_dir)
        project = library.create_project(sample_meta, sample_refs)
        assert library.find_by_url(sample_meta.url) == project.path
        assert library.find_by_url("https://other.example") is None

    def test_ignores_non_project_dirs(self, library_dir, sample_meta, sample_refs):
        library = Library(library_dir)
        (library.root / "random-folder").mkdir()
        library.create_project(sample_meta, sample_refs)
        assert len(library.list_projects()) == 1

    def test_delete_project(self, library_dir, sample_meta, sample_refs):
        library = Library(library_dir)
        project = library.create_project(sample_meta, sample_refs)
        project.close()
        library.delete_project(project.path)
        assert library.list_projects() == []

    def test_delete_refuses_non_project(self, library_dir, sample_meta, sample_refs):
        library = Library(library_dir)
        stray = library.root / "not-a-project"
        stray.mkdir()
        with pytest.raises(ValueError):
            library.delete_project(stray)
        assert stray.exists()

    def test_delete_refuses_outside_library(self, tmp_path, sample_meta, sample_refs):
        library = Library(tmp_path / "lib-a")
        other = Library(tmp_path / "lib-b")
        project = other.create_project(sample_meta, sample_refs)
        project.close()
        with pytest.raises(ValueError):
            library.delete_project(project.path)
        assert project.path.exists()


class TestDisplayTitle:
    """Feature 035 — the title override used on video output.

    The whole point of the field is stripping a source tag like `[ĐM/EDIT] ` from what
    the viewer sees, without touching the scraped metadata underneath.
    """

    def test_falls_back_through_override_then_translated_then_original(self):
        from noveltrans.models import NovelMeta

        meta = NovelMeta(url="u", site="s", title="原标题")
        assert meta.display_name() == "原标题"
        meta.translated_title = "[ĐM/EDIT] CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG"
        assert meta.display_name().startswith("[ĐM/EDIT]")
        meta.display_title = "CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG"
        assert meta.display_name() == "CHÀO MỪNG ĐẾN VỚI PHÒNG LIVESTREAM ÁC MỘNG"

    def test_a_blank_override_falls_through_rather_than_blanking_the_title(self):
        from noveltrans.models import NovelMeta

        meta = NovelMeta(url="u", site="s", title="原标题", translated_title="Tên đã dịch")
        meta.display_title = "   "
        assert meta.display_name() == "Tên đã dịch"

    def test_an_old_meta_json_without_the_key_still_loads(self):
        """`from_dict` filters unknown keys, so a project written before 035 opens fine
        and reads as "no override"."""
        from noveltrans.models import NovelMeta

        meta = NovelMeta.from_dict(
            {"url": "u", "site": "s", "title": "原标题", "translated_title": "Tên đã dịch"}
        )
        assert meta.display_title == ""
        assert meta.display_name() == "Tên đã dịch"

    def test_save_display_title_persists_and_leaves_the_slug_source_alone(
        self, library_dir, sample_meta, sample_refs
    ):
        """**The invariant this feature turns on.**

        `slugify(translated_title or title)` decides `video_dir/<stem>/<stem>.mp4` and
        every sidecar beside it, including the `<stem>.upload.json` that feature 034 uses
        to know what is already on YouTube. If editing the display title moved that,
        rendered parts would read as "chưa tạo" and the app would offer to re-upload
        videos already live on the channel.
        """
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_meta_translation("[ĐM/EDIT] Tên Truyện", "mô tả", "vi")
        before = slugify(project.meta.translated_title or project.meta.title)

        project.save_display_title("Tên Truyện")
        after = slugify(project.meta.translated_title or project.meta.title)
        assert after == before
        assert project.meta.translated_title == "[ĐM/EDIT] Tên Truyện"
        assert project.meta.display_name() == "Tên Truyện"

        path = project.path
        project.close()
        assert NovelProject.open(path).meta.display_title == "Tên Truyện"

    def test_saving_an_empty_override_clears_it(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_display_title("Tên Truyện")
        project.save_display_title("")
        path = project.path
        project.close()
        assert NovelProject.open(path).meta.display_title == ""


class TestSlugName:
    """The pinned filename stem (074).

    `slug_name()` is what every generated file is named after. The first test is the
    no-migration guarantee: an unpinned novel must resolve to exactly the pre-074 stem, or
    upgrading the app silently orphans every rendered part on disk.
    """

    def test_an_unpinned_novel_resolves_to_the_pre_074_stem(self, sample_meta):
        from noveltrans.slug import slugify

        assert sample_meta.slug == ""
        assert sample_meta.slug_name() == slugify(sample_meta.title)
        sample_meta.translated_title = "Tiểu Thuyết Thử Nghiệm"
        assert sample_meta.slug_name() == slugify("Tiểu Thuyết Thử Nghiệm")

    def test_a_cjk_only_title_still_falls_back_to_novel(self):
        from noveltrans.models import NovelMeta

        assert NovelMeta(url="u", site="s", title="测试小说").slug_name() == "novel"

    def test_a_pinned_stem_survives_a_later_retranslation(self, library_dir, sample_meta):
        """The hazard the pin exists to remove.

        Before 074 the stem was derived from `translated_title`, so re-translating into a
        different language moved it — orphaning every rendered part and every
        `.upload.json` with no error and no way back short of renaming files by hand.
        """
        project = NovelProject.create(library_dir, sample_meta, [])
        project.pin_slug("tieu-thuyet-thu-nghiem")
        project.save_meta_translation("A Completely Different Title", "desc", "en")
        assert project.meta.slug_name() == "tieu-thuyet-thu-nghiem"
        assert NovelProject.open(project.path).meta.slug_name() == "tieu-thuyet-thu-nghiem"
        project.close()

    def test_pin_slug_never_repoints_an_already_pinned_stem(self, library_dir, sample_meta):
        project = NovelProject.create(library_dir, sample_meta, [])
        project.pin_slug("first")
        project.pin_slug("second")
        assert project.meta.slug == "first"
        project.close()

    def test_an_old_meta_json_without_the_key_still_loads(self, library_dir, sample_meta):
        project = NovelProject.create(library_dir, sample_meta, [])
        path = project.path / "meta.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["slug"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        project.close()
        assert NovelProject.open(path.parent).meta.slug == ""


class TestRenameNovel:
    def test_it_writes_the_name_and_the_stem_together(self, library_dir, sample_meta):
        project = NovelProject.create(library_dir, sample_meta, [])
        project.rename_novel("Trọng Sinh", pin_slug="trong-sinh")
        # One meta.json write, not two: a crash between them would leave the novel named
        # but stemless, and every rendered part looking for a stem nobody records.
        data = json.loads((project.path / "meta.json").read_text(encoding="utf-8"))
        assert data["display_title"] == "Trọng Sinh"
        assert data["slug"] == "trong-sinh"
        project.close()

    def test_save_display_title_pins_the_stem_the_novel_already_uses(
        self, library_dir, sample_meta, sample_refs
    ):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_meta_translation("[ĐM/EDIT] Tên Truyện", "mô tả", "vi")
        before = project.meta.slug_name()
        project.save_display_title("Tên Truyện")
        assert project.meta.slug_name() == before  # files stay exactly where they are
        assert project.meta.display_name() == "Tên Truyện"
        project.close()

    def test_it_leaves_the_scraped_and_translated_titles_alone(
        self, library_dir, sample_meta
    ):
        project = NovelProject.create(library_dir, sample_meta, [])
        project.save_meta_translation("Tên đã dịch", "mô tả", "vi")
        project.rename_novel("Tên mới", pin_slug="ten-moi")
        assert project.meta.title == sample_meta.title
        assert project.meta.translated_title == "Tên đã dịch"
        project.close()

    def test_a_rescan_does_not_undo_a_rename(self, library_dir, sample_meta):
        """`refresh_meta` overwrites `title` on every re-scan — which is exactly why the
        editable name is `display_title` and not `title`."""
        from noveltrans.models import NovelMeta

        project = NovelProject.create(library_dir, sample_meta, [])
        project.rename_novel("Tên mới", pin_slug="ten-moi")
        project.refresh_meta(
            NovelMeta(url=sample_meta.url, site="fake", title="新标题", author="tác giả")
        )
        assert project.meta.display_title == "Tên mới"
        assert project.meta.slug == "ten-moi"
        project.close()

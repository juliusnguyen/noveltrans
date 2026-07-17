
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
            "errors": 1,
            "audio": 0,
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
        reopened.save_translation(0, "t", "dịch", "vi", translator="CLI (agy)", seconds=3.0)
        assert reopened.chapter(0).translator == "CLI (agy)"
        assert reopened.chapter(0).translate_seconds == 3.0


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

    def test_clear_audio(self, library_dir, sample_meta, sample_refs):
        project = self._translated_project(library_dir, sample_meta, sample_refs)
        project.save_audio(0, "exports/audio/0001-chuong-1.wav", "Ngọc Lan", 9.0)
        project.clear_audio()
        chapter = project.chapter(0)
        assert not chapter.has_audio
        assert chapter.audio_voice == "" and chapter.audio_seconds == 0
        # translation state untouched
        assert chapter.translated == "bản dịch"

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

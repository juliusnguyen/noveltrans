
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
        db.execute("ALTER TABLE chapters DROP COLUMN translator")
        db.execute("ALTER TABLE chapters DROP COLUMN translate_seconds")
        db.commit()
        db.close()

        reopened = NovelProject.open(path)
        assert reopened.chapter(0).translator == ""
        assert reopened.chapter(0).translate_seconds == 0
        reopened.save_translation(0, "t", "dịch", "vi", translator="CLI (agy)", seconds=3.0)
        assert reopened.chapter(0).translator == "CLI (agy)"
        assert reopened.chapter(0).translate_seconds == 3.0


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

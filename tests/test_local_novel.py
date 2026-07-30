"""Novels the user writes themselves — identity, chapter add/delete, status."""

from __future__ import annotations

from noveltrans.models import (
    STATUS_DOWNLOADED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSLATED,
    NovelMeta,
    new_local_url,
)
from noveltrans.scrapers import HttpClient, adapter_for_url
from noveltrans.storage import Library, NovelProject
from noveltrans.tts.merge import plan_merge_windows


def _local_meta(title: str = "Truyện của tôi") -> NovelMeta:
    return NovelMeta(url="", site="", title=title, source_lang="vi")


# ------------------------------------------------------------------- identity


def test_two_local_novels_with_the_same_title_get_different_folders(library_dir):
    # The whole reason the URL is a uuid rather than "": the project folder hashes
    # meta.url, so a blank URL would give both novels the same folder and merge the
    # second one's chapters into the first one's chapters.db.
    library = Library(library_dir)
    first = library.create_local_project(_local_meta())
    second = library.create_local_project(_local_meta())
    assert first.path != second.path
    first.close()
    second.close()
    assert len(library.list_projects()) == 2


def test_create_local_project_marks_site_and_url(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    assert project.meta.site == "local"
    assert project.meta.url.startswith("local://")
    assert project.meta.is_local
    project.close()
    # …and survives a round-trip through meta.json
    assert NovelProject.open(project.path).meta.is_local


def test_a_scraped_meta_is_not_local(sample_meta):
    assert not sample_meta.is_local


def test_is_local_holds_if_either_field_survives():
    assert NovelMeta(url="", site="local", title="T").is_local
    assert NovelMeta(url=new_local_url(), site="", title="T").is_local


def test_no_adapter_ever_claims_a_local_url():
    # If one did, the download path would try to fetch a novel that only exists on disk.
    assert adapter_for_url(new_local_url(), HttpClient(delay_seconds=0)) is None


def test_find_by_url_refuses_a_blank_url(library_dir, sample_meta, sample_refs):
    library = Library(library_dir)
    library.create_project(sample_meta, sample_refs).close()
    assert library.find_by_url("") is None


def test_find_by_url_finds_a_local_project(library_dir):
    library = Library(library_dir)
    project = library.create_local_project(_local_meta())
    url, path = project.meta.url, project.path
    project.close()
    assert library.find_by_url(url) == path


# --------------------------------------------------------------- add chapters


def test_add_chapters_on_an_empty_project_starts_at_zero(library_dir):
    # MAX(idx) over an empty table is NULL, not 0 — a fresh local novel has no rows.
    project = Library(library_dir).create_local_project(_local_meta())
    assert project.add_chapters(["Chương 1"]) == [0]
    assert [c.index for c in project.chapters()] == [0]
    project.close()


def test_add_chapters_appends_strips_and_drops_blanks(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["A"])
    assert project.add_chapters(["  B  ", "", "   ", "C"]) == [1, 2]
    assert [c.title for c in project.chapters()] == ["A", "B", "C"]
    project.close()


def test_add_chapters_with_nothing_usable_writes_nothing(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    assert project.add_chapters(["", "   "]) == []
    assert project.chapters() == []
    project.close()


def test_new_chapters_have_no_url_and_start_pending(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1"])
    chapter = project.chapter(0)
    assert chapter.url == ""  # the column is NOT NULL — never None
    assert chapter.status == STATUS_PENDING
    project.close()


# ------------------------------------------------------------ delete chapters


def test_delete_chapter_removes_the_row_and_returns_it(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["A", "B", "C"])
    deleted = project.delete_chapter(1)
    assert deleted.title == "B"
    assert [c.index for c in project.chapters()] == [0, 2]  # a gap, on purpose
    project.close()


def test_delete_chapter_on_a_missing_index_returns_none(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    assert project.delete_chapter(7) is None
    project.close()


def test_add_after_deleting_a_middle_chapter_does_not_fill_the_gap(library_dir):
    # Filling idx 1 would adopt the deleted chapter's audio filename (0002-…) as this
    # chapter's, and its slot in any already-rendered video part.
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["A", "B", "C"])
    project.delete_chapter(1)
    assert project.add_chapters(["D"]) == [3]
    assert [c.index for c in project.chapters()] == [0, 2, 3]
    project.close()


def test_deleting_the_last_chapter_frees_its_number_for_reuse(library_dir):
    # The documented limit of MAX(idx)+1: nothing remembers a trimmed tail, so the next
    # chapter takes the number back. Safe because the tab unlinks the deleted chapter's
    # audio and cues, so there is no stale take left for the new chapter to inherit.
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["A", "B", "C"])
    project.delete_chapter(2)
    assert project.add_chapters(["D"]) == [2]
    project.close()


def test_merge_windows_still_batch_by_chapter_number_across_a_gap(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters([f"C{i}" for i in range(1, 5)])
    for idx in range(4):
        project.save_audio(idx, f"audio/{idx}.mp3", "v1", 1.0, source="original")
    project.delete_chapter(1)
    windows = plan_merge_windows(project.chapters(), "v1", "batch", batch=2)
    # Batches are the number ranges 1..2 and 3..4. The missing chapter 2 leaves the
    # first batch holding one chapter instead of pulling chapter 3 back into it.
    assert [(w.first_num, w.last_num) for w in windows] == [(1, 1), (3, 4)]
    assert [[c.index for c in w.chapters] for w in windows] == [[0], [2, 3]]
    project.close()


# ------------------------------------------------- edit_content status promotion


def test_edit_content_promotes_pending_to_downloaded(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1"])
    project.edit_content(0, "Nội dung tôi viết.")
    assert project.chapter(0).status == STATUS_DOWNLOADED
    project.close()


def test_edit_content_promotes_error_to_downloaded(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1"])
    project.mark_error(0, "boom")
    project.edit_content(0, "Nội dung.")
    assert project.chapter(0).status == STATUS_DOWNLOADED
    project.close()


def test_edit_content_never_demotes_a_translated_chapter(library_dir, sample_meta, sample_refs):
    # A text correction is not a re-download; demoting would re-queue it for translation.
    project = NovelProject.create(library_dir, sample_meta, sample_refs)
    project.save_content(0, "原文")
    project.save_translation(0, "Tên", "Bản dịch", "vi")
    project.edit_content(0, "原文 đã sửa")
    assert project.chapter(0).status == STATUS_TRANSLATED
    project.close()


def test_clearing_content_leaves_the_status_alone(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1"])
    project.edit_content(0, "")
    assert project.chapter(0).status == STATUS_PENDING
    project.close()


def test_edit_content_feeds_counts_and_the_audio_queue(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1", "Chương 2"])
    project.edit_content(0, "Nội dung tôi viết.")
    assert project.counts()["downloaded"] == 1
    pending = project.pending_audio("v1", use_translation=False)
    assert [c.index for c in pending] == [0]
    project.close()


def test_edit_content_queues_the_chapter_for_translation(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1"])
    project.edit_content(0, "Nội dung.")
    assert [c.index for c in project.pending_translation("en")] == [0]
    project.close()


def test_edit_content_survives_a_reopen(library_dir):
    project = Library(library_dir).create_local_project(_local_meta())
    project.add_chapters(["Chương 1", "Chương 2"])
    project.edit_content(1, "Nội dung chương hai.")
    path = project.path
    project.close()

    reopened = NovelProject.open(path)
    assert reopened.meta.is_local
    chapter = reopened.chapter(1)
    assert chapter.content == "Nội dung chương hai."
    assert chapter.status == STATUS_DOWNLOADED
    assert reopened.chapter(0).status == STATUS_PENDING
    assert STATUS_ERROR not in {c.status for c in reopened.chapters()}
    reopened.close()

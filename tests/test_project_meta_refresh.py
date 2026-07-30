"""A re-scan must write corrected metadata back, not only the chapter list.

Feature 046: a project created while a site adapter was buggy kept the wrong title and
author for ever — `ScanWorker` called `replace_toc` and nothing else, so re-scanning
appeared to do nothing at all and the only cure was deleting the project.
"""

from __future__ import annotations

import json

from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage.project import META_FILE, NovelProject


def _project(tmp_path) -> NovelProject:
    meta = NovelMeta(
        url="https://tieuthuyetmang.com/truyen/abc",
        site="tieuthuyetmang",
        title="Truyện Sai",
        author="Tác Giả Sai",
        description="mô tả sai",
        cover_url="https://img.example.com/wrong.jpg",
        source_lang="vi",
    )
    refs = [ChapterRef(index=0, title="Chương 1", url="https://x/doc/1")]
    return NovelProject.create(tmp_path / "abc", meta, refs)


def _fresh() -> NovelMeta:
    return NovelMeta(
        url="https://tieuthuyetmang.com/truyen/abc",
        site="tieuthuyetmang",
        title="Truyện Đúng",
        author="Tác Giả Đúng",
        description="mô tả đúng",
        cover_url="https://img.example.com/right.jpg",
        source_lang="vi",
    )


def test_the_scraped_fields_are_written_to_disk(tmp_path):
    project = _project(tmp_path)
    project.refresh_meta(_fresh())
    stored = json.loads((project.path / META_FILE).read_text(encoding="utf-8"))
    assert stored["title"] == "Truyện Đúng"
    assert stored["author"] == "Tác Giả Đúng"
    assert stored["description"] == "mô tả đúng"
    assert stored["cover_url"].endswith("right.jpg")
    project.close()


def test_reopening_sees_the_corrected_metadata(tmp_path):
    project = _project(tmp_path)
    path = project.path  # create() derives the folder name from the title + url digest
    project.refresh_meta(_fresh())
    project.close()
    assert NovelProject.open(path).meta.title == "Truyện Đúng"


def test_work_this_app_produced_is_never_overwritten(tmp_path):
    """The translated title/description, the tag list and the thumbnail prompt cost real
    time and money to make, and a scan knows nothing about them — a fresh `NovelMeta`'s
    blank defaults must not land on top of them."""
    project = _project(tmp_path)
    project.save_meta_translation("Tựa đã dịch", "mô tả đã dịch", "vi", author="Dịch giả")
    project.save_tags("tag1,tag2")
    project.save_thumbnail_prompt("a moody alley at night")

    project.refresh_meta(_fresh())

    stored = json.loads((project.path / META_FILE).read_text(encoding="utf-8"))
    assert stored["title"] == "Truyện Đúng"  # scraped field replaced
    assert stored["translated_title"] == "Tựa đã dịch"  # our own work kept
    assert stored["translated_description"] == "mô tả đã dịch"
    assert stored["translated_author"] == "Dịch giả"
    assert stored["tags"] == "tag1,tag2"
    assert stored["thumbnail_prompt"] == "a moody alley at night"
    project.close()


def test_the_scan_worker_refreshes_an_existing_project(tmp_path):
    """The wiring, not just the method: this is the line whose absence caused the bug."""
    import inspect

    from noveltrans.gui import workers

    source = inspect.getsource(workers.ScanWorker.run)
    assert "refresh_meta(meta)" in source
    assert source.index("replace_toc") < source.index("refresh_meta")

"""Tab 2 (Dịch) — editing the "Bản gốc" pane.

This is how a hand-written novel gets its text, so the pane has to save as reliably as
the translated one beside it — and must not invent a rename nobody asked for.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QTextCursor

from noveltrans.config import AppConfig
from noveltrans.gui.tab_translate import TranslateTab
from noveltrans.models import NovelMeta
from noveltrans.storage import Library, NovelProject


@pytest.fixture
def local_tab(qapp, library_dir):
    config = AppConfig()
    config.library_dir = library_dir
    project = Library(library_dir).create_local_project(
        NovelMeta(url="", site="", title="Truyện của tôi", source_lang="vi")
    )
    project.add_chapters(["Chương 1", "Chương 2"])
    path = str(project.path)
    project.close()
    tab = TranslateTab(config)
    tab._on_project_selected(path)
    return tab


def _type(tab, text: str) -> None:
    """Replace the pane's contents the way a user would.

    Not `setPlainText`: that CLEARS the document's modified flag, so the save-on-blur
    guard would (correctly) decide nothing had been touched and every test would pass
    for the wrong reason. Editing through the cursor marks it modified, like typing.
    """
    cursor = tab.original_view.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    cursor.insertText(text)


def _blur(tab) -> None:
    tab.eventFilter(tab.original_view, QEvent(QEvent.Type.FocusOut))


def test_pane_starts_read_only_and_opens_when_a_chapter_loads(local_tab):
    assert local_tab.original_view.isReadOnly()
    local_tab._load_preview(local_tab.project.chapter(0))
    assert not local_tab.original_view.isReadOnly()


def test_loading_a_chapter_leaves_nothing_pending_to_save(local_tab):
    # If a freshly loaded pane counted as modified, the first blur would write back text
    # nobody touched — and stamp a rename onto every chapter merely visited.
    local_tab._load_preview(local_tab.project.chapter(0))
    assert not local_tab.original_view.document().isModified()


def test_typed_text_is_saved_on_focus_out(local_tab):
    path = local_tab.project.path
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nHôm nay trời đẹp.")
    _blur(local_tab)

    reopened = NovelProject.open(path)
    chapter = reopened.chapter(0)
    assert chapter.content == "Hôm nay trời đẹp."
    assert chapter.status == "downloaded"  # no longer reads "Chưa tải"
    reopened.close()


def test_a_body_without_a_blank_line_still_splits_on_the_first_line(local_tab):
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\nMột dòng duy nhất.")
    _blur(local_tab)
    assert local_tab.project.chapter(0).content == "Một dòng duy nhất."


def test_editing_the_first_line_renames_the_chapter(local_tab):
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1: Khởi đầu\n\nNội dung.")
    _blur(local_tab)

    chapter = local_tab.project.chapter(0)
    assert chapter.title == "Chương 1: Khởi đầu"
    assert chapter.title_custom
    assert chapter.title_source == "Chương 1"  # the rename stays undoable


def test_an_unchanged_first_line_does_not_mark_a_rename(local_tab):
    # edit_title sets title_custom, which puts "Lấy lại tên gốc" in the context menu.
    # Calling it on every blur would offer that on chapters nobody renamed.
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nNội dung.")
    _blur(local_tab)
    assert not local_tab.project.chapter(0).title_custom


def test_clearing_the_pane_saves_nothing(local_tab):
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nNội dung.")
    _blur(local_tab)
    _type(local_tab, "   ")
    _blur(local_tab)
    assert local_tab.project.chapter(0).content == "Nội dung."


def test_switching_rows_flushes_the_pending_edit(local_tab):
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nChưa rời ô.")
    local_tab._on_row_selected(local_tab.model.index(1, 0), None)
    assert local_tab.project.chapter(0).content == "Chưa rời ô."


def test_switching_projects_flushes_the_pending_edit(local_tab, library_dir):
    path = local_tab.project.path
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nLưu trước khi đổi truyện.")
    local_tab._on_project_selected("")

    reopened = NovelProject.open(path)
    assert reopened.chapter(0).content == "Lưu trước khi đổi truyện."
    reopened.close()


def test_deselecting_a_project_closes_the_pane(local_tab):
    local_tab._load_preview(local_tab.project.chapter(0))
    local_tab._on_project_selected("")
    assert local_tab.original_view.isReadOnly()


def test_shutdown_flushes_the_pending_edit(local_tab):
    path = local_tab.project.path
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nLưu khi đóng app.")
    local_tab.shutdown()

    reopened = NovelProject.open(path)
    assert reopened.chapter(0).content == "Lưu khi đóng app."
    reopened.close()


def test_editing_the_original_does_not_touch_the_translation(local_tab):
    local_tab.project.save_translation(0, "Tên dịch", "Bản dịch cũ", "vi")
    local_tab._load_preview(local_tab.project.chapter(0))
    _type(local_tab, "Chương 1\n\nBản gốc mới.")
    _blur(local_tab)

    chapter = local_tab.project.chapter(0)
    assert chapter.translated == "Bản dịch cũ"
    assert chapter.status == "translated"  # a correction is not a re-download

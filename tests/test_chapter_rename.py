"""Renaming a chapter in the Tải truyện tab.

The chapter title is not cosmetic here: it is what the export, the video and the TTS
narration all use. So a rename has to survive the thing that would most naturally undo it —
a re-scan, which is the normal way to pick up newly published chapters.
"""

from __future__ import annotations

import pytest

from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage.project import NovelProject


@pytest.fixture
def project(tmp_path) -> NovelProject:
    meta = NovelMeta(
        url="https://tieuthuyetmang.com/truyen/abc",
        site="tieuthuyetmang",
        title="Truyện Thử",
        source_lang="vi",
    )
    refs = [
        ChapterRef(index=i, title=f"[ YTB TẬP {i + 1} ] Chương {i + 1}", url=f"https://x/doc/{i + 1}")
        for i in range(3)
    ]
    return NovelProject.create(tmp_path, meta, refs)


def _titles(project: NovelProject) -> list[str]:
    return [chapter.title for chapter in project.chapters()]


class TestEditTitle:
    def test_a_rename_is_stored(self, project):
        project.edit_title(1, "Chương 2: Người khách")
        assert _titles(project)[1] == "Chương 2: Người khách"

    def test_a_rename_marks_the_row_as_custom(self, project):
        project.edit_title(1, "Chương 2: Người khách")
        assert project.chapter(1).title_custom is True
        assert project.chapter(0).title_custom is False

    def test_a_rename_does_not_touch_content_or_status(self, project):
        """Renaming is not a re-download: flipping status would re-queue the chapter."""
        project.save_content(1, "nội dung đã tải")
        before = project.chapter(1)
        project.edit_title(1, "Tên mới")
        after = project.chapter(1)
        assert after.content == before.content
        assert after.status == before.status

    def test_the_site_title_is_remembered(self, project):
        original = project.chapter(1).title
        project.edit_title(1, "Tên mới")
        assert project.chapter(1).title_source == original

    def test_a_second_rename_keeps_the_ORIGINAL_as_the_way_back(self, project):
        """If each edit recorded the previous one as "the original", the way back would be
        gone after the second rename."""
        original = project.chapter(1).title
        project.edit_title(1, "Tên tạm")
        project.edit_title(1, "Tên cuối")
        assert project.chapter(1).title_source == original


class TestRenameSurvivesRescan:
    def _rescan(self, project, suffix: str = "") -> None:
        project.replace_toc(
            [
                ChapterRef(
                    index=i,
                    title=f"[ YTB TẬP {i + 1} ] Chương {i + 1}{suffix}",
                    url=f"https://x/doc/{i + 1}",
                )
                for i in range(4)  # the site published one more
            ]
        )

    def test_a_renamed_chapter_keeps_its_name(self, project):
        """The point of the whole feature: re-scanning is how new chapters arrive, so a
        rename that a scan undoes would rarely survive a day."""
        project.edit_title(1, "Chương 2: Người khách")
        self._rescan(project)
        assert _titles(project)[1] == "Chương 2: Người khách"

    def test_untouched_chapters_still_take_the_sites_title(self, project):
        project.edit_title(1, "Chương 2: Người khách")
        self._rescan(project, suffix=" (đã sửa)")
        titles = _titles(project)
        assert titles[0].endswith("(đã sửa)")
        assert titles[2].endswith("(đã sửa)")

    def test_new_chapters_are_still_added(self, project):
        project.edit_title(1, "Chương 2: Người khách")
        self._rescan(project)
        assert len(project.chapters()) == 4

    def test_the_remembered_original_follows_the_site(self, project):
        """So "undo" gives the site's LATEST title, not one from months ago."""
        project.edit_title(1, "Chương 2: Người khách")
        self._rescan(project, suffix=" (đã sửa)")
        assert project.chapter(1).title_source.endswith("(đã sửa)")


class TestResetTitle:
    def test_it_puts_the_site_title_back(self, project):
        original = project.chapter(1).title
        project.edit_title(1, "Tên mới")
        project.reset_title(1)
        assert project.chapter(1).title == original

    def test_it_clears_the_custom_flag_so_scans_own_the_title_again(self, project):
        project.edit_title(1, "Tên mới")
        project.reset_title(1)
        assert project.chapter(1).title_custom is False

    def test_resetting_a_chapter_that_was_never_renamed_changes_nothing(self, project):
        original = project.chapter(0).title
        project.reset_title(0)
        assert project.chapter(0).title == original


class TestModelEditing:
    """The table model gates editing behind an explicit opt-in, because an editable cell
    whose edits nobody saves looks like it worked and loses the text on the next refresh."""

    def _model(self, editable: bool):
        from noveltrans.gui.widgets import ChapterTableModel
        from noveltrans.models import Chapter

        model = ChapterTableModel()
        model.set_chapters([Chapter(index=0, title="Chương 1", url="https://x/doc/1")])
        model.set_title_editable(editable)
        return model

    def test_the_title_cell_is_read_only_until_a_tab_opts_in(self, qapp):
        from PySide6.QtCore import Qt

        model = self._model(False)
        index = model.index(0, model.TITLE_COLUMN)
        assert not model.flags(index) & Qt.ItemFlag.ItemIsEditable

    def test_an_opted_in_table_can_edit_the_title(self, qapp):
        from PySide6.QtCore import Qt

        model = self._model(True)
        index = model.index(0, model.TITLE_COLUMN)
        assert model.flags(index) & Qt.ItemFlag.ItemIsEditable

    def test_editing_emits_the_index_and_the_new_title(self, qapp):
        model = self._model(True)
        seen: list[tuple[int, str]] = []
        model.title_edited.connect(lambda idx, title: seen.append((idx, title)))
        model.setData(model.index(0, model.TITLE_COLUMN), "  Chương 1: Mở đầu  ")
        assert seen == [(0, "Chương 1: Mở đầu")]  # whitespace stripped

    def test_a_blank_title_is_refused(self, qapp):
        """A blank name is a mis-edit, not an instruction — TTS would read out nothing."""
        model = self._model(True)
        seen: list = []
        model.title_edited.connect(lambda idx, title: seen.append(title))
        assert model.setData(model.index(0, model.TITLE_COLUMN), "   ") is False
        assert seen == []
        assert model.chapter_at(0).title == "Chương 1"

    def test_an_unchanged_title_is_not_written(self, qapp):
        model = self._model(True)
        seen: list = []
        model.title_edited.connect(lambda idx, title: seen.append(title))
        assert model.setData(model.index(0, model.TITLE_COLUMN), "Chương 1") is False
        assert seen == []

    def test_a_read_only_table_refuses_the_write_too(self, qapp):
        """Not only greyed out in the view — `setData` itself must refuse, or a
        programmatic write would slip past the opt-in."""
        model = self._model(False)
        assert model.setData(model.index(0, model.TITLE_COLUMN), "Tên mới") is False


class TestScrapeTabWiring:
    def test_the_download_tab_opts_in_and_persists(self):
        import inspect

        from noveltrans.gui import tab_scrape

        source = inspect.getsource(tab_scrape.ScrapeTab)
        assert "set_title_editable(True)" in source
        assert "title_edited.connect" in source
        assert "self.project.edit_title(idx, title)" in source

    def test_the_translate_tab_does_not_opt_in(self):
        """It has no listener for `title_edited`, so an editable cell there would silently
        discard the edit."""
        import inspect

        from noveltrans.gui import tab_translate

        assert "set_title_editable" not in inspect.getsource(tab_translate)


class TestEditorFits:
    """The editor must show the whole title while you type.

    The app stylesheet gives every QLineEdit `padding: 6px 9px` plus a border — right for
    a form field, ~14px too tall for a table row. Qt sizes an editor to the cell rect, so
    the styled editor's content area came out shorter than the text and the chapter name
    was clipped mid-glyph: visible, unreadable, and easy to mistake for lost text.
    """

    def _delegate_and_editor(self, qapp):
        """A real editable index — Qt returns no editor at all for an invalid one."""
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QStyleOptionViewItem, QWidget

        from noveltrans.gui.widgets import CellEditorDelegate, ChapterTableModel
        from noveltrans.models import Chapter

        model = ChapterTableModel()
        model.set_chapters([Chapter(index=0, title="Chương 1", url="https://x/doc/1")])
        model.set_title_editable(True)
        index = model.index(0, model.TITLE_COLUMN)

        parent = QWidget()
        delegate = CellEditorDelegate(parent)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 220, 20)  # a deliberately short row
        editor = delegate.createEditor(parent, option, index)
        return delegate, editor, option, index

    def test_the_editor_has_no_form_field_padding(self, qapp):
        _delegate, editor, _option, _index = self._delegate_and_editor(qapp)
        assert "padding: 0" in editor.styleSheet()

    def test_a_short_row_grows_the_editor_instead_of_clipping_it(self, qapp):
        delegate, editor, option, index = self._delegate_and_editor(qapp)
        delegate.updateEditorGeometry(editor, option, index)
        assert editor.geometry().height() >= editor.sizeHint().height()

    def test_a_tall_enough_row_is_left_alone(self, qapp):
        from PySide6.QtCore import QRect

        delegate, editor, option, index = self._delegate_and_editor(qapp)
        option.rect = QRect(0, 0, 220, editor.sizeHint().height() + 10)
        delegate.updateEditorGeometry(editor, option, index)
        assert editor.geometry() == option.rect

    def test_a_column_with_no_editor_is_handled(self, qapp):
        """Qt returns None there, and a delegate that assumes a widget crashes on the
        first right-click in a read-only column."""
        from PySide6.QtCore import QModelIndex, QRect
        from PySide6.QtWidgets import QStyleOptionViewItem, QWidget

        from noveltrans.gui.widgets import CellEditorDelegate

        parent = QWidget()  # held: a temporary parent is collected and takes the
        delegate = CellEditorDelegate(parent)  # delegate down with it
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 220, 20)
        assert delegate.createEditor(parent, option, QModelIndex()) is None
        delegate.updateEditorGeometry(None, option, QModelIndex())  # must not raise

    def test_both_chapter_tables_install_it(self):
        """Tên dịch in the Dịch tab has the same styled editor and the same short rows."""
        import inspect

        from noveltrans.gui import tab_scrape, tab_translate

        for module in (tab_scrape, tab_translate):
            assert "setItemDelegate(CellEditorDelegate" in inspect.getsource(module)

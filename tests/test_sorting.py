"""Sortable lists (074, item 3) — the cross-cutting guarantees.

The most important test here is `test_a_status_sort_still_acts_on_the_right_chapters`.
Chapter order is *data*: `plan_merge_windows`, `part_number`, the `{index+1:04d}` audio
filenames and `add_chapters`' MAX(idx)+1 rule all key on the chapter number. A view row is
a position on screen. They are the same number only until someone clicks a header — and
then "Tạo lại 30 chương" silently acts on thirty different chapters.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings, Qt

from noveltrans.config import AppConfig
from noveltrans.gui.widgets import (
    SORT_ROLE,
    AudioChapterTableModel,
    ChapterTableModel,
    SortableItem,
    format_duration,
    sorting_proxy,
    source_rows,
)
from noveltrans.models import (
    STATUS_DOWNLOADED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSLATED,
    Chapter,
)


def _chapters(n=12) -> list[Chapter]:
    """Twelve chapters whose display order and every sort order differ."""
    statuses = [STATUS_TRANSLATED, STATUS_PENDING, STATUS_ERROR, STATUS_DOWNLOADED]
    return [
        Chapter(
            index=i,
            title=f"Chương {i + 1}",
            url=f"https://x/{i}",
            content="nội dung",
            translated="bản dịch" if i % 2 else "",
            status=statuses[i % 4],
            translate_seconds=float((i * 37) % 200),
            error="hỏng" if statuses[i % 4] == STATUS_ERROR else "",
        )
        for i in range(n)
    ]


class TestSortKeys:
    """The displayed text does not sort; the SORT_ROLE key does."""

    def _model(self):
        model = ChapterTableModel()
        model.set_chapters(_chapters())
        return model

    def test_the_number_column_sorts_numerically(self):
        model = self._model()
        keys = [model.index(r, 0).data(SORT_ROLE) for r in range(model.rowCount())]
        assert keys == sorted(keys) == list(range(12))
        # As text it would be 1, 10, 11, 12, 2, ...
        shown = [str(model.index(r, 0).data()) for r in range(model.rowCount())]
        assert sorted(shown) != shown

    def test_duration_sorts_on_seconds_not_on_its_own_text(self):
        # "3m05s" < "10s" as text; 185 > 10 as a number.
        assert format_duration(185) > format_duration(10)
        model = self._model()
        column = ChapterTableModel.DURATION_COLUMN
        for row in range(model.rowCount()):
            assert model.index(row, column).data(SORT_ROLE) == pytest.approx(
                model._chapters[row].translate_seconds
            )

    def test_status_sorts_by_pipeline_stage_not_alphabetically(self):
        """"Lỗi" belongs beside "Chưa tải", not between "Đã tải" and "Đã dịch"."""
        model = self._model()
        column = ChapterTableModel.STATUS_COLUMN
        ranks = {}
        for row in range(model.rowCount()):
            ranks[model._chapters[row].status] = model.index(row, column).data(SORT_ROLE)
        assert ranks[STATUS_ERROR] < ranks[STATUS_PENDING] < ranks[STATUS_DOWNLOADED]
        assert ranks[STATUS_DOWNLOADED] < ranks[STATUS_TRANSLATED]

    def test_the_error_column_leads_with_whether_there_is_one(self):
        model = self._model()
        column = ChapterTableModel.ERROR_COLUMN
        keys = [model.index(r, column).data(SORT_ROLE) for r in range(model.rowCount())]
        assert all(isinstance(k, tuple) and isinstance(k[0], bool) for k in keys)

    def test_the_button_column_offers_no_key(self):
        model = self._model()
        assert model.index(0, ChapterTableModel.RETRANSLATE_COLUMN).data(SORT_ROLE) is None

    def test_the_audio_model_keys_chars_and_duration_numerically(self):
        model = AudioChapterTableModel()
        model.set_chapters(_chapters())
        assert isinstance(model.index(1, AudioChapterTableModel.CHARS_COLUMN).data(SORT_ROLE), int)
        assert isinstance(
            model.index(1, AudioChapterTableModel.DURATION_COLUMN).data(SORT_ROLE), float
        )


class TestSortedViewsStillActOnChapters:
    def _view(self, qapp):
        from PySide6.QtWidgets import QTableView

        model = ChapterTableModel()
        model.set_chapters(_chapters())
        view = QTableView()
        view.setModel(sorting_proxy(model, view))
        view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        return view, model

    def test_a_status_sort_still_acts_on_the_right_chapters(self, qapp):
        """**The promise of item 3.** Sort by status, select the top three rows, and the
        rows handed to a worker are those three chapters — in chapter order."""
        view, model = self._view(qapp)
        view.sortByColumn(ChapterTableModel.STATUS_COLUMN, Qt.SortOrder.AscendingOrder)
        proxy = view.model()
        selection = view.selectionModel()
        for row in range(3):
            selection.select(
                proxy.index(row, 0),
                selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
            )
        rows = source_rows(view)
        assert rows == sorted(rows)  # chapter order, never screen order
        # And every one of them really is a chapter that sorted to the top by status.
        top_rank = model.index(rows[0], ChapterTableModel.STATUS_COLUMN).data(SORT_ROLE)
        assert all(
            model.index(r, ChapterTableModel.STATUS_COLUMN).data(SORT_ROLE) == top_rank
            for r in rows
        )

    def test_reversing_the_sort_reverses_the_view_but_not_the_model(self, qapp):
        view, model = self._view(qapp)
        view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        proxy = view.model()
        assert proxy.index(0, 0).data() == 12  # last chapter on top
        assert model.chapter_at(0).index == 0  # the chapter list never moved

    def test_source_rows_dedups_the_cells_of_one_row(self, qapp):
        view, _model = self._view(qapp)
        proxy = view.model()
        selection = view.selectionModel()
        selection.select(
            proxy.index(4, 0), selection.SelectionFlag.Select | selection.SelectionFlag.Rows
        )
        assert len(selection.selectedIndexes()) > 1
        assert source_rows(view) == [4]

    def test_an_unproxied_view_still_works(self, qapp):
        """`source_index`/`source_rows` are identity without a proxy — so a table that
        never gains one is not broken by using them."""
        from PySide6.QtWidgets import QTableView

        model = ChapterTableModel()
        model.set_chapters(_chapters())
        view = QTableView()
        view.setModel(model)
        view.selectRow(3)
        assert source_rows(view) == [3]


class TestSortableItem:
    def test_it_sorts_by_the_key_not_by_the_text(self, qapp):
        from PySide6.QtWidgets import QTableWidget

        table = QTableWidget(3, 1)
        for row, part in enumerate((9, 100, 10)):
            table.setItem(row, 0, SortableItem(f"Phần {part}", part))
        table.sortItems(0)
        assert [table.item(r, 0).text() for r in range(3)] == [
            "Phần 9",
            "Phần 10",
            "Phần 100",
        ]

    def test_without_a_key_it_falls_back_to_the_text(self, qapp):
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

        table = QTableWidget(2, 1)
        table.setItem(0, 0, SortableItem("b"))
        table.setItem(1, 0, QTableWidgetItem("a"))
        table.sortItems(0)
        assert [table.item(r, 0).text() for r in range(2)] == ["a", "b"]


class TestSortPersistence:
    def _config(self, tmp_path) -> AppConfig:
        config = AppConfig()
        config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        return config

    def test_nothing_saved_reads_as_none(self, tmp_path):
        assert self._config(tmp_path).sort_state("chapters.scrape", 8) is None

    def test_it_round_trips(self, tmp_path):
        config = self._config(tmp_path)
        config.set_sort_state("chapters.scrape", 3, False, 8)
        assert config.sort_state("chapters.scrape", 8) == (3, False)

    def test_a_changed_column_count_drops_the_saved_state(self, tmp_path):
        """A saved sort is a column INDEX. Inserting a column in a future release would
        otherwise re-point it at a different meaning, invisibly."""
        config = self._config(tmp_path)
        config.set_sort_state("chapters.scrape", 3, True, 8)
        assert config.sort_state("chapters.scrape", 9) is None

    def test_corrupt_settings_read_as_none_rather_than_raising(self, tmp_path):
        config = self._config(tmp_path)
        config._s.setValue("sort/chapters.scrape", "rubbish")
        assert config.sort_state("chapters.scrape", 8) is None

    def test_each_list_is_remembered_separately(self, tmp_path):
        config = self._config(tmp_path)
        config.set_sort_state("chapters.scrape", 1, True, 8)
        config.set_sort_state("video.parts", 4, False, 7)
        assert config.sort_state("chapters.scrape", 8) == (1, True)
        assert config.sort_state("video.parts", 7) == (4, False)


class TestEnableTableSorting:
    """`setSortingEnabled(True)` alone leaves the header on DESCENDING, so every table
    would open backwards — chapter 12 above chapter 1. Caught only by real row numbers."""

    def _table(self, qapp):
        from PySide6.QtWidgets import QTableView

        model = ChapterTableModel()
        model.set_chapters(_chapters())
        view = QTableView()
        view.setModel(sorting_proxy(model, view))
        return view

    def test_it_opens_in_chapter_order(self, qapp):
        from noveltrans.gui.widgets import enable_table_sorting

        view = self._table(qapp)
        enable_table_sorting(view)
        assert [view.model().index(r, 0).data() for r in range(3)] == [1, 2, 3]

    def test_it_applies_a_remembered_sort(self, qapp, tmp_path):
        from noveltrans.gui.widgets import enable_table_sorting

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        config.set_sort_state("t", 0, False, len(ChapterTableModel.COLUMNS))
        view = self._table(qapp)
        enable_table_sorting(view, config=config, list_id="t")
        assert view.model().index(0, 0).data() == 12

    def test_clicking_a_header_saves_the_choice(self, qapp, tmp_path):
        from noveltrans.gui.widgets import enable_table_sorting

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        view = self._table(qapp)
        enable_table_sorting(view, config=config, list_id="t")
        view.sortByColumn(ChapterTableModel.STATUS_COLUMN, Qt.SortOrder.DescendingOrder)
        assert config.sort_state("t", len(ChapterTableModel.COLUMNS)) == (
            ChapterTableModel.STATUS_COLUMN,
            False,
        )

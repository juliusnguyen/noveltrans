"""WorkspaceTabBar — the novel bar rendered as a left-hand column (feature 068).

Qt rotates tab text 90° for a West bar, which is unreadable for Vietnamese and Chinese
titles, so the column is faked: transposed size hints plus tabs painted with a North
shape. These tests pin the two things that are easy to get silently wrong — the label
elide (Qt pre-elides it against the vertical rect, so a naive paint renders a lone "…")
and the fall-through to plain Qt in horizontal mode.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QStyle, QTabBar

from noveltrans.gui.style import TAB_COLUMN_WIDTH
from noveltrans.gui.workspace_tab_bar import WorkspaceTabBar

LONG_LABEL = "Xuyên thư thành phản diện — 穿書反派 — twkan.com"


@pytest.fixture
def bar(qapp):
    """The bar as `MainWindow` embeds it: vertical mode AND a West shape.

    The two are separate on purpose. `set_vertical` governs how the bar measures and
    paints; the layout AXIS comes from the shape, which `QTabWidget.setTabPosition(West)`
    sets on our behalf in the real window. A bare `QTabBar` defaults to a North shape and
    would lay its tabs along x however it is painted, so the fixture sets the shape too
    rather than testing a configuration the app never produces.
    """
    widget = WorkspaceTabBar()
    widget.setShape(QTabBar.Shape.RoundedWest)
    widget.addTab(LONG_LABEL)
    widget.addTab("Tru Tiên")
    yield widget
    widget.deleteLater()


class TestVerticalColumn:
    def test_it_starts_vertical(self, bar):
        assert bar.is_vertical is True

    def test_tabs_are_wide_rows_not_tall_slivers(self, bar):
        hint = bar.tabSizeHint(0)
        assert hint.width() == TAB_COLUMN_WIDTH
        assert hint.height() < hint.width(), "a rotated Qt tab would be taller than wide"

    def test_rows_stack_downward(self, bar):
        assert bar.tabRect(1).top() >= bar.tabRect(0).bottom()

    def test_every_row_is_the_same_width(self, bar):
        """A column of ragged rows would look like a bug, not a sidebar."""
        assert bar.tabSizeHint(0).width() == bar.tabSizeHint(1).width()

    def test_a_long_label_is_elided_to_the_column_not_to_an_ellipsis(self, bar):
        """The trap this class exists to work around.

        `QTabBar.initStyleOption` elides the text itself, against the text rect of the
        REAL (West) shape — about 20px — so the option arrives as a bare "…". Painting it
        as-is gives a column of ellipses. The bar has to reinstate the real text and
        elide it against the row instead.
        """
        option = bar._north_option(0)
        text_rect = bar.style().subElementRect(
            QStyle.SubElement.SE_TabBarTabText, option, bar
        )
        elided = bar.fontMetrics().elidedText(
            bar.tabText(0), bar.elideMode(), text_rect.width()
        )
        assert len(elided) > 1, "the label collapsed to an ellipsis"
        assert elided.startswith("Xuyên"), "the label must start with the real title"

    def test_a_short_label_is_not_elided_at_all(self, bar):
        option = bar._north_option(1)
        text_rect = bar.style().subElementRect(
            QStyle.SubElement.SE_TabBarTabText, option, bar
        )
        elided = bar.fontMetrics().elidedText(
            bar.tabText(1), bar.elideMode(), text_rect.width()
        )
        assert elided == "Tru Tiên"

    def test_painting_does_not_raise(self, bar):
        """grab() drives a real paintEvent offscreen — the whole custom path executes."""
        bar.resize(TAB_COLUMN_WIDTH, 200)
        assert not bar.grab().isNull()


class TestHorizontalModeDefersToQt:
    def test_the_size_hint_is_qts_own(self, bar):
        bar.setShape(QTabBar.Shape.RoundedNorth)  # what setTabPosition(North) does
        bar.set_vertical(False)
        assert bar.is_vertical is False
        assert bar.tabSizeHint(0) == QTabBar.tabSizeHint(bar, 0)

    def test_a_long_label_gets_a_wider_tab_than_a_short_one(self, bar):
        """Qt sizes a horizontal tab to its text; the fixed column width must be gone."""
        bar.setShape(QTabBar.Shape.RoundedNorth)
        bar.set_vertical(False)
        assert bar.tabSizeHint(0).width() > bar.tabSizeHint(1).width()

    def test_switching_back_restores_the_column(self, bar):
        bar.setShape(QTabBar.Shape.RoundedNorth)
        bar.set_vertical(False)
        bar.setShape(QTabBar.Shape.RoundedWest)
        bar.set_vertical(True)
        assert bar.tabSizeHint(0).width() == TAB_COLUMN_WIDTH

    def test_setting_the_same_orientation_twice_is_a_no_op(self, bar):
        bar.set_vertical(True)  # already vertical
        assert bar.tabSizeHint(0).width() == TAB_COLUMN_WIDTH

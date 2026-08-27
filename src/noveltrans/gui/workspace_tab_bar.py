"""The outer novel bar, able to run as a left-hand column with horizontal labels.

Qt rotates tab text 90° for `TabPosition.West`/`East`. Measured against this app's theme,
that turns each tab into a 37×216 sliver of sideways Vietnamese — unreadable, and it also
reinterprets every `QTabWidget#workspaceTabs` rule in `style.py` (which are written for a
tab that merges into the pane *below* it) inside a rotated frame.

So the column is faked. `tabSizeHint` is transposed into full-width rows, and each tab is
painted with `option.shape` forced to `RoundedNorth` — which has the happy side effect of
making the *existing* QSS apply almost unchanged, since `QStyleSheetStyle` resolves the tab
from the same rules either way.

Everything Qt derives from the real (West) shape then has to be redone here: the label
elide (see `paintEvent`) and the close button's rectangle (see `_layout_close_buttons`).

One object serves both orientations rather than swapping bars, because
`QTabWidget.setTabBar()` replaces the bar that owns all the tab metadata — calling it after
tabs exist would empty the window. `set_vertical(False)` simply falls through to `super()`
everywhere, which is what makes the setting flippable live.

Alternatives rejected, all measured rather than assumed:

* a `QListWidget` sidebar driving a `QStackedWidget` — loses `addTab`/`setTabText`/
  `setTabToolTip`/`indexOf`/`removeTab`/`setTabButton` and every other bit of `QTabWidget`
  that `main_window.py` and eight existing tests use;
* a QSS-only trick — does not exist; the rotation happens in the *style*
  (`QCommonStyle::drawControl(CE_TabBarTabLabel)`), which QSS cannot reach;
* a `QProxyStyle` overriding `SE_TabBarTabRightButton` for the close button — segfaults in
  PySide6 under an app-wide stylesheet, because the proxy takes ownership of the app's
  `QStyleSheetStyle`.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QStyle, QStyleOptionTab, QStylePainter, QTabBar

from noveltrans.gui.style import TAB_COLUMN_WIDTH

# Row height = text height plus breathing room, tuned to land on 32px with the app font,
# matching the 31px a horizontal tab gets from its QSS padding.
_ROW_PADDING = 16
# Width kept clear of the label so it cannot run under the ✕. The button itself is 16px
# (QToolButton#tabCloseButton in style.py); the rest is margin.
_CLOSE_SLOT = 26


class WorkspaceTabBar(QTabBar):
    """A `QTabBar` that can render as a vertical column of horizontal-text rows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vertical = True

    @property
    def is_vertical(self) -> bool:
        return self._vertical

    def set_vertical(self, vertical: bool) -> None:
        """Switch orientation in place. Cheap, and keeps every tab — see the module note."""
        if vertical == self._vertical:
            return
        self._vertical = vertical
        self.updateGeometry()
        self.update()

    # ---------------------------------------------------------------- geometry

    def tabSizeHint(self, index: int) -> QSize:
        """Full-width rows stacked downward, instead of Qt's tall narrow slivers.

        Qt lays vertical-shape tabs along the y axis, so a transposed hint and Qt's own
        layout axis agree — which is why hit-testing, drag-reorder and the scroll buttons
        all keep working on the right axis without further help.
        """
        if not self._vertical:
            return super().tabSizeHint(index)
        return QSize(TAB_COLUMN_WIDTH, self.fontMetrics().height() + _ROW_PADDING)

    def _north_option(self, index: int) -> QStyleOptionTab:
        """This tab's style option, re-shaped as if the bar ran along the top."""
        option = QStyleOptionTab()
        self.initStyleOption(option, index)
        option.shape = QTabBar.Shape.RoundedNorth
        option.rect = self.tabRect(index)
        return option

    def _layout_close_buttons(self) -> None:
        """Put each ✕ where a North tab would put it: right edge, vertically centred.

        Qt computes the button's rect from the real (West) shape, which lands it
        horizontally centred and flush with the row's TOP edge — measured at (86, 0) in a
        190×32 row. Re-asking the style with a North-shaped option gives (171, 8), which is
        what a sidebar row wants.
        """
        if not self._vertical:
            return  # Qt places them correctly on a horizontal bar
        for index in range(self.count()):
            button = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if button is None:
                continue
            rect = self.style().subElementRect(
                QStyle.SubElement.SE_TabBarTabRightButton, self._north_option(index), self
            )
            if button.geometry() != rect:
                button.setGeometry(rect)

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self._layout_close_buttons()  # so the first paint already has them right

    # ---------------------------------------------------------------- painting

    def paintEvent(self, event) -> None:
        if not self._vertical:
            super().paintEvent(event)
            return
        # Also here, not just in tabLayoutChange: scrolling re-runs Qt's own
        # layoutWidgets (re-applying the wrong rect) without a layout-change callback,
        # but it always repaints. Setting a child's geometry from a paint converges —
        # the move repaints the exposed strip and the next pass is a no-op.
        self._layout_close_buttons()

        painter = QStylePainter(self)
        for index in range(self.count()):
            option = self._north_option(index)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)

            label = QStyleOptionTab(option)
            label.rect = option.rect.adjusted(0, 0, -_CLOSE_SLOT, 0)  # keep clear of the ✕
            text_rect = self.style().subElementRect(
                QStyle.SubElement.SE_TabBarTabText, label, self
            )
            # `QTabBar.initStyleOption` already elided the text against the VERTICAL text
            # rect — about 20px wide — so `option.text` arrives as a bare "…". Reinstate
            # the real text and elide it against this row instead, or every tab in the
            # column renders as a lone ellipsis.
            label.text = self.fontMetrics().elidedText(
                self.tabText(index), self.elideMode(), text_rect.width()
            )
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabLabel, label)

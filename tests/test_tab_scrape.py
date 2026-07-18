"""Tab 1 (Tải truyện) widget behaviour."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from noveltrans.config import AppConfig
from noveltrans.gui.tab_scrape import ScrapeTab


@pytest.mark.parametrize(
    "label_name", ["title_label", "author_label", "count_label", "desc_label"]
)
def test_novel_info_labels_are_selectable(qapp, label_name):
    # "Thông tin truyện" values must be selectable so the user can copy the title,
    # author, or description (e.g. for a video title/description).
    tab = ScrapeTab(AppConfig())
    flags = getattr(tab, label_name).textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse

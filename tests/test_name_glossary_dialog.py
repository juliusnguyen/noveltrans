"""Feature 072 — the Tên nhân vật dialog: review, correct, add, and repair.

The dialog is where the user overrules the Hán-Việt table for their own novel, so the
tests weight toward the decisions surviving: an edit must persist as an override, a
hand-typed name must be storable, and a rename must not quietly rewrite chapters without
asking.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from noveltrans.gui.name_glossary_dialog import (
    _COL_COUNT,
    _COL_READING,
    _COL_SOURCE,
    NameGlossaryDialog,
)
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.name_glossary import NameEntry, read_names, write_names
from noveltrans.storage import Library

NAME = "夏檸七"
AUTO = "Hạ Nịnh Thất"
WANTED = "Hạ Ninh Thất"


@pytest.fixture
def project(tmp_path):
    library = Library(tmp_path / "lib")
    meta = NovelMeta(url="https://x/1", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i}") for i in range(2)]
    p = library.create_project(meta, refs)
    p.save_content(0, f"{NAME}走進院子。")
    p.save_translation(0, "第1章", f"{AUTO} bước vào sân.", "vi")
    p.save_content(1, "風吹過。")
    yield p
    p.close()


def _dialog(qapp, project, monkeypatch, entries=None):
    """The dialog with its background scan stubbed out — it is not what these test."""
    monkeypatch.setattr(NameGlossaryDialog, "_rescan", lambda self: None)
    if entries is not None:
        write_names(project.path, entries)
    return NameGlossaryDialog(project)


class TestReview:
    def test_stored_names_populate_the_table(self, qapp, project, monkeypatch):
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source=NAME, reading=AUTO, auto=AUTO, count=800),
            NameEntry(source="江城", reading="Giang Thành", auto="Giang Thành", count=40),
        ])
        assert dialog.table.rowCount() == 2
        # ordered by count, so the names that matter most are the ones you see first
        assert dialog.table.item(0, _COL_SOURCE).text() == NAME

    def test_a_name_with_no_reading_says_so(self, qapp, project, monkeypatch):
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source="江婳", reading="", auto="", enabled=False, count=41),
        ])
        assert "không đọc được" in dialog.table.item(0, 2).text()


class TestEditing:
    def test_editing_a_reading_saves_it_as_an_override(self, qapp, project, monkeypatch):
        """The point of the whole feature: the table says one thing, the user says another,
        and the user wins from then on."""
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source=NAME, reading=AUTO, auto=AUTO, count=800),
        ])
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Cancel)
        dialog.table.item(0, _COL_READING).setText(WANTED)

        dialog._save()

        stored = {e.source: e for e in read_names(project.path)}
        assert stored[NAME].reading == WANTED
        assert stored[NAME].edited is True, "not marked as an override — a re-detect would undo it"
        assert stored[NAME].auto == AUTO, "the machine's answer is kept for reference"

    def test_unticking_a_name_disables_it(self, qapp, project, monkeypatch):
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source="江城", reading="Giang Thành", auto="Giang Thành"),
        ])
        dialog.table.item(0, _COL_SOURCE).setCheckState(Qt.CheckState.Unchecked)

        dialog._save()

        assert read_names(project.path)[0].enabled is False

    def test_an_enabled_row_with_no_reading_is_refused(self, qapp, project, monkeypatch):
        """An empty reading would DELETE the name from the source — `apply_glossary` is a
        blind str.replace."""
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source="江婳", reading="", auto="", enabled=True),
        ])
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))

        dialog._save()

        assert warned, "saved an enabled name with nothing to substitute"
        # The save is refused, so the dialog stays open for the user to fix the row.
        assert dialog.result() != QDialog.DialogCode.Accepted

    def test_a_hand_typed_name_is_stored(self, qapp, project, monkeypatch):
        """The escape hatch for names the detector cannot find."""
        dialog = _dialog(qapp, project, monkeypatch, [])
        dialog._add_row()
        dialog.table.item(dialog.table.rowCount() - 1, _COL_SOURCE).setText("秦九霄")
        dialog.table.item(dialog.table.rowCount() - 1, _COL_READING).setText("Tần Cửu Tiêu")

        dialog._save()

        stored = read_names(project.path)
        assert [e.source for e in stored] == ["秦九霄"]
        assert stored[0].reading == "Tần Cửu Tiêu"

    def test_typing_a_name_absent_from_the_novel_is_flagged(
        self, qapp, project, monkeypatch
    ):
        """The likeliest mistakes here are a typo and pasting the wrong script — both save
        an entry that silently never matches. The count says so immediately."""
        dialog = _dialog(qapp, project, monkeypatch, [])
        dialog._add_row()
        dialog.table.item(dialog.table.rowCount() - 1, _COL_SOURCE).setText("不存在")

        assert "0" in dialog.table.item(dialog.table.rowCount() - 1, _COL_COUNT).text()

    def test_typing_a_name_present_in_the_novel_shows_its_count(
        self, qapp, project, monkeypatch
    ):
        dialog = _dialog(qapp, project, monkeypatch, [])
        dialog._add_row()
        dialog.table.item(dialog.table.rowCount() - 1, _COL_SOURCE).setText(NAME)

        assert dialog.table.item(dialog.table.rowCount() - 1, _COL_COUNT).text() == "1"


class TestRepairOffer:
    def test_renaming_offers_to_fix_existing_translations(
        self, qapp, project, monkeypatch
    ):
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source=NAME, reading=AUTO, auto=AUTO, count=800),
        ])
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        dialog.table.item(0, _COL_READING).setText(WANTED)

        dialog._save()

        assert asked, "the old spelling was left in the finished translation"
        assert project.chapter(0).translated == f"{WANTED} bước vào sân."

    def test_declining_leaves_the_translation_alone(self, qapp, project, monkeypatch):
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source=NAME, reading=AUTO, auto=AUTO, count=800),
        ])
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Cancel)
        dialog.table.item(0, _COL_READING).setText(WANTED)

        dialog._save()

        assert project.chapter(0).translated == f"{AUTO} bước vào sân."
        assert read_names(project.path)[0].reading == WANTED, "the list is still saved"

    def test_choosing_retranslate_emits_the_indices(self, qapp, project, monkeypatch):
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source=NAME, reading=AUTO, auto=AUTO, count=800),
        ])
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.No)
        requested = []
        dialog.retranslate_requested.connect(requested.append)
        dialog.table.item(0, _COL_READING).setText(WANTED)

        dialog._save()

        assert requested == [[0]]
        assert project.chapter(0).translated == f"{AUTO} bước vào sân.", "not touched here"

    def test_a_one_syllable_rename_is_never_bulk_replaced(
        self, qapp, project, monkeypatch
    ):
        """Too short to replace blind — it would hit ordinary Vietnamese words."""
        project.save_translation(1, "第2章", "Gió thổi qua.", "vi")
        dialog = _dialog(qapp, project, monkeypatch, [
            NameEntry(source="風", reading="Gió", auto="Gió", count=9),
        ])
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Yes)[1],
        )
        dialog.table.item(0, _COL_READING).setText("Phong")

        dialog._save()

        assert asked == [], "offered to bulk-replace a one-syllable name"
        assert project.chapter(1).translated == "Gió thổi qua."

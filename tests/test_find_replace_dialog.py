"""Wiring tests for FindReplaceDialog — the pure logic is covered in test_find_replace.

Drives the dialog programmatically under the offscreen qapp fixture; the heavy lifting
lives in the pure core, so these stay thin: preview gating, scope, field selection,
that apply writes through to the DB without disturbing status, and the modeless extras —
the double-click jump signal and the staleness guard that protects an apply from text
edited behind the dialog's back.
"""

from __future__ import annotations

from noveltrans.gui.find_replace_dialog import FindReplaceDialog
from noveltrans.models import STATUS_TRANSLATED, ChapterRef, NovelMeta
from noveltrans.storage import NovelProject


def _project(library_dir):
    meta = NovelMeta(url="https://x/1", site="x", title="Truyện")
    refs = [ChapterRef(index=i, title=f"Chương {i + 1}", url=f"https://x/{i}") for i in range(3)]
    project = NovelProject.create(library_dir, meta, refs)
    project.save_content(0, "Lâm Phong đến")
    project.save_translation(0, "Lâm Phong", "Lâm Phong tới đây", "vi", translator="CLI (agy)")
    project.save_content(1, "không liên quan")
    project.save_content(2, "Lâm Phong lại đến")
    project.save_translation(2, "t", "Lâm Phong xuất hiện", "vi")
    return project


class TestPreviewGating:
    def test_apply_disabled_until_a_preview_finds_matches(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        assert not dlg.apply_button.isEnabled()  # nothing previewed yet
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        assert dlg.apply_button.isEnabled()

    def test_zero_matches_keeps_apply_disabled(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("không-tồn-tại")
        dlg._preview()
        assert not dlg.apply_button.isEnabled()
        assert "Không tìm thấy" in dlg.summary_label.text()

    def test_editing_an_input_invalidates_the_preview(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        assert dlg.apply_button.isEnabled()
        dlg.replace_edit.setText("Diệp Vân")  # changing the replacement invalidates
        assert not dlg.apply_button.isEnabled()
        assert dlg._matches == []

    def test_empty_search_reports_and_does_not_scan(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg._preview()
        assert not dlg.apply_button.isEnabled()
        assert "từ khoá" in dlg.summary_label.text()


class TestScopeAndFields:
    def test_all_scope_counts_across_the_project(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        # ch.0 translated ("Lâm Phong tới đây") + translated_title, ch.2 translated.
        assert "3 chương" not in dlg.summary_label.text()  # ch.1 has no match
        assert "2 chương" in dlg.summary_label.text()

    def test_current_scope_limits_to_the_previewed_chapter(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=2)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_current.setChecked(True)
        dlg._preview()
        assert "1 chương" in dlg.summary_label.text()
        assert [m.index for m in dlg._matches] == [2]

    def test_no_preview_idx_forces_all_scope(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=None)
        assert not dlg.scope_current.isEnabled()
        assert dlg.scope_all.isChecked()

    def test_field_selection_narrows_the_scan(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        # Only the translated body — not the translated title.
        dlg.field_translated_title.setChecked(False)
        dlg._preview()
        assert all(
            all(c.field == "translated" for c in m.changes) for m in dlg._matches
        )

    def test_title_warning_follows_the_original_title_checkbox(self, qapp, library_dir):
        # isHidden() reflects the widget's own flag; isVisible() would be False anyway
        # because the dialog is never shown in the test.
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        assert dlg.title_warning.isHidden()
        dlg.field_title.setChecked(True)
        assert not dlg.title_warning.isHidden()


class TestApply:
    def test_apply_writes_and_preserves_status(self, qapp, library_dir):
        project = _project(library_dir)
        dlg = FindReplaceDialog(project, preview_idx=0)
        emitted = {}
        dlg.applied.connect(lambda idxs: emitted.update(idxs=idxs))
        dlg.search_edit.setText("Lâm Phong")
        dlg.replace_edit.setText("Diệp Vân")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        dlg._apply()

        assert project.chapter(0).translated == "Diệp Vân tới đây"
        assert project.chapter(0).translated_title == "Diệp Vân"
        assert project.chapter(2).translated == "Diệp Vân xuất hiện"
        assert project.chapter(0).status == STATUS_TRANSLATED  # untouched
        assert emitted["idxs"] == {0, 2}

    def test_apply_only_touches_selected_fields(self, qapp, library_dir):
        project = _project(library_dir)
        dlg = FindReplaceDialog(project, preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.replace_edit.setText("Diệp Vân")
        dlg.scope_all.setChecked(True)
        dlg.field_content.setChecked(False)  # leave the original body alone
        dlg._preview()
        dlg._apply()
        # ch.0's original body still has the old name — only translated fields changed.
        assert project.chapter(0).content == "Lâm Phong đến"
        assert project.chapter(0).translated == "Diệp Vân tới đây"


class TestJumpToMatch:
    def _previewed(self, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        return dlg

    def test_double_click_emits_the_chapter_field_and_search(self, qapp, library_dir):
        dlg = self._previewed(library_dir)
        seen = []
        dlg.chapter_activated.connect(lambda *args: seen.append(args))

        dlg._on_breakdown_activated(dlg.breakdown.item(0))

        # Row 0 is ch.0; translated body comes before its title in the field order.
        assert seen == [(0, "translated", "Lâm Phong", False)]

    def test_second_row_maps_to_its_own_chapter(self, qapp, library_dir):
        dlg = self._previewed(library_dir)
        seen = []
        dlg.chapter_activated.connect(lambda *args: seen.append(args))

        dlg._on_breakdown_activated(dlg.breakdown.item(1))

        assert seen[0][0] == 2  # ch.1 has no match, so row 1 is chapter index 2

    def test_case_sensitivity_travels_with_the_jump(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg.case_check.setChecked(True)
        dlg._preview()
        seen = []
        dlg.chapter_activated.connect(lambda *args: seen.append(args))

        dlg._on_breakdown_activated(dlg.breakdown.item(0))

        assert seen[0][3] is True

    def test_the_field_follows_the_checkboxes(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg.field_content.setChecked(True)
        dlg.field_translated.setChecked(False)
        dlg.field_translated_title.setChecked(False)  # original side only
        dlg._preview()
        seen = []
        dlg.chapter_activated.connect(lambda *args: seen.append(args))

        dlg._on_breakdown_activated(dlg.breakdown.item(0))

        assert seen[0][1] == "content"


class TestCurrentChapterFollowsTheTab:
    def test_scope_current_rescopes_to_the_new_chapter(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.set_current_chapter(2)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_current.setChecked(True)
        dlg._preview()
        assert [m.index for m in dlg._matches] == [2]

    def test_moving_chapters_drops_a_current_scope_preview(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_current.setChecked(True)
        dlg._preview()
        assert dlg.apply_button.isEnabled()

        dlg.set_current_chapter(2)  # the tab moved on — the preview describes ch.0

        assert not dlg.apply_button.isEnabled()
        assert dlg._matches == []

    def test_moving_chapters_keeps_an_all_scope_preview(self, qapp, library_dir):
        # The point of the jump: walking the result list must not clear the result list.
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.scope_all.setChecked(True)
        dlg._preview()

        dlg.set_current_chapter(2)

        assert dlg.apply_button.isEnabled()
        assert [m.index for m in dlg._matches] == [0, 2]

    def test_losing_the_open_chapter_falls_back_to_all_scope(self, qapp, library_dir):
        dlg = FindReplaceDialog(_project(library_dir), preview_idx=0)
        dlg.set_current_chapter(None)
        assert not dlg.scope_current.isEnabled()
        assert dlg.scope_all.isChecked()


class TestStaleness:
    def test_apply_refuses_after_the_text_changed_underneath(self, qapp, library_dir):
        project = _project(library_dir)
        dlg = FindReplaceDialog(project, preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.replace_edit.setText("Diệp Vân")
        dlg.scope_all.setChecked(True)
        dlg._preview()

        # The user jumped to ch.0 and fixed the sentence by hand.
        project.edit_translation(0, title="Lâm Phong", text="Lâm Phong vừa tới nơi")
        applied = []
        dlg.applied.connect(applied.append)
        dlg._apply()

        assert applied == []  # nothing written
        assert project.chapter(0).translated == "Lâm Phong vừa tới nơi"  # edit survived
        assert not dlg.apply_button.isEnabled()
        assert "Xem trước" in dlg.summary_label.text()

    def test_an_untouched_preview_still_applies(self, qapp, library_dir):
        project = _project(library_dir)
        dlg = FindReplaceDialog(project, preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.replace_edit.setText("Diệp Vân")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        dlg._apply()
        assert project.chapter(0).translated == "Diệp Vân tới đây"

    def test_a_chapter_deleted_after_the_preview_blocks_the_apply(self, qapp, library_dir):
        project = _project(library_dir)
        dlg = FindReplaceDialog(project, preview_idx=0)
        dlg.search_edit.setText("Lâm Phong")
        dlg.replace_edit.setText("Diệp Vân")
        dlg.scope_all.setChecked(True)
        dlg._preview()
        project.delete_chapter(2)

        applied = []
        dlg.applied.connect(applied.append)
        dlg._apply()

        assert applied == []
        assert project.chapter(0).translated == "Lâm Phong tới đây"  # untouched

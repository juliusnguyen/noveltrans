"""Wiring tests for FindReplaceDialog — the pure logic is covered in test_find_replace.

Drives the dialog programmatically under the offscreen qapp fixture; the heavy lifting
lives in the pure core, so these stay thin: preview gating, scope, field selection, and
that apply writes through to the DB without disturbing status.
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

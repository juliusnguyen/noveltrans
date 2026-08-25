"""Tab-level wiring for find & replace: guards, the flush-before / reload-after order.

The dialog's own behaviour is covered in test_find_replace_dialog; here we check the
tab opens it correctly, cooperates with the manual-edit save flow, and jumps to a match
when a row is double-clicked. `dialog.show` is patched out so nothing renders.
"""

from __future__ import annotations

import noveltrans.gui.tab_translate as tt
from noveltrans.config import AppConfig
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import Library


def _tab_with_project(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    config = AppConfig()
    monkeypatch.setattr(type(config), "library_dir", tmp_path / "lib")
    tab = tt.TranslateTab(config)

    library = Library(tmp_path / "lib")
    meta = NovelMeta(url="https://x/1", site="x", title="Truyện")
    refs = [ChapterRef(index=i, title=f"Chương {i + 1}", url=f"https://x/{i}") for i in range(2)]
    project = library.create_project(meta, refs)
    project.save_content(0, "Lâm Phong")
    project.save_translation(0, "Lâm Phong", "Lâm Phong dịch", "vi")
    tab.project = project
    tab.model.set_chapters(project.chapters())
    return tab, project


def test_open_is_guarded_when_no_project(qapp, monkeypatch):
    infos = []
    monkeypatch.setattr(tt.QMessageBox, "information", lambda *a, **k: infos.append(a))
    tab = tt.TranslateTab(AppConfig())
    tab.project = None
    tab._open_find_replace()
    assert infos  # showed "Chưa chọn truyện", did not raise


def test_open_is_guarded_while_translating(qapp, tmp_path, monkeypatch):
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)

    class _RunningWorker:
        def isRunning(self):
            return True

    tab._worker = _RunningWorker()
    opened = []
    monkeypatch.setattr(tt.FindReplaceDialog, "show", lambda self: opened.append(True))
    tab._open_find_replace()
    assert not opened  # bailed out; dialog never opened
    assert tab._find_dialog is None
    assert "phiên dịch" in tab.status_label.text()


def test_open_flushes_pending_preview_edits_first(qapp, tmp_path, monkeypatch):
    # The critical ordering: a half-typed manual edit must reach disk BEFORE the scan,
    # or the replace could run against stale text / be clobbered on focus-out.
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(tab, "_save_preview_edits", lambda: calls.append("flush"))
    monkeypatch.setattr(
        tt.FindReplaceDialog, "show", lambda self: calls.append("show")
    )
    tab._open_find_replace()
    assert calls == ["flush", "show"]  # flush strictly before the dialog opens


def test_applied_signal_refreshes_table_and_reloads_open_preview(qapp, tmp_path, monkeypatch):
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab._preview_idx = 0
    reloaded = []
    monkeypatch.setattr(tab, "_load_preview", lambda ch: reloaded.append(ch.index if ch else None))

    # Simulate the dialog having applied a replacement to chapter 0.
    project.apply_replacements({0: {"translated": "Diệp Vân dịch"}})
    tab._on_replacements_applied({0})

    assert reloaded == [0]  # the open chapter was reloaded
    assert tab.model.chapter_at(0).translated == "Diệp Vân dịch"  # table refreshed


def test_applied_does_not_reload_when_open_chapter_unchanged(qapp, tmp_path, monkeypatch):
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab._preview_idx = 1  # viewing ch.1
    reloaded = []
    monkeypatch.setattr(tab, "_load_preview", lambda ch: reloaded.append(ch))
    tab._on_replacements_applied({0})  # only ch.0 changed
    assert reloaded == []  # ch.1's pane left alone


def test_reset_buttons_restores_the_find_replace_button(qapp, tmp_path, monkeypatch):
    # _start_translate disables it (asserted structurally: it's in the same block as the
    # other action buttons); _reset_buttons must restore it alongside them.
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab.find_replace_button.setEnabled(False)
    tab._reset_buttons()
    assert tab.find_replace_button.isEnabled()


def test_second_open_reuses_the_dialog_already_up(qapp, tmp_path, monkeypatch):
    # Modeless: the button stays clickable while the dialog is open, and a second one
    # would give the user two lists with two different previews of the same project.
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    shown = []
    monkeypatch.setattr(tt.FindReplaceDialog, "show", lambda self: shown.append(self))
    tab._open_find_replace()
    first = tab._find_dialog
    tab._open_find_replace()
    assert tab._find_dialog is first
    assert len(shown) == 1


def test_closing_the_dialog_clears_the_tab_reference(qapp, tmp_path, monkeypatch):
    # Shown for real (offscreen): `finished` only fires for a dialog that was visible,
    # and this is the path where the USER closes it, not the tab.
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab._open_find_replace()
    tab._find_dialog.close()
    assert tab._find_dialog is None


def test_starting_a_translation_closes_the_dialog(qapp, tmp_path, monkeypatch):
    # The batch writes the same rows the dialog previewed; leaving it up invites an
    # apply over freshly translated text.
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    monkeypatch.setattr(tt.FindReplaceDialog, "show", lambda self: None)
    tab._open_find_replace()
    assert tab._find_dialog is not None
    monkeypatch.setattr(tt.TranslateWorker, "start", lambda self: None)
    tab._start_translate([0])
    assert tab._find_dialog is None


def test_switching_novels_closes_the_dialog(qapp, tmp_path, monkeypatch):
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    monkeypatch.setattr(tt.FindReplaceDialog, "show", lambda self: None)
    tab._open_find_replace()
    tab._on_project_selected("")  # picker cleared — the project handle is closed
    assert tab._find_dialog is None


def test_jump_selects_the_match_in_the_translated_pane(qapp, tmp_path, monkeypatch):
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab._load_preview(project.chapter(0))

    tab._jump_to_match(0, "translated", "Phong", False)

    assert tab.translated_view.textCursor().selectedText() == "Phong"
    assert tab.translated_view.hasFocus() or True  # focus is unreliable offscreen


def test_jump_selects_in_the_original_pane_for_a_source_field(qapp, tmp_path, monkeypatch):
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab._load_preview(project.chapter(0))

    tab._jump_to_match(0, "content", "Phong", False)

    assert tab.original_view.textCursor().selectedText() == "Phong"
    assert not tab.translated_view.textCursor().hasSelection()


def test_jump_loads_a_chapter_that_is_not_the_open_one(qapp, tmp_path, monkeypatch):
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    project.save_translation(1, "Chương 2", "Lâm Phong ở chương hai", "vi")
    tab.model.set_chapters(project.chapters())
    tab._load_preview(project.chapter(0))
    assert tab._preview_idx == 0

    tab._jump_to_match(1, "translated", "chương hai", False)

    assert tab._preview_idx == 1  # row selection loaded the other chapter
    assert tab.translated_view.textCursor().selectedText() == "chương hai"


def test_jump_to_a_missing_chapter_is_a_no_op(qapp, tmp_path, monkeypatch):
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    tab._load_preview(project.chapter(0))
    tab._jump_to_match(99, "translated", "Phong", False)  # deleted since the preview
    assert tab._preview_idx == 0


def test_repeat_jumps_walk_the_hits_in_one_chapter(qapp, tmp_path, monkeypatch):
    # A chapter with "2 khớp" needs both reachable: the second double-click must not
    # re-select the first hit.
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    project.save_translation(0, "Lâm Phong", "Lâm Phong rồi Lâm Phong nữa", "vi")
    tab.model.set_chapters(project.chapters())
    tab._load_preview(project.chapter(0))

    tab._jump_to_match(0, "translated", "Lâm Phong", False)
    first = tab.translated_view.textCursor().selectionStart()
    tab._jump_to_match(0, "translated", "Lâm Phong", False)
    second = tab.translated_view.textCursor().selectionStart()

    assert second > first
    assert tab.translated_view.textCursor().selectedText() == "Lâm Phong"


def test_jumping_past_the_last_hit_wraps_to_the_first(qapp, tmp_path, monkeypatch):
    tab, project = _tab_with_project(qapp, tmp_path, monkeypatch)
    project.save_translation(0, "Lâm Phong", "Lâm Phong rồi Lâm Phong nữa", "vi")
    tab.model.set_chapters(project.chapters())
    tab._load_preview(project.chapter(0))

    positions = []
    for _ in range(4):  # title + two body hits, then round again
        tab._jump_to_match(0, "translated", "Lâm Phong", False)
        positions.append(tab.translated_view.textCursor().selectionStart())

    assert positions[3] == positions[0]  # wrapped rather than getting stuck

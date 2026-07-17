"""Tab-level wiring for find & replace: guards, the flush-before / reload-after order.

The dialog's own behaviour is covered in test_find_replace_dialog; here we check the
tab opens it correctly and cooperates with the manual-edit save flow. `dialog.exec` is
patched out so nothing renders modally.
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
    monkeypatch.setattr(tt.FindReplaceDialog, "exec", lambda self: opened.append(True))
    tab._open_find_replace()
    assert not opened  # bailed out; dialog never opened
    assert "phiên dịch" in tab.status_label.text()


def test_open_flushes_pending_preview_edits_first(qapp, tmp_path, monkeypatch):
    # The critical ordering: a half-typed manual edit must reach disk BEFORE the scan,
    # or the replace could run against stale text / be clobbered on focus-out.
    tab, _project = _tab_with_project(qapp, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(tab, "_save_preview_edits", lambda: calls.append("flush"))
    monkeypatch.setattr(
        tt.FindReplaceDialog, "exec", lambda self: calls.append("exec")
    )
    tab._open_find_replace()
    assert calls == ["flush", "exec"]  # flush strictly before the dialog opens


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

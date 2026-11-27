"""Feature 113 — the pronoun fix queued behind a QC retranslate.

A QC plan that mixes full retranslations with address_drift chapters runs the translate
first and the pronoun fix after it. Two ways that hand-off went wrong:

- `finished_ok` arrives while the translate thread is still closing the project, so the
  tab still counted as busy and the queued fix was silently dropped;
- a run that returned with no signal at all (Dừng during name detection) left the queued
  fix behind, and the next, unrelated run started it.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

import noveltrans.gui.tab_translate as tt
from noveltrans.config import AppConfig
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import Library


class _FakeTranslate(QThread):
    chapter_started = Signal(int)
    progress = Signal(int, int, str)
    chapter_done = Signal(int)
    chapter_error = Signal(int, str)
    failed = Signal(str)
    finished_ok = Signal(int, int)

    mode = "ok"

    def __init__(self, *_a, **_k):
        super().__init__()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if self.mode == "ok":
            self.finished_ok.emit(1, 0)
            time.sleep(0.3)  # like TranslateWorker: project.close() after the signal
        # "silent": return without any signal, like a Dừng during name detection


class _FakeFix(QThread):
    progress = Signal(int, int, str)
    chapter_done = Signal(int)
    chapter_error = Signal(int, str)
    failed = Signal(str)
    finished_ok = Signal(int, int, int)

    started_with: list[list[int]] = []
    engines: list[tuple[str, str]] = []

    def __init__(self, _path, engine, indices, *, model="", **_k):
        super().__init__()
        self.indices = list(indices)
        _FakeFix.engines.append((engine, model))

    def cancel(self):
        pass

    def start(self):  # the real busy check in `_start_address_fix` has already passed
        _FakeFix.started_with.append(self.indices)


def _tab(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    config = AppConfig()
    monkeypatch.setattr(type(config), "library_dir", tmp_path / "lib")
    monkeypatch.setattr(tt, "TranslateWorker", _FakeTranslate)
    tab = tt.TranslateTab(config)
    library = Library(tmp_path / "lib")
    meta = NovelMeta(url="https://x/1", site="x", title="Truyện")
    refs = [ChapterRef(index=i, title=f"Chương {i + 1}", url=f"https://x/{i}") for i in range(3)]
    tab.project = library.create_project(meta, refs)
    tab.model.set_chapters(tab.project.chapters())
    fixes: list[list[int]] = []
    monkeypatch.setattr(_FakeFix, "started_with", fixes)
    monkeypatch.setattr(_FakeFix, "engines", [])
    monkeypatch.setattr("noveltrans.gui.workers.AddressFixWorker", _FakeFix)
    return tab, fixes


def _run_to_end(tab):
    worker = tab._worker
    deadline = time.monotonic() + 5
    while not worker.isFinished() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    for _ in range(20):
        QApplication.processEvents()


def test_the_queued_fix_runs_after_the_translate_thread_ends(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(_FakeTranslate, "mode", "ok")
    tab, fixes = _tab(qapp, tmp_path, monkeypatch)
    tab._pending_address_fix = [1, 2]
    tab._start_translate(indices=[0])
    _run_to_end(tab)
    assert fixes == [[1, 2]]


def test_a_run_that_ends_without_a_signal_drops_the_queued_fix(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(_FakeTranslate, "mode", "silent")
    tab, fixes = _tab(qapp, tmp_path, monkeypatch)
    tab._pending_address_fix = [1, 2]
    tab._start_translate(indices=[0])
    _run_to_end(tab)
    assert fixes == []

    monkeypatch.setattr(_FakeTranslate, "mode", "ok")
    tab._start_translate(indices=[0])  # an unrelated later run
    _run_to_end(tab)
    assert fixes == []


def test_a_cancelled_run_drops_the_queued_fix(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(_FakeTranslate, "mode", "ok")
    tab, fixes = _tab(qapp, tmp_path, monkeypatch)
    tab._pending_address_fix = [1]
    tab._start_translate(indices=[0])
    tab._worker.cancel()
    _run_to_end(tab)
    assert fixes == []


def test_the_fix_summary_names_the_failed_chapters_and_why(qapp, tmp_path, monkeypatch):
    tab, _ = _tab(qapp, tmp_path, monkeypatch)
    tab._address_fix_errors = {}
    tab._on_address_fix_error(4, "Hết thời gian chờ CLI")
    tab._on_address_fix_error(1, "đã đổi cả từ không phải xưng hô (ba)")
    tab._on_address_fix_finished(3, 0, 2)
    text = tab.status_label.text()
    assert "2 chương lỗi" in text and "chương 2, 5" in text
    assert "không phải xưng hô (ba)" in text  # the first failed chapter's own reason
    assert "vì AI đổi cả chữ khác" not in text  # no blanket guess at the cause


def test_a_fix_from_the_qc_results_uses_the_qc_engine(qapp, tmp_path, monkeypatch):
    tab, fixes = _tab(qapp, tmp_path, monkeypatch)
    monkeypatch.setattr(type(tab.config), "qc_ai_engine", "cli", raising=False)
    monkeypatch.setattr(type(tab.config), "qc_ai_model", "qc-model", raising=False)
    monkeypatch.setattr(type(tab.config), "address_ai_engine", "claude", raising=False)
    tab._start_qc_address_fix([1])
    assert fixes == [[1]] and _FakeFix.engines == [("cli", "qc-model")]
    assert "(cli)" in tab._progress_verb


def test_a_fixed_chapter_can_be_undone_on_its_own(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMenu

    tab, _ = _tab(qapp, tmp_path, monkeypatch)
    project = tab.project
    project.save_content(0, "原文")
    project.save_translation(0, "Chương 1", "Tao đi.", "vi", "CLI")
    project.save_address_fix(0, "Tớ đi.")
    tab.model.set_chapters(project.chapters())
    menu = QMenu()
    tab._add_rewrite_actions(menu, [tab.model.row_for_index(0)])
    (undo,) = [a for a in menu.actions() if a.text() == "↩︎ Hoàn tác sửa xưng hô"]
    undo.trigger()
    assert project.chapter(0).translated == "Tao đi."
    assert not any("Hoàn tác viết lại" in a.text() for a in menu.actions())

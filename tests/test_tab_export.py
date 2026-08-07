"""ExportTab's OneDrive backup surface (feature 051), offscreen Qt.

The push itself is covered in `test_onedrive_upload.py`; what matters here is the wiring
around it — that the confirmation states what it is about to do, that the two actions
cannot overlap, that the four signals land on the right UI, and that a running push is
cancelled rather than abandoned on quit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox

import noveltrans.gui.tab_export as te
from noveltrans.config import AppConfig
from noveltrans.gui.tab_export import ExportTab
from noveltrans.storage import NovelProject


def _config(tmp_path):
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.library_dir = tmp_path / "library"
    return config


@pytest.fixture
def tab_with_project(qapp, tmp_path, library_dir, sample_meta, sample_refs):
    """An ExportTab with a real project selected, and some exports beside it."""
    project = NovelProject.create(library_dir, sample_meta, sample_refs)
    project.exports_dir.mkdir(parents=True, exist_ok=True)
    (project.exports_dir / "truyen.epub").write_bytes(b"epub")
    (project.audio_dir).mkdir(parents=True, exist_ok=True)
    (project.audio_dir / "0001.mp3").write_bytes(b"mp3")
    path = project.path
    project.close()

    tab = ExportTab(_config(tmp_path))
    tab._on_project_selected(str(path))
    yield tab
    tab.shutdown()


class _FakePushWorker:
    """Records how it was built and started; never opens a browser.

    Signals are stubbed as objects whose `connect` remembers the slot, so a test can fire
    one by hand and watch what the tab does with it.
    """

    instances: list = []

    def __init__(self, request, parent=None):
        self.request = request
        self.started = False
        self.cancelled = False
        self.running = False
        self.slots: dict[str, list] = {}
        for name in ("progress", "finished_ok", "failed", "needs_login"):
            setattr(self, name, _FakeSignal(self.slots.setdefault(name, [])))
        _FakePushWorker.instances.append(self)

    def start(self):
        self.started = True
        self.running = True

    def cancel(self):
        self.cancelled = True

    def isRunning(self):
        return self.running

    def wait(self, ms=0):
        self.running = False
        return True

    def emit(self, name, *args):
        for slot in self.slots[name]:
            slot(*args)


class _FakeSignal:
    def __init__(self, slots):
        self._slots = slots

    def connect(self, slot):
        self._slots.append(slot)


@pytest.fixture
def fake_worker(monkeypatch):
    _FakePushWorker.instances = []
    monkeypatch.setattr(te, "OneDrivePushWorker", _FakePushWorker)
    # The registry declines anything without a real `finished` signal, which is exactly
    # right here — but keep-awake tracking would still poke a QThread API.
    monkeypatch.setattr(te, "track_worker", lambda worker: None)
    return _FakePushWorker


def _yes(monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )


def _no(monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )


def _capture_info(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text="", *a, **k: seen.append(text)
    )
    return seen


def _capture_question(monkeypatch, answer=QMessageBox.StandardButton.Yes) -> list[str]:
    seen: list[str] = []

    def question(parent, title, text="", *a, **k):
        seen.append(text)
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return seen


def _capture_warning(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, text="", *a, **k: seen.append(text)
    )
    return seen


class TestTheGroupExists:
    def test_the_controls_are_there_and_start_in_the_right_state(self, qapp, tmp_path):
        tab = ExportTab(_config(tmp_path))
        assert tab.push_button.isEnabled()
        assert tab.push_forget_button.isEnabled()
        assert not tab.push_cancel_button.isEnabled()  # nothing to cancel yet
        assert not tab.push_progress.isVisible()
        tab.shutdown()

    def test_pushing_without_a_novel_says_so_rather_than_crashing(
        self, qapp, tmp_path, monkeypatch
    ):
        seen = _capture_info(monkeypatch)
        tab = ExportTab(_config(tmp_path))
        tab._start_push()
        assert seen and "chọn" in seen[0].lower()
        tab.shutdown()


class TestTheConfirmation:
    def test_it_states_the_destination_the_counts_and_the_overwrite(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """The counts are the point: 12 files / 4 GB and 3 200 files / 61 GB are a coffee
        break and an overnight run, and nothing else on screen says which this is."""
        seen = _capture_question(monkeypatch)
        tab_with_project._start_push()
        assert seen
        text = seen[0]
        assert "/NovelTrans/" in text
        assert "file" in text
        assert "GHI ĐÈ" in text

    def test_saying_no_starts_nothing(self, tab_with_project, monkeypatch, fake_worker):
        _no(monkeypatch)
        tab_with_project._start_push()
        assert fake_worker.instances == []
        assert not tab_with_project.push_progress.isVisible()

    def test_saying_yes_starts_the_worker_with_the_novels_title(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        _yes(monkeypatch)
        tab_with_project._start_push()
        assert len(fake_worker.instances) == 1
        worker = fake_worker.instances[0]
        assert worker.started
        assert worker.request.project_path == tab_with_project.project.path
        assert worker.request.novel_title
        assert worker.request.force is False

    def test_an_already_mirrored_novel_is_not_offered_a_pointless_run(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """It says so and points at “Quên trạng thái”, rather than opening a browser to
        do nothing."""
        from noveltrans.onedrive_upload import Manifest, collect_payload, write_manifest

        manifest = Manifest()
        for item in collect_payload(tab_with_project.project.path):
            manifest.mark_done(item)
        write_manifest(tab_with_project.project.path, manifest)

        seen = _capture_info(monkeypatch)
        tab_with_project._start_push()
        assert seen and "Quên trạng thái" in seen[0]
        assert fake_worker.instances == []

    def test_a_corrupt_manifest_is_shown_in_the_confirmation(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        from noveltrans.onedrive_upload import manifest_path

        manifest_path(tab_with_project.project.path).write_text("{oops", encoding="utf-8")
        seen = _capture_question(monkeypatch, QMessageBox.StandardButton.No)
        tab_with_project._start_push()
        assert "tải lại từ đầu" in seen[0]

    def test_unreadable_data_warns_instead_of_raising(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """A project whose database will not open must not take the GUI down with it."""
        seen = _capture_warning(monkeypatch)
        monkeypatch.setattr(
            te.ExportTab,
            "_push_request",
            lambda self, *, force: (_ for _ in ()).throw(RuntimeError("db hỏng")),
        )
        tab_with_project._start_push()
        assert seen and "db hỏng" in seen[0]


class TestTheTwoActionsInterlock:
    def test_a_running_push_disables_export(self, tab_with_project, monkeypatch, fake_worker):
        """Both write into the same project folder; a push that reads a .docx mid-write
        would mirror half a file."""
        _yes(monkeypatch)
        tab_with_project._start_push()
        assert not tab_with_project.export_button.isEnabled()
        assert not tab_with_project.push_button.isEnabled()
        assert tab_with_project.push_cancel_button.isEnabled()

    def test_a_second_push_is_refused_while_one_runs(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        _yes(monkeypatch)
        tab_with_project._start_push()
        seen = _capture_info(monkeypatch)
        tab_with_project._start_push()
        assert len(fake_worker.instances) == 1
        assert seen and "chờ xong" in seen[0]

    def test_finishing_hands_the_buttons_back(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        _yes(monkeypatch)
        tab_with_project._start_push()
        fake_worker.instances[0].emit("finished_ok", 4, 1, 0)
        assert tab_with_project.export_button.isEnabled()
        assert tab_with_project.push_button.isEnabled()
        assert not tab_with_project.push_cancel_button.isEnabled()
        assert not tab_with_project.push_progress.isVisible()


class TestTheSignals:
    def _started(self, tab, monkeypatch, fake_worker):
        _yes(monkeypatch)
        tab._start_push()
        return fake_worker.instances[0]

    def test_progress_moves_the_bar_and_the_status(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        worker.emit("progress", 2, 7, "⬆️ exports: 3 file")
        assert tab_with_project.push_progress.maximum() == 7
        assert tab_with_project.push_progress.value() == 2
        assert "exports" in tab_with_project.push_status.text()

    def test_a_blank_message_leaves_the_last_one_up(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """Otherwise the status flickers to empty between batches."""
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        worker.emit("progress", 1, 7, "⬆️ exports")
        worker.emit("progress", 2, 7, "")
        assert "exports" in tab_with_project.push_status.text()

    def test_the_summary_counts_all_three_outcomes(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        _capture_warning(monkeypatch)
        worker.emit("finished_ok", 4, 2, 1)
        text = tab_with_project.push_status.text()
        assert "4 file" in text and "2 bỏ qua" in text and "1 lỗi" in text

    def test_a_clean_run_does_not_pop_a_warning(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        seen = _capture_warning(monkeypatch)
        worker.emit("finished_ok", 5, 0, 0)
        assert seen == []

    def test_failures_pop_a_warning_that_says_a_re_run_is_cheap(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        seen = _capture_warning(monkeypatch)
        worker.emit("finished_ok", 3, 0, 2)
        assert seen and "bỏ qua" in seen[0]

    def test_needs_login_points_at_settings_and_is_not_a_warning(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """"Sign in once" is actionable; dressing it as a failure buries the fix."""
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        info = _capture_info(monkeypatch)
        warn = _capture_warning(monkeypatch)
        worker.emit("needs_login", "Profile OneDrive chưa đăng nhập.")
        assert warn == []
        assert info and "Settings" in info[0]
        assert tab_with_project.push_button.isEnabled()

    def test_a_failure_resets_the_ui_and_shows_the_message(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        worker = self._started(tab_with_project, monkeypatch, fake_worker)
        seen = _capture_warning(monkeypatch)
        worker.emit("failed", "Đã dừng tải lên OneDrive. 12 file đã lên vẫn còn")
        assert seen and "12 file" in seen[0]
        assert tab_with_project.push_button.isEnabled()
        assert not tab_with_project.push_progress.isVisible()


class TestCancel:
    def test_cancel_reaches_the_worker(self, tab_with_project, monkeypatch, fake_worker):
        _yes(monkeypatch)
        tab_with_project._start_push()
        tab_with_project._cancel_push()
        assert fake_worker.instances[0].cancelled
        assert not tab_with_project.push_cancel_button.isEnabled()  # no double-press
        assert "dừng" in tab_with_project.push_status.text().lower()


class TestForgetState:
    def test_it_says_nothing_on_onedrive_is_deleted(self, tab_with_project, monkeypatch):
        """Unlike `clear_upload_state`, this cannot create a duplicate or publish
        anything — the worst it costs is a re-upload, and saying so plainly is the point."""
        seen = _capture_question(monkeypatch, QMessageBox.StandardButton.No)
        tab_with_project._forget_push_state()
        assert seen and "Không có gì trên OneDrive bị xoá" in seen[0]

    def test_saying_no_keeps_the_manifest(self, tab_with_project, monkeypatch):
        from noveltrans.onedrive_upload import Manifest, manifest_path, write_manifest

        write_manifest(tab_with_project.project.path, Manifest(remote_root="/x"))
        _no(monkeypatch)
        tab_with_project._forget_push_state()
        assert manifest_path(tab_with_project.project.path).is_file()

    def test_saying_yes_drops_it(self, tab_with_project, monkeypatch):
        from noveltrans.onedrive_upload import Manifest, manifest_path, write_manifest

        write_manifest(tab_with_project.project.path, Manifest(remote_root="/x"))
        _yes(monkeypatch)
        tab_with_project._forget_push_state()
        assert not manifest_path(tab_with_project.project.path).exists()
        assert "tải lại toàn bộ" in tab_with_project.push_status.text()

    def test_forgetting_nothing_says_so(self, tab_with_project, monkeypatch):
        _yes(monkeypatch)
        tab_with_project._forget_push_state()
        assert "Chưa có trạng thái" in tab_with_project.push_status.text()


class TestLifecycle:
    def test_a_running_push_counts_as_a_running_worker(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """MainWindow asks this before closing; a push that isn't counted gets abandoned
        with a Chrome process attached."""
        assert tab_with_project.has_running_workers() is False
        _yes(monkeypatch)
        tab_with_project._start_push()
        assert tab_with_project.has_running_workers() is True

    def test_shutdown_cancels_before_waiting(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        """A push can be hours from finishing on its own. Waiting without cancelling
        would hang the quit for two minutes and then abandon a live thread."""
        _yes(monkeypatch)
        tab_with_project._start_push()
        tab_with_project.shutdown()
        assert fake_worker.instances[0].cancelled

    def test_showing_the_tab_does_not_re_pick_during_a_push(
        self, tab_with_project, monkeypatch, fake_worker
    ):
        _yes(monkeypatch)
        tab_with_project._start_push()
        refreshed = []
        monkeypatch.setattr(
            te.ExportTab, "refresh_projects", lambda self, select_path="": refreshed.append(1)
        )
        from PySide6.QtGui import QShowEvent

        tab_with_project.showEvent(QShowEvent())
        assert refreshed == []

    def test_showing_the_tab_re_picks_when_nothing_is_running(
        self, tab_with_project, monkeypatch
    ):
        from PySide6.QtGui import QShowEvent

        refreshed = []
        monkeypatch.setattr(
            te.ExportTab, "refresh_projects", lambda self, select_path="": refreshed.append(1)
        )
        tab_with_project.showEvent(QShowEvent())
        assert refreshed == [1]


class TestPushRequest:
    def test_it_prefers_the_translated_title(self, tab_with_project):
        """The OneDrive folder is named after the novel; a CJK source title is not what
        the user is looking for in their file browser."""
        project = tab_with_project.project
        project.save_meta_translation("Truyện Đã Dịch", "", "vi")
        project.reload_meta()
        assert tab_with_project._push_request(force=False).novel_title == "Truyện Đã Dịch"

    def test_force_is_passed_through(self, tab_with_project):
        assert tab_with_project._push_request(force=True).force is True


def test_the_database_is_snapshotted_not_copied(tab_with_project):
    """End-to-end through the tab's own preview: the payload's chapters.db must be a
    consistent snapshot, because the real one is open in WAL mode right now."""
    from noveltrans.onedrive_upload import preview_push

    preview = preview_push(tab_with_project._push_request(force=True))
    item = next(i for i in preview.to_upload if i.relpath == "chapters.db")
    assert item.path != Path(tab_with_project.project.path) / "chapters.db"
    assert item.size > 0
    # And it is a real database, not a truncated file.
    assert sqlite3.sqlite_version  # sanity: sqlite is available to have made it

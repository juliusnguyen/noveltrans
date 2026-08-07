"""Backing several novels up in one go: the picker, the run window, and the two workers.

The picker's job is that the choice is made against real numbers — a library sync can be
sixty gigabytes, and "tick some novels" is only a safe thing to offer if the dialog says
what each one costs before anything moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

import noveltrans.gui.onedrive_sync_dialog as osd
import noveltrans.onedrive_upload as od
from noveltrans.gui.onedrive_sync_dialog import OneDriveSyncPickerDialog
from noveltrans.gui.workers import OneDriveSyncWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject


def _novel(library: Path, title: str, *, extra_bytes: int = 0) -> Path:
    meta = NovelMeta(url=f"https://x/{title}", site="x", title=title)
    refs = [ChapterRef(index=0, title="C1", url="https://x/1")]
    project = NovelProject.create(library, meta, refs)
    project.exports_dir.mkdir(parents=True, exist_ok=True)
    (project.exports_dir / "book.epub").write_bytes(b"e" * max(extra_bytes, 1))
    path = project.path
    project.close()
    return path


@pytest.fixture
def library(tmp_path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    _novel(root, "Đấu La", extra_bytes=100)
    _novel(root, "Truyện Hai", extra_bytes=200)
    return root


@pytest.fixture(autouse=True)
def _no_background_scan(monkeypatch):
    """Keep the scan thread from starting, so tests drive `run()` themselves.

    Cancelling it instead would be wrong in a way that took a moment to spot: `cancel()`
    sets the flag the run loop checks, so a later `run()` returns having scanned nothing.
    """
    monkeypatch.setattr(
        "noveltrans.gui.workers.OneDriveSyncScanWorker.start", lambda self: None
    )


def _scan(dialog) -> None:
    """Run the picker's scan synchronously, on the GUI thread."""
    dialog._worker.run()


@pytest.fixture
def picker(qapp, library):
    dialog = OneDriveSyncPickerDialog(library, "/Fox Novel")
    _scan(dialog)
    yield dialog


class TestThePicker:
    def test_it_lists_every_novel_with_what_it_would_send(self, picker):
        titles = {picker.table.item(i, 0).text() for i in range(picker.table.rowCount())}
        assert titles == {"Đấu La", "Truyện Hai"}
        assert all(
            "file" in picker.table.item(i, 1).text()
            for i in range(picker.table.rowCount())
        )

    def test_novels_with_something_to_send_start_ticked(self, picker):
        for index in range(picker.table.rowCount()):
            assert picker.table.item(index, 0).checkState() == Qt.CheckState.Checked

    def test_the_total_reflects_the_ticks(self, picker):
        assert "2 truyện" in picker.total_label.text()
        picker.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        assert "1 truyện" in picker.total_label.text()

    def test_the_total_names_a_size_not_just_a_count(self, picker):
        """Sixty gigabytes and four are the same "2 truyện"; only the size says which."""
        assert "B" in picker.total_label.text()  # B / KB / MB / GB

    def test_starting_is_refused_until_something_is_ticked(self, picker):
        from PySide6.QtWidgets import QDialogButtonBox

        ok = picker.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok.isEnabled()
        picker._set_all(False)
        assert not ok.isEnabled()
        assert "Chưa chọn" in picker.total_label.text()

    def test_select_all_and_none(self, picker):
        picker._set_all(False)
        assert picker._ticked() == []
        picker._set_all(True)
        assert len(picker._ticked()) == 2

    def test_accepting_builds_requests_with_the_destination(self, picker, library):
        picker.accept()
        assert len(picker.requests) == 2
        for request in picker.requests:
            assert request.root_folder == "/Fox Novel"
            assert Path(request.project_path).parent == library
            assert request.novel_title

    def test_cancelling_yields_nothing(self, picker):
        picker.reject()
        assert picker.requests == []


class TestAlreadyMirroredNovels:
    def test_they_are_listed_but_unticked_and_uncheckable(self, qapp, library):
        """Hiding them would leave the user wondering whether they were missed; ticking
        them would queue a run with nothing to do."""
        from noveltrans.onedrive_upload import (
            Manifest,
            collect_payload,
            write_manifest,
        )

        target = sorted(library.iterdir())[0]
        manifest = Manifest()
        for item in collect_payload(target):
            manifest.mark_done(item)
        write_manifest(target, manifest)

        dialog = OneDriveSyncPickerDialog(library, "/Fox Novel")
        _scan(dialog)
        done = [
            i
            for i in range(dialog.table.rowCount())
            if dialog.table.item(i, 1).text() == "đã đồng bộ"
        ]
        assert len(done) == 1
        item = dialog.table.item(done[0], 0)
        assert item.checkState() == Qt.CheckState.Unchecked
        assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable

    def test_a_fully_mirrored_library_says_so(self, qapp, tmp_path):
        root = tmp_path / "empty-library"
        root.mkdir()
        dialog = OneDriveSyncPickerDialog(root, "/Fox Novel")
        _scan(dialog)
        assert "chưa có truyện nào" in dialog.status.text()


class TestScanWorkerResilience:
    def test_one_unreadable_novel_does_not_stop_the_scan(self, qapp, library, monkeypatch):
        """The others are still worth offering, and the row says why this one cannot be
        ticked."""
        from noveltrans.gui.workers import OneDriveSyncScanWorker

        calls = {"n": 0}
        real = od.preview_push

        def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hỏng")
            return real(request)

        monkeypatch.setattr(od, "preview_push", flaky)
        seen = []
        worker = OneDriveSyncScanWorker(library, "/Fox Novel")
        worker.scanned.connect(lambda *a: seen.append(a))
        worker.run()
        assert len(seen) == 2
        assert any(row[4] == "db hỏng" for row in seen)
        assert any(row[4] == "" for row in seen)

    @pytest.mark.parametrize(
        "size", [2**31, 165_618_764_084, 2**40], ids=["2GB", "154GB-measured", "1TB"]
    )
    def test_a_library_bigger_than_two_gigabytes_can_be_reported(self, qapp, size):
        """Qt's `int` is 32-bit, so a byte count over ~2 GB overflows the signal and the
        emit raises OverflowError — which is exactly what a real 154 GB library did. The
        parameter is `qlonglong`; this fails the moment someone "tidies" it back to int.
        """
        from noveltrans.gui.workers import OneDriveSyncScanWorker

        seen = []
        worker = OneDriveSyncScanWorker("/tmp/x", "/y")
        worker.scanned.connect(lambda *a: seen.append(a))
        worker.scanned.emit("/p", "Truyện", 3, size, "")
        assert seen[0][3] == size  # exact, not truncated or wrapped

    def test_an_unreadable_library_does_not_raise(self, qapp, tmp_path):
        from noveltrans.gui.workers import OneDriveSyncScanWorker

        finished = []
        worker = OneDriveSyncScanWorker(tmp_path / "nope", "/x")
        worker.finished_ok.connect(lambda: finished.append(True))
        worker.run()
        assert finished == [True]


class TestSyncWorker:
    def _requests(self, n=3):
        return [
            od.PushRequest(project_path=Path(f"/x/{i}"), novel_title=f"Truyện {i}")
            for i in range(n)
        ]

    def test_it_pushes_each_novel_in_turn(self, qapp, monkeypatch):
        pushed = []
        monkeypatch.setattr(
            od,
            "push_project",
            lambda request, **kw: (
                pushed.append(request.novel_title) or od.PushResult(uploaded=2)
            ),
        )
        done = []
        worker = OneDriveSyncWorker(self._requests())
        worker.finished_ok.connect(lambda s, e: done.append((s, e)))
        worker.run()
        assert pushed == ["Truyện 0", "Truyện 1", "Truyện 2"]
        assert done == [(3, 0)]

    def test_one_failing_novel_does_not_stop_the_rest(self, qapp, monkeypatch):
        """The same rule `push_project` applies to a batch, one level up."""

        def push(request, **kw):
            if request.novel_title == "Truyện 1":
                raise od.OneDriveUploadError("giao diện đổi")
            return od.PushResult(uploaded=1)

        monkeypatch.setattr(od, "push_project", push)
        results, done = [], []
        worker = OneDriveSyncWorker(self._requests())
        worker.novel_done.connect(lambda *a: results.append(a))
        worker.finished_ok.connect(lambda s, e: done.append((s, e)))
        worker.run()
        assert len(results) == 3
        assert done == [(2, 1)]

    def test_needs_login_stops_everything(self, qapp, monkeypatch):
        """Every remaining novel would fail the same way."""
        pushed = []

        def push(request, **kw):
            pushed.append(request.novel_title)
            raise od.OneDriveUploadError("chưa đăng nhập", needs_login=True)

        monkeypatch.setattr(od, "push_project", push)
        login, done = [], []
        worker = OneDriveSyncWorker(self._requests())
        worker.needs_login.connect(login.append)
        worker.finished_ok.connect(lambda s, e: done.append((s, e)))
        worker.run()
        assert pushed == ["Truyện 0"]  # it stopped rather than trying the rest
        assert login and done == []

    def test_cancelling_reports_how_far_it_got(self, qapp, monkeypatch):
        monkeypatch.setattr(od, "push_project", lambda request, **kw: od.PushResult())
        failed = []
        worker = OneDriveSyncWorker(self._requests())
        worker.failed.connect(failed.append)
        worker.cancel()
        worker.run()
        assert failed and "0/3" in failed[0]
        assert "bỏ qua" in failed[0]

    def test_an_unexpected_error_still_reaches_the_screen(self, qapp, monkeypatch):
        def push(request, **kw):
            raise RuntimeError("chrome chết")

        monkeypatch.setattr(od, "push_project", push)
        failed = []
        worker = OneDriveSyncWorker(self._requests(1))
        worker.failed.connect(failed.append)
        worker.run()
        assert failed and "chrome chết" in failed[0]


class TestSyncWindow:
    def _window(self, qapp, monkeypatch, requests):
        started = {}

        class _FakeWorker:
            def __init__(self, reqs, parent=None):
                started["requests"] = reqs
                self._slots = {
                    n: []
                    for n in ("progress", "novel_done", "finished_ok", "failed",
                              "needs_login")
                }
                self.cancelled = False
                self.running = True

            def __getattr__(self, name):
                if name in self._slots:
                    return _Sig(self._slots[name])
                raise AttributeError(name)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

            def isRunning(self):
                return self.running

            def wait(self, ms=0):
                self.running = False
                return True

            def emit(self, name, *args):
                for slot in self._slots[name]:
                    slot(*args)

        class _Sig:
            def __init__(self, slots):
                self._slots = slots

            def connect(self, slot):
                self._slots.append(slot)

        monkeypatch.setattr(osd, "OneDriveSyncWorker", _FakeWorker)
        monkeypatch.setattr(osd, "track_worker", lambda w: None)
        window = osd.OneDriveSyncWindow(requests)
        return window, window._worker

    def _requests(self, n=2):
        return [
            od.PushRequest(project_path=Path(f"/x/{i}"), novel_title=f"Truyện {i}")
            for i in range(n)
        ]

    def test_it_logs_each_novel_as_it_finishes(self, qapp, monkeypatch):
        window, worker = self._window(qapp, monkeypatch, self._requests())
        worker.emit("novel_done", "Đấu La", 4, 1, 0, "")
        assert window.log.item(0, 0).text() == "Đấu La"
        assert "4 file" in window.log.item(0, 1).text()

    def test_a_failed_novel_is_marked_not_hidden(self, qapp, monkeypatch):
        window, worker = self._window(qapp, monkeypatch, self._requests())
        worker.emit("novel_done", "Đấu La", 0, 0, 0, "giao diện đổi")
        assert "⚠️" in window.log.item(0, 1).text()

    def test_finishing_frees_the_close_button(self, qapp, monkeypatch):
        window, worker = self._window(qapp, monkeypatch, self._requests())
        assert not window.close_button.isEnabled()
        worker.emit("finished_ok", 2, 0)
        assert window.close_button.isEnabled()
        assert not window.stop_button.isEnabled()
        assert "2 truyện" in window.status.text()

    def test_stopping_reaches_the_worker(self, qapp, monkeypatch):
        window, worker = self._window(qapp, monkeypatch, self._requests())
        window._stop()
        assert worker.cancelled
        assert not window.stop_button.isEnabled()

    def test_closing_mid_run_cancels_rather_than_abandoning_the_browser(
        self, qapp, monkeypatch
    ):
        window, worker = self._window(qapp, monkeypatch, self._requests())
        window.close()
        assert worker.cancelled
        assert not worker.isRunning()

    def test_needs_login_points_at_settings(self, qapp, monkeypatch):
        window, worker = self._window(qapp, monkeypatch, self._requests())
        worker.emit("needs_login", "Chưa đăng nhập.")
        assert "Settings" in window.status.text()
        assert window.close_button.isEnabled()

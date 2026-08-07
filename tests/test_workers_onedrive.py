"""The two OneDrive workers: what they emit, and which failures go down which signal.

Workers are run synchronously here — `run()` directly, no event loop — the same way
`test_workers_identity.py` does. They never touch widgets, so there is nothing to pump.

The distinction that earns most of these tests is `needs_login` versus `failed`. They are
different signals because they mean different things to the user: one is "sign in once",
the other is "something broke". Routing a login failure down `failed` buries the one
message that tells them what to do about it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import noveltrans.onedrive_upload as od
from noveltrans.gui.workers import OneDriveLoginWorker, OneDrivePushWorker


class _Signals:
    """Collect everything a worker emits, so a test can assert on the whole run."""

    def __init__(self, worker, names):
        self.seen: dict[str, list] = {name: [] for name in names}
        for name in names:
            getattr(worker, name).connect(
                lambda *args, _n=name: self.seen[_n].append(args)
            )

    def __getitem__(self, name):
        return self.seen[name]


def _request(tmp_path: Path):
    return od.PushRequest(project_path=tmp_path, novel_title="Đấu La")


class TestOneDriveLoginWorker:
    def test_reports_the_account_it_connected(self, qapp, monkeypatch):
        monkeypatch.setattr(od, "open_login", lambda *, switch: "ai-do@example.com")
        worker = OneDriveLoginWorker()
        signals = _Signals(worker, ("done", "failed"))
        worker.run()
        assert signals["done"] == [("ai-do@example.com",)]
        assert signals["failed"] == []

    def test_an_unreadable_account_name_is_still_a_success(self, qapp, monkeypatch):
        """The session is in the profile whether or not we could read a name off the page."""
        monkeypatch.setattr(od, "open_login", lambda *, switch: "")
        worker = OneDriveLoginWorker()
        signals = _Signals(worker, ("done", "failed"))
        worker.run()
        assert signals["done"] == [("",)]

    @pytest.mark.parametrize("switch", [False, True])
    def test_switch_is_passed_through(self, qapp, monkeypatch, switch):
        """Without it a valid session loads straight through and the window closes before
        the user can change account."""
        seen = {}
        monkeypatch.setattr(
            od, "open_login", lambda *, switch: seen.setdefault("switch", switch) or ""
        )
        OneDriveLoginWorker(switch=switch).run()
        assert seen["switch"] is switch

    def test_a_module_error_is_shown_as_written(self, qapp, monkeypatch):
        def boom(*, switch):
            raise od.OneDriveUploadError("Đăng nhập OneDrive chưa hoàn tất: hết giờ")

        monkeypatch.setattr(od, "open_login", boom)
        worker = OneDriveLoginWorker()
        signals = _Signals(worker, ("done", "failed"))
        worker.run()
        assert "chưa hoàn tất" in signals["failed"][0][0]
        assert signals["done"] == []

    def test_an_unexpected_error_still_reaches_the_screen(self, qapp, monkeypatch):
        """A crash the module did not anticipate must not vanish into a dead thread."""

        def boom(*, switch):
            raise RuntimeError("chrome đã tắt")

        monkeypatch.setattr(od, "open_login", boom)
        worker = OneDriveLoginWorker()
        signals = _Signals(worker, ("done", "failed"))
        worker.run()
        assert "chrome đã tắt" in signals["failed"][0][0]


class TestOneDrivePushWorker:
    SIGNALS = ("progress", "file_done", "finished_ok", "failed", "needs_login")

    def _run(self, monkeypatch, tmp_path, fake_push):
        monkeypatch.setattr(od, "push_project", fake_push)
        worker = OneDrivePushWorker(_request(tmp_path))
        signals = _Signals(worker, self.SIGNALS)
        worker.run()
        return worker, signals

    def test_reports_the_three_counts(self, qapp, monkeypatch, tmp_path):
        def push(request, **kw):
            return od.PushResult(uploaded=7, skipped=3, failed=1, remote_root="/x")

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        assert signals["finished_ok"] == [(7, 3, 1)]
        assert signals["failed"] == []

    def test_progress_and_per_file_results_are_forwarded(self, qapp, monkeypatch, tmp_path):
        def push(request, *, on_progress, on_file_done, **kw):
            on_progress(0, 2, "⬆️ exports: 2 file")
            on_file_done("exports/a.epub", "")
            on_file_done("exports/b.docx", "hỏng")
            on_progress(2, 2, "✅ exports")
            return od.PushResult(uploaded=1, failed=1)

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        assert (0, 2, "⬆️ exports: 2 file") in signals["progress"]
        assert signals["file_done"] == [
            ("exports/a.epub", ""),
            ("exports/b.docx", "hỏng"),
        ]

    def test_a_finished_run_says_how_much_went_and_where(self, qapp, monkeypatch, tmp_path):
        def push(request, **kw):
            return od.PushResult(
                uploaded=2, bytes_sent=4 * 1024**3, remote_root="/NovelTrans/Đấu La"
            )

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        last = signals["progress"][-1][2]
        assert "4,0 GB" in last
        assert "/NovelTrans/Đấu La" in last

    def test_a_run_that_sent_nothing_does_not_claim_it_did(self, qapp, monkeypatch, tmp_path):
        """Everything was already mirrored — "Đã tải lên 0 B" would be a strange thing to
        put on screen."""

        def push(request, **kw):
            return od.PushResult(uploaded=0, skipped=5, bytes_sent=0)

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        assert not any("Đã tải lên" in p[2] for p in signals["progress"])
        assert signals["finished_ok"] == [(0, 5, 0)]

    def test_needs_login_goes_down_its_own_signal(self, qapp, monkeypatch, tmp_path):
        """"Sign in once" is actionable; burying it in a generic failure is not."""

        def push(request, **kw):
            raise od.OneDriveUploadError("Chưa đăng nhập.", needs_login=True)

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        assert signals["needs_login"] == [("Chưa đăng nhập.",)]
        assert signals["failed"] == []

    def test_an_ordinary_failure_goes_down_failed(self, qapp, monkeypatch, tmp_path):
        def push(request, **kw):
            raise od.OneDriveUploadError("Tài khoản OneDrive đã hết dung lượng.", fatal=True)

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        assert "hết dung lượng" in signals["failed"][0][0]
        assert signals["needs_login"] == []

    def test_cancelling_says_what_survived_rather_than_sounding_like_a_loss(
        self, qapp, monkeypatch, tmp_path
    ):
        """Nothing is left half-written on OneDrive — a file is either fully there or
        not — so this is NOT the YouTube worker's "a stray draft is on your channel"."""

        def push(request, **kw):
            raise od.OneDriveCancelled(uploaded=12)

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        message = signals["failed"][0][0]
        assert "12 file" in message
        assert "bỏ qua" in message
        assert signals["finished_ok"] == []

    def test_an_unexpected_error_still_reaches_the_screen(self, qapp, monkeypatch, tmp_path):
        def push(request, **kw):
            raise RuntimeError("playwright đã chết")

        _worker, signals = self._run(monkeypatch, tmp_path, push)
        assert "playwright đã chết" in signals["failed"][0][0]

    def test_cancel_reaches_the_run_through_should_cancel(self, qapp, monkeypatch, tmp_path):
        seen = {}

        def push(request, *, should_cancel, **kw):
            seen["before"] = should_cancel()
            return od.PushResult()

        monkeypatch.setattr(od, "push_project", push)
        worker = OneDrivePushWorker(_request(tmp_path))
        worker.cancel()
        worker.run()
        assert seen["before"] is True

    def test_the_pause_gate_is_handed_over_as_the_checkpoint(
        self, qapp, monkeypatch, tmp_path
    ):
        """Pause holds between batches; mid-transfer would mean sitting on a half-sent
        batch with a browser holding it open."""
        seen = {}

        def push(request, *, on_checkpoint, **kw):
            seen["checkpoint"] = on_checkpoint
            return od.PushResult()

        monkeypatch.setattr(od, "push_project", push)
        worker = OneDrivePushWorker(_request(tmp_path))
        worker.run()
        assert seen["checkpoint"] == worker._checkpoint

    def test_the_request_is_handed_over_whole(self, qapp, monkeypatch, tmp_path):
        """Built on the GUI thread; the worker never touches a NovelProject."""
        seen = {}

        def push(request, **kw):
            seen["request"] = request
            return od.PushResult()

        monkeypatch.setattr(od, "push_project", push)
        request = _request(tmp_path)
        OneDrivePushWorker(request).run()
        assert seen["request"] is request

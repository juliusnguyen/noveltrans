"""The cleanup dialog (offscreen Qt).

The deleting itself is covered in `test_cleanup.py`. What matters here is the gate: a
part-video must be **impossible to tick** until OneDrive has actually been checked. The
manifest is not evidence, and the UI is the last place that rule can be quietly lost.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

import noveltrans.gui.cleanup_dialog as cd
from noveltrans.cleanup import KIND_AUDIO, KIND_VIDEO, Removable
from noveltrans.gui.cleanup_dialog import CleanupDialog
from noveltrans.gui.widgets import SORT_ROLE


@pytest.fixture
def project(tmp_path):
    """One chapter of audio, one rendered part, published to YouTube."""
    root = tmp_path / "novel"
    audio = root / "exports" / "audio"
    audio.mkdir(parents=True)
    (audio / "0001-chuong-1.mp3").write_bytes(b"a" * 1000)
    (audio / "0001-chuong-1.cues.json").write_text("[]", encoding="utf-8")
    part = root / "exports" / "video" / "truyen-0001-0001"
    part.mkdir(parents=True)
    for suffix in (".mp4", ".srt"):
        (part / f"truyen-0001-0001{suffix}").write_bytes(b"v" * 5000)
    (part / "truyen-0001-0001.upload.json").write_text(
        json.dumps({"status": "published", "video_id": "x"}), encoding="utf-8"
    )
    return root


class _FakeVerifyWorker:
    instances: list = []

    def __init__(self, project_path, candidates, parent=None):
        self.candidates = list(candidates)
        self._slots = {n: [] for n in ("progress", "done", "failed", "needs_login")}
        self.started = False
        _FakeVerifyWorker.instances.append(self)

    def __getattr__(self, name):
        if name in self._slots:
            return _Sig(self._slots[name])
        raise AttributeError(name)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def wait(self, ms=0):
        return True

    def emit(self, name, *args):
        for slot in self._slots[name]:
            slot(*args)


class _Sig:
    def __init__(self, slots):
        self._slots = slots

    def connect(self, slot):
        self._slots.append(slot)


@pytest.fixture
def fake_verify(monkeypatch):
    _FakeVerifyWorker.instances = []
    monkeypatch.setattr(cd, "OneDriveVerifyWorker", _FakeVerifyWorker)
    return _FakeVerifyWorker


def _rows(dialog):
    return {
        dialog.table.item(i, 0).text(): dialog.table.item(i, 0)
        for i in range(dialog.table.rowCount())
    }


def _ok(dialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)


class TestWhatItShows:
    def test_it_lists_audio_and_video_with_sizes_and_reasons(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        assert dialog.table.rowCount() == 3  # mp3 + cues + mp4
        for index in range(dialog.table.rowCount()):
            assert dialog.table.item(index, 2).text()  # a size
            assert dialog.table.item(index, cd._STATUS_COLUMN).text()  # a reason

    def test_audio_arrives_ticked_because_it_is_already_proven(self, qapp, project, fake_verify):
        """The rendered .mp4 holding it is on disk — no network needed to know that."""
        dialog = CleanupDialog(project, "Truyện")
        for name, item in _rows(dialog).items():
            if name.endswith(".mp4"):
                continue
            assert item.checkState() == Qt.CheckState.Checked

    def test_a_project_with_nothing_to_clean_says_so(self, qapp, tmp_path, fake_verify):
        bare = tmp_path / "bare"
        bare.mkdir()
        dialog = CleanupDialog(bare, "Truyện")
        assert dialog.table.rowCount() == 0
        assert "Không có file nào" in dialog.status.text()
        assert not _ok(dialog).isEnabled()


class TestVideoIsLockedUntilVerified:
    """The rule the whole feature rests on."""

    def test_video_cannot_be_ticked_before_the_check(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        video = next(i for n, i in _rows(dialog).items() if n.endswith(".mp4"))
        assert video.checkState() == Qt.CheckState.Unchecked
        assert not video.flags() & Qt.ItemFlag.ItemIsUserCheckable

    def test_select_all_audio_does_not_reach_the_video(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        dialog._set_all(KIND_AUDIO, True)
        assert all(i.kind == KIND_AUDIO for i in dialog._ticked())

    def test_a_manifest_saying_backed_up_changes_nothing(self, qapp, project, fake_verify):
        """Measured on a real library: one part in twenty-nine was missing while the
        manifest was content."""
        from noveltrans.onedrive_upload import Manifest, write_manifest

        relpath = "exports/video/truyen-0001-0001/truyen-0001-0001.mp4"
        manifest = Manifest(remote_root="/NovelTrans/x")
        manifest.files[relpath] = {"status": "done", "size": 5000, "mtime": 1.0}
        write_manifest(project, manifest)

        dialog = CleanupDialog(project, "Truyện")
        video = next(i for n, i in _rows(dialog).items() if n.endswith(".mp4"))
        assert not video.flags() & Qt.ItemFlag.ItemIsUserCheckable

    def test_confirming_unlocks_only_the_confirmed_ones(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        dialog._verify()
        worker = fake_verify.instances[0]
        confirmed = worker.candidates  # all of them, this time
        worker.emit("done", confirmed, [])
        video = next(i for n, i in _rows(dialog).items() if n.endswith(".mp4"))
        assert video.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert video.checkState() == Qt.CheckState.Checked
        assert "có trên OneDrive" in dialog.status.text()

    def test_an_unconfirmed_part_stays_locked_and_says_why(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        dialog._verify()
        worker = fake_verify.instances[0]
        worker.emit("done", [], worker.candidates)  # none found on OneDrive
        video = next(i for n, i in _rows(dialog).items() if n.endswith(".mp4"))
        assert not video.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert "KHÔNG thấy trên OneDrive" in dialog.table.item(video.row(), cd._STATUS_COLUMN).text()

    def test_a_failed_check_leaves_everything_locked(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        dialog._verify()
        fake_verify.instances[0].emit("failed", "giao diện đổi")
        video = next(i for n, i in _rows(dialog).items() if n.endswith(".mp4"))
        assert not video.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert "giao diện đổi" in dialog.status.text()
        assert dialog.verify_button.isEnabled()  # can try again

    def test_needs_login_points_at_settings(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        dialog._verify()
        fake_verify.instances[0].emit("needs_login", "Chưa đăng nhập.")
        assert "Settings" in dialog.status.text()


class TestTheTotal:
    def test_it_names_a_size_not_just_a_count(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        assert "file" in dialog.total_label.text()
        assert "B" in dialog.total_label.text()

    def test_deleting_is_refused_with_nothing_ticked(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        dialog._set_all(None, False)
        assert not _ok(dialog).isEnabled()
        assert "Chưa chọn" in dialog.total_label.text()


class TestDeleting:
    def test_it_confirms_first_and_a_no_deletes_nothing(
        self, qapp, project, fake_verify, monkeypatch
    ):
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        dialog = CleanupDialog(project, "Truyện")
        dialog._delete()
        assert (project / "exports" / "audio" / "0001-chuong-1.mp3").exists()
        assert dialog.freed == 0

    def test_the_confirmation_says_it_cannot_be_undone(
        self, qapp, project, fake_verify, monkeypatch
    ):
        seen = []

        def question(parent, title, text="", *a, **k):
            seen.append(text)
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(QMessageBox, "question", question)
        CleanupDialog(project, "Truyện")._delete()
        assert seen and "KHÔNG khôi phục được" in seen[0]

    def test_saying_yes_deletes_exactly_the_ticked_files(
        self, qapp, project, fake_verify, monkeypatch
    ):
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        dialog = CleanupDialog(project, "Truyện")
        dialog._delete()
        assert not (project / "exports" / "audio" / "0001-chuong-1.mp3").exists()
        # the unverified video is untouched
        assert (project / "exports/video/truyen-0001-0001/truyen-0001-0001.mp4").exists()
        assert dialog.freed > 0

    def test_the_upload_record_survives(self, qapp, project, fake_verify, monkeypatch):
        """Deleting it would make the app republish the episode to the channel."""
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        CleanupDialog(project, "Truyện")._delete()
        assert (
            project / "exports/video/truyen-0001-0001/truyen-0001-0001.upload.json"
        ).exists()

    def test_errors_are_reported_rather_than_swallowed(
        self, qapp, project, fake_verify, monkeypatch
    ):
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        warned = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda p, t, text="", *a, **k: warned.append(text)
        )
        monkeypatch.setattr(
            cd, "remove_files", lambda items: (0, 0, ["a.mp3: quyền bị từ chối"])
        )
        CleanupDialog(project, "Truyện")._delete()
        assert warned and "quyền bị từ chối" in warned[0]


def test_removable_is_what_the_dialog_hands_to_the_deleter(qapp, project, fake_verify):
    """A guard on the seam: the dialog must pass the planner's own objects through, not
    rebuild them from table text."""
    dialog = CleanupDialog(project, "Truyện")
    assert all(isinstance(i, Removable) for i in dialog._ticked())
    assert {i.kind for i in dialog._ticked()} <= {KIND_AUDIO, KIND_VIDEO}


class TestSorting:
    """The row→file map must survive a sort.

    Before 074 both `_ticked` and `_on_verified` walked `enumerate(self._rows)` and read
    `self.table.item(index, 0)`. Sorting the table made row *i* stop being `_rows[i]`, so
    ticking one file would have deleted **a different one** — in a dialog whose own header
    says deletion cannot be undone. These fail against that code.
    """

    def test_ticking_a_row_after_a_sort_returns_the_file_that_row_shows(
        self, qapp, project, fake_verify
    ):
        dialog = CleanupDialog(project, "Truyện")
        dialog._set_all(None, False)
        dialog.table.sortItems(0, Qt.SortOrder.DescendingOrder)  # by name, reversed
        item = dialog.table.item(0, 0)
        if not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        assert [i.relpath for i in dialog._ticked()] == [item.text()]

    def test_size_sorts_on_bytes_not_on_its_own_text(self, qapp, project, fake_verify):
        dialog = CleanupDialog(project, "Truyện")
        keys = [
            dialog.table.item(r, 2).data(SORT_ROLE) for r in range(dialog.table.rowCount())
        ]
        assert all(isinstance(k, int) for k in keys)

    def test_select_audio_still_ticks_only_audio_after_a_sort(
        self, qapp, project, fake_verify
    ):
        dialog = CleanupDialog(project, "Truyện")
        dialog._set_all(None, False)
        dialog.table.sortItems(0, Qt.SortOrder.DescendingOrder)
        dialog._set_all(KIND_AUDIO, True)
        assert dialog._ticked()
        assert all(i.kind == KIND_AUDIO for i in dialog._ticked())

    def test_verifying_unlocks_the_row_that_shows_the_verified_file(
        self, qapp, project, fake_verify
    ):
        dialog = CleanupDialog(project, "Truyện")
        dialog.table.sortItems(0, Qt.SortOrder.DescendingOrder)
        dialog._verify()
        worker = fake_verify.instances[0]
        worker.emit("done", worker.candidates, [])
        for row in range(dialog.table.rowCount()):
            item = dialog.table.item(row, 0)
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry.kind == KIND_VIDEO:
                assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                assert "có trên OneDrive" in dialog.table.item(row, cd._STATUS_COLUMN).text()

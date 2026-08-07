"""The OneDrive destination picker (offscreen Qt).

Browsing exists because typing a path does not fail on a typo — it *creates* a folder,
and someone with years of files in their OneDrive should not find that out afterwards.
So the tests here are mostly about navigation being honest: where you are, what you get
back, and what happens when a listing fails.
"""

from __future__ import annotations

import pytest

import noveltrans.gui.onedrive_folder_dialog as ofd
from noveltrans.gui.onedrive_folder_dialog import OneDriveFolderDialog


class _FakeFoldersWorker:
    """Stands in for OneDriveFoldersWorker; never opens a browser.

    Records every path it was asked for, so a test can assert what the dialog navigated
    to rather than only what it displays.
    """

    instances: list = []

    def __init__(self, path="", parent=None):
        self.path = path
        self.running = False
        self._slots = {"fetched": [], "failed": [], "needs_login": []}
        _FakeFoldersWorker.instances.append(self)

    def __getattr__(self, name):
        if name in ("fetched", "failed", "needs_login"):
            return _FakeSignal(self._slots[name])
        raise AttributeError(name)

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def wait(self, ms=0):
        self.running = False
        return True

    def emit(self, name, *args):
        self.running = False
        for slot in list(self._slots[name]):
            slot(*args)


class _FakeSignal:
    def __init__(self, slots):
        self._slots = slots

    def connect(self, slot):
        self._slots.append(slot)


@pytest.fixture
def fake_worker(monkeypatch):
    _FakeFoldersWorker.instances = []
    monkeypatch.setattr(ofd, "OneDriveFoldersWorker", _FakeFoldersWorker)
    return _FakeFoldersWorker


def _names(dialog) -> list[str]:
    return [
        dialog.list.item(i).text().removeprefix("📁 ")
        for i in range(dialog.list.count())
    ]


class TestOpening:
    def test_it_starts_at_the_root_by_default(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog()
        assert dialog.selected_path == "/"
        assert fake_worker.instances[0].path == "/"

    def test_it_starts_where_the_setting_points(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog("/Fox Novel")
        assert dialog.selected_path == "/Fox Novel"
        assert fake_worker.instances[0].path == "/Fox Novel"

    def test_it_lists_what_comes_back(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog()
        fake_worker.instances[0].emit("fetched", "/", ["Audio", "Fox Novel"])
        assert _names(dialog) == ["Audio", "Fox Novel"]

    def test_it_is_busy_until_the_list_arrives(self, qapp, fake_worker):
        """A dialog that looks ready while a browser is still opening invites a
        double-click that goes nowhere."""
        dialog = OneDriveFolderDialog()
        assert not dialog.list.isEnabled()
        assert dialog.status.text()
        fake_worker.instances[0].emit("fetched", "/", ["Audio"])
        assert dialog.list.isEnabled()
        assert dialog.status.text() == ""


class TestNavigating:
    def _at_root(self, fake_worker, folders=("Fox Novel",)):
        dialog = OneDriveFolderDialog()
        fake_worker.instances[0].emit("fetched", "/", list(folders))
        return dialog

    def test_double_click_enters_and_re_lists(self, qapp, fake_worker):
        dialog = self._at_root(fake_worker)
        dialog._enter(dialog.list.item(0))
        assert dialog.selected_path == "/Fox Novel"
        assert fake_worker.instances[-1].path == "/Fox Novel"

    def test_going_up_returns_to_the_parent(self, qapp, fake_worker):
        dialog = self._at_root(fake_worker)
        dialog._enter(dialog.list.item(0))
        fake_worker.instances[-1].emit("fetched", "/Fox Novel", ["Truyện"])
        dialog._go_up()
        assert dialog.selected_path == "/"

    def test_up_is_disabled_at_the_root(self, qapp, fake_worker):
        dialog = self._at_root(fake_worker)
        assert not dialog.up_button.isEnabled()

    def test_a_deep_path_round_trips(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog("/Backup/Truyện")
        assert dialog.selected_path == "/Backup/Truyện"
        dialog._go_up()
        assert dialog.selected_path == "/Backup"

    def test_an_empty_folder_can_still_be_chosen(self, qapp, fake_worker):
        """A brand-new folder is exactly the sort of thing someone picks as a
        destination, so "no subfolders" must not read as "nothing here for you"."""
        dialog = OneDriveFolderDialog("/Fox Novel")
        fake_worker.instances[0].emit("fetched", "/Fox Novel", [])
        assert _names(dialog) == []
        assert "vẫn có thể chọn" in dialog.status.text()
        assert dialog.selected_path == "/Fox Novel"


class TestFailures:
    def test_a_failed_listing_keeps_you_where_you_were(self, qapp, fake_worker):
        """Dropping the user back to the root because one listing failed would lose the
        place they had navigated to."""
        dialog = OneDriveFolderDialog("/Fox Novel")
        fake_worker.instances[0].emit("failed", "Giao diện đã thay đổi")
        assert dialog.selected_path == "/Fox Novel"
        assert "Giao diện đã thay đổi" in dialog.status.text()
        assert dialog.reload_button.isEnabled()

    def test_needs_login_says_where_to_fix_it(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog()
        fake_worker.instances[0].emit("needs_login", "Chưa đăng nhập.")
        assert "Settings" in dialog.status.text()

    def test_a_second_reload_is_refused_while_one_runs(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog()
        dialog._reload()
        assert len(fake_worker.instances) == 1


class TestResult:
    def test_accepting_returns_the_open_folder(self, qapp, fake_worker):
        dialog = OneDriveFolderDialog("/Fox Novel")
        fake_worker.instances[0].emit("fetched", "/Fox Novel", [])
        dialog.accept()
        assert dialog.selected_path == "/Fox Novel"

    def test_closing_waits_for_the_worker_rather_than_abandoning_it(
        self, qapp, fake_worker
    ):
        """It owns a Chrome process; a dialog that closes over a live one leaks it."""
        dialog = OneDriveFolderDialog()
        worker = fake_worker.instances[0]
        assert worker.isRunning()
        dialog.reject()
        assert not worker.isRunning()

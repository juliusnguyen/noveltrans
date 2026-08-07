"""Tests for the browser-free parts of the OneDrive upload module.

The Playwright automation itself needs a live, signed-in Microsoft account and can't run
in CI, so we cover what can go wrong without a browser — which is most of what matters,
because every decision that can lose a file or waste a night of bandwidth is browser-free
by design:

  * the dedicated profile, and its separation from every other profile the app keeps —
    Chromium will not open one `user-data-dir` twice, so a shared path would silently
    make an OneDrive push and a YouTube upload mutually exclusive;
  * `flavour()`, which decides personal-vs-business off the landed URL;
  * `clear_profile`'s refusal to recursively delete something that isn't a profile, and
    `open_login`'s switch/no-switch contract;
  * `collect_payload` — what travels and, more importantly, what doesn't;
  * `snapshot_database`, including a test that pins the WAL failure it exists to prevent;
  * `plan_uploads`, the rule that decides whether 60 GB moves or 3 GB does;
  * the manifest, including the deliberate divergence from `read_upload_state` — a
    corrupt file reads as EMPTY here, and the reasoning is in `TestManifest`.

Same shape as `test_youtube_upload.py`, including the fake-Playwright injection.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from noveltrans.onedrive_upload import (
    FLAVOUR_BUSINESS,
    FLAVOUR_PERSONAL,
    STATUS_DONE,
    STATUS_SENDING,
    Manifest,
    OneDriveCancelled,
    OneDriveUploadError,
    PayloadItem,
    _check_cancel,
    _is_logged_out,
    _report,
    batch_payload,
    batch_timeout_ms,
    clear_manifest,
    collect_payload,
    flavour,
    format_size,
    is_quota_error,
    manifest_path,
    onedrive_folder_name,
    plan_uploads,
    profile_dir,
    read_manifest,
    remote_root_for,
    snapshot_database,
    swap_in_database_snapshot,
    total_bytes,
    upload_status,
    write_manifest,
)


def test_profile_dir_is_dedicated_and_separate_from_the_others():
    """A shared user-data-dir would serialise OneDrive against YouTube at the OS level."""
    from noveltrans.discord_unlock import profile_dir as discord_profile
    from noveltrans.youtube_upload import profile_dir as youtube_profile

    assert profile_dir().name == ".onedrive-profile"
    assert profile_dir() != youtube_profile()
    assert profile_dir() != discord_profile()


class TestFlavour:
    """Which OneDrive a landed URL belongs to. Drives the only two things that differ."""

    def test_personal(self):
        assert flavour("https://onedrive.live.com/?id=root&cid=A1B2C3") == FLAVOUR_PERSONAL

    def test_personal_short_link(self):
        assert flavour("https://1drv.ms/f/s!AbCdEf") == FLAVOUR_PERSONAL

    def test_business(self):
        url = (
            "https://contoso-my.sharepoint.com/personal/an_contoso_com/"
            "_layouts/15/onedrive.aspx?id=%2Fpersonal%2Fan_contoso_com%2FDocuments"
        )
        assert flavour(url) == FLAVOUR_BUSINESS

    def test_business_wins_over_the_onedrive_in_its_own_path(self):
        """`…/_layouts/15/onedrive.aspx` contains "onedrive"; personal must not claim it."""
        url = "https://contoso-my.sharepoint.com/personal/x/_layouts/15/onedrive.aspx"
        assert flavour(url) == FLAVOUR_BUSINESS

    @pytest.mark.parametrize(
        "url",
        [
            "https://login.live.com/login.srf?wa=wsignin1.0",
            "https://login.microsoftonline.com/common/oauth2/authorize",
        ],
    )
    def test_a_login_page_is_neither_flavour(self, url):
        """Answering "personal" here would send the caller navigating a page with no
        file list on it."""
        assert flavour(url) == ""

    @pytest.mark.parametrize("url", ["", "https://example.com/", "not a url"])
    def test_unknown_urls_are_blank(self, url):
        assert flavour(url) == ""

    def test_the_anonymous_marketing_page_is_neither_flavour(self):
        """MEASURED: signed out, onedrive.live.com lands here. It has no file list, so
        claiming a flavour would send the caller navigating a brochure."""
        assert flavour(MARKETING_URL) == ""


# MEASURED 2026-08-06 against a real signed-out profile: this is where
# `https://onedrive.live.com/` actually redirects an anonymous visitor. Not a login page —
# the product marketing site.
MARKETING_URL = "https://www.microsoft.com/en/microsoft-365/onedrive/online-cloud-storage"


class _FakePage:
    """Stands in for a Playwright page: a URL, and a locator that finds nothing.

    `_is_logged_out` checks the URL first and only falls back to the DOM, so a page whose
    every locator misses is exactly the "signed in, no login form" case.
    """

    def __init__(self, url: str, *, has_signin_form: bool = False):
        self.url = url
        self._has_form = has_signin_form

    def locator(self, selector: str):
        return _FakeLocator(self._has_form)


class _FakeLocator:
    def __init__(self, present: bool):
        self._present = present

    @property
    def first(self):
        return self

    def wait_for(self, *, state="visible", timeout=0):
        if not self._present:
            raise RuntimeError("no such element")


class TestIsLoggedOut:
    def test_a_login_url_is_logged_out(self):
        assert _is_logged_out(_FakePage("https://login.live.com/login.srf"))

    def test_a_onedrive_url_is_not(self):
        assert not _is_logged_out(_FakePage("https://onedrive.live.com/?id=root"))

    def test_a_login_form_at_an_unexpected_url_is_still_logged_out(self):
        """The DOM fallback is what stops an unanticipated URL reading as "signed in"."""
        page = _FakePage("https://example.com/whatever", has_signin_form=True)
        assert _is_logged_out(page)

    def test_the_marketing_page_counts_as_signed_out(self):
        """MEASURED, and it was NOT what this module first assumed: a signed-out visit to
        onedrive.live.com lands on the product marketing site, which carries no sign-in
        form and matches no login host. Left unnamed, "you are signed out" reads as
        "OneDrive would not open" — sending the user after a broken selector instead of
        the sign-in button."""
        assert _is_logged_out(_FakePage(MARKETING_URL))


class TestClearProfile:
    def _profile(self, tmp_path, monkeypatch, *, chromium_like=True):
        import noveltrans.onedrive_upload as od

        path = tmp_path / ".onedrive-profile"
        path.mkdir()
        if chromium_like:
            (path / "Default").mkdir()
            (path / "Local State").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(od, "profile_dir", lambda: path)
        return path

    def test_removes_an_existing_profile(self, tmp_path, monkeypatch):
        from noveltrans.onedrive_upload import clear_profile

        path = self._profile(tmp_path, monkeypatch)
        assert clear_profile() is True
        assert not path.exists()

    def test_no_profile_is_not_an_error(self, tmp_path, monkeypatch):
        import noveltrans.onedrive_upload as od

        monkeypatch.setattr(od, "profile_dir", lambda: tmp_path / "nothing-here")
        assert od.clear_profile() is False

    def test_refuses_to_delete_something_that_is_not_a_browser_profile(
        self, tmp_path, monkeypatch
    ):
        """If the path isn't what we think, recursive delete is reckless — bail loudly."""
        from noveltrans.onedrive_upload import clear_profile

        path = self._profile(tmp_path, monkeypatch, chromium_like=False)
        (path / "important.txt").write_text("do not delete me", encoding="utf-8")
        with pytest.raises(OneDriveUploadError, match="không giống profile"):
            clear_profile()
        assert (path / "important.txt").exists()

    def test_switch_login_clears_the_profile_first(self, tmp_path, monkeypatch):
        """Without this, a valid session loads straight through and the window closes
        before the user can pick a different account — the whole point of “Đổi tài khoản”."""
        import noveltrans.onedrive_upload as od

        self._profile(tmp_path, monkeypatch)
        cleared = []
        monkeypatch.setattr(od, "clear_profile", lambda: cleared.append(True))
        # Stop right after the clear: we only care that it happened before the launch.
        monkeypatch.setattr(
            od, "_require_playwright", lambda: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        with pytest.raises(RuntimeError):
            od.open_login(switch=True)
        assert cleared == [True]

    def test_plain_login_does_not_clear_the_profile(self, tmp_path, monkeypatch):
        import noveltrans.onedrive_upload as od

        self._profile(tmp_path, monkeypatch)
        cleared = []
        monkeypatch.setattr(od, "clear_profile", lambda: cleared.append(True))
        monkeypatch.setattr(
            od, "_require_playwright", lambda: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        with pytest.raises(RuntimeError):
            od.open_login()
        assert cleared == []


class TestErrors:
    def test_upload_error_carries_the_login_flag_and_the_file(self):
        exc = OneDriveUploadError("hỏng", needs_login=True, relpath="exports/x.mp4")
        assert exc.needs_login is True
        assert exc.relpath == "exports/x.mp4"

    def test_upload_error_defaults_are_quiet(self):
        exc = OneDriveUploadError("hỏng")
        assert exc.needs_login is False
        assert exc.relpath == ""

    def test_cancelled_carries_how_far_it_got(self):
        exc = OneDriveCancelled(uploaded=12)
        assert exc.uploaded == 12
        assert "huỷ" in str(exc)

    def test_cancelled_does_not_shadow_the_youtube_one(self):
        """`workers.py` reaches into both modules; two same-named classes in one file is
        a shadowing bug waiting to be written."""
        from noveltrans.youtube_upload import UploadCancelled

        assert OneDriveCancelled is not UploadCancelled
        assert not issubclass(OneDriveCancelled, UploadCancelled)


class TestUploadInputSelectors:
    """The one selector invariant worth a test rather than a comment.

    OneDrive ships both a file input and a directory input. Handing our paths to the
    directory one *looks* like it works — Playwright's `set_input_files` does not
    populate `webkitRelativePath`, so every file lands flat in whichever folder is open
    and the tree is silently lost. Every other selector in the module can miss and the
    run stops with a message; this one can miss and the run reports success.
    """

    def test_every_upload_selector_excludes_the_directory_input(self):
        from noveltrans.onedrive_upload import _UPLOAD_INPUT_SELS

        for selector in _UPLOAD_INPUT_SELS:
            if "input[type='file']" in selector:
                assert ":not([webkitdirectory])" in selector, selector

    def test_the_directory_selector_exists_only_to_be_refused(self):
        """It is imported by the diagnose script to *report* it, never to feed it."""
        import noveltrans.onedrive_upload as od

        assert "webkitdirectory" in od._UPLOAD_DIRECTORY_INPUT_SEL
        assert od._UPLOAD_DIRECTORY_INPUT_SEL not in od._UPLOAD_INPUT_SELS


class TestBilingualTexts:
    """We do not control the account's UI language, so every text ladder needs both.

    The app is Vietnamese; the OneDrive account may be anything. A ladder with only one
    language works on the developer's machine and fails on the user's.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "_NEW_MENU_TEXTS",
            "_NEW_FOLDER_TEXTS",
            "_CREATE_TEXTS",
            "_UPLOAD_FILE_TEXTS",
            "_REPLACE_TEXTS",
            "_KEEP_BOTH_TEXTS",
        ],
    )
    def test_has_a_vietnamese_and_an_ascii_form(self, name):
        import noveltrans.onedrive_upload as od

        texts = getattr(od, name)
        assert len(texts) >= 2, name
        assert any(not t.isascii() for t in texts), f"{name}: no Vietnamese form"
        assert any(t.isascii() for t in texts), f"{name}: no English form"


class TestCallbackHelpers:
    def test_check_cancel_is_a_no_op_without_a_callback(self):
        _check_cancel(None)

    def test_check_cancel_raises_with_the_count(self):
        with pytest.raises(OneDriveCancelled) as info:
            _check_cancel(lambda: True, uploaded=7)
        assert info.value.uploaded == 7

    def test_check_cancel_passes_when_the_callback_says_no(self):
        _check_cancel(lambda: False, uploaded=7)

    def test_report_is_a_no_op_without_a_callback(self):
        _report(None, "gì đó")

    def test_report_forwards(self):
        seen = []
        _report(seen.append, "đang tải lên")
        assert seen == ["đang tải lên"]


# -- the pure core (step 3) ---------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project folder laid out the way `NovelProject` and feature 026 leave one.

    Includes the debris a real one accumulates — a WAL pair, a half-written `.tmp`, a
    `.DS_Store` — because deciding what NOT to send is half of what `collect_payload`
    does.
    """
    root = tmp_path / "dau-la-dai-luc"
    (root / "exports" / "audio").mkdir(parents=True)
    part = root / "exports" / "video" / "dau-la-0001-0010"
    part.mkdir(parents=True)

    (root / "meta.json").write_text('{"title": "Đấu La"}', encoding="utf-8")
    (root / "chapters.db").write_bytes(b"sqlite-ish")
    (root / "chapters.db-wal").write_bytes(b"write ahead log")
    (root / "chapters.db-shm").write_bytes(b"shared memory")
    (root / ".DS_Store").write_bytes(b"finder junk")
    (root / "exports" / "dau-la.epub").write_bytes(b"epub")
    (root / "exports" / "dau-la.docx").write_bytes(b"docx")
    (root / "exports" / "audio" / "0001.mp3").write_bytes(b"mp3")
    (root / "exports" / "audio" / "0002.mp3").write_bytes(b"mp3")
    for suffix, blob in (
        (".mp4", b"video" * 100),
        (".title.txt", b"title"),
        (".txt", b"description"),
        (".tags.txt", b"tags"),
        (".jpg", b"thumb"),
        (".srt", b"subs"),
        (".upload.json", b'{"status": "published"}'),
    ):
        (part / f"dau-la-0001-0010{suffix}").write_bytes(blob)
    (part / "dau-la-0001-0010.upload.json.tmp").write_bytes(b"half a write")
    return root


def _relpaths(items) -> list[str]:
    return [item.relpath for item in items]


class TestCollectPayload:
    def test_takes_everything_the_novel_produced(self, project):
        found = _relpaths(collect_payload(project))
        for expected in (
            "meta.json",
            "chapters.db",
            "exports/dau-la.epub",
            "exports/dau-la.docx",
            "exports/audio/0001.mp3",
            "exports/video/dau-la-0001-0010/dau-la-0001-0010.mp4",
        ):
            assert expected in found

    def test_part_sidecars_all_travel_with_their_video(self, project):
        """A part that arrives without its title, description, tags and cover is a part
        nobody can re-upload from the backup."""
        found = _relpaths(collect_payload(project))
        stem = "exports/video/dau-la-0001-0010/dau-la-0001-0010"
        for suffix in (".title.txt", ".txt", ".tags.txt", ".jpg", ".srt", ".upload.json"):
            assert f"{stem}{suffix}" in found

    @pytest.mark.parametrize(
        "excluded",
        [
            "chapters.db-wal",  # meaningless beside a snapshot taken at another instant
            "chapters.db-shm",
            ".DS_Store",
            "exports/video/dau-la-0001-0010/dau-la-0001-0010.upload.json.tmp",
        ],
    )
    def test_debris_is_left_behind(self, project, excluded):
        assert excluded not in _relpaths(collect_payload(project))

    def test_our_own_manifest_is_never_part_of_the_payload(self, project):
        """Otherwise every run's payload would depend on the previous run's bookkeeping."""
        write_manifest(project, Manifest(remote_root="/NovelTrans/Đấu La"))
        assert manifest_path(project).is_file()
        assert ".onedrive-upload.json" not in _relpaths(collect_payload(project))

    def test_relpaths_are_posix_even_on_windows_style_input(self, project):
        assert all("\\" not in item.relpath for item in collect_payload(project))

    def test_order_is_deterministic(self, project):
        """Batch composition depends on it, and a preview that reshuffles between two
        runs is one nobody can compare."""
        assert _relpaths(collect_payload(project)) == _relpaths(collect_payload(project))
        assert _relpaths(collect_payload(project)) == sorted(
            _relpaths(collect_payload(project))
        )

    def test_folder_is_the_posix_parent(self, project):
        by_relpath = {i.relpath: i for i in collect_payload(project)}
        assert by_relpath["meta.json"].folder == ""
        assert by_relpath["exports/audio/0001.mp3"].folder == "exports/audio"

    def test_the_database_mtime_counts_its_write_ahead_log(self, project):
        """In WAL mode sqlite may not touch the main file's mtime for hours. Reading it
        alone says "unchanged" about a database that gained fifty chapters today."""
        import os

        db = project / "chapters.db"
        os.utime(db, (1_000_000, 1_000_000))
        os.utime(project / "chapters.db-wal", (2_000_000, 2_000_000))
        item = next(i for i in collect_payload(project) if i.relpath == "chapters.db")
        assert item.mtime == 2_000_000

    def test_an_empty_project_is_an_empty_payload(self, tmp_path):
        (tmp_path / "empty").mkdir()
        assert collect_payload(tmp_path / "empty") == []


class TestSnapshotDatabase:
    def _wal_db(self, project: Path) -> None:
        db = sqlite3.connect(project / "chapters.db")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE chapters (idx INTEGER PRIMARY KEY, title TEXT)")
        db.executemany(
            "INSERT INTO chapters VALUES (?, ?)", [(i, f"Chương {i}") for i in range(50)]
        )
        db.commit()
        # Deliberately left open and un-checkpointed: this is the state the app is in
        # while a push runs, and the state a naive copy gets wrong.
        self._conn = db

    def test_a_naive_file_copy_of_a_wal_database_loses_everything(self, tmp_path):
        """The failure this function exists to prevent, pinned so nobody "simplifies" it
        back into a `shutil.copy`. Not a hypothetical: with the WAL un-checkpointed the
        copy does not even contain the table."""
        import shutil

        project = tmp_path / "p"
        project.mkdir()
        self._wal_db(project)
        naive = tmp_path / "naive.db"
        shutil.copy(project / "chapters.db", naive)
        tables = sqlite3.connect(naive).execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'chapters'"
        ).fetchone()[0]
        assert tables == 0
        self._conn.close()

    def test_the_snapshot_has_the_rows(self, tmp_path):
        project = tmp_path / "p"
        project.mkdir()
        self._wal_db(project)
        snapshot = snapshot_database(project, tmp_path / "scratch")
        rows = sqlite3.connect(snapshot).execute(
            "SELECT count(*) FROM chapters"
        ).fetchone()[0]
        assert rows == 50
        self._conn.close()

    def test_the_snapshot_is_one_self_contained_file(self, tmp_path):
        """No -wal/-shm beside it — which is what makes excluding those from the payload
        correct rather than lossy."""
        project = tmp_path / "p"
        project.mkdir()
        self._wal_db(project)
        scratch = tmp_path / "scratch"
        snapshot = snapshot_database(project, scratch)
        assert snapshot.name == "chapters.db"
        assert sorted(p.name for p in scratch.iterdir()) == ["chapters.db"]
        self._conn.close()

    def test_a_missing_database_says_so(self, tmp_path):
        (tmp_path / "p").mkdir()
        with pytest.raises(OneDriveUploadError, match="cơ sở dữ liệu"):
            snapshot_database(tmp_path / "p", tmp_path / "scratch")


class TestSwapInDatabaseSnapshot:
    def test_the_database_item_points_at_the_snapshot(self, project, tmp_path):
        snapshot = tmp_path / "scratch" / "chapters.db"
        snapshot.parent.mkdir()
        snapshot.write_bytes(b"a consistent copy, longer than the original")
        swapped = swap_in_database_snapshot(collect_payload(project), snapshot)
        item = next(i for i in swapped if i.relpath == "chapters.db")
        assert item.path == snapshot
        assert item.size == snapshot.stat().st_size

    def test_the_mtime_stays_the_sources(self, project, tmp_path):
        """Taking the snapshot's would stamp "now" on every run and re-upload the
        database every single time."""
        snapshot = tmp_path / "scratch" / "chapters.db"
        snapshot.parent.mkdir()
        snapshot.write_bytes(b"copy")
        before = next(i for i in collect_payload(project) if i.relpath == "chapters.db")
        after = next(
            i
            for i in swap_in_database_snapshot(collect_payload(project), snapshot)
            if i.relpath == "chapters.db"
        )
        assert after.mtime == before.mtime
        assert after.relpath == "chapters.db"

    def test_nothing_else_is_touched(self, project, tmp_path):
        snapshot = tmp_path / "scratch" / "chapters.db"
        snapshot.parent.mkdir()
        snapshot.write_bytes(b"copy")
        items = collect_payload(project)
        swapped = swap_in_database_snapshot(items, snapshot)
        assert [i for i in items if i.relpath != "chapters.db"] == [
            i for i in swapped if i.relpath != "chapters.db"
        ]


class TestFolderName:
    def test_diacritics_are_preserved(self):
        """`slugify` would give `dau-la-dai-luc`, which is not what "a folder named after
        the novel" means to the person looking at it in OneDrive."""
        assert onedrive_folder_name("Đấu La Đại Lục") == "Đấu La Đại Lục"

    @pytest.mark.parametrize("char", list('"*:<>?/\\|'))
    def test_illegal_characters_go(self, char):
        assert char not in onedrive_folder_name(f"Tên{char}truyện")

    def test_illegal_characters_become_spaces_not_deletions(self):
        """`A/B` must not silently merge into `AB`."""
        assert onedrive_folder_name("Phần A/B") == "Phần A B"

    def test_trailing_dots_and_spaces_go(self):
        """The server strips them silently, which would make our "is this folder here?"
        check compare against a name that no longer exists."""
        assert onedrive_folder_name("Truyện hay... ") == "Truyện hay"

    @pytest.mark.parametrize("name", ["con", "COM1", "nul", "desktop.ini"])
    def test_reserved_names_are_escaped(self, name):
        assert onedrive_folder_name(name) == f"_{name}"

    def test_a_title_of_nothing_but_junk_falls_back_to_the_slug(self):
        assert onedrive_folder_name('"*:<>?|') == "novel"

    def test_empty_title_still_produces_a_usable_name(self):
        assert onedrive_folder_name("") == "novel"

    def test_a_very_long_title_is_cut_to_something_creatable(self):
        name = onedrive_folder_name("Truyện " * 100)
        assert 0 < len(name) <= 100
        assert not name.endswith(" ")

    def test_remote_root_is_the_full_path(self):
        assert remote_root_for("Đấu La") == "/NovelTrans/Đấu La"


def _item(relpath: str, size: int = 10, mtime: float = 1_000.0) -> PayloadItem:
    return PayloadItem(path=Path("/x") / relpath, relpath=relpath, size=size, mtime=mtime)


class TestBatchPayload:
    def test_a_batch_never_spans_two_folders(self):
        """The upload input uploads into whatever folder is open, so a mixed batch would
        land in one folder — the same flattening the directory input causes."""
        items = [
            _item("meta.json"),
            _item("exports/a.epub"),
            _item("exports/audio/1.mp3"),
            _item("exports/audio/2.mp3"),
        ]
        for batch in batch_payload(items):
            assert len({i.folder for i in batch}) == 1

    def test_respects_the_file_count_cap(self):
        items = [_item(f"exports/audio/{i}.mp3") for i in range(45)]
        batches = batch_payload(items, max_files=20)
        assert [len(b) for b in batches] == [20, 20, 5]

    def test_respects_the_byte_cap(self):
        items = [_item(f"exports/audio/{i}.mp3", size=400) for i in range(5)]
        batches = batch_payload(items, max_bytes=1000)
        assert [len(b) for b in batches] == [2, 2, 1]

    def test_a_file_bigger_than_the_cap_still_gets_sent(self):
        """A 6 GB part-video is exactly the thing the user most wants backed up; dropping
        it because it exceeds a batch cap would be absurd."""
        items = [_item("exports/video/p1/p1.mp4", size=6 * 1024**3)]
        batches = batch_payload(items, max_bytes=4 * 1024**3)
        assert batches == [items]

    def test_an_oversized_file_does_not_drag_neighbours_with_it(self):
        items = [
            _item("exports/video/p1/a.mp4", size=6 * 1024**3),
            _item("exports/video/p1/b.jpg", size=10),
        ]
        batches = batch_payload(items, max_bytes=4 * 1024**3)
        assert [len(b) for b in batches] == [1, 1]

    def test_every_item_appears_exactly_once(self):
        items = [_item(f"exports/audio/{i}.mp3", size=300) for i in range(25)]
        flat = [i for b in batch_payload(items, max_files=7, max_bytes=1000) for i in b]
        assert flat == items

    def test_no_batch_is_empty(self):
        items = [_item(f"exports/audio/{i}.mp3", size=5000) for i in range(4)]
        assert all(batch_payload(items, max_bytes=1000))

    def test_nothing_in_nothing_out(self):
        assert batch_payload([]) == []


class TestManifest:
    def test_a_project_that_was_never_pushed_reads_empty(self, project):
        manifest = read_manifest(project)
        assert manifest.files == {}
        assert manifest.note == ""

    def test_round_trip(self, project):
        manifest = Manifest(remote_root="/NovelTrans/Đấu La", account="a@b.com")
        manifest.mark_done(_item("meta.json", size=42, mtime=1_700.0))
        write_manifest(project, manifest)

        read = read_manifest(project)
        assert read.remote_root == "/NovelTrans/Đấu La"
        assert read.account == "a@b.com"
        assert read.files["meta.json"]["size"] == 42
        assert read.files["meta.json"]["status"] == STATUS_DONE
        assert read.updated_at

    def test_a_corrupt_manifest_reads_as_empty_not_as_unknown(self, project):
        """**The deliberate divergence from `youtube_upload.read_upload_state`.**

        There, a truncated file reads as `unknown` and the part is never touched again,
        because guessing wrong publishes an episode twice. Here it reads as empty and the
        files are re-sent, because guessing wrong costs bandwidth — while the opposite
        mistake leaves the user believing a file is backed up when it is not.
        """
        manifest_path(project).write_text('{"files": {"a": ', encoding="utf-8")
        manifest = read_manifest(project)
        assert manifest.files == {}
        assert "hỏng" in manifest.note

    def test_a_manifest_that_is_not_an_object_reads_as_empty(self, project):
        manifest_path(project).write_text("[1, 2, 3]", encoding="utf-8")
        manifest = read_manifest(project)
        assert manifest.files == {}
        assert manifest.note

    def test_a_missing_manifest_carries_no_note(self, project):
        """"Never pushed" and "couldn't read the record" must be distinguishable, or the
        GUI cannot tell the user which one they are looking at."""
        assert read_manifest(project).note == ""

    def test_junk_entries_are_dropped_rather_than_trusted(self, project):
        manifest_path(project).write_text(
            json.dumps({"files": {"a.txt": "not a dict", "b.txt": {"size": 1}}}),
            encoding="utf-8",
        )
        assert list(read_manifest(project).files) == ["b.txt"]

    def test_write_leaves_no_temp_file_behind(self, project):
        write_manifest(project, Manifest())
        assert [p.name for p in project.iterdir() if p.name.endswith(".tmp")] == []

    def test_mark_sending_then_done(self):
        manifest = Manifest()
        item = _item("meta.json", size=9, mtime=5.0)
        manifest.mark_sending(item)
        assert manifest.files["meta.json"]["status"] == STATUS_SENDING
        assert "uploaded_at" not in manifest.files["meta.json"]
        manifest.mark_done(item)
        assert manifest.files["meta.json"]["status"] == STATUS_DONE
        assert manifest.files["meta.json"]["uploaded_at"]

    def test_clear_removes_it(self, project):
        write_manifest(project, Manifest())
        assert clear_manifest(project) is True
        assert not manifest_path(project).exists()

    def test_clearing_nothing_is_not_an_error(self, project):
        assert clear_manifest(project) is False


class TestPlanUploads:
    """The load-bearing suite: this is what decides whether 60 GB moves or 3 GB does."""

    def _done(self, item: PayloadItem) -> Manifest:
        manifest = Manifest()
        manifest.mark_done(item)
        return manifest

    def test_an_unchanged_file_is_skipped(self):
        item = _item("meta.json", size=10, mtime=1_000.0)
        to_upload, to_skip = plan_uploads([item], self._done(item))
        assert (to_upload, to_skip) == ([], [item])

    def test_a_file_with_no_record_is_uploaded(self):
        item = _item("meta.json")
        assert plan_uploads([item], Manifest()) == ([item], [])

    def test_a_changed_size_is_uploaded(self):
        recorded = _item("meta.json", size=10, mtime=1_000.0)
        now = _item("meta.json", size=99, mtime=1_000.0)
        assert plan_uploads([now], self._done(recorded)) == ([now], [])

    def test_a_newer_mtime_is_uploaded(self):
        recorded = _item("meta.json", size=10, mtime=1_000.0)
        now = _item("meta.json", size=10, mtime=9_000.0)
        assert plan_uploads([now], self._done(recorded)) == ([now], [])

    def test_sub_second_jitter_does_not_trigger_a_re_upload(self):
        """Some filesystems round mtimes; without a tolerance an untouched file is
        re-sent on every single run."""
        recorded = _item("meta.json", size=10, mtime=1_000.0)
        now = _item("meta.json", size=10, mtime=1_001.0)
        assert plan_uploads([now], self._done(recorded)) == ([], [now])

    def test_an_older_mtime_is_still_skipped(self):
        """We only ever ask "is the local copy newer": this is a push, not a sync."""
        recorded = _item("meta.json", size=10, mtime=9_000.0)
        now = _item("meta.json", size=10, mtime=1_000.0)
        assert plan_uploads([now], self._done(recorded)) == ([], [now])

    def test_a_sending_entry_is_re_uploaded_without_asking(self):
        """A run died mid-batch. Replacing a file in a private folder is idempotent in a
        way a YouTube publish never is, so there is no state to resolve and nobody to ask."""
        item = _item("meta.json")
        manifest = Manifest()
        manifest.mark_sending(item)
        assert plan_uploads([item], manifest) == ([item], [])

    def test_an_unreadable_mtime_in_the_record_is_re_uploaded(self):
        item = _item("meta.json", size=10, mtime=1_000.0)
        manifest = Manifest(files={"meta.json": {"size": 10, "mtime": "hôm qua",
                                                 "status": STATUS_DONE}})
        assert plan_uploads([item], manifest) == ([item], [])

    def test_force_uploads_everything(self):
        items = [_item("a"), _item("b")]
        manifest = Manifest()
        for item in items:
            manifest.mark_done(item)
        assert plan_uploads(items, manifest, force=True) == (items, [])

    def test_a_corrupt_manifest_therefore_uploads_everything(self, project):
        """The end-to-end consequence of the read-as-empty rule."""
        manifest_path(project).write_text("{oops", encoding="utf-8")
        items = collect_payload(project)
        to_upload, to_skip = plan_uploads(items, read_manifest(project))
        assert to_upload == items
        assert to_skip == []

    def test_a_mixed_tree_splits_correctly(self):
        unchanged = _item("meta.json", size=10, mtime=1_000.0)
        changed = _item("exports/a.epub", size=10, mtime=1_000.0)
        fresh = _item("exports/b.docx")
        manifest = Manifest()
        manifest.mark_done(unchanged)
        manifest.mark_done(_item("exports/a.epub", size=10, mtime=500.0))
        to_upload, to_skip = plan_uploads([unchanged, changed, fresh], manifest)
        assert to_upload == [changed, fresh]
        assert to_skip == [unchanged]


class TestTotals:
    def test_total_bytes(self):
        assert total_bytes([_item("a", size=10), _item("b", size=32)]) == 42

    def test_total_of_nothing(self):
        assert total_bytes([]) == 0

    @pytest.mark.parametrize(
        "num_bytes,expected",
        [
            (0, "0 B"),
            (512, "512 B"),
            (1536, "1,5 KB"),
            (812 * 1024**2, "812,0 MB"),
            (4 * 1024**3, "4,0 GB"),
            (60 * 1024**3, "60,0 GB"),
        ],
    )
    def test_format_size_uses_a_vietnamese_decimal_comma(self, num_bytes, expected):
        assert format_size(num_bytes) == expected


class TestUploadStatus:
    @pytest.mark.parametrize(
        "text",
        [
            "Đang tải lên 3 mục",
            "Uploading 3 items",
            "Uploading 3 items — 45%",
            "1 minute remaining",
            "Còn lại khoảng 2 phút",
        ],
    )
    def test_in_flight_is_not_finished(self, text):
        finished, _count, _percent = upload_status(text)
        assert finished is False

    @pytest.mark.parametrize(
        "text",
        ["Đã tải lên 3 mục", "Tải lên xong", "Upload complete", "3 items uploaded"],
    )
    def test_done_forms_in_both_languages(self, text):
        finished, _count, _percent = upload_status(text)
        assert finished is True

    def test_uploading_at_a_full_hundred_percent_counts_as_done(self):
        assert upload_status("Uploading 100%")[0] is True

    def test_percent_is_extracted(self):
        assert upload_status("Đang tải lên 45%")[2] == 45

    def test_item_count_is_extracted(self):
        assert upload_status("Đang tải lên 7 mục")[1] == 7
        assert upload_status("Uploading 7 files")[1] == 7

    def test_unrecognised_text_is_not_finished(self):
        """Erring this way costs time; erring the other way abandons a transfer when the
        browser closes."""
        assert upload_status("một câu gì đó lạ")[0] is False

    def test_empty_text_is_not_finished(self):
        assert upload_status("") == (False, None, None)
        assert upload_status(None) == (False, None, None)

    @pytest.mark.parametrize(
        "text",
        ["Your storage is full", "Hết dung lượng OneDrive", "not enough storage"],
    )
    def test_quota_is_recognised(self, text):
        assert is_quota_error(text)

    def test_ordinary_progress_is_not_a_quota_error(self):
        assert not is_quota_error("Đang tải lên 3 mục")


class TestBatchTimeout:
    def test_small_batches_still_get_the_floor(self):
        """"Small" says nothing about whether the network is having a bad afternoon."""
        assert batch_timeout_ms(0) == 10 * 60_000
        assert batch_timeout_ms(1024) == 10 * 60_000

    def test_a_four_gigabyte_batch_gets_hours(self):
        """Generously: the ceiling is a backstop, and it must stay above the stall window
        or it would be the thing that ends a healthy transfer."""
        hours = batch_timeout_ms(4 * 1024**3) / 3_600_000
        assert 4 < hours <= 6

    def test_it_is_capped(self):
        assert batch_timeout_ms(10**15) == 6 * 3_600_000

    def test_it_is_monotonic(self):
        sizes = [0, 10**6, 10**9, 4 * 1024**3, 10**12, 10**15]
        timeouts = [batch_timeout_ms(s) for s in sizes]
        assert timeouts == sorted(timeouts)

    def test_negative_input_does_not_produce_a_negative_wait(self):
        assert batch_timeout_ms(-1) == 10 * 60_000


# -- navigation (step 4) ------------------------------------------------------


class _FakeOneDrivePage:
    """A OneDrive file list backed by a real folder tree, driven by the module's selectors.

    Faithful to the two traits the navigation code is built around: clicking a folder row
    changes what the breadcrumb and the listing say, and creating a folder is a
    three-click ladder (New → Folder → name → Create) any step of which can be missing.

    `missing` names selectors/labels this page pretends not to have, which is how selector
    drift is simulated. `auto_rename` makes it behave like a OneDrive that answers a name
    collision by inventing `X 1` instead of failing — the outcome `_create_folder` exists
    to catch.
    """

    def __init__(
        self,
        tree=None,
        *,
        url=None,
        missing=(),
        auto_rename=False,
        hidden_folders=(),
        opens_on="click",
        upload_accepting=None,
        status_texts=(),
        arrive_after=1,
        conflict=False,
        rename_on_arrival=(),
        account="",
        fail_in=(),
        goto_raises=False,
        url_sequence=(),
        missing_texts=(),
    ):
        from noveltrans.onedrive_upload import _UPLOAD_INPUT_SELS

        self.tree = tree if tree is not None else {}
        # `url` is derived from `cwd` unless a test pins one, so `?id=` deep links behave
        # the way they do live — that is the whole mechanism being tested here.
        self._url_override = url
        self.missing = set(missing)
        self.auto_rename = auto_rename
        # -- transfer model --
        # Which upload inputs actually work. Default: only the first, so a test that
        # wants the fallbacks exercised has to say so.
        self.upload_accepting = (
            {_UPLOAD_INPUT_SELS[0]} if upload_accepting is None else set(upload_accepting)
        )
        self.status_texts = list(status_texts)
        self.arrive_after = arrive_after  # polls between handing files over and delivery
        self.conflict = conflict
        self.rename_on_arrival = set(rename_on_arrival)  # land as `x 1.ext`, not `x.ext`
        self.sent: list = []
        self.polls = 0
        self.pending: list[str] = []
        self.pending_at = 0
        self.replaced = False
        self.kept_both = False
        self.chooser_opened = False
        self.account = account
        # Folder names where the upload input is "not there" — how a per-batch failure
        # is simulated without breaking the whole page.
        self.fail_in = set(fail_in)
        self.goto_raises = goto_raises
        self.url_sequence = list(url_sequence)
        self.signin_form = False  # Microsoft's email box is on the page
        # Labels that are NOT on this page. Matched against the selector's quoted text
        # rather than against whole selector strings, so a test says "there is no Tạo
        # button" once and stays true however the code builds its selector — has-text,
        # text-is, dialog-scoped or otherwise.
        self.missing_texts = tuple(missing_texts)
        self.clicked_selectors: list[str] = []  # what each click actually targeted
        # Explicit (name, size-cell) pairs, for the folder-vs-file tests — the size column
        # is the only thing that says which rows are folders. Either a flat list (same
        # everywhere) or a dict keyed by the current folder name ("" for the root), which
        # is what a navigate-then-list test needs.
        self.rows = None
        # Which gesture actually opens a folder here. OneDrive's name link takes a
        # single click and the row around it takes a double-click; which selector we
        # matched decides which we need, and we do not know yet which one matches.
        self.opens_on = opens_on
        # Folders that exist but are not rendered — OneDrive's list virtualisation.
        self.hidden = set(hidden_folders)
        self.cwd: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.menu: str = ""  # "", "new", "new-folder"
        self.goto_urls: list[str] = []

    # -- the model -------------------------------------------------------
    def _here(self) -> dict:
        node = self.tree
        for segment in self.cwd:
            node = node.setdefault(segment, {})
        return node

    def _visible_names(self) -> list[str]:
        return [n for n in self._here() if n not in self.hidden]

    def _rows_here(self):
        """The explicit rows for the folder currently open, or None to use the tree."""
        if self.rows is None:
            return None
        if isinstance(self.rows, dict):
            return self.rows.get(self.cwd[-1] if self.cwd else "")
        return self.rows

    # -- the Playwright surface ------------------------------------------
    ROOT_ID = "/personal/fake/Documents"

    @property
    def url(self):
        if self._url_override is not None:
            return self._url_override
        from urllib.parse import quote

        if not self.cwd:
            return "https://onedrive.live.com/my"
        path = "/".join([self.ROOT_ID, *self.cwd])
        return "https://onedrive.live.com/my?id=" + quote(path, safe="")

    @url.setter
    def url(self, value):
        self._url_override = value

    def _cd(self, segments) -> None:
        """Walk the tree to `segments`; stay at the root if any of them is missing."""
        node, walked = self.tree, []
        for seg in segments:
            if seg not in node:
                self.cwd = []
                return
            node = node[seg]
            walked.append(seg)
        self.cwd = walked

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_urls.append(url)
        self.cwd = []  # a navigation lands at the root, as the real one does
        if "?id=" in url:
            from urllib.parse import unquote

            path = unquote(url.split("?id=", 1)[1].split("&")[0])
            if path.startswith(self.ROOT_ID):
                self._cd([s for s in path[len(self.ROOT_ID):].split("/") if s])
        if self.url_sequence:
            # Where each successive navigation ends up. A work account and a consumer one
            # land in different places from the same two entry points.
            self.url = self.url_sequence.pop(0)
        if self.goto_raises:
            # A heavy page can miss its load event and still have navigated. Modelled
            # because that is exactly what onedrive.live.com does.
            raise RuntimeError("Timeout 60000ms exceeded")

    def title(self):
        """MEASURED: OneDrive names the current folder in `document.title`, and there is
        no breadcrumb anywhere on the page. At the root it reads "Tệp của tôi"."""
        here = self.cwd[-1] if self.cwd else "Tệp của tôi"
        return f"{here} - OneDrive"

    def wait_for_timeout(self, ms):
        """Every wait is a poll; delivery happens on the clock, as it does live."""
        self.polls += 1
        if self.pending and self.polls - self.pending_at >= self.arrive_after:
            here = self._here()
            for name in self.pending:
                landed = _twin_name(name) if name in self.rename_on_arrival else name
                here.setdefault(landed, {})
            self.pending = []

    @property
    def status(self) -> str:
        if not self.status_texts:
            return ""
        return self.status_texts[min(self.polls, len(self.status_texts) - 1)]

    @property
    def keyboard(self):
        return self

    def press(self, key):
        pass

    def insert_text(self, text):
        self.typed.append(text)

    def evaluate(self, script):
        """Only `document.body.innerText` is modelled — the collision prompt is a toast
        whose text is the only way to tell "no prompt" from "prompt with no Replace"."""
        if "body.innerText" in script:
            return (
                "Đã tồn tại tệp có tên này nên chúng tôi không thể tải lên notes.txt."
                if self.conflict
                else "Tệp của tôi"
            )
        raise RuntimeError("no JS in the fake")  # exercises the diagnostics' fallbacks

    def locator(self, selector):
        return _FakeRows(self, selector)


class _FakeRows:
    def __init__(self, page, selector, index=None):
        self.page = page
        self.selector = selector
        self._index = index

    # What this selector stands for on the fake page.
    def _entries(self) -> list[str]:
        from noveltrans.onedrive_upload import (
            _LIST_HEADER_SEL,
            _ROW_NAME_SEL,
            _ROW_SEL,
            _UPLOAD_STATUS_SEL,
        )

        if self.selector in self.page.missing:
            return []
        if self.selector == _LIST_HEADER_SEL:
            # MEASURED: "Tệp của tôi\nDocuments" — the LAST line is the current folder.
            return ["\n".join(["Tệp của tôi", *self.page.cwd])]
        from noveltrans.onedrive_upload import _ROW_SIZE_SEL

        rows = self.page._rows_here()
        if rows is not None:
            if self.selector in (_ROW_NAME_SEL, _ROW_SEL):
                return [name for name, _size in rows]
            if self.selector == _ROW_SIZE_SEL:
                return [size for _name, size in rows]
        if self.selector in (_ROW_NAME_SEL, _ROW_SEL):
            # Row 0 is the grid's column header, not an item — as it is live.
            return ["Name", *self.page._visible_names()]
        if self.selector == _ROW_SIZE_SEL:
            # Folders in the tree model: every one reads as an item count.
            return ["Kích thước tệp", *[f"{0} mục" for _ in self.page._visible_names()]]
        if self.selector == _UPLOAD_STATUS_SEL:
            return [self.page.status] if self.page.status else []
        from noveltrans.onedrive_upload import _SIGNIN_FORM_SELS

        if self.selector in _SIGNIN_FORM_SELS:
            return ["đăng nhập"] if self.page.signin_form else []
        return []

    def _is_button(self) -> bool:
        """Buttons are matched by the ladder's selectors and text-matching forms."""
        if any(f'"{t}"' in self.selector for t in self.page.missing_texts):
            return False
        from noveltrans.onedrive_upload import (
            _CREATE_TEXTS,
            _FOLDER_NAME_INPUT_SELS,
            _NEW_FOLDER_TEXTS,
            _NEW_MENU_SELS,
            _NEW_MENU_TEXTS,
        )

        if self.selector in self.page.missing:
            return False
        if self.selector in _NEW_MENU_SELS:
            return True
        if self.selector in _FOLDER_NAME_INPUT_SELS:
            return self.page.menu == "new-folder"
        from noveltrans.onedrive_upload import (
            _KEEP_BOTH_TEXTS,
            _NEW_FOLDER_SELS,
            _REPLACE_TEXTS,
            _UPLOAD_FILE_SELS,
            _UPLOAD_FILE_TEXTS,
        )

        # MEASURED: both the folder and the upload entries live INSIDE the one
        # "Tạo hoặc tải lên" menu, so neither exists until it is open.
        if self.selector in _NEW_FOLDER_SELS:
            return self.page.menu == "new"
        if self.selector in _UPLOAD_FILE_SELS:
            return self.page.menu == "new"
        for texts, needed in (
            (_NEW_MENU_TEXTS, ""),
            (_NEW_FOLDER_TEXTS, "new"),
            (_CREATE_TEXTS, "new-folder"),
            (_UPLOAD_FILE_TEXTS, "new"),
        ):
            if any(f'"{t}"' in self.selector for t in texts):
                return self.page.menu == needed
        # The conflict dialog's buttons exist only while it is up.
        if any(f'"{t}"' in self.selector for t in _REPLACE_TEXTS + _KEEP_BOTH_TEXTS):
            return self.page.conflict
        return False

    def _is_input(self) -> bool:
        """A file input this page actually exposes — the rest are "not there"."""
        if self.page.cwd and self.page.cwd[-1] in self.page.fail_in:
            return False
        return (
            self.selector not in self.page.missing
            and self.selector in self.page.upload_accepting
        )

    def get_attribute(self, name, timeout=None):
        from noveltrans.onedrive_upload import _SIGNED_IN_ACCOUNT_SELS

        if self.selector in _SIGNED_IN_ACCOUNT_SELS and name == "aria-label":
            return self.page.account or None
        return None

    def count(self):
        return len(self._entries())

    @property
    def first(self):
        return _FakeRows(self.page, self.selector, 0)

    def nth(self, i):
        return _FakeRows(self.page, self.selector, i)

    def inner_text(self, timeout=None):
        entries = self._entries()
        index = self._index or 0
        if index >= len(entries):
            raise RuntimeError("no such row")
        return entries[index]

    def is_visible(self, timeout=None):
        return self._is_button() or bool(self._entries())

    def wait_for(self, state="visible", timeout=None):
        if not (self._is_button() or self._is_input() or self._entries()):
            raise RuntimeError(f"not found: {self.selector}")

    def fill(self, text):
        self.page.typed.append(text)
        self.page.menu = "new-folder"

    def click(self, timeout=None):
        # `timeout=` is passed by the real callers so a modal-blocked click fails fast
        # instead of eating Playwright's 30-second default.
        self.page.clicked_selectors.append(self.selector)
        self._press("click")

    def dblclick(self, timeout=None):
        self._press("dblclick")

    def set_input_files(self, files):
        if self.selector not in self.page.upload_accepting:
            raise RuntimeError(f"not a usable file input: {self.selector}")
        files = [files] if isinstance(files, str) else list(files)
        self.page.sent.append((self.selector, files))
        self.page.pending = [Path(f).name for f in files]
        self.page.pending_at = self.page.polls

    def _press(self, gesture):
        from noveltrans.onedrive_upload import (
            _CREATE_TEXTS,
            _KEEP_BOTH_TEXTS,
            _NEW_FOLDER_SELS,
            _NEW_FOLDER_TEXTS,
            _NEW_MENU_SELS,
            _NEW_MENU_TEXTS,
            _REPLACE_TEXTS,
            _UPLOAD_FILE_SELS,
            _UPLOAD_FILE_TEXTS,
        )

        if self.selector in _UPLOAD_FILE_SELS or any(
            f'"{t}"' in self.selector for t in _UPLOAD_FILE_TEXTS
        ):
            self.page.chooser_opened = True
            self.page.menu = ""
            return
        if self.selector in _NEW_FOLDER_SELS:
            self.page.menu = "new-folder"
            return
        if any(f'"{t}"' in self.selector for t in _REPLACE_TEXTS):
            self.page.conflict = False
            self.page.replaced = True
            return
        if any(f'"{t}"' in self.selector for t in _KEEP_BOTH_TEXTS):
            self.page.kept_both = True
            return
        if self.selector in _NEW_MENU_SELS or any(
            f'"{t}"' in self.selector for t in _NEW_MENU_TEXTS
        ):
            self.page.menu = "new"
            return
        if any(f'"{t}"' in self.selector for t in _NEW_FOLDER_TEXTS):
            self.page.menu = "new-folder"
            return
        if any(f'"{t}"' in self.selector for t in _CREATE_TEXTS):
            name = self.page.typed[-1] if self.page.typed else ""
            here = self.page._here()
            if name in here and self.page.auto_rename:
                here[f"{name} 1"] = {}
            elif name in self.page.hidden and self.page.auto_rename:
                here[f"{name} 1"] = {}
            else:
                here.setdefault(name, {})
            self.page.menu = ""
            return
        # A row: entering a folder — but only with the gesture this page responds to.
        name = self.inner_text()
        self.page.clicked.append((gesture, name))
        if gesture == self.page.opens_on and name in self.page._here():
            self.page.cwd.append(name)


class TestOpenRoot:
    def test_returns_the_flavour_of_whatever_it_landed_on(self):
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(url="https://onedrive.live.com/?id=root")
        assert _open_root(page) == FLAVOUR_PERSONAL

    def test_business_is_recognised_too(self):
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(
            url="https://contoso-my.sharepoint.com/personal/x/_layouts/15/onedrive.aspx"
        )
        assert _open_root(page) == FLAVOUR_BUSINESS

    def test_a_signed_out_profile_asks_for_a_login_rather_than_failing_vaguely(self):
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(url="https://login.live.com/login.srf")
        with pytest.raises(OneDriveUploadError) as info:
            _open_root(page)
        assert info.value.needs_login is True

    def test_the_marketing_landing_asks_for_a_login(self):
        """The real signed-out path. Before this was recognised the run fell through to
        the business entry point and reported "Không mở được OneDrive" — technically true
        and completely unhelpful."""
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(url=MARKETING_URL)
        with pytest.raises(OneDriveUploadError) as info:
            _open_root(page)
        assert info.value.needs_login is True

    def test_a_goto_that_times_out_is_still_judged_on_where_it_landed(self):
        """MEASURED: onedrive.live.com does not fire domcontentloaded within sixty
        seconds. Treating that as "this URL failed" would send every personal account
        down the business entry point for no reason."""
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(
            url=None, goto_raises=True
        )
        assert _open_root(page) == FLAVOUR_PERSONAL

    def test_a_work_account_is_not_told_to_sign_in_again(self):
        """The hazard the marketing-page check created. A work/school account has no
        consumer OneDrive, so `onedrive.live.com` sends it to the same brochure it sends
        an anonymous visitor to. Concluding "chưa đăng nhập" there would tell a
        signed-in business user to sign in, for ever."""
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(
            url_sequence=[
                MARKETING_URL,
                "https://contoso-my.sharepoint.com/personal/an_contoso_com/"
                "_layouts/15/onedrive.aspx",
            ]
        )
        assert _open_root(page) == FLAVOUR_BUSINESS

    def test_a_login_form_is_unambiguous_and_stops_at_once(self):
        """Unlike the brochure: an email box means signed out, whatever the URL says."""
        from noveltrans.onedrive_upload import _open_root

        page = _FakeOneDrivePage(url="https://example.com/whatever")
        page.signin_form = True
        with pytest.raises(OneDriveUploadError) as info:
            _open_root(page)
        assert info.value.needs_login is True
        assert len(page.goto_urls) == 1  # did not keep trying

    def test_landing_somewhere_unrecognised_names_what_was_tried(self):
        """A wrong URL guess renders a page rather than failing the navigation, so the
        error has to say where it actually ended up."""
        from noveltrans.onedrive_upload import _ROOT_URLS, _open_root

        page = _FakeOneDrivePage(url="https://example.com/nope")
        with pytest.raises(OneDriveUploadError, match="Không mở được OneDrive"):
            _open_root(page)
        assert page.goto_urls == list(_ROOT_URLS)


class TestCurrentFolderAndListing:
    """MEASURED: OneDrive has NO breadcrumb. The current folder comes from the page title,
    the list header's last line, or the URL's `id=` — in that order."""

    def test_the_page_title_names_the_current_folder(self):
        from noveltrans.onedrive_upload import _current_folder

        page = _FakeOneDrivePage({"NovelTrans": {"Đấu La": {}}})
        page.cwd = ["NovelTrans", "Đấu La"]
        assert _current_folder(page) == "Đấu La"

    def test_the_root_reads_as_my_files(self):
        from noveltrans.onedrive_upload import _current_folder

        assert _current_folder(_FakeOneDrivePage()) == "Tệp của tôi"

    def test_the_list_header_is_the_fallback_when_the_title_is_useless(self):
        from noveltrans.onedrive_upload import _current_folder

        page = _FakeOneDrivePage({"NovelTrans": {}})
        page.cwd = ["NovelTrans"]
        page.title = lambda: "OneDrive"  # no " - OneDrive" suffix to strip
        assert _current_folder(page) == "NovelTrans"

    def test_the_url_is_the_last_resort(self):
        from noveltrans.onedrive_upload import _current_folder

        page = _FakeOneDrivePage(
            url="https://onedrive.live.com/my?id=%2Fpersonal%2Fabc%2FDocuments%2F%C4%90%E1%BA%A5u%20La"
        )
        page.title = lambda: ""
        page.missing.add("[data-automationid='appListHeader']")
        assert _current_folder(page) == "Đấu La"

    def test_the_column_header_row_is_not_an_item(self):
        """MEASURED: the first `field-LinkFilename` reads "Name". Counting it would have
        us find a folder called Name, and treat it as a delivered file when verifying."""
        from noveltrans.onedrive_upload import _list_names

        assert "Name" not in _list_names(_FakeOneDrivePage({"Đấu La": {}}))

    def test_listing_reads_the_current_folder(self):
        from noveltrans.onedrive_upload import _list_names

        page = _FakeOneDrivePage({"NovelTrans": {"Đấu La": {}, "Khác": {}}})
        page.cwd = ["NovelTrans"]
        assert sorted(_list_names(page)) == ["Khác", "Đấu La"]

    def test_both_degrade_to_empty_rather_than_raising(self):
        from noveltrans.onedrive_upload import (
            _LIST_HEADER_SEL,
            _ROW_NAME_SEL,
            _ROW_SEL,
            _current_folder,
            _list_names,
        )

        page = _FakeOneDrivePage(
            {"a": {}},
            url="https://onedrive.live.com/my",  # no `id=` to fall back on
            missing={_LIST_HEADER_SEL, _ROW_NAME_SEL, _ROW_SEL},
        )
        page.title = lambda: ""
        assert _current_folder(page) == ""
        assert _list_names(page) == []


class TestEnterFolder:
    def test_enters_an_existing_folder(self):
        from noveltrans.onedrive_upload import _current_folder, _enter_folder

        page = _FakeOneDrivePage({"NovelTrans": {}})
        assert _enter_folder(page, "NovelTrans") is True
        assert _current_folder(page) == "NovelTrans"

    def test_a_missing_folder_is_false_not_an_error(self):
        """"Not there" is a normal state — it is how `_ensure_folder` learns to create it."""
        from noveltrans.onedrive_upload import _enter_folder

        page = _FakeOneDrivePage({"Khác": {}})
        assert _enter_folder(page, "NovelTrans") is False
        assert page.clicked == []

    def test_matching_is_case_insensitive_like_onedrive_itself(self):
        """OneDrive will not let `NovelTrans` and `noveltrans` coexist, so treating them
        as different would make us create a folder the server then refuses or renames."""
        from noveltrans.onedrive_upload import _enter_folder

        page = _FakeOneDrivePage({"noveltrans": {}})
        assert _enter_folder(page, "NovelTrans") is True

    def test_clicking_without_landing_raises_rather_than_carrying_on(self):
        """The drift case. Continuing would upload the whole novel into whatever folder
        happened to be open."""
        from noveltrans.onedrive_upload import _enter_folder

        # Neither gesture opens anything — the shape of a renamed row element.
        page = _FakeOneDrivePage({"NovelTrans": {}}, opens_on="nothing")
        with pytest.raises(OneDriveUploadError, match="không mở nó"):
            _enter_folder(page, "NovelTrans")

    def test_double_click_is_tried_first_and_stops_there(self):
        """MEASURED: a double-click opens a folder. A single click on the name only
        selects the row — the first live probe clicked it and the view did not move."""
        from noveltrans.onedrive_upload import _current_folder, _enter_folder

        page = _FakeOneDrivePage({"NovelTrans": {}}, opens_on="dblclick")
        assert _enter_folder(page, "NovelTrans") is True
        assert page.clicked == [("dblclick", "NovelTrans")]
        assert _current_folder(page) == "NovelTrans"

    def test_it_still_falls_back_to_a_single_click(self):
        """Kept in case a later build makes the name a real link."""
        from noveltrans.onedrive_upload import _enter_folder

        page = _FakeOneDrivePage({"NovelTrans": {}}, opens_on="click")
        assert _enter_folder(page, "NovelTrans") is True
        assert [g for g, _ in page.clicked] == ["dblclick", "click"]

    def test_a_diacritic_name_round_trips(self):
        from noveltrans.onedrive_upload import _current_folder, _enter_folder

        page = _FakeOneDrivePage({"Đấu La Đại Lục": {}})
        assert _enter_folder(page, "Đấu La Đại Lục") is True
        assert _current_folder(page) == "Đấu La Đại Lục"


class TestCreateFolder:
    def test_creates_and_verifies(self):
        from noveltrans.onedrive_upload import _create_folder, _list_names

        page = _FakeOneDrivePage({})
        _create_folder(page, "NovelTrans")
        assert "NovelTrans" in _list_names(page)
        assert page.typed[-1] == "NovelTrans"

    def test_the_create_click_is_confined_to_the_dialog(self):
        """**The live hang.** “Tạo” is a substring of “Tạo hoặc tải lên”, the command-bar
        button, and `has-text` is a substring match — so the page-wide ladder found that
        button *behind the modal* and Playwright retried a blocked click until it gave
        up. The run sat there with the dialog open and the name already typed.

        Measured live: `button:has-text("Tạo")` returns 2 with the command-bar button
        first, while `.ms-Dialog-actions button:has-text("Tạo")` returns exactly 1.
        Scope is the fix.
        """
        from noveltrans.onedrive_upload import _CREATE_TEXTS, _click_in_dialog

        page = _FakeOneDrivePage({})
        page.menu = "new-folder"
        assert _click_in_dialog(page, _CREATE_TEXTS) is True
        for branch in page.clicked_selectors[-1].split(","):
            # Every branch must be "<dialog root> <button>", never a bare button.
            assert " " in branch.strip(), f"unscoped branch: {branch!r}"
            assert not branch.strip().startswith("button"), branch

    def test_the_dialog_roots_are_not_comma_joined_into_the_selector(self):
        """Interpolating a comma list would yield `rootA, rootB button:has-text(…)`,
        whose first branch matches a whole dialog — clicking that, not the button."""
        from noveltrans.onedrive_upload import _CREATE_TEXTS, _click_in_dialog

        page = _FakeOneDrivePage({})
        page.menu = "new-folder"
        _click_in_dialog(page, _CREATE_TEXTS)
        for branch in page.clicked_selectors[-1].split(","):
            assert ":has-text(" in branch, f"bare dialog branch: {branch!r}"

    def test_an_auto_renamed_twin_stops_the_run(self):
        """The dangerous outcome: OneDrive answering a collision with `NovelTrans 1`
        builds a second, divergent tree nobody notices until they open the folder."""
        from noveltrans.onedrive_upload import _create_folder

        page = _FakeOneDrivePage(
            {"NovelTrans": {}}, auto_rename=True, hidden_folders={"NovelTrans"}
        )
        with pytest.raises(OneDriveUploadError, match="thay vì"):
            _create_folder(page, "NovelTrans")

    @pytest.mark.parametrize(
        "broken,message",
        [
            ("new-menu", "menu “Tạo hoặc tải lên”"),
            ("folder-item", "mục “Thư mục”"),
            ("name-box", "ô nhập tên"),
            ("create-button", "nút “Tạo”"),
        ],
    )
    def test_every_step_of_the_ladder_fails_by_name(self, broken, message):
        """A failure report has to say which rung broke, or retuning selectors is guesswork."""
        from noveltrans.onedrive_upload import (
            _CREATE_BUTTON_SELS,
            _CREATE_TEXTS,
            _FOLDER_NAME_INPUT_SELS,
            _NEW_FOLDER_SELS,
            _NEW_FOLDER_TEXTS,
            _NEW_MENU_SELS,
            _NEW_MENU_TEXTS,
            _create_folder,
        )

        selectors, texts = {
            "new-menu": (set(_NEW_MENU_SELS), _NEW_MENU_TEXTS),
            "folder-item": (set(_NEW_FOLDER_SELS), _NEW_FOLDER_TEXTS),
            "name-box": (set(_FOLDER_NAME_INPUT_SELS), ()),
            "create-button": (set(_CREATE_BUTTON_SELS), _CREATE_TEXTS),
        }[broken]
        page = _FakeOneDrivePage({}, missing=selectors, missing_texts=texts)
        with pytest.raises(OneDriveUploadError, match=message):
            _create_folder(page, "NovelTrans")


class TestEnsureFolderAndPath:
    def test_existing_folder_is_entered_not_recreated(self):
        from noveltrans.onedrive_upload import _current_folder, _ensure_folder

        page = _FakeOneDrivePage({"NovelTrans": {}})
        _ensure_folder(page, "NovelTrans")
        assert page.typed == []  # nothing was created
        assert _current_folder(page) == "NovelTrans"

    def test_missing_folder_is_created_then_entered(self):
        from noveltrans.onedrive_upload import _current_folder, _ensure_folder

        page = _FakeOneDrivePage({})
        _ensure_folder(page, "NovelTrans")
        assert page.typed == ["NovelTrans"]
        assert _current_folder(page) == "NovelTrans"

    def test_ensure_path_walks_the_whole_way_down(self):
        from noveltrans.onedrive_upload import _current_folder, _ensure_path

        page = _FakeOneDrivePage({})
        _ensure_path(page, ["NovelTrans", "Đấu La", "exports", "audio"])
        assert _current_folder(page) == "audio"
        assert page.cwd == ["NovelTrans", "Đấu La", "exports", "audio"]

    def test_ensure_path_reuses_what_is_already_there(self):
        from noveltrans.onedrive_upload import _ensure_path

        page = _FakeOneDrivePage({"NovelTrans": {"Đấu La": {"exports": {}}}})
        _ensure_path(page, ["NovelTrans", "Đấu La", "exports"])
        assert page.typed == []

    def test_blank_segments_are_skipped(self):
        """A root-level file's `folder_segments` is empty; a stray "" must not try to
        create a folder with no name."""
        from noveltrans.onedrive_upload import _ensure_path

        page = _FakeOneDrivePage({})
        _ensure_path(page, ["", "NovelTrans", ""])
        assert page.cwd == ["NovelTrans"]


class TestFolderSegments:
    @pytest.mark.parametrize(
        "relpath,expected",
        [
            ("meta.json", []),
            ("exports/a.epub", ["exports"]),
            ("exports/audio/0001.mp3", ["exports", "audio"]),
            ("exports/video/p1/p1.mp4", ["exports", "video", "p1"]),
            ("/exports/audio/x.mp3", ["exports", "audio"]),
            ("", []),
        ],
    )
    def test_segments(self, relpath, expected):
        from noveltrans.onedrive_upload import folder_segments

        assert folder_segments(relpath) == expected

    def test_matches_the_payload_items_own_folder(self, project):
        from noveltrans.onedrive_upload import folder_segments

        for item in collect_payload(project):
            assert "/".join(folder_segments(item.relpath)) == item.folder


# -- the transfer (step 5) ----------------------------------------------------


def _expected(files) -> dict:
    """`{filename: size}` — what `_wait_for_batch` now takes. A list of names is no longer
    enough: a replaced file is present under its own name before anything is sent."""
    return {p.name: p.stat().st_size for p in files}


def _twin_name(name: str) -> str:
    """What OneDrive calls a file it refused to overwrite. Mirrors `_renamed_twin`."""
    stem, dot, ext = name.rpartition(".")
    return f"{stem} 1{dot}{ext}" if dot else f"{name} 1"


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    paths = []
    for name in ("phan-1.mp4", "phan-1.jpg", "phan-1.title.txt"):
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(path)
    return paths


class TestRenamedTwin:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("phan-1.mp4", "phan-1 1.mp4"),
            ("a.b.srt", "a.b 1.srt"),
            ("README", "README 1"),
        ],
    )
    def test_twin_names(self, name, expected):
        from noveltrans.onedrive_upload import _renamed_twin

        assert _renamed_twin(name) == expected


class TestSendFiles:
    """The one step with no alternative: if the files never go in, nothing else matters."""

    def test_uses_the_first_input_that_works(self, files):
        from noveltrans.onedrive_upload import _UPLOAD_INPUT_SELS, _send_files

        page = _FakeOneDrivePage(arrive_after=1)
        _send_files(page, files)
        assert page.sent == [(_UPLOAD_INPUT_SELS[0], [str(p) for p in files])]

    def test_falls_through_to_the_plain_html_selector(self, files):
        """The narrow `data-automationid` forms are the ones that drift; plain
        `input[type=file]` cannot be renamed away."""
        from noveltrans.onedrive_upload import _UPLOAD_INPUT_SELS, _send_files

        page = _FakeOneDrivePage(upload_accepting={_UPLOAD_INPUT_SELS[-1]})
        _send_files(page, files)
        assert page.sent[0][0] == _UPLOAD_INPUT_SELS[-1]

    def test_never_touches_the_directory_input(self, files):
        """`set_input_files` on the webkitdirectory input silently flattens the tree —
        and the manifest would then record the flattening as a success."""
        from noveltrans.onedrive_upload import _UPLOAD_DIRECTORY_INPUT_SEL, _send_files

        page = _FakeOneDrivePage(
            upload_accepting={_UPLOAD_DIRECTORY_INPUT_SEL},
            arrive_after=1,
        )
        page.expect_file_chooser = None
        with pytest.raises(OneDriveUploadError):
            _send_files(page, files)
        assert page.sent == []

    def test_a_silent_no_op_fails_here_not_ten_steps_later(self, files):
        """Setting the input and nothing happening must be caught at the step that did
        it, while we still know which one it was."""
        from noveltrans.onedrive_upload import _send_files

        page = _FakeOneDrivePage(arrive_after=10_000)  # never lands
        page.expect_file_chooser = None
        with pytest.raises(OneDriveUploadError, match="không bắt đầu tải lên"):
            _send_files(page, files)

    def test_a_progress_line_alone_counts_as_accepted(self, files):
        """A multi-GB batch shows progress long before anything is in the listing."""
        from noveltrans.onedrive_upload import _send_files

        page = _FakeOneDrivePage(arrive_after=10_000, status_texts=["Đang tải lên 3 mục"])
        _send_files(page, files)  # does not raise

    def test_files_already_in_the_listing_also_count(self, files):
        """A batch of small text files can finish before the first poll; gating only on a
        progress line would call that a failure."""
        from noveltrans.onedrive_upload import _send_files

        page = _FakeOneDrivePage(arrive_after=1)
        _send_files(page, files)
        assert page.polls <= 2


class TestResolveConflicts:
    def test_no_dialog_is_not_a_problem(self):
        from noveltrans.onedrive_upload import _resolve_conflicts

        assert _resolve_conflicts(_FakeOneDrivePage()) is False

    def test_replace_is_chosen(self):
        """We are mirroring a local tree, so the local copy is the intended truth."""
        from noveltrans.onedrive_upload import _resolve_conflicts

        page = _FakeOneDrivePage(conflict=True)
        assert _resolve_conflicts(page) is True
        assert page.replaced is True
        assert page.kept_both is False

    def test_a_dialog_with_no_replace_button_stops_the_run(self):
        """Pressing whatever else is there is how you end up with `phan-1 1.mp4`."""
        from noveltrans.onedrive_upload import _REPLACE_TEXTS, _resolve_conflicts

        page = _FakeOneDrivePage(conflict=True, missing_texts=_REPLACE_TEXTS)
        with pytest.raises(OneDriveUploadError, match="Giữ cả hai|Thay thế"):
            _resolve_conflicts(page)


class TestVerifyBatch:
    def test_all_present_is_silent(self):
        from noveltrans.onedrive_upload import _verify_batch

        page = _FakeOneDrivePage({"a.mp4": {}, "b.jpg": {}})
        _verify_batch(page, {"a.mp4": 5, "b.jpg": 5})

    def test_a_missing_file_is_named(self):
        from noveltrans.onedrive_upload import _verify_batch

        page = _FakeOneDrivePage({"a.mp4": {}})
        with pytest.raises(OneDriveUploadError, match="không nhận đủ 1 file") as info:
            _verify_batch(page, {"a.mp4": 5, "b.jpg": 5})
        assert info.value.relpath == "b.jpg"

    def test_an_auto_renamed_twin_stops_the_run(self):
        """A batch that silently became a set of ` 1` copies reads as a successful upload
        while the file the user believes is mirrored still holds the old bytes."""
        from noveltrans.onedrive_upload import _verify_batch

        page = _FakeOneDrivePage({"a.mp4": {}, "a 1.mp4": {}})
        with pytest.raises(OneDriveUploadError, match="bản sao"):
            _verify_batch(page, {"a.mp4": 5})

    def test_the_twin_check_runs_before_the_missing_check(self):
        """A twin means the upload happened and went wrong; "missing" would be a
        misleading way to report that."""
        from noveltrans.onedrive_upload import _verify_batch

        page = _FakeOneDrivePage({"a 1.mp4": {}})
        with pytest.raises(OneDriveUploadError, match="bản sao"):
            _verify_batch(page, {"a.mp4": 5, "b.jpg": 5})


class TestWaitForBatch:
    def test_returns_once_every_file_is_listed(self, files):
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=2, status_texts=["Đang tải lên 3 mục"])
        _send_files(page, files)
        _wait_for_batch(page, _expected(files), timeout_ms=600_000)

    def test_progress_is_reported_only_when_the_line_changes(self, files):
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(
            arrive_after=4,
            status_texts=["Đang tải lên 3 mục", "Đang tải lên 3 mục", "Uploading 50%"],
        )
        seen: list[str] = []
        _send_files(page, files)
        _wait_for_batch(
            page, _expected(files), timeout_ms=600_000, on_progress=seen.append
        )
        assert seen == list(dict.fromkeys(seen))  # no repeats

    def test_a_stall_fails_with_what_it_stalled_on(self, files):
        """The guard that actually fires on a dropped connection — long before the
        size-derived ceiling would."""
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=10_000, status_texts=["Đang tải lên 3 mục"])
        _send_files(page, files)
        with pytest.raises(OneDriveUploadError, match="ngừng tiến triển") as info:
            _wait_for_batch(page, _expected(files), timeout_ms=6 * 3_600_000)
        assert "Đang tải lên 3 mục" in str(info.value)

    def test_the_stall_detector_beats_the_ceiling(self, files):
        """With a six-hour ceiling and a ten-minute stall window, a dead transfer must
        report the stall — not sit there for six hours."""
        from noveltrans.onedrive_upload import _STALL_MS, _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=10_000, status_texts=["Đang tải lên"])
        _send_files(page, files)
        with pytest.raises(OneDriveUploadError, match="ngừng tiến triển"):
            _wait_for_batch(page, _expected(files), timeout_ms=6 * 3_600_000)
        # Polls are 2s; it must not have run far past the stall window.
        assert page.polls < (_STALL_MS // 2_000) + 10

    def test_status_saying_done_while_files_are_missing_is_reported_as_missing(
        self, files
    ):
        """The listing is the authority. "OneDrive said it finished" is not evidence."""
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=10_000, status_texts=["Đã tải lên 3 mục"])
        _send_files(page, files)
        with pytest.raises(OneDriveUploadError, match="không nhận đủ 3 file"):
            _wait_for_batch(page, _expected(files), timeout_ms=6 * 3_600_000)

    def test_an_auto_renamed_arrival_is_caught(self, files):
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        names = [p.name for p in files]
        page = _FakeOneDrivePage(
            arrive_after=1,
            status_texts=["Đã tải lên"],
            rename_on_arrival=set(names),
        )
        _send_files(page, files)
        with pytest.raises(OneDriveUploadError, match="bản sao"):
            _wait_for_batch(page, {n: 1 for n in names}, timeout_ms=600_000)

    def test_a_quota_message_stops_immediately(self, files):
        """Every remaining batch would fail identically, so this is not a per-file error."""
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(
            arrive_after=10_000, status_texts=["Đang tải lên", "Your storage is full"]
        )
        _send_files(page, files)
        with pytest.raises(OneDriveUploadError, match="hết dung lượng"):
            _wait_for_batch(page, _expected(files), timeout_ms=600_000)

    def test_a_conflict_dialog_mid_transfer_is_answered(self, files):
        """It appears at no predictable moment, so it is answered every poll."""
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=3, status_texts=["Đang tải lên"])
        _send_files(page, files)
        page.conflict = True
        _wait_for_batch(page, _expected(files), timeout_ms=600_000)
        assert page.replaced is True

    def test_cancel_is_honoured_and_carries_the_count(self, files):
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=10_000, status_texts=["Đang tải lên"])
        _send_files(page, files)
        with pytest.raises(OneDriveCancelled) as info:
            _wait_for_batch(
                page,
                _expected(files),
                timeout_ms=600_000,
                should_cancel=lambda: True,
                uploaded=5,
            )
        assert info.value.uploaded == 5

    def test_the_ceiling_still_fires_when_something_keeps_moving(self, files):
        """A status line that changes forever but never delivers defeats the stall
        detector; the size-derived ceiling is the backstop for exactly that."""
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        page = _FakeOneDrivePage(arrive_after=10_000)
        page.status_texts = [f"Đang tải lên {i}%" for i in range(600)]
        _send_files(page, files)
        with pytest.raises(OneDriveUploadError, match="Quá thời gian chờ"):
            _wait_for_batch(page, _expected(files), timeout_ms=120_000)


# -- the whole run (step 6) ---------------------------------------------------


@pytest.fixture
def pushable(tmp_path: Path) -> Path:
    """A small but real project: a genuine sqlite database, an export, two audio files."""
    root = tmp_path / "dau-la"
    (root / "exports" / "audio").mkdir(parents=True)
    (root / "meta.json").write_text('{"title": "Đấu La"}', encoding="utf-8")
    db = sqlite3.connect(root / "chapters.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE chapters (idx INTEGER PRIMARY KEY)")
    db.executemany("INSERT INTO chapters VALUES (?)", [(i,) for i in range(5)])
    db.commit()
    db.close()
    (root / "exports" / "dau-la.epub").write_bytes(b"epub")
    (root / "exports" / "audio" / "0001.mp3").write_bytes(b"mp3")
    (root / "exports" / "audio" / "0002.mp3").write_bytes(b"mp3")
    return root


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]

    def new_page(self):
        return self.pages[0]


@pytest.fixture
def drive(monkeypatch):
    """Wire a fake OneDrive in behind `push_project`'s browser plumbing.

    Returns a factory: `drive(page)` installs `page` as the browser this run gets.
    """
    import noveltrans.onedrive_upload as od

    def install(page):
        monkeypatch.setattr(od, "_require_playwright", lambda: object())
        monkeypatch.setattr(
            od, "_launch_context", lambda pw, *, headless: (object(), _FakeContext(page))
        )
        monkeypatch.setattr(od, "_close", lambda context, pw: None)
        return page

    return install


def _rel(project: Path, path) -> str:
    """The manifest key for a source path.

    Everything is under the project except `chapters.db`, which is sent from a snapshot
    in a temp dir — its key is still the plain relpath.
    """
    path = Path(path)
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.name


def _request(project: Path, title="Đấu La", force=False):
    from noveltrans.onedrive_upload import PushRequest

    return PushRequest(project_path=project, novel_title=title, force=force)


class TestPreviewPush:
    def test_a_fresh_project_uploads_everything(self, pushable):
        from noveltrans.onedrive_upload import preview_push

        preview = preview_push(_request(pushable))
        assert sorted(i.relpath for i in preview.to_upload) == [
            "chapters.db",
            "exports/audio/0001.mp3",
            "exports/audio/0002.mp3",
            "exports/dau-la.epub",
            "meta.json",
        ]
        assert preview.to_skip == []
        assert preview.remote_root == "/NovelTrans/Đấu La"
        assert preview.upload_bytes > 0

    def test_it_opens_no_browser(self, pushable, monkeypatch):
        """The confirmation dialog builds from this; launching Chrome to draw a dialog
        would be absurd."""
        import noveltrans.onedrive_upload as od

        monkeypatch.setattr(
            od, "_require_playwright", lambda: pytest.fail("launched a browser")
        )
        od.preview_push(_request(pushable))

    def test_it_leaves_no_scratch_files_behind(self, pushable):
        """The database snapshot goes to a temp dir, not into the project — otherwise the
        next run would find it and try to upload it."""
        from noveltrans.onedrive_upload import preview_push

        before = sorted(p.name for p in pushable.rglob("*"))
        preview_push(_request(pushable))
        assert sorted(p.name for p in pushable.rglob("*")) == before

    def test_a_second_preview_after_a_push_skips_what_landed(self, pushable, drive):
        from noveltrans.onedrive_upload import preview_push, push_project

        drive(_FakeOneDrivePage())
        push_project(_request(pushable))
        preview = preview_push(_request(pushable))
        assert preview.to_upload == []
        assert len(preview.to_skip) == 5

    def test_force_ignores_the_manifest(self, pushable, drive):
        from noveltrans.onedrive_upload import preview_push, push_project

        drive(_FakeOneDrivePage())
        push_project(_request(pushable))
        preview = preview_push(_request(pushable, force=True))
        assert len(preview.to_upload) == 5
        assert preview.to_skip == []

    def test_a_corrupt_manifest_is_surfaced_not_swallowed(self, pushable):
        """The GUI has to be able to say "không đọc được trạng thái cũ" rather than
        silently offering to re-upload sixty gigabytes."""
        from noveltrans.onedrive_upload import manifest_path, preview_push

        manifest_path(pushable).write_text("{oops", encoding="utf-8")
        preview = preview_push(_request(pushable))
        assert preview.manifest_note
        assert len(preview.to_upload) == 5

    def test_the_database_size_agrees_with_what_the_run_records(self, pushable, drive):
        """The snapshot is what goes up, so it is what the skip rule must compare — and
        the preview has to reach the same number as the run, or the dialog offers to
        upload a file the run then skips.

        (The preview's `chapters.db` item points into a temp dir that is gone by the time
        it returns. That is fine and deliberate: a preview is counted, never sent.)
        """
        from noveltrans.onedrive_upload import preview_push, push_project, read_manifest

        previewed = next(
            i for i in preview_push(_request(pushable)).to_upload
            if i.relpath == "chapters.db"
        )
        drive(_FakeOneDrivePage())
        push_project(_request(pushable))
        assert previewed.size == read_manifest(pushable).files["chapters.db"]["size"]
        assert previewed.size > 0


class TestPushProject:
    def test_mirrors_the_tree(self, pushable, drive):
        from noveltrans.onedrive_upload import push_project

        page = drive(_FakeOneDrivePage())
        result = push_project(_request(pushable))

        assert result.uploaded == 5
        assert result.failed == 0
        assert result.remote_root == "/NovelTrans/Đấu La"
        novel = page.tree["NovelTrans"]["Đấu La"]
        assert set(novel) >= {"meta.json", "chapters.db", "exports"}
        assert set(novel["exports"]) == {"dau-la.epub", "audio"}
        assert set(novel["exports"]["audio"]) == {"0001.mp3", "0002.mp3"}

    def test_nothing_to_do_launches_no_browser(self, pushable, drive, monkeypatch):
        """A no-op push must not cost the user a Chrome window and thirty seconds."""
        import noveltrans.onedrive_upload as od

        drive(_FakeOneDrivePage())
        od.push_project(_request(pushable))

        monkeypatch.setattr(
            od, "_require_playwright", lambda: pytest.fail("launched a browser")
        )
        result = od.push_project(_request(pushable))
        assert (result.uploaded, result.skipped) == (0, 5)

    def test_the_manifest_says_sending_while_the_batch_is_in_flight(
        self, pushable, drive, monkeypatch
    ):
        """Written before the batch goes out, so a crash mid-transfer leaves a record
        saying "this was in flight" rather than nothing at all.

        Checked *during* `_send_files` — reading the file afterwards would only prove the
        final state, which is the part that was never in doubt.
        """
        import noveltrans.onedrive_upload as od

        drive(_FakeOneDrivePage())
        in_flight: list[dict] = []
        original = od._send_files

        def peek(page, paths):
            on_disk = json.loads(
                od.manifest_path(pushable).read_text(encoding="utf-8")
            )["files"]
            in_flight.append(
                {Path(p).name: on_disk.get(_rel(pushable, p), {}).get("status")
                 for p in paths}
            )
            return original(page, paths)

        monkeypatch.setattr(od, "_send_files", peek)
        od.push_project(_request(pushable))

        # Every batch saw its own files recorded as `sending` before they were handed over.
        assert in_flight
        for batch in in_flight:
            assert set(batch.values()) == {od.STATUS_SENDING}, batch

        final = od.read_manifest(pushable)
        assert len(final.files) == 5
        assert {f["status"] for f in final.files.values()} == {od.STATUS_DONE}

    def test_a_crash_mid_batch_leaves_sending_which_re_uploads(self, pushable, drive):
        from noveltrans.onedrive_upload import (
            STATUS_SENDING,
            collect_payload,
            plan_uploads,
            push_project,
            read_manifest,
        )

        drive(_FakeOneDrivePage(fail_in={"audio"}))
        push_project(_request(pushable))
        manifest = read_manifest(pushable)
        stuck = [k for k, v in manifest.files.items() if v["status"] == STATUS_SENDING]
        assert sorted(stuck) == ["exports/audio/0001.mp3", "exports/audio/0002.mp3"]
        # And the next run picks them up again without being asked.
        to_upload, _ = plan_uploads(collect_payload(pushable), manifest)
        assert set(stuck) <= {i.relpath for i in to_upload}

    def test_a_failed_batch_does_not_kill_the_run(self, pushable, drive):
        """A drifted selector in one folder must not cost the user the other fifty-nine
        gigabytes."""
        from noveltrans.onedrive_upload import push_project

        page = drive(_FakeOneDrivePage(fail_in={"audio"}))
        errors: list[tuple[str, str]] = []
        result = push_project(
            _request(pushable), on_file_done=lambda r, e: errors.append((r, e))
        )

        assert result.uploaded == 3  # meta.json, chapters.db, the epub
        assert result.failed == 2  # the two mp3s
        assert sorted(r for r, e in errors if e) == [
            "exports/audio/0001.mp3",
            "exports/audio/0002.mp3",
        ]
        assert "dau-la.epub" in page.tree["NovelTrans"]["Đấu La"]["exports"]

    def test_a_fatal_error_stops_the_whole_run(self, pushable, drive):
        """Quota: every remaining batch would fail identically, so marking file after
        file as failed would be noise."""
        from noveltrans.onedrive_upload import push_project

        page = drive(
            _FakeOneDrivePage(arrive_after=10_000, status_texts=["Your storage is full"])
        )
        with pytest.raises(OneDriveUploadError, match="hết dung lượng"):
            push_project(_request(pushable))
        assert len(page.sent) == 1  # it stopped rather than trying the rest

    def test_needs_login_is_fatal_and_reaches_the_caller(self, pushable, drive):
        from noveltrans.onedrive_upload import push_project

        drive(_FakeOneDrivePage(url="https://login.live.com/login.srf"))
        with pytest.raises(OneDriveUploadError) as info:
            push_project(_request(pushable))
        assert info.value.needs_login is True
        assert info.value.fatal is True

    def test_cancel_raises_with_how_far_it_got(self, pushable, drive):
        from noveltrans.onedrive_upload import push_project

        drive(_FakeOneDrivePage())
        with pytest.raises(OneDriveCancelled):
            push_project(_request(pushable), should_cancel=lambda: True)

    def test_cancelling_still_writes_what_landed(self, pushable, drive):
        """Otherwise the next run re-sends files that are already up there."""
        from noveltrans.onedrive_upload import manifest_path, push_project

        drive(_FakeOneDrivePage())
        calls = {"n": 0}

        def cancel_after_two_batches():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(OneDriveCancelled):
            push_project(_request(pushable), should_cancel=cancel_after_two_batches)
        assert manifest_path(pushable).is_file()

    def test_checkpoint_runs_between_batches_never_mid_transfer(self, pushable, drive):
        """Pausing mid-transfer would mean holding a half-sent batch open."""
        from noveltrans.onedrive_upload import push_project

        page = drive(_FakeOneDrivePage())
        sent_at_checkpoint: list[int] = []
        push_project(
            _request(pushable),
            on_checkpoint=lambda: sent_at_checkpoint.append(len(page.sent)),
        )
        # One checkpoint per batch, each before that batch was handed over.
        assert sent_at_checkpoint == list(range(len(sent_at_checkpoint)))

    def test_progress_reports_counts_and_a_message(self, pushable, drive):
        from noveltrans.onedrive_upload import push_project

        drive(_FakeOneDrivePage())
        seen: list[tuple[int, int, str]] = []
        push_project(_request(pushable), on_progress=lambda d, t, m: seen.append((d, t, m)))
        assert seen[0][1] == 5  # total known up front
        assert seen[-1][0] == 5  # finished
        assert any("/NovelTrans/Đấu La" in m for _d, _t, m in seen)

    def test_every_file_is_reported_once(self, pushable, drive):
        from noveltrans.onedrive_upload import push_project

        drive(_FakeOneDrivePage())
        done: list[str] = []
        push_project(_request(pushable), on_file_done=lambda r, e: done.append(r))
        assert sorted(done) == [
            "chapters.db",
            "exports/audio/0001.mp3",
            "exports/audio/0002.mp3",
            "exports/dau-la.epub",
            "meta.json",
        ]

    def test_the_account_is_recorded(self, pushable, drive):
        from noveltrans.onedrive_upload import push_project, read_manifest

        drive(_FakeOneDrivePage(account="ai-do@example.com"))
        push_project(_request(pushable))
        assert read_manifest(pushable).account == "ai-do@example.com"

    def test_renaming_the_novel_does_not_strand_the_uploaded_tree(self, pushable, drive):
        """The first push writes the remote path down and every later push honours it, so
        a rename changes what the app shows and nothing on OneDrive."""
        from noveltrans.onedrive_upload import push_project

        drive(_FakeOneDrivePage())
        push_project(_request(pushable))
        (pushable / "exports" / "new.epub").write_bytes(b"more")

        page = drive(_FakeOneDrivePage({"NovelTrans": {"Đấu La": {"exports": {}}}}))
        result = push_project(_request(pushable, title="Một tên khác"))
        assert result.remote_root == "/NovelTrans/Đấu La"
        assert "Một tên khác" not in page.tree["NovelTrans"]

    def test_the_tree_is_only_re_walked_when_the_folder_changes(self, pushable, drive):
        """`exports/audio` can be two thousand files and a hundred batches; re-navigating
        from the root for each would cost more than the transfers."""
        import noveltrans.onedrive_upload as od

        drive(_FakeOneDrivePage())
        original = od.batch_payload
        # One file per batch: five batches across three distinct folders, so two of the
        # five follow a batch in the SAME folder.
        od.batch_payload = lambda items, **kw: original(items, max_files=1)
        navigations = []
        real_send = od._send_files
        od._send_files = lambda pg, paths: (
            navigations.append(len(pg.goto_urls)) or real_send(pg, paths)
        )
        try:
            od.push_project(_request(pushable))
        finally:
            od.batch_payload = original
            od._send_files = real_send

        # The property that matters: a batch whose folder is unchanged navigates NOWHERE.
        # (Counting total gotos would only measure how deep-linking is implemented.)
        assert len(navigations) == 5
        same_folder_hops = [
            after - before
            for before, after in zip(navigations, navigations[1:])
        ]
        assert same_folder_hops.count(0) == 2, same_folder_hops


class TestWaitForSignin:
    """Signing in walks through several redirects; "no form right now" is not "done"."""

    class _Flow:
        """A page that reports a scripted sequence of (url, form-present) per poll."""

        def __init__(self, steps):
            self.steps = list(steps)
            self.polls = 0
            self.url, self.signin_form = self.steps[0]

        def wait_for_timeout(self, ms):
            self.polls += 1
            if self.polls < len(self.steps):
                self.url, self.signin_form = self.steps[self.polls]

        def locator(self, selector):
            from noveltrans.onedrive_upload import _SIGNIN_FORM_SELS

            present = self.signin_form and selector in _SIGNIN_FORM_SELS
            return _FakeLocator(present)

    def test_a_momentary_gap_between_redirects_is_not_a_finished_login(self):
        """Email box → (blank redirect) → password box. One clear reading would call the
        gap a finished login and go hunting for a file list mid-flow."""
        from noveltrans.onedrive_upload import _wait_for_signin

        page = self._Flow(
            [
                ("https://login.microsoftonline.com/common/oauth2", True),
                ("https://login.microsoftonline.com/common/redirect", False),
                ("https://login.microsoftonline.com/common/oauth2", True),
                ("https://www.office.com/onedrive", False),
                ("https://www.office.com/onedrive", False),
            ]
        )
        _wait_for_signin(page, timeout_ms=60_000)
        assert page.polls >= 4  # it did not stop at the gap on poll 1

    def test_an_already_valid_session_returns_almost_at_once(self):
        from noveltrans.onedrive_upload import _wait_for_signin

        page = self._Flow([("https://onedrive.live.com/?id=root", False)] * 5)
        _wait_for_signin(page, timeout_ms=60_000)
        assert page.polls <= 2

    def test_never_finishing_says_what_to_do(self):
        from noveltrans.onedrive_upload import _wait_for_signin

        page = self._Flow([("https://login.live.com/", True)] * 200)
        with pytest.raises(OneDriveUploadError, match="Hết thời gian chờ"):
            _wait_for_signin(page, timeout_ms=9_000)


class TestOpenLogin:
    def test_it_drives_the_one_url_that_shows_a_sign_in_form(self, monkeypatch):
        """MEASURED: of four candidates only office.com/onedrive presents an email box.
        onedrive.live.com — the obvious guess, and the one that failed live — shows the
        product brochure."""
        import noveltrans.onedrive_upload as od

        page = _FakeOneDrivePage(url="https://onedrive.live.com/?id=root")
        monkeypatch.setattr(od, "_require_playwright", lambda: object())
        monkeypatch.setattr(
            od, "_launch_context", lambda pw, *, headless: (object(), _FakeContext(page))
        )
        monkeypatch.setattr(od, "_close", lambda ctx, pw: None)
        od.open_login(timeout_ms=10_000)
        assert page.goto_urls[0] == od._LOGIN_URL

    def test_success_means_a_file_list_was_reached_not_a_url_matched(self, monkeypatch):
        """Signing in through office.com can land anywhere in the Microsoft 365 shell, so
        a URL pattern would be a guess. Opening the root proves the session does the one
        thing the rest of the module needs."""
        import noveltrans.onedrive_upload as od

        page = _FakeOneDrivePage(url="https://www.office.com/home", account="a@b.com")
        monkeypatch.setattr(od, "_require_playwright", lambda: object())
        monkeypatch.setattr(
            od, "_launch_context", lambda pw, *, headless: (object(), _FakeContext(page))
        )
        monkeypatch.setattr(od, "_close", lambda ctx, pw: None)
        # office.com/home is not a OneDrive surface, so `_open_root` cannot confirm it.
        with pytest.raises(OneDriveUploadError):
            od.open_login(timeout_ms=10_000)

    def test_a_module_error_is_not_reworded_into_a_generic_one(self, monkeypatch):
        """`needs_login` has to survive: it is what the GUI routes to its own dialog."""
        import noveltrans.onedrive_upload as od

        page = _FakeOneDrivePage(url=MARKETING_URL)
        monkeypatch.setattr(od, "_require_playwright", lambda: object())
        monkeypatch.setattr(
            od, "_launch_context", lambda pw, *, headless: (object(), _FakeContext(page))
        )
        monkeypatch.setattr(od, "_close", lambda ctx, pw: None)
        with pytest.raises(OneDriveUploadError) as info:
            od.open_login(timeout_ms=10_000)
        assert info.value.needs_login is True


class TestOneDestinationForTheWholeLibrary:
    """The user picks one folder in Settings; every novel is a subfolder of it."""

    def test_the_configured_root_is_used(self):
        assert remote_root_for("Đấu La", "/Fox Novel") == "/Fox Novel/Đấu La"

    def test_a_missing_root_falls_back_to_the_default(self):
        from noveltrans.onedrive_upload import ROOT_FOLDER

        assert remote_root_for("Đấu La", "") == f"/{ROOT_FOLDER}/Đấu La"

    def test_a_nested_root_works(self):
        assert remote_root_for("Đấu La", "/Backup/Truyện") == "/Backup/Truyện/Đấu La"

    def test_slashes_are_forgiven(self):
        for root in ("Fox Novel", "/Fox Novel", "Fox Novel/", "//Fox Novel//"):
            assert remote_root_for("Đấu La", root) == "/Fox Novel/Đấu La"

    def test_every_root_segment_is_sanitised(self):
        """A root typed with a colon in it would otherwise fail at creation time with a
        message about the novel rather than about the root."""
        assert remote_root_for("Đấu La", "/Fox: Novel") == "/Fox Novel/Đấu La"

    def test_the_request_carries_it_to_the_preview(self, pushable):
        from noveltrans.onedrive_upload import PushRequest, preview_push

        preview = preview_push(
            PushRequest(
                project_path=pushable, novel_title="Đấu La", root_folder="/Fox Novel"
            )
        )
        assert preview.remote_root == "/Fox Novel/Đấu La"
        assert preview.root_note == ""

    def test_an_already_mirrored_novel_stays_where_it_is(self, pushable, drive):
        """Changing the destination must not strand a tree that is already up there, so
        the recorded path still wins — same rule as renaming a novel."""
        from noveltrans.onedrive_upload import PushRequest, preview_push, push_project

        drive(_FakeOneDrivePage())
        push_project(
            PushRequest(project_path=pushable, novel_title="Đấu La", root_folder="/Cũ")
        )
        preview = preview_push(
            PushRequest(
                project_path=pushable, novel_title="Đấu La", root_folder="/Mới"
            )
        )
        assert preview.remote_root == "/Cũ/Đấu La"

    def test_and_says_so_rather_than_moving_silently(self, pushable, drive):
        """Silently ignoring the new setting would look like the setting is broken."""
        from noveltrans.onedrive_upload import PushRequest, preview_push, push_project

        drive(_FakeOneDrivePage())
        push_project(
            PushRequest(project_path=pushable, novel_title="Đấu La", root_folder="/Cũ")
        )
        note = preview_push(
            PushRequest(project_path=pushable, novel_title="Đấu La", root_folder="/Mới")
        ).root_note
        assert "/Cũ/Đấu La" in note
        assert "/Mới/Đấu La" in note
        assert "Quên trạng thái" in note

    def test_a_never_pushed_novel_goes_to_the_new_root(self, pushable):
        from noveltrans.onedrive_upload import PushRequest, preview_push

        preview = preview_push(
            PushRequest(project_path=pushable, novel_title="Đấu La", root_folder="/Mới")
        )
        assert preview.remote_root == "/Mới/Đấu La"
        assert preview.root_note == ""

    def test_the_run_creates_the_root_then_the_novel_folder(self, pushable, drive):
        from noveltrans.onedrive_upload import PushRequest, push_project

        page = drive(_FakeOneDrivePage())
        push_project(
            PushRequest(
                project_path=pushable, novel_title="Đấu La", root_folder="/Fox Novel"
            )
        )
        assert "Fox Novel" in page.tree
        assert "Đấu La" in page.tree["Fox Novel"]

    def test_a_nested_root_is_created_segment_by_segment(self, pushable, drive):
        from noveltrans.onedrive_upload import PushRequest, push_project

        page = drive(_FakeOneDrivePage())
        push_project(
            PushRequest(
                project_path=pushable, novel_title="Đấu La", root_folder="/Backup/Truyện"
            )
        )
        assert "Đấu La" in page.tree["Backup"]["Truyện"]


class TestListFolders:
    """MEASURED: the size column is what tells a folder from a file — a folder reads
    "30 mục", a file reads "1,2 MB". There is no folder icon or role that says so."""

    def _page(self, rows):
        """rows: list of (name, size-cell-text)."""
        page = _FakeOneDrivePage()
        page.rows = rows
        return page

    def test_only_folders_come_back(self):
        from noveltrans.onedrive_upload import _list_folders

        page = self._page(
            [("Name", "Kích thước tệp"), ("Audio", "2 mục"), ("Documents", "30 mục"),
             ("Document.docx", "24,5 KB"), ("goodbye.rar", "1,2 MB")]
        )
        assert _list_folders(page) == ["Audio", "Documents"]

    def test_english_item_counts_are_understood_too(self):
        from noveltrans.onedrive_upload import _list_folders

        page = self._page([("Name", "File size"), ("Audio", "2 items"), ("a.txt", "3 KB")])
        assert _list_folders(page) == ["Audio"]

    def test_an_empty_folder_still_counts_as_a_folder(self):
        from noveltrans.onedrive_upload import _list_folders

        assert _list_folders(self._page([("Film", "0 mục")])) == ["Film"]

    def test_the_column_header_is_never_offered(self):
        from noveltrans.onedrive_upload import _list_folders

        page = self._page([("Name", "12 mục"), ("Audio", "2 mục")])
        assert _list_folders(page) == ["Audio"]

    def test_a_blank_size_cell_does_not_shift_every_later_row(self):
        """The columns are read separately and zipped, so dropping blanks would pair each
        name with the next row's size and mislabel everything after the gap."""
        from noveltrans.onedrive_upload import _list_folders

        page = self._page(
            [("Audio", ""), ("Documents", "30 mục"), ("a.docx", "24 KB")]
        )
        assert _list_folders(page) == ["Documents"]

    def test_mismatched_columns_fall_back_to_offering_everything(self):
        """Pairing them would be guesswork; the picker's own navigation then rejects
        anything that is not a folder, loudly."""
        from noveltrans.onedrive_upload import _ROW_SIZE_SEL, _list_folders

        page = self._page([("Audio", "2 mục"), ("Documents", "30 mục")])
        page.missing.add(_ROW_SIZE_SEL)
        assert _list_folders(page) == ["Audio", "Documents"]

    def test_nothing_listed_is_no_folders(self):
        from noveltrans.onedrive_upload import _list_folders

        assert _list_folders(self._page([])) == []


class TestOpenPath:
    def test_it_enters_each_segment(self):
        from noveltrans.onedrive_upload import _current_folder, _open_path

        page = _FakeOneDrivePage({"Fox Novel": {"Truyện": {}}})
        assert _open_path(page, ["Fox Novel", "Truyện"]) is True
        assert _current_folder(page) == "Truyện"

    def test_a_missing_folder_is_false_and_creates_nothing(self):
        """A picker that silently created the folder you mistyped would be a poor thing
        to hand someone whose OneDrive already has years of files in it."""
        from noveltrans.onedrive_upload import _open_path

        page = _FakeOneDrivePage({"Fox Novel": {}})
        assert _open_path(page, ["Fox Novel", "Không có"]) is False
        assert page.typed == []  # nothing was created

    def test_an_empty_path_stays_at_the_root(self):
        from noveltrans.onedrive_upload import _current_folder, _open_path

        page = _FakeOneDrivePage({"Fox Novel": {}})
        assert _open_path(page, []) is True
        assert _current_folder(page) == "Tệp của tôi"


class TestListDestinationFolders:
    def test_it_lists_the_root(self, drive):
        from noveltrans.onedrive_upload import list_destination_folders

        page = _FakeOneDrivePage({"Fox Novel": {}, "Audio": {}})
        page.rows = [("Fox Novel", "0 mục"), ("Audio", "2 mục")]
        drive(page)
        assert sorted(list_destination_folders()) == ["Audio", "Fox Novel"]

    def test_it_navigates_first(self, drive):
        from noveltrans.onedrive_upload import list_destination_folders

        page = _FakeOneDrivePage({"Fox Novel": {"Truyện": {}}})
        page.rows = {
            "": [("Fox Novel", "1 mục"), ("note.txt", "2 KB")],
            "Fox Novel": [("Truyện", "5 mục")],
        }
        drive(page)
        assert list_destination_folders("/Fox Novel") == ["Truyện"]
        assert page.cwd == ["Fox Novel"]

    def test_a_path_that_is_not_there_says_so(self, drive):
        from noveltrans.onedrive_upload import list_destination_folders

        drive(_FakeOneDrivePage({"Fox Novel": {}}))
        with pytest.raises(OneDriveUploadError, match="Không tìm thấy thư mục"):
            list_destination_folders("/Không có")


class TestReplacingAnExistingFile:
    """**The bug this class exists for.** Re-uploading a changed file silently kept the
    OLD bytes on OneDrive while the run reported success and the manifest recorded it as
    done. Two causes, both fixed here, both pinned:

      1. the collision prompt is a TOAST, not a modal, so gating on a dialog container
         meant it was never answered — and unanswered, OneDrive keeps the old file;
      2. every check was "is the name in the listing", which is vacuous for a replacement:
         the old copy is already sitting there under exactly that name.
    """

    def _page_with(self, name, remote_size):
        page = _FakeOneDrivePage()
        page.rows = [("Name", "Kích thước tệp"), (name, f"{remote_size} byte")]
        return page

    def test_presence_alone_is_not_treated_as_delivery(self):
        from noveltrans.onedrive_upload import _not_yet_landed

        page = self._page_with("notes.txt", 5)  # old copy still there
        assert _not_yet_landed(page, {"notes.txt": 777}) == ["notes.txt"]

    def test_the_right_size_counts_as_delivered(self):
        from noveltrans.onedrive_upload import _not_yet_landed

        assert _not_yet_landed(self._page_with("notes.txt", 777), {"notes.txt": 777}) == []

    def test_verify_batch_refuses_a_stale_remote_copy(self):
        """This is the assertion that would have caught the live failure."""
        from noveltrans.onedrive_upload import _verify_batch

        page = self._page_with("notes.txt", 5)
        with pytest.raises(OneDriveUploadError, match="vẫn là bản cũ"):
            _verify_batch(page, {"notes.txt": 777})

    def test_upload_started_is_not_satisfied_by_the_old_copy(self):
        """It used to return True the instant the name appeared — which for a replacement
        was immediately, whether or not a single byte had moved."""
        from noveltrans.onedrive_upload import _upload_started

        page = self._page_with("notes.txt", 5)
        assert _upload_started(page, {"notes.txt": 777}, timeout_ms=4_000) is False

    def test_a_conflict_prompt_counts_as_the_upload_having_started(self):
        """OneDrive asking about a collision proves it took the file."""
        from noveltrans.onedrive_upload import _upload_started

        page = self._page_with("notes.txt", 5)
        page.conflict = True
        assert _upload_started(page, {"notes.txt": 777}, timeout_ms=4_000) is True

    def test_the_prompt_is_answered_without_any_dialog_container(self):
        """MEASURED: `[class*='ms-Dialog']`, `[class*='ms-Modal']` and `[role=dialog]` all
        match ZERO while the collision toast is up. Gating on one is why it was never
        answered."""
        from noveltrans.onedrive_upload import _resolve_conflicts

        page = _FakeOneDrivePage(conflict=True)
        assert _resolve_conflicts(page) is True
        assert page.replaced is True
        assert page.kept_both is False

    def test_rounded_sizes_still_match(self):
        """OneDrive rounds above a kilobyte, so exact comparison is impossible there."""
        from noveltrans.onedrive_upload import _size_matches, parse_remote_size

        assert _size_matches(parse_remote_size("8,3 KB"), 8500)
        assert not _size_matches(parse_remote_size("8,3 KB"), 40_000)

    def test_an_unreadable_size_never_invents_a_failure(self):
        """The size check exists to catch a silent no-op, not to fail on an unparsed cell."""
        from noveltrans.onedrive_upload import _size_matches

        assert _size_matches(None, 12345)


class TestParseRemoteSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("5 byte", 5),
            ("320 byte", 320),
            ("1.234 byte", 1234),  # thousands separator
            ("8,3 KB", 8499),  # Vietnamese decimal comma
            ("41,0 GB", 44023414784),
            ("30 mục", None),  # a folder has no size
            ("Kích thước tệp", None),  # the column header
            ("", None),
        ],
    )
    def test_sizes(self, text, expected):
        from noveltrans.onedrive_upload import parse_remote_size

        assert parse_remote_size(text) == expected


class TestStallWindowScalesWithTheBiggestFile:
    """MEASURED: OneDrive's status region stays EMPTY during an upload, so the only
    movement signal is a file landing. A flat ten-minute window would therefore fail a
    456 MB part-video — about nineteen minutes at the assumed floor — mid-transfer."""

    def test_small_batches_keep_the_flat_floor(self):
        from noveltrans.onedrive_upload import _STALL_MS, stall_ms_for

        assert stall_ms_for(0) == _STALL_MS
        assert stall_ms_for(7 * 1024**2) == _STALL_MS

    def test_a_large_file_widens_it_past_its_own_transfer_time(self):
        from noveltrans.onedrive_upload import _ASSUMED_FLOOR_BPS, stall_ms_for

        size = 456 * 1024**2
        transfer_ms = size / _ASSUMED_FLOOR_BPS * 1000
        assert stall_ms_for(size) > transfer_ms

    def test_it_is_monotonic(self):
        from noveltrans.onedrive_upload import stall_ms_for

        sizes = [0, 10**6, 10**8, 456 * 1024**2, 10**10]
        assert [stall_ms_for(s) for s in sizes] == sorted(stall_ms_for(s) for s in sizes)

    def test_it_stays_under_the_batch_ceiling(self):
        """The size-derived ceiling must remain the outer guard, or the stall detector
        would be the thing that ends a run."""
        from noveltrans.onedrive_upload import batch_timeout_ms, stall_ms_for

        for size in (10**6, 10**8, 456 * 1024**2, 4 * 1024**3):
            assert stall_ms_for(size) <= batch_timeout_ms(size)


class TestDeepLinkNavigation:
    """OneDrive renders ~60 rows however many items a folder holds, so clicking through
    the listing cannot reach the 30th part folder. `?id=` addresses a folder by path and
    does not care about the sort order or the row budget."""

    def _deep(self):
        tree = {"NovelTrans": {"Truyện": {"exports": {"video": {f"phan-{i:04d}": {}
                                                                for i in range(80)}}}}}
        # `opens_on="dblclick"` is what OneDrive measures as, so one gesture per click.
        return _FakeOneDrivePage(tree, opens_on="dblclick")

    def test_it_reaches_a_folder_the_listing_never_renders(self):
        from noveltrans.onedrive_upload import _current_folder, _open_path

        page = self._deep()
        assert _open_path(
            page, ["NovelTrans", "Truyện", "exports", "video", "phan-0079"]
        ) is True
        assert _current_folder(page) == "phan-0079"

    def test_one_click_then_one_jump(self):
        """The root carries no `id`, so the first segment must still be clicked; the rest
        is a single URL hop rather than one per level."""
        from noveltrans.onedrive_upload import _open_path

        page = self._deep()
        _open_path(page, ["NovelTrans", "Truyện", "exports", "video"])
        assert [g for g, _ in page.clicked] == ["dblclick"]  # exactly one click

    def test_a_missing_folder_is_still_false(self):
        from noveltrans.onedrive_upload import _open_path

        page = self._deep()
        assert _open_path(page, ["NovelTrans", "Không có"]) is False

    def test_a_failed_jump_restores_where_we_were(self):
        """**The bug the fake caught.** A failed deep link lands at the root, so anything
        that followed searched — or CREATED in — the wrong folder. `_ensure_folder` would
        have put the novel beside `NovelTrans` instead of inside it."""
        from noveltrans.onedrive_upload import _current_folder, _ensure_folder

        page = self._deep()
        _ensure_folder(page, "NovelTrans")
        _ensure_folder(page, "Truyện")
        _ensure_folder(page, "Mới")  # does not exist → deep link fails → must create HERE
        assert page.cwd == ["NovelTrans", "Truyện", "Mới"]
        assert _current_folder(page) == "Mới"

    def test_an_existing_folder_outside_the_rendered_rows_is_not_recreated(self):
        """The sync bug: `_ensure_folder` could not see it, so it tried to create it, and
        OneDrive answered with an auto-renamed twin."""
        from noveltrans.onedrive_upload import _ensure_folder, _open_path

        page = self._deep()
        _open_path(page, ["NovelTrans", "Truyện", "exports", "video"])
        page.hidden.add("phan-0079")  # exists, but never rendered
        _ensure_folder(page, "phan-0079")
        assert page.typed == []  # nothing was created
        assert page.cwd[-1] == "phan-0079"


class TestWithinBatchProgress:
    """A batch of 20 audio files takes minutes and OneDrive's status region stays empty,
    so without per-file reporting the bar sits on one number long enough to look frozen —
    which is how a real run was reported as “stuck”."""

    def test_landed_files_are_reported_as_they_arrive(self, qapp_unused=None):
        from noveltrans.onedrive_upload import _send_files, _wait_for_batch

        files = []
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.mp3", "b.mp3", "c.mp3"):
                path = Path(tmp) / name
                path.write_bytes(b"x" * 10)
                files.append(path)
            page = _FakeOneDrivePage(arrive_after=2, status_texts=["Đang tải lên"])
            _send_files(page, files)
            seen = []
            _wait_for_batch(
                page,
                {f.name: f.stat().st_size for f in files},
                timeout_ms=600_000,
                on_landed=lambda n, of: seen.append((n, of)),
            )
        assert seen, "nothing was reported inside the batch"
        assert seen[-1] == (3, 3)

    def test_the_callback_is_optional(self):
        """`_wait_for_batch` is called directly in tests and scripts without one."""
        from noveltrans.onedrive_upload import _wait_for_batch

        page = _FakeOneDrivePage({"a.mp3": {}})
        _wait_for_batch(page, {"a.mp3": 1}, timeout_ms=60_000)

    def test_push_project_advances_the_bar_before_the_batch_ends(self, pushable, drive):
        """The count it reports is display-only — the manifest still records a file when
        its batch completes, so a crash mid-batch cannot leave a file recorded as done."""
        from noveltrans.onedrive_upload import PushRequest, push_project

        drive(_FakeOneDrivePage(arrive_after=2))
        seen = []
        push_project(
            PushRequest(project_path=pushable, novel_title="Đấu La"),
            on_progress=lambda d, t, m: seen.append((d, m)),
        )
        assert any("/" in m and "file" in m for _d, m in seen), seen

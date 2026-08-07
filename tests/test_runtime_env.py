"""Tests for making the process environment sane regardless of how the app was launched."""

from __future__ import annotations

import os
import subprocess

from noveltrans import runtime_env
from noveltrans.runtime_env import augment_tool_path, ensure_std_streams, no_console_kwargs


def test_prepends_existing_tool_dirs_and_user_local_bin(monkeypatch, tmp_path):
    # A real ~/.local/bin under a fake HOME that holds a "tool" — it must be prepended.
    # Pinned to darwin: this exercises the macOS/Linux candidate list specifically,
    # regardless of which platform actually runs the test suite.
    monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    env = {"PATH": os.pathsep.join(["/usr/bin", "/bin"])}

    new = augment_tool_path(env, home=home)
    parts = new.split(os.pathsep)

    # ~/.local/bin is prepended ahead of the inherited entries (other standard dirs that
    # happen to exist on this machine may sort ahead of it — that's fine).
    assert str(local_bin) in parts
    assert parts.index(str(local_bin)) < parts.index("/usr/bin")
    assert "/usr/bin" in parts and "/bin" in parts  # original entries kept
    assert env["PATH"] == new  # mutated in place


def test_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    env = {"PATH": "/usr/bin"}

    once = augment_tool_path(env, home=home)
    twice = augment_tool_path(env, home=home)

    assert once == twice  # no duplicate entries on a second call


def test_skips_dirs_that_do_not_exist(monkeypatch, tmp_path):
    # No ~/.local/bin and (presumably) no Homebrew under the fake home → PATH unchanged.
    monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.pathsep.join(["/usr/bin", "/bin"])}

    # Only real, existing standard dirs may be added; a missing ~/.local/bin must not be.
    new = augment_tool_path(env, home=home)

    assert str(home / ".local" / "bin") not in new.split(os.pathsep)


def test_handles_empty_path(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    env: dict[str, str] = {}

    new = augment_tool_path(env, home=home)

    assert str(home / ".local" / "bin") in new.split(os.pathsep)


def test_windows_candidates_include_scoop_and_winget(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_env.sys, "platform", "win32")
    home = tmp_path / "home"
    scoop_shims = home / "scoop" / "shims"
    winget_links = home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"
    scoop_shims.mkdir(parents=True)
    winget_links.mkdir(parents=True)
    env: dict[str, str] = {}

    new = augment_tool_path(env, home=home)
    parts = new.split(os.pathsep)

    assert str(scoop_shims) in parts
    assert str(winget_links) in parts
    # the macOS/Linux candidate list must not leak in on this platform
    assert str(home / ".local" / "bin") not in parts


def test_macos_candidates_do_not_leak_onto_windows_and_vice_versa(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / "scoop" / "shims").mkdir(parents=True)

    monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
    darwin_parts = augment_tool_path({}, home=home).split(os.pathsep)
    assert str(home / ".local" / "bin") in darwin_parts
    assert str(home / "scoop" / "shims") not in darwin_parts

    monkeypatch.setattr(runtime_env.sys, "platform", "win32")
    windows_parts = augment_tool_path({}, home=home).split(os.pathsep)
    assert str(home / "scoop" / "shims") in windows_parts
    assert str(home / ".local" / "bin") not in windows_parts


def test_bundle_dir_and_its_ffmpeg_subfolder_are_appended_last(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_env.sys, "platform", "darwin")
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    bundle_dir = tmp_path / "_internal"
    bundle_dir.mkdir()
    (bundle_dir / "ffmpeg").mkdir()
    env = {"PATH": "/usr/bin"}

    new = augment_tool_path(env, home=home, bundle_dir=bundle_dir)
    parts = new.split(os.pathsep)

    assert str(bundle_dir) in parts
    assert str(bundle_dir / "ffmpeg") in parts
    # a bundled copy must not shadow a real install (a standard candidate dir) ahead of it
    assert parts.index(str(bundle_dir)) > parts.index(str(local_bin))


def test_a_missing_bundle_dir_is_skipped_like_any_other_missing_candidate(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env: dict[str, str] = {}

    new = augment_tool_path(env, home=home, bundle_dir=tmp_path / "nope")

    assert str(tmp_path / "nope") not in new.split(os.pathsep)


def test_meipass_derives_the_bundle_dir(monkeypatch, tmp_path):
    # PyInstaller's bootloader sets sys._MEIPASS to wherever bundled files actually live —
    # for a onedir build that's an `_internal` folder, NOT the .exe's own directory (which
    # is why this must not be derived from sys.executable; see the module docstring).
    home = tmp_path / "home"
    home.mkdir()
    bundle_dir = tmp_path / "NovelTrans" / "_internal"
    bundle_dir.mkdir(parents=True)
    monkeypatch.setattr(runtime_env.sys, "_MEIPASS", str(bundle_dir), raising=False)
    env: dict[str, str] = {}

    new = augment_tool_path(env, home=home)

    assert str(bundle_dir) in new.split(os.pathsep)


def test_no_meipass_means_no_bundle_dir_candidate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delattr(runtime_env.sys, "_MEIPASS", raising=False)
    env: dict[str, str] = {}

    # must not raise when running from source (no bootloader, no _MEIPASS at all)
    augment_tool_path(env, home=home)


class TestEnsureStdStreams:
    """A console-less Windows build (see NovelTrans-windows.spec's console=False) has
    real `None` stdio, not just redirected — this is what makes VieNeu-TTS's first-run
    download progress bar (and anything else that prints) crash with "'NoneType' object
    has no attribute 'write'"."""

    def test_fills_in_missing_streams(self, monkeypatch):
        monkeypatch.setattr(runtime_env.sys, "stdout", None)
        monkeypatch.setattr(runtime_env.sys, "stderr", None)
        monkeypatch.setattr(runtime_env.sys, "stdin", None)

        ensure_std_streams()

        assert runtime_env.sys.stdout is not None
        assert runtime_env.sys.stderr is not None
        assert runtime_env.sys.stdin is not None
        runtime_env.sys.stdout.write("x")  # must not raise
        runtime_env.sys.stderr.write("x")  # must not raise

    def test_leaves_existing_streams_alone(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(runtime_env.sys, "stdout", sentinel)

        ensure_std_streams()

        assert runtime_env.sys.stdout is sentinel


class TestNoConsoleKwargs:
    """Every ffmpeg/ffprobe spawn passes `**no_console_kwargs()` so a console-less Windows
    build doesn't flash a fresh console window per spawn — see the module docstring's
    "Flashing consoles" gap."""

    def test_returns_create_no_window_flag_on_win32(self, monkeypatch):
        monkeypatch.setattr(runtime_env.sys, "platform", "win32")
        # subprocess.CREATE_NO_WINDOW only exists as an attribute on a real Windows
        # interpreter (see the module docstring) — fall back to its documented value
        # (0x08000000) so this test itself runs on any platform's CI.
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        assert no_console_kwargs() == {"creationflags": create_no_window}

    def test_empty_on_other_platforms(self, monkeypatch):
        monkeypatch.setattr(runtime_env.sys, "platform", "darwin")

        assert no_console_kwargs() == {}

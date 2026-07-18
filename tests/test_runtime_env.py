"""Tests for PATH augmentation that makes ffmpeg findable in a Finder-launched .app."""

from __future__ import annotations

import os

from noveltrans.runtime_env import augment_tool_path


def test_prepends_existing_tool_dirs_and_user_local_bin(tmp_path):
    # A real ~/.local/bin under a fake HOME that holds a "tool" — it must be prepended.
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    env = {"PATH": "/usr/bin:/bin"}

    new = augment_tool_path(env, home=home)
    parts = new.split(os.pathsep)

    # ~/.local/bin is prepended ahead of the inherited entries (other standard dirs that
    # happen to exist on this machine may sort ahead of it — that's fine).
    assert str(local_bin) in parts
    assert parts.index(str(local_bin)) < parts.index("/usr/bin")
    assert "/usr/bin" in parts and "/bin" in parts  # original entries kept
    assert env["PATH"] == new  # mutated in place


def test_is_idempotent(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    env = {"PATH": "/usr/bin"}

    once = augment_tool_path(env, home=home)
    twice = augment_tool_path(env, home=home)

    assert once == twice  # no duplicate entries on a second call


def test_skips_dirs_that_do_not_exist(tmp_path):
    # No ~/.local/bin and (presumably) no Homebrew under the fake home → PATH unchanged.
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin"}

    # Only real, existing standard dirs may be added; a missing ~/.local/bin must not be.
    new = augment_tool_path(env, home=home)

    assert str(home / ".local" / "bin") not in new.split(os.pathsep)


def test_handles_empty_path(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    env: dict[str, str] = {}

    new = augment_tool_path(env, home=home)

    assert str(home / ".local" / "bin") in new.split(os.pathsep)

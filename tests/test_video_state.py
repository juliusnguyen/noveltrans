"""Tests for the manual "đã tạo" override sidecar (`noveltrans.video_state`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.video_state import (
    created_override,
    effective_created,
    read_state,
    set_created_override,
    state_path,
    write_state,
)


@pytest.fixture
def part(tmp_path: Path) -> Path:
    """A stand-in for a rendered part: `<dir>/<stem>/<stem>.mp4`."""
    folder = tmp_path / "truyen-0001-0010"
    folder.mkdir()
    return folder / "truyen-0001-0010.mp4"


class TestStateRoundTrip:
    def test_sidecar_sits_beside_the_video(self, part):
        assert state_path(part) == part.parent / "truyen-0001-0010.created.json"

    def test_untouched_part_reads_empty(self, part):
        assert read_state(part) == {}
        assert created_override(part) is None

    def test_write_then_read(self, part):
        write_state(part, created=True)
        assert read_state(part) == {"created": True}
        assert created_override(part) is True

    def test_write_merges_rather_than_overwrites(self, part):
        write_state(part, created=True)
        write_state(part, note="manual")
        state = read_state(part)
        assert state["created"] is True
        assert state["note"] == "manual"

    def test_write_leaves_no_temp_file_behind(self, part):
        write_state(part, created=True)
        leftovers = [p.name for p in part.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []


class TestCorruptStateReadsAsNoOverride:
    """Unlike the upload sidecar, there's no "unresolved" state to protect — a corrupt or
    missing file just means "no manual override", same as never having touched the tick."""

    @pytest.mark.parametrize(
        "raw",
        [
            '{"created": tru',  # truncated mid-write
            "",  # zero-length: the classic torn write
            "not json at all",
            "[1, 2, 3]",  # valid JSON, wrong shape
            "null",
        ],
    )
    def test_unreadable_state_reads_as_no_override(self, part, raw):
        state_path(part).write_text(raw, encoding="utf-8")
        assert read_state(part) == {}
        assert created_override(part) is None


class TestSetCreatedOverride:
    def test_ticking_when_file_missing_persists_override(self, part):
        assert not part.is_file()
        set_created_override(part, True, file_exists=False)
        assert created_override(part) is True
        assert state_path(part).is_file()

    def test_unticking_when_file_exists_persists_override(self, part):
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"not really an mp4")
        set_created_override(part, False, file_exists=True)
        assert created_override(part) is False
        assert state_path(part).is_file()

    def test_toggling_back_into_agreement_clears_the_sidecar(self, part):
        set_created_override(part, True, file_exists=False)
        assert state_path(part).is_file()
        set_created_override(part, False, file_exists=False)
        assert created_override(part) is None
        assert not state_path(part).is_file()

    def test_agreeing_toggle_with_no_prior_sidecar_writes_nothing(self, part):
        set_created_override(part, False, file_exists=False)
        assert not state_path(part).is_file()
        assert created_override(part) is None


class TestEffectiveCreated:
    def test_no_override_follows_disk_missing(self, part):
        assert effective_created(part) is False

    def test_no_override_follows_disk_present(self, part):
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"not really an mp4")
        assert effective_created(part) is True

    def test_override_wins_over_disk(self, part):
        set_created_override(part, True, file_exists=False)
        assert effective_created(part) is True

    def test_override_self_heals_once_file_appears(self, part):
        set_created_override(part, True, file_exists=False)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"not really an mp4")
        # the override is still recorded as True until something recomputes it; but a
        # fresh `set_created_override` call at the new `file_exists` clears the stale record
        set_created_override(part, True, file_exists=True)
        assert created_override(part) is None
        assert effective_created(part) is True

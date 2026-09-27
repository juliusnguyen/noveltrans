"""Tests for the manual split/merge boundary sidecar (`noveltrans.video_windows`)."""

from __future__ import annotations

import pytest

from noveltrans.video_windows import (
    manual_windows_path,
    merge_windows,
    read_manual_windows,
    split_window,
    write_manual_windows,
)


class TestRoundTrip:
    def test_sidecar_sits_at_the_project_root(self, tmp_path):
        assert manual_windows_path(tmp_path) == tmp_path / "video_manual_windows.json"

    def test_untouched_project_reads_empty(self, tmp_path):
        assert read_manual_windows(tmp_path) == {}

    def test_write_then_read(self, tmp_path):
        write_manual_windows(tmp_path, {1: 10, 11: 20})
        assert read_manual_windows(tmp_path) == {1: 10, 11: 20}

    def test_write_overwrites_rather_than_merges(self, tmp_path):
        write_manual_windows(tmp_path, {1: 10})
        write_manual_windows(tmp_path, {11: 20})
        assert read_manual_windows(tmp_path) == {11: 20}

    def test_write_leaves_no_temp_file_behind(self, tmp_path):
        write_manual_windows(tmp_path, {1: 10})
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    @pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null"])
    def test_corrupt_file_reads_as_empty(self, tmp_path, raw):
        manual_windows_path(tmp_path).write_text(raw, encoding="utf-8")
        assert read_manual_windows(tmp_path) == {}


class TestSplitWindow:
    def test_splits_off_the_trailing_chapters(self, tmp_path):
        first_half, second_half = split_window(tmp_path, 91, 100, 5)
        assert first_half == (91, 95)
        assert second_half == (96, 100)
        assert read_manual_windows(tmp_path) == {91: 95, 96: 100}

    def test_replaces_a_prior_single_span_entry_for_the_same_start(self, tmp_path):
        write_manual_windows(tmp_path, {91: 100})  # e.g. an earlier merge landed here
        split_window(tmp_path, 91, 100, 3)
        windows = read_manual_windows(tmp_path)
        assert windows == {91: 97, 98: 100}

    @pytest.mark.parametrize("tail", [0, 10, 11, -1])
    def test_rejects_a_tail_that_would_leave_a_half_empty(self, tmp_path, tail):
        with pytest.raises(ValueError):
            split_window(tmp_path, 91, 100, tail)  # 10 chapters total

    def test_does_not_disturb_unrelated_manual_windows(self, tmp_path):
        write_manual_windows(tmp_path, {1: 10})
        split_window(tmp_path, 91, 100, 5)
        assert read_manual_windows(tmp_path) == {1: 10, 91: 95, 96: 100}


class TestMergeWindows:
    def test_merges_two_adjacent_windows(self, tmp_path):
        merged = merge_windows(tmp_path, 91, 95, 96, 100)
        assert merged == (91, 100)
        assert read_manual_windows(tmp_path) == {91: 100}

    def test_rejects_non_adjacent_windows(self, tmp_path):
        with pytest.raises(ValueError):
            merge_windows(tmp_path, 81, 90, 96, 100)  # a gap in between

    def test_merges_across_a_gap_of_only_skipped_chapters(self, tmp_path):
        # The reported case: Phần 13 is chương 223-233 and Phần 14 is 235-241, because
        # chương 234 is disabled and so never lands in a window. The two parts are
        # contiguous in everything that gets rendered, so the merge must be allowed.
        merged = merge_windows(
            tmp_path, 223, 233, 235, 241,
            disabled_numbers={65, 192, 199, 200, 209, 227, 234},
        )
        assert merged == (223, 241)
        assert read_manual_windows(tmp_path) == {223: 241}

    def test_rejects_overlapping_windows(self, tmp_path):
        with pytest.raises(ValueError):
            merge_windows(tmp_path, 91, 96, 95, 100)

    def test_still_rejects_overlapping_windows_when_the_gap_is_skipped(self, tmp_path):
        # The vacuous-`range` trap: "every chapter between them is disabled" is trivially
        # true for a reversed pair, so the ordering test has to be its own clause.
        with pytest.raises(ValueError):
            merge_windows(tmp_path, 91, 96, 95, 100, disabled_numbers={95})
        assert read_manual_windows(tmp_path) == {}

    def test_rejects_a_gap_holding_a_chapter_that_still_renders(self, tmp_path):
        with pytest.raises(ValueError) as exc:
            merge_windows(tmp_path, 223, 233, 237, 241, disabled_numbers={234, 235})
        # 236 is the only one blocking; the two skipped ones must not be blamed.
        assert "chương 236" in str(exc.value)
        assert "234" not in str(exc.value)

    def test_rejects_a_gap_holding_a_chapter_that_does_not_exist(self, tmp_path):
        # Deliberate: a number with no chapter can be re-scraped later, and would then be
        # swallowed into a part that may already be uploaded. Same rule as "not yet voiced".
        with pytest.raises(ValueError):
            merge_windows(tmp_path, 223, 233, 235, 241, disabled_numbers=set())

    def test_a_refused_merge_writes_nothing(self, tmp_path):
        with pytest.raises(ValueError):
            merge_windows(tmp_path, 81, 90, 96, 100)
        assert read_manual_windows(tmp_path) == {}

    def test_a_wide_skipped_gap_is_summarised_not_listed_in_full(self, tmp_path):
        with pytest.raises(ValueError) as exc:
            merge_windows(tmp_path, 10, 20, 31, 40)  # 21..30 all still render
        assert "chương 21, 22, 23 và 7 chương khác" in str(exc.value)

    def test_merge_undoes_a_prior_split(self, tmp_path):
        split_window(tmp_path, 91, 100, 5)
        assert read_manual_windows(tmp_path) == {91: 95, 96: 100}
        merge_windows(tmp_path, 91, 95, 96, 100)
        assert read_manual_windows(tmp_path) == {91: 100}

    def test_removes_both_originals_replacing_with_the_merged_span(self, tmp_path):
        write_manual_windows(tmp_path, {1: 10, 91: 95, 96: 100})
        merge_windows(tmp_path, 91, 95, 96, 100)
        assert read_manual_windows(tmp_path) == {1: 10, 91: 100}

"""Tests for the manual part-number corrections sidecar (`noveltrans.video_part_numbers`)."""

from __future__ import annotations

import json

import pytest

from noveltrans.video_part_numbers import (
    apply_part_offsets,
    clear_part_offset_at,
    clear_part_offsets,
    effective_part_number,
    part_offsets_path,
    plan_renumber,
    read_part_offsets,
    renumber_part,
    write_part_offsets,
)


class TestRoundTrip:
    def test_sidecar_sits_at_the_project_root(self, tmp_path):
        assert part_offsets_path(tmp_path) == tmp_path / "video_part_offsets.json"

    def test_untouched_project_reads_empty(self, tmp_path):
        assert read_part_offsets(tmp_path) == {}

    def test_write_then_read(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        assert read_part_offsets(tmp_path) == {1501: -1}

    def test_write_leaves_no_temp_file_behind(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_the_version_is_stamped(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        data = json.loads(part_offsets_path(tmp_path).read_text(encoding="utf-8"))
        assert data["version"] == 1

    def test_a_zero_delta_is_not_stored(self, tmp_path):
        write_part_offsets(tmp_path, {1501: 0})
        assert read_part_offsets(tmp_path) == {}

    @pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null"])
    def test_corrupt_file_reads_as_empty(self, tmp_path, raw):
        part_offsets_path(tmp_path).write_text(raw, encoding="utf-8")
        assert read_part_offsets(tmp_path) == {}

    def test_junk_entries_are_skipped_rather_than_poisoning_the_read(self, tmp_path):
        part_offsets_path(tmp_path).write_text(
            json.dumps({"version": 1, "chapters": {"1501": -1, "oops": "nope"}}),
            encoding="utf-8",
        )
        assert read_part_offsets(tmp_path) == {1501: -1}

    def test_a_section_of_the_wrong_type_reads_as_empty(self, tmp_path):
        part_offsets_path(tmp_path).write_text(
            json.dumps({"version": 1, "chapters": [1, 2]}), encoding="utf-8"
        )
        assert read_part_offsets(tmp_path) == {}


class TestEditionsAreSeparate:
    def test_the_two_editions_do_not_see_each_other(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        write_part_offsets(tmp_path, {7: 2}, source_audio=True)
        assert read_part_offsets(tmp_path) == {1501: -1}
        assert read_part_offsets(tmp_path, source_audio=True) == {7: 2}

    def test_writing_chapters_preserves_the_source_edition(self, tmp_path):
        write_part_offsets(tmp_path, {7: 2}, source_audio=True)
        write_part_offsets(tmp_path, {1501: -1})
        assert read_part_offsets(tmp_path, source_audio=True) == {7: 2}

    def test_writing_the_source_edition_preserves_chapters(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        write_part_offsets(tmp_path, {7: 2}, source_audio=True)
        assert read_part_offsets(tmp_path) == {1501: -1}

    def test_clearing_one_edition_leaves_the_other_alone(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        write_part_offsets(tmp_path, {7: 2}, source_audio=True)
        clear_part_offsets(tmp_path)
        assert read_part_offsets(tmp_path) == {}
        assert read_part_offsets(tmp_path, source_audio=True) == {7: 2}


class TestEffectivePartNumber:
    def test_no_corrections_leaves_the_base_alone(self):
        assert effective_part_number(1501, 75, {}) == 75

    def test_parts_before_the_anchor_are_untouched(self):
        assert effective_part_number(1480, 74, {1501: -1}) == 74

    def test_the_anchor_and_everything_after_shift(self):
        offsets = {1501: -1}
        assert effective_part_number(1501, 75, offsets) == 74
        assert effective_part_number(1521, 76, offsets) == 75

    def test_two_corrections_compose_additively(self):
        offsets = {1501: -1, 2001: -1}
        assert effective_part_number(1501, 75, offsets) == 74
        assert effective_part_number(2001, 100, offsets) == 98

    def test_an_orphaned_anchor_still_applies_from_that_chapter_on(self):
        # The window that started at 1501 was merged away, so no part starts there any
        # more — the correction must still apply to everything past it.
        assert effective_part_number(1521, 76, {1501: -1}) == 75

    def test_apply_maps_a_whole_plan(self):
        base = {1481: 74, 1501: 75, 1521: 76}
        assert apply_part_offsets(base, {1501: -1}) == {1481: 74, 1501: 74, 1521: 75}

    def test_apply_with_no_offsets_is_a_copy(self):
        base = {1481: 74, 1501: 75}
        result = apply_part_offsets(base, {})
        assert result == base
        assert result is not base


class TestPlanRenumber:
    def test_the_case_from_the_request(self):
        # Parts 75 and 76 are the ones plannable; 75 should become 74 and 76 follow to 75.
        numbers = {1481: 75, 1501: 76}
        offsets = plan_renumber(numbers, {}, 1481, 74)
        assert offsets == {1481: -1}
        assert apply_part_offsets(numbers, offsets) == {1481: 74, 1501: 75}

    def test_renumbering_mid_plan_leaves_the_earlier_parts_alone(self):
        numbers = {1461: 73, 1481: 75, 1501: 76}  # 74 was published as part of 73
        offsets = plan_renumber(numbers, {}, 1481, 74)
        assert apply_part_offsets(numbers, offsets) == {1461: 73, 1481: 74, 1501: 75}

    def test_shifting_upward_works_too(self):
        assert plan_renumber({1501: 75}, {}, 1501, 77) == {1501: 2}

    def test_an_existing_correction_at_the_anchor_accumulates(self):
        assert plan_renumber({1501: 74}, {1501: -1}, 1501, 73) == {1501: -2}

    def test_returning_to_the_base_drops_the_entry(self):
        assert plan_renumber({1501: 74}, {1501: -1}, 1501, 75) == {}

    def test_rejects_a_number_below_one(self):
        with pytest.raises(ValueError, match="từ 1 trở lên"):
            plan_renumber({1: 1}, {}, 1, 0)

    def test_rejects_a_collision_with_the_preceding_part(self):
        with pytest.raises(ValueError, match="lớn hơn 74"):
            plan_renumber({1481: 74, 1501: 75}, {}, 1501, 74)

    def test_the_collision_message_points_at_the_earlier_part(self):
        # Wanting 75 → 74 when 74 is right there means the numbering went wrong earlier;
        # a bare refusal would leave the user stuck.
        with pytest.raises(ValueError, match="từ Phần 74 trở đi"):
            plan_renumber({1481: 74, 1501: 75}, {}, 1501, 74)

    def test_rejects_dropping_below_the_preceding_part(self):
        with pytest.raises(ValueError, match="lớn hơn 74"):
            plan_renumber({1481: 74, 1501: 75}, {}, 1501, 70)

    def test_the_first_part_has_no_predecessor_to_collide_with(self):
        assert plan_renumber({1: 5, 21: 6}, {}, 1, 1) == {1: -4}

    def test_rejects_an_unknown_anchor(self):
        with pytest.raises(ValueError, match="Không tìm thấy"):
            plan_renumber({1501: 75}, {}, 9999, 74)

    def test_nothing_is_written_on_a_rejected_renumber(self, tmp_path):
        with pytest.raises(ValueError):
            renumber_part(tmp_path, {1481: 74, 1501: 75}, 1501, 74)
        assert not part_offsets_path(tmp_path).exists()


class TestRenumberPart:
    def test_persists_and_returns_the_new_numbering(self, tmp_path):
        numbers = {1481: 75, 1501: 76}
        result = renumber_part(tmp_path, numbers, 1481, 74)
        assert result == {1481: 74, 1501: 75}
        assert read_part_offsets(tmp_path) == {1481: -1}

    def test_the_source_edition_is_stored_separately(self, tmp_path):
        renumber_part(tmp_path, {1: 1, 2: 2}, 2, 5, source_audio=True)
        assert read_part_offsets(tmp_path, source_audio=True) == {2: 3}
        assert read_part_offsets(tmp_path) == {}

    def test_clear_at_one_anchor_keeps_the_others(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1, 2001: -1})
        remaining = clear_part_offset_at(tmp_path, 1501)
        assert remaining == {2001: -1}
        assert read_part_offsets(tmp_path) == {2001: -1}

    def test_clear_at_a_missing_anchor_is_a_no_op(self, tmp_path):
        write_part_offsets(tmp_path, {1501: -1})
        assert clear_part_offset_at(tmp_path, 9999) == {1501: -1}

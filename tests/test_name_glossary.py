"""Feature 072 — the per-novel character-name list. Pure: no Qt, no sqlite, no network.

The headline is `merge_detected`. A user who corrects a reading is making the one judgement
the machine cannot make for them, so a later re-detect must never quietly undo it — that
would destroy the only thing this file exists to provide.
"""

from __future__ import annotations

import json

import pytest

from noveltrans.name_glossary import (
    ORIGIN_AUTO,
    ORIGIN_MANUAL,
    NameEntry,
    applied_glossary,
    merge_detected,
    names_path,
    read_names,
    write_names,
)


def _by_source(entries):
    return {e.source: e for e in entries}


class TestStorage:
    def test_the_file_sits_at_the_project_root(self, tmp_path):
        assert names_path(tmp_path) == tmp_path / "names.json"

    def test_a_project_with_no_file_reads_as_empty(self, tmp_path):
        assert read_names(tmp_path) == []

    def test_every_field_round_trips(self, tmp_path):
        entry = NameEntry(
            source="夏檸七", reading="Hạ Ninh Thất", auto="Hạ Nịnh Thất",
            edited=True, enabled=False, count=812, origin=ORIGIN_MANUAL,
        )
        write_names(tmp_path, [entry], chapters_scanned=195)
        assert read_names(tmp_path) == [entry]

    @pytest.mark.parametrize(
        "raw", ["", "not json", "[1,2]", "null", '{"names": 3}', '{"names": [1, "x"]}']
    )
    def test_unreadable_content_degrades_to_empty(self, tmp_path, raw):
        """A hand-edited or truncated file must not fail a translate run."""
        names_path(tmp_path).write_text(raw, encoding="utf-8")
        assert read_names(tmp_path) == []

    def test_no_temp_file_is_left_behind(self, tmp_path):
        write_names(tmp_path, [NameEntry(source="江城", reading="Giang Thành")])
        assert list(tmp_path.glob("*.tmp")) == []

    def test_duplicate_sources_are_collapsed(self, tmp_path):
        names_path(tmp_path).write_text(
            json.dumps({"names": [
                {"source": "江城", "reading": "A"}, {"source": "江城", "reading": "B"},
            ]}),
            encoding="utf-8",
        )
        assert [e.reading for e in read_names(tmp_path)] == ["A"]


class TestMergeRule:
    """A detection never changes an edited reading, and never removes an entry."""

    def test_a_user_edited_reading_survives_a_redetect(self):
        stored = [NameEntry(source="夏檸七", reading="Hạ Ninh Thất",
                            auto="Hạ Nịnh Thất", edited=True, count=100)]
        merged = _by_source(merge_detected(stored, [("夏檸七", "Hạ Nịnh Thất", 812)]))

        assert merged["夏檸七"].reading == "Hạ Ninh Thất", "the user's correction was undone"
        assert merged["夏檸七"].auto == "Hạ Nịnh Thất", "but the machine's answer is refreshed"
        assert merged["夏檸七"].count == 812

    def test_an_unedited_reading_does_follow_the_detector(self):
        """An entry nobody touched should still track a better table later."""
        stored = [NameEntry(source="江城", reading="Giang Thanh", auto="Giang Thanh")]
        merged = _by_source(merge_detected(stored, [("江城", "Giang Thành", 40)]))
        assert merged["江城"].reading == "Giang Thành"

    def test_a_manual_entry_the_detector_cannot_see_is_kept(self):
        """The escape hatch: the detector needs 5+ occurrences and a known surname, so a
        hand-typed name must not vanish the next time it runs."""
        stored = [NameEntry(source="秦九霄", reading="Tần Cửu Tiêu",
                            origin=ORIGIN_MANUAL, count=7)]
        merged = _by_source(merge_detected(stored, [("江城", "Giang Thành", 40)]))

        assert "秦九霄" in merged
        assert merged["秦九霄"].reading == "Tần Cửu Tiêu"
        assert merged["秦九霄"].count == 0, "no longer counted, but not removed"

    def test_a_disabled_entry_stays_disabled_through_a_redetect(self):
        """Otherwise a false positive would come straight back on the next scan."""
        stored = [NameEntry(source="江城", reading="Giang Thành", enabled=False)]
        merged = _by_source(merge_detected(stored, [("江城", "Giang Thành", 40)]))
        assert merged["江城"].enabled is False

    def test_a_new_name_is_appended_enabled(self):
        merged = _by_source(merge_detected([], [("江城", "Giang Thành", 40)]))
        assert merged["江城"].enabled is True
        assert merged["江城"].origin == ORIGIN_AUTO
        assert merged["江城"].edited is False

    def test_a_name_with_no_reading_is_appended_disabled(self):
        """`to_hanviet` drops a name whole when one character has no entry. It is shown
        so it can be filled in, but it must not be substituted as an empty string."""
        merged = _by_source(merge_detected([], [("江婳", "", 41)]))
        assert merged["江婳"].reading == ""
        assert merged["江婳"].enabled is False


class TestAppliedGlossary:
    def test_disabled_entries_are_not_substituted(self):
        entries = [NameEntry(source="江城", reading="Giang Thành", enabled=False)]
        assert applied_glossary(entries) == {}

    def test_an_empty_reading_is_never_substituted(self):
        """`apply_glossary` is a blind str.replace — an empty replacement would DELETE
        every occurrence of the name from the source text."""
        entries = [NameEntry(source="江婳", reading="", enabled=True)]
        assert applied_glossary(entries) == {}

    def test_a_reading_still_containing_chinese_is_refused(self):
        """It would defeat the point and confuse the leftover-CJK retry scoring."""
        entries = [NameEntry(source="江城", reading="Giang 城", enabled=True)]
        assert applied_glossary(entries) == {}

    def test_a_good_entry_is_substituted(self):
        entries = [NameEntry(source="江城", reading="Giang Thành", enabled=True)]
        assert applied_glossary(entries) == {"江城": "Giang Thành"}


class TestTheReportedSymptomShape:
    """`Hạ Ninh Thất` came back as something else in one chapter. Three spellings are
    reachable for the same name, and only one of them is the user's."""

    def test_the_table_reading_and_the_users_reading_can_differ(self):
        from noveltrans.translators.names import to_hanviet

        assert to_hanviet("夏寧七") == "Hạ Ninh Thất"
        assert to_hanviet("夏檸七") == "Hạ Nịnh Thất"
        # 宁 is the simplified form of 寧, but the table carries the reading of the
        # unrelated character simplification merged it with.
        assert to_hanviet("夏宁七") == "Hạ Trữ Thất"

    def test_the_user_can_pin_a_reading_the_table_disagrees_with(self):
        """The whole point of the editable list: whatever the table says, the reader of
        that novel gets the final say, and a re-detect leaves it alone."""
        stored = [NameEntry(source="夏宁七", reading="Hạ Ninh Thất",
                            auto="Hạ Trữ Thất", edited=True, count=500)]
        merged = _by_source(merge_detected(stored, [("夏宁七", "Hạ Trữ Thất", 640)]))
        assert merged["夏宁七"].reading == "Hạ Ninh Thất"

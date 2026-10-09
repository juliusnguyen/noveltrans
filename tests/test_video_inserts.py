"""Feature 104: the "a chapter was inserted into this part" marker."""

from __future__ import annotations

from noveltrans.tts.video import resolve_part_video
from noveltrans.video_inserts import (
    clear_rendered,
    marked_part_covering,
    marker_path,
    read_marker,
    set_prompted,
    write_marker,
)


def test_roundtrip_and_empty_removes(tmp_path):
    video = tmp_path / "p" / "p.mp4"
    write_marker(video, {"chapters": [5, 3, 5]})
    assert read_marker(video)["chapters"] == [3, 5]
    assert read_marker(video)["prompted"] == ""
    write_marker(video, {"chapters": []})
    assert not marker_path(video).exists()


def test_corrupt_reads_as_none(tmp_path):
    video = tmp_path / "p.mp4"
    marker_path(video).write_text("{nope", encoding="utf-8")
    assert read_marker(video) == {}


def test_clear_rendered_keeps_what_is_still_missing(tmp_path):
    video = tmp_path / "p.mp4"
    write_marker(video, {"chapters": [3, 5], "prompted": "ready"})
    assert clear_rendered(video, {3, 4}) == [5]
    assert read_marker(video) == {**read_marker(video), "chapters": [5], "prompted": ""}
    assert clear_rendered(video, {5}) == []
    assert not marker_path(video).exists()


def test_set_prompted(tmp_path):
    video = tmp_path / "p.mp4"
    write_marker(video, {"chapters": [3]})
    set_prompted(video, "waiting")
    assert read_marker(video)["prompted"] == "waiting"


def test_marked_part_covering_and_resolver(tmp_path):
    folder = tmp_path / "s-0001-0006"
    folder.mkdir()
    (folder / "s-0001-0006.mp4").write_bytes(b"v")
    # Unmarked: a narrower window does not borrow the folder.
    assert marked_part_covering(tmp_path, "s", 1, 5) is None
    assert resolve_part_video(tmp_path, "s", 1, 5) == tmp_path / "s-0001-0005" / "s-0001-0005.mp4"
    write_marker(folder / "s-0001-0006.mp4", {"chapters": [4]})
    assert marked_part_covering(tmp_path, "s", 1, 5) == folder / "s-0001-0006.mp4"
    assert resolve_part_video(tmp_path, "s", 1, 5) == folder / "s-0001-0006.mp4"
    assert resolve_part_video(tmp_path, "s", 1, 6) == folder / "s-0001-0006.mp4"
    # The source edition never borrows a chapter-edition folder.
    assert resolve_part_video(tmp_path, "s", 1, 5, source_audio=True).parent.name.startswith(
        "s-nguon"
    )


def test_cleanup_treats_a_marked_part_as_covering_nothing(tmp_path):
    from noveltrans.cleanup import covered_chapters

    folder = tmp_path / "exports" / "video" / "s-0001-0003"
    folder.mkdir(parents=True)
    (folder / "s-0001-0003.mp4").write_bytes(b"v")
    assert covered_chapters(tmp_path) == {1, 2, 3}
    write_marker(folder / "s-0001-0003.mp4", {"chapters": [2]})
    assert covered_chapters(tmp_path) == set()

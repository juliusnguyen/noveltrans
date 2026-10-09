"""Feature 104: inserting a chapter anywhere carries every number-keyed file along, so
every part keeps exactly its chapters except the one that takes the new chapter."""

from __future__ import annotations

import pytest

from noveltrans.chapter_insert import InsertShift, apply_insert, plan_insert
from noveltrans.storage import NovelProject
from noveltrans.video_inserts import read_marker


@pytest.fixture
def project(library_dir, sample_meta, sample_refs):
    """Ten chapters (sample_refs gives five; add five more)."""
    from noveltrans.models import ChapterRef

    refs = sample_refs + [
        ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://example.com/novel/123/{i + 1}")
        for i in range(5, 10)
    ]
    project = NovelProject.create(library_dir, sample_meta, refs)
    yield project
    project.close()


def _part(project, a, b, *extra):
    slug = project.meta.slug_name()
    stem = f"{slug}-{a:04d}-{b:04d}"
    folder = project.video_dir / stem
    folder.mkdir(parents=True, exist_ok=True)
    for ext in (".mp4",) + extra:
        (folder / f"{stem}{ext}").write_text(stem, encoding="utf-8")
    return folder


def _video(project, a, b):
    slug = project.meta.slug_name()
    stem = f"{slug}-{a:04d}-{b:04d}"
    return project.video_dir / stem / f"{stem}.mp4"


class TestInsertShift:
    # Insert at N=5, chapters 5..10 shift; the part 1-5 owns it (inserted below chapter 4).
    shift = InsertShift(new_num=5, last_shifted=10, owner=4)

    def test_numbers(self):
        assert [self.shift.num(n) for n in (4, 5, 10, 11)] == [4, 6, 11, 11]

    def test_containing_part_grows_by_one(self):
        assert self.shift.span(1, 5) == (1, 6)

    def test_later_parts_shift_whole(self):
        assert self.shift.span(6, 10) == (7, 11)

    def test_above_the_first_chapter_of_a_part(self):
        shift = InsertShift(new_num=6, last_shifted=10, owner=6)
        assert shift.span(1, 5) == (1, 5)
        assert shift.span(6, 10) == (6, 11)

    def test_below_the_last_chapter_of_a_part(self):
        shift = InsertShift(new_num=6, last_shifted=10, owner=5)
        assert shift.span(1, 5) == (1, 6)
        assert shift.span(6, 10) == (7, 11)

    def test_a_span_starting_at_the_free_slot_moves(self):
        # Chapters 5..7 shift into 6..8; slot 8 was free, so a span starting there moves.
        shift = InsertShift(new_num=5, last_shifted=7, owner=None)
        assert shift.span(8, 10) == (9, 10)

    def test_append_owns_nothing(self):
        shift = InsertShift(new_num=11, last_shifted=10, owner=None)
        assert shift.span(1, 10) == (1, 10)


class TestPlanAndApply:
    def test_middle_insert_renames_everything_consistently(self, project):
        audio = project.audio_dir
        audio.mkdir(parents=True)
        slug = project.meta.slug_name()
        for n in (3, 7):
            (audio / f"{n:04d}-t-voice.wav").write_bytes(b"a")
            (audio / f"{n:04d}-t-voice.cues.json").write_text("[]", encoding="utf-8")
            project.save_audio(n - 1, f"exports/audio/{n:04d}-t-voice.wav", "voice", 1.0)
        (audio / f"{slug}-0001-0005.mp3").write_bytes(b"m")
        (audio / f"{slug}-0006-0010.mp3").write_bytes(b"m")
        _part(project, 1, 5, ".upload.json", ".txt")
        _part(project, 6, 10, ".upload.json")

        plan = plan_insert(project, 3, "Chen", 2, below=True)  # below chapter 3 → new 4
        assert not plan.collisions
        apply_insert(project, plan)

        assert project.chapter(3).title == "Chen"
        assert project.chapter(2).audio_path == "exports/audio/0003-t-voice.wav"  # before: same
        assert project.chapter(7).audio_path == "exports/audio/0008-t-voice.wav"
        assert (audio / "0008-t-voice.cues.json").is_file()
        assert not (audio / "0007-t-voice.wav").exists()
        assert not (audio / f"{slug}-0001-0005.mp3").exists()  # lost its continuity
        assert (audio / f"{slug}-0007-0011.mp3").is_file()
        grown = _video(project, 1, 6)
        assert grown.is_file() and (grown.parent / f"{grown.stem}.txt").is_file()
        assert read_marker(grown)["chapters"] == [4]
        later = _video(project, 7, 11)
        assert later.is_file() and (later.parent / f"{later.stem}.upload.json").is_file()
        assert not read_marker(later)

    def test_part_count_and_numbers_survive(self, project):
        from noveltrans.tts.video import discover_committed_video_windows

        _part(project, 1, 5)
        _part(project, 6, 10)
        plan = plan_insert(project, 5, "Chen", 5, below=False)  # above chapter 6
        apply_insert(project, plan)
        committed = discover_committed_video_windows(project.video_dir, project.meta.slug_name())
        assert committed == {1: 5, 6: 11}

    def test_append_after_the_last_chapter_touches_nothing(self, project):
        _part(project, 1, 10)
        plan = plan_insert(project, 10, "Cuối", 9, below=True)
        assert plan.shift.owner is None and not plan.moves and not plan.affected
        apply_insert(project, plan)
        assert _video(project, 1, 10).is_file() and not read_marker(_video(project, 1, 10))

    def test_source_edition_is_untouched(self, project):
        slug = project.meta.slug_name()
        source = project.video_dir / f"{slug}-nguon-0001-0005"
        source.mkdir(parents=True)
        apply_insert(project, plan_insert(project, 1, "Chen", 1, below=False))
        assert source.is_dir()

    def test_whole_novel_video_is_marked_not_renamed(self, project):
        slug = project.meta.slug_name()
        whole = project.video_dir / slug / f"{slug}.mp4"
        whole.parent.mkdir(parents=True)
        whole.write_bytes(b"v")
        plan = plan_insert(project, 2, "Chen", 2, below=False)
        assert any(p.old is None for p in plan.affected)
        apply_insert(project, plan)
        assert whole.is_file() and read_marker(whole)["chapters"] == [3]

    def test_manual_windows_and_offsets_follow(self, project):
        from noveltrans.video_part_numbers import read_part_offsets, write_part_offsets
        from noveltrans.video_windows import read_manual_windows, write_manual_windows

        write_manual_windows(project.path, {1: 4, 5: 10})
        write_part_offsets(project.path, {5: 2})
        apply_insert(project, plan_insert(project, 1, "Chen", 0, below=True))
        assert read_manual_windows(project.path) == {1: 5, 6: 11}
        assert read_part_offsets(project.path) == {6: 2}

    def test_existing_marker_numbers_shift(self, project):
        _part(project, 1, 5)
        _part(project, 6, 10)
        apply_insert(project, plan_insert(project, 7, "X", 7, below=False))  # into 6-10
        apply_insert(project, plan_insert(project, 1, "Y", 1, below=False))  # into 1-5
        assert read_marker(_video(project, 7, 12))["chapters"] == [9]
        assert read_marker(_video(project, 1, 6))["chapters"] == [2]

    def test_collision_refuses_the_plan(self, project):
        project.audio_dir.mkdir(parents=True)
        (project.audio_dir / "0010-t-voice.wav").write_bytes(b"a")
        # An orphan in the free slot past the last chapter, already holding the name
        # chapter 10's audio is about to move to.
        (project.audio_dir / "0011-t-voice.wav").write_bytes(b"o")
        plan = plan_insert(project, 2, "Chen", 2, below=False)
        assert plan.collisions
        with pytest.raises(ValueError):
            apply_insert(project, plan)

    def test_a_failed_rename_rolls_everything_back(self, project, monkeypatch):
        from pathlib import Path

        from noveltrans.video_windows import read_manual_windows, write_manual_windows

        _part(project, 1, 5)
        _part(project, 6, 10)
        write_manual_windows(project.path, {6: 10})
        plan = plan_insert(project, 2, "Chen", 2, below=False)
        real_rename = Path.rename
        calls = []

        def flaky(self, target):
            calls.append(self)
            if len(calls) == 3:
                raise OSError("locked")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky)
        with pytest.raises(OSError):
            apply_insert(project, plan)
        monkeypatch.setattr(Path, "rename", real_rename)
        assert _video(project, 1, 5).is_file() and _video(project, 6, 10).is_file()
        assert read_manual_windows(project.path) == {6: 10}
        assert len(project.chapters()) == 10

    def test_a_failed_db_write_rolls_the_files_back(self, project, monkeypatch):
        _part(project, 6, 10)

        def boom(*a, **k):
            raise RuntimeError("db")

        monkeypatch.setattr(project, "insert_chapter", boom)
        with pytest.raises(RuntimeError):
            apply_insert(project, plan_insert(project, 2, "Chen", 2, below=False))
        assert _video(project, 6, 10).is_file()
        assert not read_marker(_video(project, 6, 10))

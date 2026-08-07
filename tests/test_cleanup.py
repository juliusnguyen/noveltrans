"""Reclaiming disk space — and, mostly, everything this must refuse to delete.

The module deletes the user's files, so the tests are weighted the way the code is: a few
confirm that something IS offered, and the rest confirm that things are NOT. A bug here
costs someone a rendered video or a whole novel's audio, and neither comes back.
"""

from __future__ import annotations

import json

import pytest

from noveltrans import cleanup as cl


def _project(tmp_path, *, chapters=(1, 2, 3), parts=()):
    """A project with per-chapter audio and whatever part folders a test asks for.

    `parts` entries are `(first, last, files)` where `files` are the suffixes present in
    that part folder — so a test can build a part that was never rendered, or one with no
    subtitles, and see what that changes.
    """
    root = tmp_path / "novel"
    audio = root / "exports" / "audio"
    audio.mkdir(parents=True)
    for index in chapters:
        (audio / f"{index:04d}-chuong-{index}.mp3").write_bytes(b"a" * 100)
        (audio / f"{index:04d}-chuong-{index}.cues.json").write_text("[]", encoding="utf-8")
    for first, last, files in parts:
        name = f"truyen-{first:04d}-{last:04d}"
        folder = root / "exports" / "video" / name
        folder.mkdir(parents=True)
        for suffix in files:
            (folder / f"{name}{suffix}").write_bytes(b"v" * 50)
    return root


def _publish(project, part_name, *, published=True):
    folder = project / "exports" / "video" / part_name
    (folder / f"{part_name}.upload.json").write_text(
        json.dumps({"status": "published" if published else "draft", "video_id": "x"}),
        encoding="utf-8",
    )


def _relpaths(items):
    return sorted(i.relpath for i in items)


class TestPureHelpers:
    @pytest.mark.parametrize(
        "name,expected",
        [("0041-chuong-41.mp3", 41), ("0001-a.cues.json", 1), ("chuong.mp3", None), ("", None)],
    )
    def test_chapter_index(self, name, expected):
        assert cl.chapter_index(name) == expected

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("truyen-0041-0060", (41, 60)),
            ("a-b-c--0001-0020", (1, 20)),
            ("truyen-0060-0041", (41, 60)),  # reversed is still a range
            ("truyen", None),
            ("", None),
        ],
    )
    def test_part_range(self, name, expected):
        assert cl.part_range(name) == expected

    def test_a_folder_with_no_range_covers_nothing(self, tmp_path):
        """Guessing a range from an unreadable name would delete audio for chapters no
        video contains."""
        project = _project(tmp_path, parts=[(1, 3, (".mp4",))])
        (project / "exports" / "video" / "khong-co-so").mkdir()
        (project / "exports" / "video" / "khong-co-so" / "x.mp4").write_bytes(b"v")
        assert cl.covered_chapters(project) == {1, 2, 3}


class TestAudioCleanup:
    def test_audio_inside_a_rendered_part_is_offered(self, tmp_path):
        project = _project(tmp_path, chapters=(1, 2), parts=[(1, 2, (".mp4", ".srt"))])
        assert _relpaths(cl.plan_audio_cleanup(project)) == [
            "exports/audio/0001-chuong-1.cues.json",
            "exports/audio/0001-chuong-1.mp3",
            "exports/audio/0002-chuong-2.cues.json",
            "exports/audio/0002-chuong-2.mp3",
        ]

    def test_a_chapter_no_part_covers_is_left_alone(self, tmp_path):
        """A half-rendered novel must lose nothing."""
        project = _project(tmp_path, chapters=(1, 2, 3), parts=[(1, 2, (".mp4", ".srt"))])
        kept = {"exports/audio/0003-chuong-3.mp3", "exports/audio/0003-chuong-3.cues.json"}
        assert not kept & set(_relpaths(cl.plan_audio_cleanup(project)))

    def test_a_part_that_was_never_rendered_protects_its_audio(self, tmp_path):
        """The folder exists and names a range, but there is no .mp4 — so the audio is
        still the only copy of that content."""
        project = _project(tmp_path, chapters=(1, 2), parts=[(1, 2, (".srt", ".title.txt"))])
        assert cl.plan_audio_cleanup(project) == []

    def test_cues_wait_for_the_subtitle_file_the_mp3_does_not(self, tmp_path):
        """The cues become the part's .srt; without it they are still the only timings."""
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4",))])
        offered = _relpaths(cl.plan_audio_cleanup(project))
        assert offered == ["exports/audio/0001-chuong-1.mp3"]

    def test_nothing_but_audio_and_cues_is_ever_offered(self, tmp_path):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", ".srt"))])
        (project / "exports" / "audio" / "0001-notes.txt").write_text("x", encoding="utf-8")
        assert all(
            i.relpath.endswith((".mp3", ".wav", ".cues.json"))
            for i in cl.plan_audio_cleanup(project)
        )

    def test_a_project_with_no_audio_folder_is_fine(self, tmp_path):
        root = tmp_path / "bare"
        root.mkdir()
        assert cl.plan_audio_cleanup(root) == []


class TestVideoCleanupRefusesWithoutProof:
    def _ready(self, tmp_path):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", ".srt"))])
        _publish(project, "truyen-0001-0001")
        return project

    def test_an_unpublished_part_is_not_even_a_candidate(self, tmp_path):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4",))])
        _publish(project, "truyen-0001-0001", published=False)
        assert cl.video_cleanup_candidates(project) == []

    def test_a_part_with_no_upload_record_is_not_a_candidate(self, tmp_path):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4",))])
        assert cl.video_cleanup_candidates(project) == []

    def test_a_published_part_is_a_candidate_but_says_it_is_unverified(self, tmp_path):
        candidates = cl.video_cleanup_candidates(self._ready(tmp_path))
        assert len(candidates) == 1
        assert "chờ kiểm tra" in candidates[0].reason

    def test_plan_cleanup_never_includes_video(self, tmp_path):
        """**The load-bearing test.** Video deletion requires looking at OneDrive, and
        `plan_cleanup` does not look — so it must not offer any."""
        project = self._ready(tmp_path)
        assert all(i.kind == cl.KIND_AUDIO for i in cl.plan_cleanup(project))

    def test_the_manifest_alone_is_never_treated_as_proof(self, tmp_path):
        """MEASURED against the real library: the manifest claimed 28 part-videos were
        `done` while the OneDrive folders were EMPTY. Deleting on that would have cost
        10 GB whose only other copy is YouTube."""
        from noveltrans.onedrive_upload import Manifest, write_manifest

        project = self._ready(tmp_path)
        relpath = "exports/video/truyen-0001-0001/truyen-0001-0001.mp4"
        size = (project / relpath).stat().st_size
        manifest = Manifest(remote_root="/NovelTrans/x")
        manifest.files[relpath] = {"status": "done", "size": size, "mtime": 1.0}
        write_manifest(project, manifest)

        assert cl.manifest_claims_backed_up(project, relpath, size) is True
        # …and it STILL is not offered for deletion.
        assert all(i.kind == cl.KIND_AUDIO for i in cl.plan_cleanup(project))

    def test_a_manifest_recording_a_different_size_does_not_even_claim_it(self, tmp_path):
        """A part re-rendered since the backup is a different video."""
        from noveltrans.onedrive_upload import Manifest, write_manifest

        project = self._ready(tmp_path)
        relpath = "exports/video/truyen-0001-0001/truyen-0001-0001.mp4"
        manifest = Manifest(remote_root="/x")
        manifest.files[relpath] = {"status": "done", "size": 999_999, "mtime": 1.0}
        write_manifest(project, manifest)
        assert cl.manifest_claims_backed_up(project, relpath, 50) is False

    def test_a_corrupt_manifest_claims_nothing(self, tmp_path):
        from noveltrans.onedrive_upload import manifest_path

        project = self._ready(tmp_path)
        manifest_path(project).write_text("{oops", encoding="utf-8")
        assert cl.manifest_claims_backed_up(project, "anything", 1) is False


class TestTheUploadRecordIsSacred:
    def test_upload_json_is_never_offered(self, tmp_path):
        """Deleting it makes the app believe the part was never uploaded, and the next run
        publishes the episode to the channel a second time."""
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", ".srt"))])
        _publish(project, "truyen-0001-0001")
        offered = _relpaths(cl.plan_cleanup(project)) + _relpaths(
            cl.video_cleanup_candidates(project)
        )
        assert not any(p.endswith(".upload.json") for p in offered)

    @pytest.mark.parametrize(
        "suffix", [".srt", ".jpg", ".title.txt", ".tags.txt", ".txt", ".upload.json"]
    )
    def test_part_sidecars_are_never_offered(self, tmp_path, suffix):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", suffix))])
        _publish(project, "truyen-0001-0001")
        offered = _relpaths(cl.plan_cleanup(project)) + _relpaths(
            cl.video_cleanup_candidates(project)
        )
        assert not any(p.endswith(suffix) and suffix != ".mp4" for p in offered)

    def test_the_database_and_meta_are_never_offered(self, tmp_path):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", ".srt"))])
        (project / "chapters.db").write_bytes(b"db")
        (project / "meta.json").write_text("{}", encoding="utf-8")
        offered = _relpaths(cl.plan_cleanup(project))
        assert not any(p.endswith(("chapters.db", "meta.json")) for p in offered)


class TestRemoveFiles:
    def test_it_deletes_exactly_what_it_was_given(self, tmp_path):
        project = _project(tmp_path, chapters=(1, 2), parts=[(1, 2, (".mp4", ".srt"))])
        items = [i for i in cl.plan_audio_cleanup(project) if i.relpath.endswith(".mp3")]
        deleted, freed, errors = cl.remove_files(items)
        assert (deleted, errors) == (2, [])
        assert freed == 200
        # the cues it was not given are untouched
        assert (project / "exports/audio/0001-chuong-1.cues.json").exists()

    def test_an_already_missing_file_is_not_an_error(self, tmp_path):
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", ".srt"))])
        items = cl.plan_audio_cleanup(project)
        for item in items:
            item.path.unlink()
        deleted, freed, errors = cl.remove_files(items)
        assert (deleted, freed, errors) == (0, 0, [])

    def test_it_re_derives_nothing(self, tmp_path):
        """It does only what the plan says, so the plan is the single place safety lives."""
        project = _project(tmp_path, chapters=(1,), parts=[(1, 1, (".mp4", ".srt"))])
        target = project / "exports" / "audio" / "0001-chuong-1.mp3"
        deleted, _freed, _errors = cl.remove_files(
            [cl.Removable(path=target, relpath="x", size=1, kind="audio", reason="")]
        )
        assert deleted == 1
        assert not target.exists()

    def test_nothing_in_nothing_out(self):
        assert cl.remove_files([]) == (0, 0, [])


def test_total_size():
    items = [
        cl.Removable(path=None, relpath="a", size=10, kind="audio", reason=""),
        cl.Removable(path=None, relpath="b", size=32, kind="video", reason=""),
    ]
    assert cl.total_size(items) == 42


class TestSizesMatchForDelete:
    """OneDrive shows a ROUNDED size in the grid, so comparing it to the exact local byte
    count is not an equality test. Assuming it was rejected every file: a 419,928,664-byte
    video displays as "400,0 MB" and reads back as exactly 419,430,400."""

    def test_the_real_rounding_case_matches(self):
        assert cl.sizes_match_for_delete(419_430_400, 419_928_664)

    def test_a_different_render_does_not(self):
        assert not cl.sizes_match_for_delete(398_000_000, 419_928_664)

    def test_an_unreadable_size_never_authorises_a_delete(self):
        """The opposite of the upload path, where an unparsed cell is assumed fine —
        there it costs a re-upload, here it costs the file."""
        assert not cl.sizes_match_for_delete(None, 419_928_664)

    def test_small_files_still_compare_exactly_enough(self):
        assert cl.sizes_match_for_delete(100, 100)
        assert not cl.sizes_match_for_delete(50, 100)

    def test_a_zero_or_negative_local_size_is_refused(self):
        assert not cl.sizes_match_for_delete(0, 0)

    def test_the_tolerance_is_far_tighter_than_the_upload_path(self):
        """The upload path allows 12% because a false mismatch only costs a re-upload."""
        from noveltrans.onedrive_upload import _SIZE_TOLERANCE

        assert cl._DELETE_SIZE_TOLERANCE < _SIZE_TOLERANCE / 5

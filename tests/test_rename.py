"""plan_rename / apply_rename — pure, no Qt, no sqlite.

The highest-stakes assertion in this file is `test_an_upload_record_travels_with_its_part`:
losing a `.upload.json` re-publishes an episode to a live YouTube channel. It is written
before the GUI that will call any of this exists.
"""

from __future__ import annotations

import pytest

from noveltrans.rename import apply_rename, plan_rename, resync_title_sidecars


def _part(video_dir, stem: str, *, sidecars=(".mp4", ".srt", ".jpg", ".title.txt")):
    part = video_dir / stem
    part.mkdir(parents=True)
    for ext in sidecars:
        (part / f"{stem}{ext}").write_text(ext, encoding="utf-8")
    return part


@pytest.fixture
def project(tmp_path):
    """A novel with two rendered chapter parts, one source part and merged audio."""
    video = tmp_path / "exports" / "video"
    audio = tmp_path / "exports" / "audio"
    video.mkdir(parents=True)
    audio.mkdir(parents=True)
    _part(video, "cuu-chuyen-0001-0010")
    _part(video, "cuu-chuyen-0011-0020")
    _part(video, "cuu-chuyen-nguon-0001-0005")
    (audio / "cuu-chuyen-0001-0010.mp3").write_text("mp3", encoding="utf-8")
    # Chapter-keyed, must never move:
    (audio / "0001-chuong-mot-vi-VN-A.wav").write_text("wav", encoding="utf-8")
    (audio / "nguon-0001-tap-mot.mp3").write_text("nguon", encoding="utf-8")
    return tmp_path


class TestPlanRename:
    def test_it_finds_every_part_its_sidecars_and_the_merged_audio(self, project):
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        kinds = [m.kind for m in plan.moves]
        assert kinds.count("video-dir") == 3  # two chapter parts + the source edition
        assert kinds.count("video-file") == 12  # four sidecars each
        assert kinds.count("audio") == 1

    def test_the_source_edition_moves_too(self, project):
        # iter_rendered_part_dirs deliberately filters `-nguon-` out of a chapter scan; a
        # rename must NOT, or the source edition is left behind under the old name.
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        assert any(m.dst.name == "trong-sinh-nguon-0001-0005" for m in plan.moves)

    def test_chapter_keyed_audio_is_left_alone(self, project):
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        moved = {m.src.name for m in plan.moves}
        assert "0001-chuong-mot-vi-VN-A.wav" not in moved
        assert "nguon-0001-tap-mot.mp3" not in moved

    def test_another_novel_sharing_a_prefix_is_not_swept_up(self, project):
        # "cuu-chuyen" must not claim "cuu-chuyen-lam-nong": the remainder is neither an
        # edition marker nor a part range.
        _part(project / "exports" / "video", "cuu-chuyen-lam-nong-0001-0010")
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        assert not any("lam-nong" in str(m.src) for m in plan.moves)

    def test_a_whole_novel_part_is_found(self, tmp_path):
        video = tmp_path / "exports" / "video"
        video.mkdir(parents=True)
        _part(video, "cuu-chuyen")
        plan = plan_rename(tmp_path, "cuu-chuyen", "trong-sinh")
        assert [m.dst.name for m in plan.moves if m.kind == "video-dir"] == ["trong-sinh"]

    def test_the_pre_per_folder_flat_layout_is_found(self, tmp_path):
        video = tmp_path / "exports" / "video"
        video.mkdir(parents=True)
        (video / "cuu-chuyen-0001-0010.mp4").write_text("mp4", encoding="utf-8")
        (video / "cuu-chuyen-0001-0010.upload.json").write_text("{}", encoding="utf-8")
        plan = plan_rename(tmp_path, "cuu-chuyen", "trong-sinh")
        assert {m.dst.name for m in plan.moves} == {
            "trong-sinh-0001-0010.mp4",
            "trong-sinh-0001-0010.upload.json",
        }

    def test_a_novel_with_no_exports_yields_an_empty_plan(self, tmp_path):
        plan = plan_rename(tmp_path, "cuu-chuyen", "trong-sinh")
        assert plan.is_empty and plan.is_safe

    def test_renaming_to_the_same_slug_yields_an_empty_plan(self, project):
        assert plan_rename(project, "cuu-chuyen", "cuu-chuyen").is_empty

    def test_it_counts_the_bytes_it_would_move(self, project):
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        assert plan.total_bytes > 0

    def test_it_counts_the_parts_carrying_an_upload_record(self, project):
        video = project / "exports" / "video"
        (video / "cuu-chuyen-0001-0010" / "cuu-chuyen-0001-0010.upload.json").write_text(
            '{"state": "published"}', encoding="utf-8"
        )
        assert plan_rename(project, "cuu-chuyen", "trong-sinh").published == 1


class TestCollisions:
    def test_an_existing_destination_makes_the_plan_unsafe(self, project):
        _part(project / "exports" / "video", "trong-sinh-0001-0010")
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        assert not plan.is_safe

    def test_an_unsafe_plan_moves_nothing(self, project):
        _part(project / "exports" / "video", "trong-sinh-0001-0010")
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        with pytest.raises(ValueError):
            apply_rename(plan)
        assert (project / "exports" / "video" / "cuu-chuyen-0001-0010").is_dir()


class TestApplyRename:
    def test_parts_sidecars_and_audio_all_land_under_the_new_slug(self, project):
        apply_rename(plan_rename(project, "cuu-chuyen", "trong-sinh"))
        video = project / "exports" / "video"
        assert (video / "trong-sinh-0001-0010" / "trong-sinh-0001-0010.mp4").is_file()
        assert (video / "trong-sinh-0001-0010" / "trong-sinh-0001-0010.title.txt").is_file()
        assert (video / "trong-sinh-nguon-0001-0005").is_dir()
        assert (project / "exports" / "audio" / "trong-sinh-0001-0010.mp3").is_file()
        assert not (video / "cuu-chuyen-0001-0010").exists()

    def test_an_upload_record_travels_with_its_part(self, project):
        part = project / "exports" / "video" / "cuu-chuyen-0001-0010"
        (part / "cuu-chuyen-0001-0010.upload.json").write_text(
            '{"state": "published", "video_id": "abc"}', encoding="utf-8"
        )
        apply_rename(plan_rename(project, "cuu-chuyen", "trong-sinh"))
        moved = (
            project
            / "exports"
            / "video"
            / "trong-sinh-0001-0010"
            / "trong-sinh-0001-0010.upload.json"
        )
        assert moved.is_file() and "published" in moved.read_text(encoding="utf-8")

    def test_chapter_audio_is_still_where_it_was(self, project):
        apply_rename(plan_rename(project, "cuu-chuyen", "trong-sinh"))
        assert (project / "exports" / "audio" / "0001-chuong-mot-vi-VN-A.wav").is_file()

    def test_a_half_finished_rename_can_be_finished_by_re_planning(self, project):
        # What a permission error or an antivirus lock leaves behind on Windows.
        plan = plan_rename(project, "cuu-chuyen", "trong-sinh")
        first = [m for m in plan.moves if m.kind == "video-file"][:2]
        for move in first:
            move.src.rename(move.dst)
        done = apply_rename(plan_rename(project, "cuu-chuyen", "trong-sinh"))
        assert done  # the rest went through
        video = project / "exports" / "video"
        assert (video / "trong-sinh-0001-0010" / "trong-sinh-0001-0010.mp4").is_file()
        assert not any(p.name.startswith("cuu-chuyen") for p in video.iterdir())


class TestResyncTitleSidecars:
    """`.title.txt` must not keep publishing the old name.

    `_upload_request` prefers this sidecar over recomputing the title, so a part rendered
    before a rename and uploaded after it would go to YouTube under the old name. That
    was true of the pre-074 display-title override too — this is a bug fix, not just a
    consequence of renaming.
    """

    def _rendered(self, tmp_path, stem: str, title: str):
        video = tmp_path / "exports" / "video"
        part = video / stem
        part.mkdir(parents=True, exist_ok=True)
        (part / f"{stem}.mp4").write_bytes(b"mp4")
        (part / f"{stem}.title.txt").write_text(title + "\n", encoding="utf-8")
        return video, part / f"{stem}.title.txt"

    def test_a_generated_part_title_takes_the_new_name(self, tmp_path):
        video, sidecar = self._rendered(
            tmp_path, "cuu-chuyen-0001-0010", "Cứu Chuyện - Phần 1"
        )
        assert resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"]) == 1
        assert sidecar.read_text(encoding="utf-8").strip() == "Trọng Sinh - Phần 1"

    def test_the_part_number_is_preserved(self, tmp_path):
        # Read back out of the sidecar rather than recomputed, so a later batch-size
        # change cannot renumber a part that is already on YouTube.
        video, sidecar = self._rendered(
            tmp_path, "cuu-chuyen-0071-0080", "Cứu Chuyện - Phần 8"
        )
        resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"])
        assert sidecar.read_text(encoding="utf-8").strip() == "Trọng Sinh - Phần 8"

    def test_a_hand_written_title_is_left_alone(self, tmp_path):
        video, sidecar = self._rendered(
            tmp_path, "cuu-chuyen-0001-0010", "Bản đặc biệt - Phần 1"
        )
        assert resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"]) == 0
        assert sidecar.read_text(encoding="utf-8").strip() == "Bản đặc biệt - Phần 1"

    def test_a_whole_novel_title_is_just_the_name(self, tmp_path):
        video, sidecar = self._rendered(tmp_path, "cuu-chuyen", "Cứu Chuyện")
        assert resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"]) == 1
        assert sidecar.read_text(encoding="utf-8").strip() == "Trọng Sinh"

    def test_the_source_edition_is_included(self, tmp_path):
        # Unlike the description resync, which must stay chapter-only.
        video, sidecar = self._rendered(
            tmp_path, "cuu-chuyen-nguon-0001-0005", "Cứu Chuyện - Phần 1"
        )
        assert resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"]) == 1
        assert sidecar.read_text(encoding="utf-8").strip() == "Trọng Sinh - Phần 1"

    def test_an_unrendered_part_is_skipped(self, tmp_path):
        video, sidecar = self._rendered(
            tmp_path, "cuu-chuyen-0001-0010", "Cứu Chuyện - Phần 1"
        )
        (video / "cuu-chuyen-0001-0010" / "cuu-chuyen-0001-0010.mp4").unlink()
        assert resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"]) == 0

    def test_a_sidecar_already_holding_the_new_name_is_not_rewritten(self, tmp_path):
        video, _ = self._rendered(
            tmp_path, "cuu-chuyen-0001-0010", "Trọng Sinh - Phần 1"
        )
        assert (
            resync_title_sidecars(
                video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện", "Trọng Sinh"]
            )
            == 0
        )

    def test_it_round_trips_a_title_built_by_build_upload_title(self, tmp_path):
        """Keeps the regex here in step with the one place titles are actually made."""
        from noveltrans.tts.video import build_upload_title

        video, sidecar = self._rendered(
            tmp_path, "cuu-chuyen-0001-0010", build_upload_title("Cứu Chuyện", 3)
        )
        assert resync_title_sidecars(video, "cuu-chuyen", "Trọng Sinh", ["Cứu Chuyện"]) == 1
        assert sidecar.read_text(encoding="utf-8").strip() == build_upload_title(
            "Trọng Sinh", 3
        )

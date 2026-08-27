"""Feature 025 — the pure title/description builders + the meta fields they read."""

from __future__ import annotations

from noveltrans.storage import NovelProject
from noveltrans.tts.merge import MergeSegment
from noveltrans.tts.description import (
    YOUTUBE_DESCRIPTION_CHAR_LIMIT,
    description_length,
    indexed_chapter_count,
)
from noveltrans.tts.video import (
    build_upload_title,
    build_video_description,
    fit_video_description,
)


class TestBuildUploadTitle:
    def test_with_part_number(self):
        assert build_upload_title("Người hầu", 3) == "Người hầu - Phần 3"

    def test_whole_novel_has_no_part_suffix(self):
        assert build_upload_title("Người hầu", None) == "Người hầu"

    def test_strips_surrounding_whitespace(self):
        assert build_upload_title("  X  ", 1) == "X - Phần 1"


def _segs():
    return [
        MergeSegment(path="a", seconds=351, title="Chương 1 Mở đầu"),
        MergeSegment(path="b", seconds=342, title="Chương 2 Tiếp theo"),
    ]


class TestBuildVideoDescription:
    def _out(self, **over):
        kw = dict(
            original_title="穿书反派", vn_title="Xuyên sách phản diện",
            original_author="远赴人间", vn_author="Lữ khách phương xa",
            total_chapters=199, credit="Fox Novel",
        )
        kw.update(over)
        return build_video_description(_segs(), **kw)

    def test_exact_header_shape(self):
        lines = self._out().splitlines()
        assert lines[0] == 'Tên truyện: 穿书反派 — "Xuyên sách phản diện"'
        assert lines[1] == 'Tác giả: 远赴人间 "Lữ khách phương xa"'
        assert lines[2] == "Số chương: 199"
        assert lines[3] == ""
        assert lines[4] == "Mục lục chương:"

    def test_first_chapter_timestamp_is_zero(self):
        assert "\n0:00 Chương 1 Mở đầu\n" in self._out()

    def test_second_chapter_start_is_cumulative(self):
        # 351s → 5:51
        assert "5:51 Chương 2 Tiếp theo" in self._out()

    def test_over_one_hour_uses_h_mm_ss(self):
        segs = [MergeSegment("a", 3700, "C1"), MergeSegment("b", 10, "C2")]
        out = build_video_description(
            segs, original_title="", vn_title="", original_author="",
            vn_author="", total_chapters=2,
        )
        assert "1:01:40 C2" in out

    def test_empty_vn_author_drops_the_quoted_clause(self):
        out = self._out(vn_author="")
        assert "Tác giả: 远赴人间\n" in out
        assert '远赴人间 "' not in out

    def test_total_chapters_is_whole_novel_not_part_size(self):
        # two segments in the part, but 199 chapters in the novel
        assert "Số chương: 199" in self._out()

    def test_one_timestamp_line_per_chapter(self):
        body = self._out().split("Mục lục chương:\n", 1)[1]
        chapter_lines = [ln for ln in body.splitlines() if ln and not ln.startswith("Tạo bởi")]
        assert len(chapter_lines) == 2

    def test_trailing_credit_line(self):
        assert self._out().rstrip().endswith("Tạo bởi: Fox Novel")

    def test_custom_credit(self):
        assert "Tạo bởi: Kênh Khác" in self._out(credit="Kênh Khác")


def _many(n: int, title_len: int = 45) -> list[MergeSegment]:
    """`n` chapters with realistic Vietnamese titles — enough of them to bust the cap."""
    return [
        MergeSegment(path=f"{i}", seconds=350, title=f"Chương {i}: {'ả' * title_len}")
        for i in range(1, n + 1)
    ]


class TestVideoDescriptionCap:
    """Feature 065 — a part with too many chapters must not overflow YouTube's 5000."""

    def _out(self, segments, **over):
        kw = dict(
            original_title="穿书反派", vn_title="Xuyên sách phản diện",
            original_author="远赴人间", vn_author="Lữ khách phương xa",
            total_chapters=1990, credit="Fox Novel",
        )
        kw.update(over)
        return fit_video_description(segments, **kw)

    def test_never_exceeds_the_youtube_limit(self):
        text, _ = self._out(_many(400))
        assert description_length(text) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT

    def test_header_and_credit_survive_truncation(self):
        text, _ = self._out(_many(400))
        assert "Tên truyện: 穿书反派" in text
        assert "Tác giả: 远赴人间" in text
        assert "Số chương: 1990" in text
        assert text.rstrip().endswith("Tạo bởi: Fox Novel")

    def test_first_chapter_is_still_0_00_after_truncation(self):
        # YouTube only makes clickable chapters when the first timestamp is 0:00
        text, dropped = self._out(_many(400))
        assert dropped > 0
        assert "\n0:00 Chương 1:" in text

    def test_fit_reports_how_many_chapters_were_dropped(self):
        segments = _many(400)
        text, dropped = self._out(segments)
        assert dropped > 0
        # listed + reported-dropped accounts for the whole part, nothing goes missing
        assert indexed_chapter_count(text) == len(segments)

    def test_marker_line_names_the_remaining_chapters(self):
        text, dropped = self._out(_many(400))
        assert f"… còn {dropped} chương nữa" in text

    def test_no_marker_at_a_normal_batch_size(self):
        # The regression guard: the default batch of 10 must be completely unaffected
        text, dropped = self._out(_many(10))
        assert dropped == 0
        assert "… còn " not in text

    def test_max_chars_override_is_respected(self):
        text, dropped = self._out(_many(50), max_chars=400)
        assert description_length(text) <= 400
        assert dropped > 0

    def test_build_video_description_still_returns_just_the_text(self):
        segments = _many(400)
        kw = dict(
            original_title="穿书反派", vn_title="Xuyên sách phản diện",
            original_author="远赴人间", vn_author="Lữ khách phương xa",
            total_chapters=1990, credit="Fox Novel",
        )
        assert build_video_description(segments, **kw) == fit_video_description(
            segments, **kw
        )[0]


class TestMetaRoundtrip:
    def test_translated_author_and_tags_persist(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_meta_translation("Tựa", "Mô tả", "vi", "Tác giả tiếng Việt")
        project.save_tags("a, b, c")
        project.save_thumbnail_prompt("epic xianxia hero on a cliff, cinematic, 16:9")
        path = project.path
        project.close()

        reopened = NovelProject.open(path)
        assert reopened.meta.translated_author == "Tác giả tiếng Việt"
        assert reopened.meta.tags == "a, b, c"
        assert reopened.meta.thumbnail_prompt == "epic xianxia hero on a cliff, cinematic, 16:9"
        reopened.close()

    def test_old_meta_without_new_fields_loads(self, library_dir, sample_meta, sample_refs):
        # NovelMeta.from_dict must tolerate a meta.json written before these fields existed
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        path = project.path
        project.close()
        reopened = NovelProject.open(path)
        assert reopened.meta.translated_author == ""
        assert reopened.meta.tags == ""
        assert reopened.meta.video_image_path == ""
        assert reopened.meta.upload_playlist == ""
        assert reopened.meta.upload_visibility == ""
        reopened.close()

    def test_video_image_path_and_upload_playlist_persist(
        self, library_dir, sample_meta, sample_refs
    ):
        """Each novel remembers its own background image + playlist choice — without
        this, switching novels in the video tab left the previous novel's image/playlist
        selected on the newly opened one."""
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_video_image_path("C:/covers/bg.png")
        project.save_upload_playlist("Truyện A")
        path = project.path
        project.close()

        reopened = NovelProject.open(path)
        assert reopened.meta.video_image_path == "C:/covers/bg.png"
        assert reopened.meta.upload_playlist == "Truyện A"
        reopened.close()

    def test_upload_visibility_persists(self, library_dir, sample_meta, sample_refs):
        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_upload_visibility("public")
        path = project.path
        project.close()

        reopened = NovelProject.open(path)
        assert reopened.meta.upload_visibility == "public"
        reopened.close()

    def test_save_tags_caps_to_youtube_budget(self, library_dir, sample_meta, sample_refs):
        # A caller that skips parse_tags (e.g. a raw textbox edit) must still come out
        # capped — save_tags is the one choke point every tags write goes through.
        from noveltrans.tts.tags import format_tags, parse_tags

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        over_budget = ", ".join(f"tag{i}" for i in range(300))  # far more than 500 chars raw
        project.save_tags(over_budget)
        path = project.path
        project.close()

        reopened = NovelProject.open(path)
        assert reopened.meta.tags == format_tags(parse_tags(over_budget))
        assert len(reopened.meta.tags) <= 500  # the literal string YouTube's tag box counts
        reopened.close()

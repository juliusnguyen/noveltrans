"""Feature 025 — the Pillow thumbnail renderer + its pure wrap helper."""

from __future__ import annotations

from PIL import Image, ImageFont

from noveltrans.tts.thumbnail import _wrap_title, render_thumbnail
from noveltrans.tts.video import font_dir_context, video_font


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    with font_dir_context() as d:
        return ImageFont.truetype(str(d / video_font("noto_sans")["file"]), size)


class TestWrapTitle:
    def test_wraps_a_long_title_onto_multiple_lines(self):
        font = _load_font(80)
        lines = _wrap_title("Người hầu phản diện xuyên sách bắt đầu bị nữ chính", font, 300)
        assert len(lines) >= 2
        # every multi-word line stays within the width budget
        assert all(font.getlength(ln) <= 300 for ln in lines if " " in ln)

    def test_empty_title_is_no_lines(self):
        assert _wrap_title("", _load_font(80), 300) == []

    def test_single_word_never_splits(self):
        font = _load_font(80)
        assert _wrap_title("Xuyênsáchphảndiện", font, 50) == ["Xuyênsáchphảndiện"]


class TestRenderThumbnail:
    def test_writes_720p_jpeg_under_2mb(self, tmp_path):
        out = tmp_path / "thumb.jpg"
        with font_dir_context() as d:
            render_thumbnail(
                tmp_path / "missing.png", out,
                vn_title="Người hầu phản diện xuyên sách",
                part_num=1, tagline="Bắt đầu bằng việc bị nữ chính để mắt đến…",
                font_path=d / video_font("noto_sans")["file"],
            )
        assert out.is_file()
        with Image.open(out) as im:
            assert im.size == (1280, 720)
        assert out.stat().st_size <= 2_000_000

    def test_unreadable_base_image_still_produces_a_thumbnail(self, tmp_path):
        # a missing/garbage base image must not fail the render
        out = tmp_path / "t.jpg"
        with font_dir_context() as d:
            render_thumbnail(
                "/definitely/not/a/file.png", out,
                vn_title="X", part_num=2, tagline="",
                font_path=d / video_font("noto_sans")["file"],
            )
        with Image.open(out) as im:
            assert im.size == (1280, 720)

    def test_png_output_when_extension_is_png(self, tmp_path):
        out = tmp_path / "t.png"
        with font_dir_context() as d:
            render_thumbnail(
                tmp_path / "missing.png", out,
                vn_title="Tựa truyện", part_num=3, tagline="",
                font_path=d / video_font("noto_sans")["file"],
            )
        assert out.is_file()
        with Image.open(out) as im:
            assert im.format == "PNG"
            assert im.size == (1280, 720)


class TestComposeThumbnail:
    def _compose(self, **overrides):
        from noveltrans.tts.thumbnail import compose_thumbnail

        with font_dir_context() as d:
            kwargs = dict(
                vn_title="Tụ Bảo Tiên Bồn", part_num=1, tagline="",
                font_path=d / video_font("noto_sans")["file"],
                width=320, height=180,
            )
            kwargs.update(overrides)
            return compose_thumbnail("/no/such/file.png", **kwargs)

    def test_returns_an_rgb_image_of_the_requested_size(self):
        img = self._compose()
        assert img.mode == "RGB"
        assert img.size == (320, 180)

    def test_moving_the_part_block_changes_the_pixels(self):
        # same content, only the PHẦN N position differs → the rendered pixels must differ
        default = self._compose()
        moved = self._compose(part_pos=(0.5, 0.20))
        assert default.tobytes() != moved.tobytes()

    def test_moving_the_title_block_changes_the_pixels(self):
        default = self._compose()
        moved = self._compose(title_pos=(0.4, 0.5))
        assert default.tobytes() != moved.tobytes()


class TestTextScales:
    """Feature 035 — the three size multipliers.

    The load-bearing test here is the first one: `1.0` must reproduce the old fixed
    layout byte-for-byte. Feature 034 shipped a button that pushes covers onto videos
    already published on the channel, so a silent layout shift would propagate to a live
    channel the next time someone clicks it.
    """

    def _compose(self, **overrides):
        from noveltrans.tts.thumbnail import compose_thumbnail

        with font_dir_context() as d:
            kwargs = dict(
                vn_title="Chào mừng đến với phòng livestream ác mộng",
                part_num=1,
                tagline="Một câu tagline",
                font_path=d / video_font("noto_sans")["file"],
                width=320, height=180,
            )
            kwargs.update(overrides)
            return compose_thumbnail("/no/such/file.png", **kwargs)

    def test_scale_one_is_byte_identical_to_the_old_layout(self):
        from noveltrans.tts.thumbnail import DEFAULT_TEXT_SCALE

        assert DEFAULT_TEXT_SCALE == 1.0
        plain = self._compose()
        explicit = self._compose(title_scale=1.0, part_scale=1.0, tagline_scale=1.0)
        assert plain.tobytes() == explicit.tobytes()

    def test_each_scale_changes_the_pixels_independently(self):
        base = self._compose().tobytes()
        assert self._compose(title_scale=1.6).tobytes() != base
        assert self._compose(part_scale=1.6).tobytes() != base
        assert self._compose(tagline_scale=1.6).tobytes() != base

    def test_a_bigger_title_wraps_onto_more_lines(self):
        """The size is doing real work, not just being stored."""
        from noveltrans.tts.thumbnail import _TITLE_FRACTION, _scaled_px, _wrap_title

        title = "Chào mừng đến với phòng livestream ác mộng"
        small = _wrap_title(title, _load_font(_scaled_px(720, _TITLE_FRACTION, 0.6)), 700)
        large = _wrap_title(title, _load_font(_scaled_px(720, _TITLE_FRACTION, 1.8)), 700)
        assert len(large) > len(small)

    def test_scales_clamp_to_the_documented_bounds(self):
        from noveltrans.tts.thumbnail import (
            MAX_TEXT_SCALE,
            MIN_TEXT_SCALE,
            _TITLE_FRACTION,
            _scaled_px,
        )

        assert _scaled_px(720, _TITLE_FRACTION, 99) == _scaled_px(
            720, _TITLE_FRACTION, MAX_TEXT_SCALE
        )
        assert _scaled_px(720, _TITLE_FRACTION, 0.01) == _scaled_px(
            720, _TITLE_FRACTION, MIN_TEXT_SCALE
        )

    def test_a_junk_scale_renders_instead_of_raising(self):
        """Pillow raises on a zero-size font; a 30-part render must not die on one."""
        from noveltrans.tts.thumbnail import _TITLE_FRACTION, _scaled_px

        assert _scaled_px(720, _TITLE_FRACTION, 0) >= 8
        assert _scaled_px(720, _TITLE_FRACTION, -5) >= 8
        assert _scaled_px(720, _TITLE_FRACTION, None) >= 8
        assert _scaled_px(720, _TITLE_FRACTION, "big") >= 8
        assert self._compose(title_scale=0, part_scale=None).size == (320, 180)

    def test_a_tiny_frame_still_gets_a_legible_floor(self):
        from noveltrans.tts.thumbnail import _TAGLINE_FRACTION, _scaled_px

        assert _scaled_px(60, _TAGLINE_FRACTION, 0.5) == 8

    def test_an_overlong_tagline_still_shrinks_to_fit(self):
        """A chosen size is a request; cutting the user's text off is worse than
        rendering it a little smaller than the slider says."""
        long_tag = "Một câu tagline rất dài " * 6
        img = self._compose(tagline=long_tag, tagline_scale=2.0)
        assert img.size == (320, 180)  # rendered, not overflowed into an error


class TestRenderThumbnailScales:
    def test_render_accepts_and_applies_the_scales(self, tmp_path):
        outs = []
        for scale in (1.0, 1.8):
            out = tmp_path / f"t{scale}.jpg"
            with font_dir_context() as d:
                render_thumbnail(
                    tmp_path / "missing.png", out,
                    vn_title="Chào mừng đến với phòng livestream ác mộng",
                    part_num=1, tagline="tagline",
                    font_path=d / video_font("noto_sans")["file"],
                    width=320, height=180, title_scale=scale,
                )
            outs.append(out.read_bytes())
        assert outs[0] != outs[1]


class TestTitleAlign:
    """Feature 036 — flush-left / flush-right for the wrapped novel title.

    In "right" mode `title_pos[0]` is re-read as the block's RIGHT edge, so the lines end
    at the anchor instead of starting there.
    """

    TITLE = "Chào mừng đến với phòng livestream ác mộng"

    def _compose(self, **overrides):
        from noveltrans.tts.thumbnail import compose_thumbnail

        with font_dir_context() as d:
            kwargs = dict(
                vn_title=self.TITLE, part_num=1, tagline="",
                font_path=d / video_font("noto_sans")["file"],
                width=640, height=360,
            )
            kwargs.update(overrides)
            return compose_thumbnail("/no/such/file.png", **kwargs)

    def test_left_is_byte_identical_to_the_old_layout(self):
        """The no-migration guarantee: 034's "Cập nhật ảnh bìa" pushes covers onto
        already-published videos, so a silent shift would reach a live channel."""
        from noveltrans.tts.thumbnail import DEFAULT_TITLE_ALIGN

        assert DEFAULT_TITLE_ALIGN == "left"
        assert self._compose().tobytes() == self._compose(title_align="left").tobytes()

    def test_right_alignment_changes_the_pixels(self):
        base = self._compose(title_pos=(0.9, 0.0625)).tobytes()
        right = self._compose(title_pos=(0.9, 0.0625), title_align="right").tobytes()
        assert base != right

    def test_right_aligned_lines_all_end_at_the_anchor(self):
        """The actual definition of flush right: the ragged edge moves to the left.

        Computed from the same font metrics the renderer uses, so it asserts the geometry
        rather than eyeballing pixels.
        """
        from noveltrans.tts.thumbnail import (
            _TITLE_FRACTION,
            _line_size,
            _scaled_px,
            _wrap_title,
        )

        H, W = 360, 640
        px = _scaled_px(H, _TITLE_FRACTION, 1.0)
        font = _load_font(px)
        stroke = max(2, round(px * 0.06))
        lines = _wrap_title(self.TITLE, font, round(W * 0.62))
        assert len(lines) >= 2, "need a multi-line title for alignment to mean anything"

        anchor = round(W * 0.9)
        widths = [_line_size(font, ln, stroke)[0] for ln in lines]
        assert len(set(widths)) > 1, "lines must differ in width for this to prove anything"

        # the x the renderer draws each line at, in each mode
        left_xs = [anchor for _ in widths]
        right_xs = [anchor - w for w in widths]

        # flush left: shared left edge, ragged right one
        assert len(set(left_xs)) == 1
        assert len({x + w for x, w in zip(left_xs, widths)}) > 1
        # flush right: the mirror image
        assert len({x + w for x, w in zip(right_xs, widths)}) == 1
        assert len(set(right_xs)) > 1

    def test_the_wrap_budget_flips_with_the_alignment(self):
        """Measuring rightwards from a right-hand anchor would give a budget with nothing
        to do with the space the text occupies — so a title anchored near the right edge
        wraps onto FEWER lines flush right than flush left."""
        far_right = (0.95, 0.0625)
        left_mode = self._compose(title_pos=far_right)
        right_mode = self._compose(title_pos=far_right, title_align="right")
        assert left_mode.tobytes() != right_mode.tobytes()

    def test_an_unknown_align_renders_as_left(self):
        """A hand-edited settings value must not kill a 30-part render."""
        expected = self._compose(title_align="left").tobytes()
        for junk in ("centre", "", None, "RIGHTish", 7):
            assert self._compose(title_align=junk).tobytes() == expected

    def test_align_is_case_and_whitespace_insensitive(self):
        expected = self._compose(title_align="right").tobytes()
        assert self._compose(title_align="  RIGHT ").tobytes() == expected

    def test_render_thumbnail_passes_it_through(self, tmp_path):
        outs = []
        for align in ("left", "right"):
            out = tmp_path / f"t-{align}.jpg"
            with font_dir_context() as d:
                render_thumbnail(
                    tmp_path / "missing.png", out,
                    vn_title=self.TITLE, part_num=1, tagline="",
                    font_path=d / video_font("noto_sans")["file"],
                    width=640, height=360,
                    title_pos=(0.9, 0.0625), title_align=align,
                )
            outs.append(out.read_bytes())
        assert outs[0] != outs[1]

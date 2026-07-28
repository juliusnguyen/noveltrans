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

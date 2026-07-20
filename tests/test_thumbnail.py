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

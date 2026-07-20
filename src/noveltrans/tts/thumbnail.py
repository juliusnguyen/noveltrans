"""Compose a YouTube thumbnail: a base photo + styled Vietnamese overlay text.

Layout (see 025.00-example-thumbnail.png):
  * top-left — the novel title (Vietnamese), word-wrapped over several lines, in a
    decorative "glow" style (a blurred cyan halo under a white, dark-stroked title);
  * bottom-center — `PHẦN {N}` large, plus a smaller tagline subtitle line beneath it.

Pure Pillow (like `player_skin.py`, whose `_cover_fit` we reuse) — no ffmpeg. The
decorative glow is approximated with a blurred coloured copy of the text under a
stroked title, so it works with the feature-023 bundled fonts without shipping a new
display face. `_wrap_title` is pure arithmetic on font metrics, so it unit-tests without
rendering. `render_thumbnail` writes a 1280×720 JPEG (≤ YouTube's 2 MB limit) or a PNG
when `out_path` ends in `.png`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from noveltrans.tts.player_skin import _cover_fit

# Palette tuned to the reference thumbnail: white text, a dark navy edge for contrast on
# any photo, and a cyan halo for the "glow".
_TITLE_FILL = (248, 251, 255)
_TITLE_STROKE = (12, 32, 60)
_GLOW = (128, 214, 255)


def _wrap_title(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word-wrap `text` so each line's rendered width is ≤ `max_width`.

    A single word wider than `max_width` becomes its own (overflowing) line rather than
    being split mid-word. Pure: measures with the font's own metrics, no image needed.
    """
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if font.getlength(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _line_size(font: ImageFont.FreeTypeFont, text: str, stroke_width: int) -> tuple[int, int]:
    """(width, height) of one rendered line, accounting for the stroke."""
    x0, y0, x1, y1 = font.getbbox(text, stroke_width=stroke_width)
    return x1 - x0, y1 - y0


def _draw_glow_line(
    base: Image.Image,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    stroke_width: int,
    glow_radius: int,
) -> None:
    """Draw `text` at top-left `xy` on `base` (RGBA): a blurred cyan halo + a white,
    dark-stroked title on top."""
    x, y = xy
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(
        (x, y), text, font=font, fill=(*_GLOW, 255),
        stroke_width=stroke_width, stroke_fill=(*_GLOW, 255),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(glow_radius))
    base.alpha_composite(glow)
    base.alpha_composite(glow)  # twice → a denser, more visible halo
    ImageDraw.Draw(base).text(
        (x, y), text, font=font, fill=(*_TITLE_FILL, 255),
        stroke_width=stroke_width, stroke_fill=(*_TITLE_STROKE, 255),
    )


def _scrim(w: int, h: int) -> Image.Image:
    """A transparent overlay darkened at the top and bottom, clear through the middle.

    Each band ramps its alpha linearly so it blends into any photo — no hard edge.
    """
    scrim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scrim)
    top_h = round(h * 0.44)
    bot_h = round(h * 0.44)
    for y in range(top_h):  # dark at y=0 → clear at top_h
        a = round(120 * (1 - y / top_h))
        draw.line([(0, y), (w, y)], fill=(0, 0, 20, a))
    for i in range(bot_h):  # clear at h-bot_h → dark at h
        y = h - bot_h + i
        a = round(150 * (i / bot_h))
        draw.line([(0, y), (w, y)], fill=(0, 0, 20, a))
    return scrim


def _font(font_path: Path | str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(font_path), size)
    except OSError:
        try:
            return ImageFont.load_default(size)  # Pillow ≥ 10.1
        except TypeError:  # older Pillow: default font is a fixed-size bitmap
            return ImageFont.load_default()


def render_thumbnail(
    base_image_path: Path | str,
    out_path: Path,
    *,
    vn_title: str,
    part_num: int,
    tagline: str,
    font_path: Path | str,
    width: int = 1280,
    height: int = 720,
) -> Path:
    """Render a `width`×`height` thumbnail to `out_path` and return it.

    An unreadable/empty base image still yields a valid thumbnail (a neutral dark
    backdrop) so a bad photo never fails a render. Saved as JPEG unless `out_path` ends
    `.png`.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    W, H = width, height

    # background: the photo, cover-fit; a neutral dark panel if it can't be read.
    try:
        photo = Image.open(base_image_path).convert("RGB")
        bg = _cover_fit(photo, W, H).convert("RGBA")
    except (OSError, ValueError):
        bg = Image.new("RGBA", (W, H), (26, 24, 34, 255))

    # soft dark scrims (top + bottom) that fade to clear in the middle, so text reads over
    # a busy photo without a visible hard edge
    bg.alpha_composite(_scrim(W, H))

    margin = round(W * 0.035)

    # -- top-left: the wrapped novel title -----------------------------------
    title_px = round(H * 0.11)
    title_font = _font(font_path, title_px)
    title_stroke = max(2, round(title_px * 0.06))
    max_text_w = round(W * 0.62)  # leave the right side for the photo's subject
    lines = _wrap_title(vn_title, title_font, max_text_w)
    y = margin
    line_gap = round(title_px * 0.18)
    for line in lines:
        _draw_glow_line(
            bg, (margin, y), line, title_font,
            stroke_width=title_stroke, glow_radius=max(4, round(title_px * 0.18)),
        )
        _, line_h = _line_size(title_font, line, title_stroke)
        y += line_h + line_gap

    # -- bottom-center: PHẦN {N} + tagline -----------------------------------
    part_px = round(H * 0.15)
    part_font = _font(font_path, part_px)
    part_stroke = max(3, round(part_px * 0.06))
    part_text = f"PHẦN {part_num}"
    part_w, part_h = _line_size(part_font, part_text, part_stroke)
    part_x = (W - part_w) // 2
    part_y = round(H * 0.66)
    _draw_glow_line(
        bg, (part_x, part_y), part_text, part_font,
        stroke_width=part_stroke, glow_radius=max(6, round(part_px * 0.2)),
    )

    tagline = (tagline or "").strip()
    if tagline:
        tag_px = round(H * 0.055)
        tag_font = _font(font_path, tag_px)
        tag_stroke = max(2, round(tag_px * 0.08))
        # keep the tagline on one line: shrink until it fits the width budget
        while tag_px > 12 and tag_font.getlength(tagline) > W - margin * 2:
            tag_px -= 2
            tag_font = _font(font_path, tag_px)
        tag_stroke = max(2, round(tag_px * 0.08))
        tag_w, tag_h = _line_size(tag_font, tagline, tag_stroke)
        tag_x = (W - tag_w) // 2
        tag_y = part_y + part_h + round(H * 0.03)
        _draw_glow_line(
            bg, (tag_x, tag_y), tagline, tag_font,
            stroke_width=tag_stroke, glow_radius=max(3, round(tag_px * 0.18)),
        )

    if out_path.suffix.lower() == ".png":
        bg.convert("RGB").save(out_path)
    else:
        bg.convert("RGB").save(out_path, "JPEG", quality=85, optimize=True)
    return out_path

"""Draw the music-player artwork the video is composited over.

`render_video` bakes three PNGs per render and ffmpeg animates them:

  * `build_player_skin` — the full-frame static backdrop: a pastel gradient, the chosen
    photo framed (rounded + drop shadow) on the left, and the empty progress-bar track on
    the right. Everything that never moves.
  * `build_vinyl` — a transparent square record disc with the bundled logo as its centre
    label; ffmpeg `rotate`s it every frame so it spins.
  * `build_knob` — a small transparent accent dot; ffmpeg slides it along the track as a
    real playhead (its x is a function of playback time / total duration).

Pillow does gradients, rounded masks, blurred shadows, and anti-aliased circles cleanly —
ffmpeg's drawing primitives do not. `PlayerLayout` is the single source of truth for the
coordinates that must agree across modules: the spinning-vinyl box, the visualizer-bars
rectangle, the progress track + playhead, and the title placement (all read by
`video._filtergraph` / `video.build_ass_subtitles`). It is pure arithmetic on
width/height, so those callers stay unit-testable without rendering anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

_ACCENT = (216, 130, 224)  # the pink-purple used for the playhead + progress fill
_DISC = (24, 22, 30)       # vinyl body
_KNOB_PAD = 4              # transparent margin around the knob dot (room for its ring)


@dataclass(frozen=True)
class PlayerLayout:
    """Pixel geometry of the player, derived from the output size.

    Exposes only what other modules read: the photo box + progress track (skin), the
    spinning-vinyl box, the visualizer-bars rectangle, the playhead radius, and the title
    placement. All proportional to width/height, so any resolution stays laid out.
    """

    width: int
    height: int
    # left photo frame
    photo_x: int
    photo_y: int
    photo_w: int
    photo_h: int
    photo_radius: int
    # spinning vinyl (square box; the disc fills it edge-to-edge)
    vinyl_x: int
    vinyl_y: int
    vinyl_size: int
    # visualizer bars — right column, where the old progress bar sat
    bars_x: int
    bars_y: int
    bars_w: int
    bars_h: int
    # progress track + sliding playhead knob (right column, below the bars)
    track_x: int
    track_y: int
    track_w: int
    track_thickness: int
    knob_r: int
    knob_half: int  # half the knob PNG's side, for centring the overlay
    # 'now playing' text (ASS Alignment 8, framed into the right column by L/R margins)
    text_margin_l: int
    text_margin_r: int
    novel_margin_v: int
    chapter_margin_v: int
    novel_font_px: int
    chapter_font_px: int

    @classmethod
    def of(cls, width: int, height: int) -> PlayerLayout:
        vinyl_r = round(height * 0.139)
        knob_r = max(6, round(height * 0.012))
        return cls(
            width=width,
            height=height,
            photo_x=round(width * 0.031),
            photo_y=round(height * 0.065),
            photo_w=round(width * 0.432),
            photo_h=round(height * 0.593),
            photo_radius=round(width * 0.019),
            vinyl_x=round(width * 0.740) - vinyl_r,
            vinyl_y=round(height * 0.231) - vinyl_r,
            vinyl_size=vinyl_r * 2,
            bars_x=round(width * 0.589),
            bars_y=round(height * 0.555),
            bars_w=round(width * 0.307),
            bars_h=round(height * 0.111),
            track_x=round(width * 0.589),
            track_y=round(height * 0.720),
            track_w=round(width * 0.307),
            track_thickness=max(3, round(height * 0.005)),
            knob_r=knob_r,
            knob_half=knob_r + _KNOB_PAD,
            text_margin_l=round(width * 0.589),
            text_margin_r=round(width * 0.104),
            novel_margin_v=round(height * 0.435),
            chapter_margin_v=round(height * 0.486),
            novel_font_px=round(height * 0.035),
            chapter_font_px=round(height * 0.048),
        )


# -- drawing helpers ----------------------------------------------------------

def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _lighten(rgb: tuple, amt: float) -> tuple:
    """Blend `rgb` toward white by `amt` (0..1)."""
    return _lerp(rgb, (255, 255, 255), amt)


def _darken(rgb: tuple, amt: float) -> tuple:
    """Blend `rgb` toward black by `amt` (0..1)."""
    return _lerp(rgb, (0, 0, 0), amt)


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    """Parse a "#rrggbb" (or "#rgb") string to an (r, g, b) tuple; None if invalid/empty."""
    value = (value or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return None
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return None


def _vertical_gradient(w: int, h: int, stops: list[tuple[float, tuple]]) -> Image.Image:
    """A vertical multi-stop gradient. `stops` is ascending (pos 0..1, (r,g,b))."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        col = stops[-1][1]
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= t <= p1:
                col = _lerp(c0, c1, (t - p0) / (p1 - p0) if p1 > p0 else 0.0)
                break
        for x in range(w):
            px[x, y] = col
    return img


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return m


def _circle_mask(size: tuple[int, int]) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).ellipse((0, 0, size[0] - 1, size[1] - 1), fill=255)
    return m


def _drop_shadow(base: Image.Image, box: tuple[int, int, int, int], radius: int,
                 blur: int, alpha: int) -> None:
    """Composite a soft, slightly-lowered drop shadow for a rounded box onto `base`."""
    x, y, w, h = box
    pad = blur * 3
    sh = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        (pad, pad, pad + w, pad + h), radius=radius, fill=(60, 40, 90, alpha)
    )
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(sh, (x - pad, y - pad + round(blur * 0.4)))


def _cover_fit(photo: Image.Image, w: int, h: int) -> Image.Image:
    """Scale+centre-crop `photo` to exactly w×h (CSS object-fit: cover)."""
    scale = max(w / photo.width, h / photo.height)
    photo = photo.resize((max(1, round(photo.width * scale)), max(1, round(photo.height * scale))))
    left = (photo.width - w) // 2
    top = (photo.height - h) // 2
    return photo.crop((left, top, left + w, top + h))


# -- the three baked layers ---------------------------------------------------

def build_player_skin(
    image_path: Path, out_path: Path, *, width: int, height: int,
    bg_color: tuple[int, int, int] | None = None,
) -> Path:
    """Render the static backdrop (gradient + framed photo + empty progress track).

    Returns `out_path`. The spinning vinyl, the bars, the sliding playhead, and the titles
    are NOT drawn here — ffmpeg overlays those so they animate over this still.

    `bg_color` (r, g, b) replaces the default pastel gradient with one derived from that hue
    (a light-to-slightly-darker vertical gradient plus soft tinted glows); `None` keeps the
    original pastel look.
    """
    lay = PlayerLayout.of(width, height)
    W, H = width, height

    if bg_color is None:
        bg = _vertical_gradient(W, H, [
            (0.0, (233, 213, 255)),
            (0.45, (252, 231, 243)),
            (1.0, (219, 234, 254)),
        ]).convert("RGBA")
        glow_colors = ((255, 182, 217, 70), (199, 210, 254, 80))
    else:
        bg = _vertical_gradient(W, H, [
            (0.0, _lighten(bg_color, 0.22)),
            (0.5, bg_color),
            (1.0, _darken(bg_color, 0.10)),
        ]).convert("RGBA")
        glow_colors = (
            (*_lighten(bg_color, 0.40), 70),
            (*_lighten(bg_color, 0.25), 80),
        )
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((round(-W * 0.1), round(H * 0.46), round(W * 0.26), round(H * 1.1)),
               fill=glow_colors[0])
    gd.ellipse((round(W * 0.78), round(-H * 0.18), round(W * 1.09), round(H * 0.37)),
               fill=glow_colors[1])
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(round(H * 0.11))))
    draw = ImageDraw.Draw(bg)

    # framed photo, left
    box = (lay.photo_x, lay.photo_y, lay.photo_w, lay.photo_h)
    _drop_shadow(bg, box, radius=lay.photo_radius, blur=round(H * 0.035), alpha=90)
    try:
        photo = Image.open(image_path).convert("RGB")
        photo = _cover_fit(photo, lay.photo_w, lay.photo_h)
        bg.paste(photo, (lay.photo_x, lay.photo_y), _rounded_mask((lay.photo_w, lay.photo_h), lay.photo_radius))
    except OSError:
        draw.rounded_rectangle(
            (lay.photo_x, lay.photo_y, lay.photo_x + lay.photo_w, lay.photo_y + lay.photo_h),
            radius=lay.photo_radius, fill=(230, 224, 240),
        )
    draw.rounded_rectangle(
        (lay.photo_x, lay.photo_y, lay.photo_x + lay.photo_w, lay.photo_y + lay.photo_h),
        radius=lay.photo_radius, outline=(255, 255, 255, 220), width=max(2, round(W * 0.0026)),
    )

    # empty progress-bar track (the sliding playhead + fill are overlaid by ffmpeg)
    t = lay.track_thickness
    draw.rounded_rectangle(
        (lay.track_x, lay.track_y - t, lay.track_x + lay.track_w, lay.track_y + t),
        radius=t, fill=(255, 255, 255, 170),
    )

    bg.convert("RGB").save(out_path)
    return out_path


def build_vinyl(logo_path: Path, out_path: Path, *, size: int) -> Path:
    """Render a transparent `size`×`size` record disc with the logo as its centre label.

    The disc fills the square edge-to-edge, so ffmpeg can `rotate` the PNG in place (keeping
    the same frame size) and the disc stays put while the label spins. A readable logo is
    circle-cropped into the label; if it can't be read, a plain accent label is drawn.
    """
    r = size // 2
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(disc)
    d.ellipse((0, 0, size - 1, size - 1), fill=(*_DISC, 255))
    for i in range(6):  # subtle concentric grooves
        rr = r - round(r * 0.09) - round(i * r * 0.093)
        if rr > 0:
            d.ellipse((r - rr, r - rr, r + rr, r + rr), outline=(70, 66, 78, 255), width=2)

    lab = round(r * 0.62)
    try:
        logo = Image.open(logo_path).convert("RGB")
        side = min(logo.size)
        lx, ly = (logo.width - side) // 2, (logo.height - side) // 2
        logo = logo.crop((lx, ly, lx + side, ly + side)).resize((lab * 2, lab * 2))
        disc.paste(logo, (r - lab, r - lab), _circle_mask((lab * 2, lab * 2)))
    except OSError:
        d.ellipse((r - lab, r - lab, r + lab, r + lab), fill=(*_ACCENT, 255))
    d.ellipse((r - lab, r - lab, r + lab, r + lab), outline=(255, 255, 255, 230), width=max(3, round(r * 0.027)))
    spindle = max(4, round(r * 0.047))
    d.ellipse((r - spindle, r - spindle, r + spindle, r + spindle), fill=(240, 240, 245, 255))

    disc.save(out_path)
    return out_path


def build_knob(out_path: Path, *, radius: int) -> Path:
    """Render the small transparent playhead dot (accent fill + white ring)."""
    half = radius + _KNOB_PAD
    knob = Image.new("RGBA", (half * 2, half * 2), (0, 0, 0, 0))
    ImageDraw.Draw(knob).ellipse(
        (_KNOB_PAD, _KNOB_PAD, half * 2 - _KNOB_PAD - 1, half * 2 - _KNOB_PAD - 1),
        fill=(*_ACCENT, 255), outline=(255, 255, 255, 255), width=max(2, round(radius * 0.23)),
    )
    knob.save(out_path)
    return out_path

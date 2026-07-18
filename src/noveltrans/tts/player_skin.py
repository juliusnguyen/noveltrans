"""Draw the static 'music player' skin that the video is composited over.

`render_video` bakes one of these PNGs per render, then ffmpeg just loops it and
overlays the animated visualizer bars + the burned-in titles. Everything that ffmpeg
can't draw well lives here: a soft pastel gradient, the chosen photo rounded with a drop
shadow on the left, and a decorative record-player widget (vinyl + tonearm, a seek bar,
and play/prev/next buttons) on the right. Pillow does gradients, rounded masks, blurred
shadows, and anti-aliased circles cleanly — ffmpeg's drawing primitives do not.

`PlayerLayout` is the single source of truth for the few coordinates that must agree
across modules: the bars rectangle (used by `video._filtergraph`) and the title text
placement (used by `video.build_ass_subtitles`). It is pure arithmetic on width/height,
so those callers stay unit-testable without rendering anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# A soft, warm-to-cool rainbow reused for the vinyl grooves and centre label.
_RAINBOW = [
    (255, 99, 132), (255, 159, 64), (255, 205, 86),
    (75, 192, 132), (54, 162, 235), (153, 102, 255),
]


@dataclass(frozen=True)
class PlayerLayout:
    """Pixel geometry of the player skin, derived from the output size.

    Only the fields the *other* modules need are exposed: the photo box (skin only),
    the bars rectangle (the ffmpeg overlay), and the title placement (the ASS margins).
    The decorative widget coordinates live inside `build_player_skin` — nothing else
    reads them.
    """

    width: int
    height: int
    # left photo frame
    photo_x: int
    photo_y: int
    photo_w: int
    photo_h: int
    photo_radius: int
    # full-width visualizer band along the bottom
    bars_x: int
    bars_y: int
    bars_w: int
    bars_h: int
    # 'now playing' text (ASS Alignment 8, framed into the right column by L/R margins)
    text_margin_l: int
    text_margin_r: int
    novel_margin_v: int
    chapter_margin_v: int
    novel_font_px: int
    chapter_font_px: int

    @classmethod
    def of(cls, width: int, height: int) -> PlayerLayout:
        return cls(
            width=width,
            height=height,
            photo_x=round(width * 0.031),
            photo_y=round(height * 0.065),
            photo_w=round(width * 0.432),
            photo_h=round(height * 0.593),
            photo_radius=round(width * 0.019),
            bars_x=round(width * 0.026),
            bars_y=round(height * 0.778),
            bars_w=round(width * 0.948),
            bars_h=round(height * 0.194),
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


# -- the skin -----------------------------------------------------------------

def build_player_skin(image_path: Path, out_path: Path, *, width: int, height: int) -> Path:
    """Render the full-frame player skin (gradient + framed photo + widget) to `out_path`.

    Returns `out_path`. The visualizer bars and the titles are NOT drawn here — ffmpeg
    overlays those so they animate; this PNG is the static backdrop everything sits on.
    """
    lay = PlayerLayout.of(width, height)
    W, H = width, height

    # Pastel lavender -> pink -> light-blue backdrop with two soft colour glows.
    bg = _vertical_gradient(W, H, [
        (0.0, (233, 213, 255)),
        (0.45, (252, 231, 243)),
        (1.0, (219, 234, 254)),
    ]).convert("RGBA")
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((round(-W * 0.1), round(H * 0.46), round(W * 0.26), round(H * 1.1)),
               fill=(255, 182, 217, 70))
    gd.ellipse((round(W * 0.78), round(-H * 0.18), round(W * 1.09), round(H * 0.37)),
               fill=(199, 210, 254, 80))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(round(H * 0.11))))
    draw = ImageDraw.Draw(bg)

    # --- framed photo, left ---
    box = (lay.photo_x, lay.photo_y, lay.photo_w, lay.photo_h)
    _drop_shadow(bg, box, radius=lay.photo_radius, blur=round(H * 0.035), alpha=90)
    try:
        photo = Image.open(image_path).convert("RGB")
        photo = _cover_fit(photo, lay.photo_w, lay.photo_h)
        bg.paste(photo, (lay.photo_x, lay.photo_y), _rounded_mask((lay.photo_w, lay.photo_h), lay.photo_radius))
    except OSError:
        # Unreadable image: leave a soft placeholder card rather than failing the render.
        draw.rounded_rectangle(
            (lay.photo_x, lay.photo_y, lay.photo_x + lay.photo_w, lay.photo_y + lay.photo_h),
            radius=lay.photo_radius, fill=(230, 224, 240),
        )
    draw.rounded_rectangle(
        (lay.photo_x, lay.photo_y, lay.photo_x + lay.photo_w, lay.photo_y + lay.photo_h),
        radius=lay.photo_radius, outline=(255, 255, 255, 220), width=max(2, round(W * 0.0026)),
    )

    # --- vinyl record widget, upper right ---
    cx, cy, R = round(W * 0.740), round(H * 0.231), round(H * 0.139)
    draw.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(24, 22, 30))
    for i, col in enumerate(_RAINBOW):  # rainbow grooves on the right arc
        rr = R - round(R * 0.08) - round(i * R * 0.06)
        draw.arc((cx - rr, cy - rr, cx + rr, cy + rr), start=-70, end=110,
                 fill=col, width=max(2, round(R * 0.027)))
    lab = round(R * 0.39)  # rainbow centre label
    label = Image.new("RGBA", (lab * 2, lab * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(label)
    for i in range(lab):
        c = _RAINBOW[min(len(_RAINBOW) - 1, int(i / lab * len(_RAINBOW)))]
        ld.ellipse((i, i, lab * 2 - i, lab * 2 - i), outline=c, width=2)
    bg.alpha_composite(label, (cx - lab, cy - lab))
    hole = round(R * 0.05)
    draw.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=(30, 28, 36))
    arm = (120, 180, 235)  # tonearm
    draw.line((cx - R - round(R * 0.27), cy + round(R * 0.13), cx - round(R * 0.27), cy - round(R * 0.07)),
              fill=arm, width=max(4, round(R * 0.067)))
    ah = round(R * 0.1)
    draw.ellipse((cx - round(R * 0.37) - ah, cy - round(R * 0.13) - ah,
                  cx - round(R * 0.37) + ah, cy - round(R * 0.13) + ah), fill=arm)

    # --- seek bar ---
    bx0, bx1, by = round(W * 0.589), round(W * 0.896), round(H * 0.639)
    th = max(3, round(H * 0.004))
    draw.rounded_rectangle((bx0, by - th, bx1, by + th), radius=th, fill=(255, 255, 255, 180))
    knobx = round(bx0 + (bx1 - bx0) * 0.42)
    accent = (216, 130, 224)
    draw.rounded_rectangle((bx0, by - th, knobx, by + th), radius=th, fill=accent)
    kr = round(H * 0.011)
    draw.ellipse((knobx - kr, by - kr, knobx + kr, by + kr), fill=accent, outline=(255, 255, 255),
                 width=max(2, round(kr * 0.25)))

    # --- playback buttons ---
    bcy = round(H * 0.722)
    tri = round(H * 0.02)
    stem = max(3, round(W * 0.003))
    pcx = round(W * 0.746)
    prev_x, next_x = round(W * 0.651), round(W * 0.828)
    flat = (150, 110, 190)
    draw.polygon([(prev_x, bcy), (prev_x + tri, bcy - tri), (prev_x + tri, bcy + tri)], fill=flat)
    draw.rectangle((prev_x - stem, bcy - tri, prev_x, bcy + tri), fill=flat)
    draw.polygon([(next_x, bcy), (next_x - tri, bcy - tri), (next_x - tri, bcy + tri)], fill=flat)
    draw.rectangle((next_x, bcy - tri, next_x + stem, bcy + tri), fill=flat)
    pr = round(H * 0.044)
    draw.ellipse((pcx - pr, bcy - pr, pcx + pr, bcy + pr), fill=accent)  # play/pause
    bw = max(4, round(W * 0.0073))
    bh = round(H * 0.024)
    gap = round(W * 0.006)
    draw.rounded_rectangle((pcx - gap - bw, bcy - bh, pcx - gap, bcy + bh), radius=th, fill=(255, 255, 255))
    draw.rounded_rectangle((pcx + gap, bcy - bh, pcx + gap + bw, bcy + bh), radius=th, fill=(255, 255, 255))

    bg.convert("RGB").save(out_path)
    return out_path

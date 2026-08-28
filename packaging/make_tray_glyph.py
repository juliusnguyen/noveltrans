"""Convert design/bar-icon.png into the menu-bar glyph shipped as package data.

Run: python packaging/make_tray_glyph.py [src/noveltrans/gui/assets/tray-glyph.png]
Wired into `make icon` / `.\\make.ps1 icon` alongside the app icon.

The source is the wolf mark in flat black line art — and, like design/logo-icon.png, its
transparency is fake: the alpha channel is fully opaque and the "transparent" ground is
painted into the RGB pixels. Successive exports have drawn that ground differently (a flat
grey with a printed grid, a checkerboard, a flat grey with faint plus marks), which does
not matter as long as one property holds: the ground is light, its decoration sits within a
few levels of it, the strokes are 0-2, and nothing at all lives in between. Luminance then
maps straight to alpha and the whole ground falls away on its own.

**Why a ramp with a floor.** `alpha = (FLOOR - L) / FLOOR` keeps the antialiased edges of
the strokes — a hard threshold would alias them, and the mark renders as small as 18 px
tall in the menu bar, where that shows. The floor has to sit well below the ground, not
just at it: at the ground's own value every background pixel still picks up an alpha of 2
or so, `getbbox()` then returns the whole canvas, and the glyph can no longer be trimmed to
its own extent.

**Aspect ratio is not assumed anywhere.** The mark has been both portrait and landscape
across exports, so the master keeps whatever ratio it is trimmed to and `tray._glyph`
fits it into the icon box on both axes.

The result is pure black with a real alpha channel, which is what both platforms want:
macOS takes it as a template mask (it reads the alpha and recolours per theme), and
`tray._glyph` recolours the same silhouette through CompositionMode_SourceIn everywhere
else. See src/noveltrans/gui/tray.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
_SOURCE = _ROOT / "design" / "bar-icon.png"
_DEFAULT_OUT = _ROOT / "src" / "noveltrans" / "gui" / "assets" / "tray-glyph.png"

# Measured from the source: the ground is ≈230-232, the strokes are 0-2. Everything above
# the floor is dropped; the gap is wide enough that the exact value does not matter (100,
# 120 and 150 all give the same trim).
_FLOOR = 150
_EXPECTED_BBOX = (24, 102, 1000, 922)  # measured glyph extent; asserted, not assumed
_BBOX_TOLERANCE = 4

HEIGHT = 512  # master height; width follows the trim (currently ~609, i.e. landscape)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_OUT

    if not _SOURCE.is_file():
        print(f"missing source {_SOURCE}", file=sys.stderr)
        return 1

    grey = Image.open(_SOURCE).convert("L")
    alpha = grey.point(lambda v: max(0, min(255, round((_FLOOR - v) * 255 / _FLOOR))))

    bbox = alpha.getbbox()
    if bbox is None:
        print("source has no ink at all", file=sys.stderr)
        return 1
    drift = max(abs(a - b) for a, b in zip(bbox, _EXPECTED_BBOX))
    if drift > _BBOX_TOLERANCE:
        # The source changed shape. Re-measure and update _EXPECTED_BBOX deliberately
        # rather than let a silently different trim ship.
        print(f"glyph bbox {bbox} differs from expected {_EXPECTED_BBOX}", file=sys.stderr)
        return 1

    alpha = alpha.crop(bbox)
    width = round(alpha.width * HEIGHT / alpha.height)
    alpha = alpha.resize((width, HEIGHT), Image.LANCZOS)

    glyph = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    glyph.putalpha(alpha)

    out.parent.mkdir(parents=True, exist_ok=True)
    glyph.save(out)
    print(f"wrote {out} ({width}x{HEIGHT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

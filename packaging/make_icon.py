"""Convert design/logo-icon.png into packaging/NovelTrans.png (the app icon master).

Run: python packaging/make_icon.py packaging/NovelTrans.png [--asset <path>]
The Makefile then turns the PNG into NovelTrans.icns via sips + iconutil, and
make_ico.py turns it into the Windows .ico.

**Why this crops and re-masks instead of just resizing the source.** `design/logo-icon.png`
looks like it has a transparent background, but its alpha channel is fully opaque — the
grey/white "transparency checkerboard" around the tile is painted into the RGB pixels.
Resizing it as-is would ship an icon sitting on a literal checkerboard. Nor can the
checkerboard be colour-keyed away: the tile's own frame is a near-white neutral, i.e. the
same colour as half the checker squares. So the tile rect is cropped out by measurement
and given a fresh rounded-rect alpha mask, which discards the checkerboard by
construction.

`_SRC_TILE` was measured from this specific source file: the tile is a 900x900 rounded
square inset 62 px on every side, edged with a thin grey outline stroke (darkest pixels at
x=62..63 and x=960..962 on every mid-height row). The crop sits 2 px inside that stroke so
no checker pixel can survive; the rounded mask hides the tight crop.

Output geometry follows Apple's macOS icon grid: an 824x824 tile centred in a 1024x1024
canvas (100 px padding) with a corner radius of 0.2246 x 824. That is a *larger* radius
than the source tile's own (~181/900), so the mask always cuts inside the source's corner
— which is what keeps the checkerboard out of the corners.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

_ROOT = Path(__file__).resolve().parent.parent  # repo root, not the cwd: `make app`
_SOURCE = _ROOT / "design" / "logo-icon.png"    # and `make.ps1 app` run from packaging/

_SRC_TILE = (64, 64, 960, 960)  # measured — see the module docstring

SIZE = 1024   # macOS icon master
TILE = 824    # Apple's content square inside it
MARGIN = (SIZE - TILE) // 2
RADIUS = round(TILE * 0.2246)   # Big Sur corner proportion
_SUPERSAMPLE = 4  # ImageDraw.rounded_rectangle aliases badly at 1x

ASSET_SIZE = 256  # the in-app copy used by app.setWindowIcon (src/noveltrans/gui/assets)


def render() -> Image.Image:
    """The app icon as a 1024x1024 RGBA image with genuinely transparent corners."""
    tile = Image.open(_SOURCE).convert("RGB").crop(_SRC_TILE)
    tile = tile.resize((TILE, TILE), Image.LANCZOS)

    big = TILE * _SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=RADIUS * _SUPERSAMPLE, fill=255
    )
    mask = mask.resize((TILE, TILE), Image.LANCZOS)

    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(tile, (MARGIN, MARGIN), mask)
    return icon


def main() -> int:
    argv = sys.argv[1:]
    asset_out = None
    if "--asset" in argv:
        i = argv.index("--asset")
        asset_out = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    out = argv[0] if argv else "NovelTrans.png"

    if not _SOURCE.is_file():
        print(f"missing source {_SOURCE}", file=sys.stderr)
        return 1

    icon = render()
    icon.save(out)
    print(f"wrote {out}")

    if asset_out:
        icon.resize((ASSET_SIZE, ASSET_SIZE), Image.LANCZOS).save(asset_out)
        print(f"wrote {asset_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

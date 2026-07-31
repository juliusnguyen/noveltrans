"""Render NovelTrans.png (from make_icon.py) into a multi-size Windows .ico.

Run: python packaging/make_ico.py packaging/NovelTrans.png [packaging/NovelTrans.ico]
Wired into `make.ps1 icon` (Windows) alongside the macOS .icns build in the Makefile.
"""

from __future__ import annotations

import sys

from PIL import Image

_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "NovelTrans.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "packaging/NovelTrans.ico"

    image = Image.open(src).convert("RGBA")
    image.save(out, format="ICO", sizes=_SIZES)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

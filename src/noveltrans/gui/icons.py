"""Loader for the brand images shipped in `noveltrans/gui/assets/`.

Two of them, both generated from `design/` by `make icon` (see packaging/make_icon.py and
packaging/make_tray_glyph.py):

* `app-icon.png` — the rounded tile, used by `app.setWindowIcon`.
* `tray-glyph.png` — the wolf mark in black-on-transparent, used by `tray._glyph`.

**Read as bytes, not through `resources.as_file`.** The `as_file` form in `tts/video.py`
exists because ffmpeg and libass are separate processes that need a real path on disk;
Qt takes the bytes directly, so there is no reason to pay for a temp-dir context manager
that would have to stay open for the pixmap's lifetime. `QPixmap.loadFromData` also works
unchanged inside the frozen PyInstaller archive.
"""

from __future__ import annotations

from importlib import resources

from PySide6.QtGui import QPixmap

_cache: dict[str, QPixmap] = {}


def load_pixmap(name: str) -> QPixmap:
    """The named asset as a QPixmap; a null QPixmap if it is missing or unreadable.

    Never raises: a missing asset should cost the app its icon, not its startup. Cached
    because `build_tray_icon()` asks for the same master once per size.
    """
    if name not in _cache:
        pixmap = QPixmap()
        try:
            data = resources.files("noveltrans.gui").joinpath("assets", name).read_bytes()
        except (FileNotFoundError, OSError, ModuleNotFoundError):
            pass
        else:
            pixmap.loadFromData(data)
        _cache[name] = pixmap
    return _cache[name]

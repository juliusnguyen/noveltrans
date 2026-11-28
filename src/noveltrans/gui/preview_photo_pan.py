"""Drag the small photo inside the video preview to choose which part of the image shows.

The preview window (Video tab → "Xem trước") shows one rendered frame. A full re-render
is a bake plus an ffmpeg call, a couple of seconds, far too slow to follow a drag. So
while the user drags, `PhotoPanLabel` repaints only the photo box of the last rendered
frame with Pillow, using the same `_cover_fit` crop the renderer uses. On release it
reports the final framing and the tab saves it and runs one real re-render.

The pan math (`player_skin.pan_photo_focus`) is in slot units at any scale, so the label
works in its own display pixels and never needs the output resolution, only the
proportions `PlayerLayout` gives.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel

from noveltrans.tts.player_skin import (
    DEFAULT_PHOTO_FOCUS,
    DEFAULT_PHOTO_ZOOM,
    MAX_PHOTO_ZOOM,
    PlayerLayout,
    _cover_fit,
    _rounded_mask,
    clamp_photo_focus,
    clamp_photo_zoom,
    load_photo,
    pan_photo_focus,
)

# The rendered frame is shown no larger than this (the dialog's old fixed preview size).
PREVIEW_MAX_SIZE = (900, 520)


def _pil_to_pixmap(img: Image.Image) -> QPixmap:
    """Convert a Pillow image to a QPixmap (copied, so it owns its buffer)."""
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def display_size(frame_size: tuple[int, int], max_size=PREVIEW_MAX_SIZE) -> tuple[int, int]:
    """The frame scaled down (never up) to fit `max_size`, keeping its aspect ratio."""
    fw, fh = frame_size
    k = min(max_size[0] / fw, max_size[1] / fh, 1.0)
    return (max(1, round(fw * k)), max(1, round(fh * k)))


def photo_rect(frame_size: tuple[int, int], shown_size: tuple[int, int]) -> tuple[int, int, int, int, int]:
    """(x, y, w, h, radius) of the small photo box in display pixels."""
    lay = PlayerLayout.of(*frame_size)
    k = shown_size[0] / frame_size[0]
    return (
        round(lay.photo_x * k), round(lay.photo_y * k),
        max(1, round(lay.photo_w * k)), max(1, round(lay.photo_h * k)),
        max(0, round(lay.photo_radius * k)),
    )


def compose_pan_overlay(base: Image.Image, source: Image.Image,
                        rect: tuple[int, int, int, int, int], focus, zoom) -> Image.Image:
    """`base` with the photo box repainted from `source` at `focus` / `zoom`.

    The quick stand-in for a full re-render while dragging. Only pixels inside the box
    change, and the box keeps the renderer's thin white outline.
    """
    x, y, w, h, radius = rect
    out = base.convert("RGBA").copy()
    crop = _cover_fit(source, w, h, focus=focus, zoom=zoom)
    out.paste(crop, (x, y), _rounded_mask((w, h), radius))
    outline = max(1, round(base.width * 0.0026))
    ImageDraw.Draw(out).rounded_rectangle(
        (x, y, x + w, y + h), radius=radius, outline=(255, 255, 255, 220), width=outline,
    )
    return out


class PhotoPanLabel(QLabel):
    """The preview image; drag on the small photo to pan it, wheel over it to zoom.

    `panned` fires during a drag with the live focus (nothing is saved), `pan_finished`
    once on release if the focus moved, and `zoom_step` (+1 / -1) on the mouse wheel.
    """

    panned = Signal(float, float)
    pan_finished = Signal(float, float)
    zoom_step = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._base: Image.Image | None = None    # last rendered frame, at display size
        self._source: Image.Image | None = None  # background image, downscaled for drawing
        self._src_size: tuple[int, int] = (0, 0)  # its ORIGINAL size, for the pan math
        self._rect: tuple[int, int, int, int, int] | None = None
        self._focus = DEFAULT_PHOTO_FOCUS
        self._zoom = DEFAULT_PHOTO_ZOOM
        self._interactive = True
        self._press: QPointF | None = None
        self._press_focus = DEFAULT_PHOTO_FOCUS
        self._moved = False

    # ------------------------------------------------------------ state

    @property
    def focus(self) -> tuple[float, float]:
        return self._focus

    @property
    def can_pan(self) -> bool:
        return self._rect is not None and self._source is not None

    def set_frame(self, png_path: str, image_path: str, focus, zoom,
                  max_size=PREVIEW_MAX_SIZE) -> None:
        """Show the rendered frame at `png_path`; `image_path` is the background it framed."""
        frame = Image.open(png_path).convert("RGBA")
        shown = display_size(frame.size, max_size)
        self._base = frame.resize(shown, Image.Resampling.LANCZOS)
        self._rect = photo_rect(frame.size, shown)
        self._focus = clamp_photo_focus(focus)
        self._zoom = clamp_photo_zoom(zoom)
        self._load_source(image_path)
        self.setFixedSize(*shown)  # label px == pixmap px, so mouse positions map 1:1
        self.setToolTip(
            "Kéo trên ảnh nhỏ để chọn phần ảnh hiện trong khung · lăn chuột để phóng to/thu nhỏ."
            if self.can_pan else "Không đọc được ảnh nền nên không kéo chỉnh được khung ảnh."
        )
        self.setPixmap(_pil_to_pixmap(self._base))

    def _load_source(self, image_path: str) -> None:
        self._source, self._src_size = None, (0, 0)
        if not image_path or self._rect is None:
            return
        try:
            src = load_photo(Path(image_path))
        except OSError:
            return
        self._src_size = src.size
        # Keep only the resolution the overlay can ever use (box size at max zoom).
        _x, _y, w, h, _r = self._rect
        need = max(w / src.width, h / src.height) * MAX_PHOTO_ZOOM
        if need < 1.0:
            src = src.resize(
                (max(1, round(src.width * need)), max(1, round(src.height * need))),
                Image.Resampling.LANCZOS,
            )
        self._source = src

    def set_focus_zoom(self, focus, zoom) -> None:
        """Show a new framing right away, without waiting for a re-render."""
        self._focus = clamp_photo_focus(focus)
        self._zoom = clamp_photo_zoom(zoom)
        self._repaint_overlay()

    def set_interactive(self, interactive: bool) -> None:
        """Ignore drags and wheel while a re-render is running (it would be dropped)."""
        self._interactive = interactive
        if not interactive:
            self._press = None
            self.unsetCursor()

    def _repaint_overlay(self) -> None:
        if self._base is None or not self.can_pan:
            return
        self.setPixmap(_pil_to_pixmap(
            compose_pan_overlay(self._base, self._source, self._rect, self._focus, self._zoom)
        ))

    def _in_photo(self, pos: QPointF) -> bool:
        if self._rect is None:
            return False
        x, y, w, h, _r = self._rect
        return x <= pos.x() < x + w and y <= pos.y() < y + h

    # ------------------------------------------------------------ Qt events

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        pos = event.position()
        if (self._interactive and self.can_pan and event.button() == Qt.MouseButton.LeftButton
                and self._in_photo(pos)):
            self._press = pos
            self._press_focus = self._focus
            self._moved = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        pos = event.position()
        if self._press is not None:
            _x, _y, w, h, _r = self._rect
            focus = pan_photo_focus(
                self._src_size, (w, h), self._press_focus, self._zoom,
                pos.x() - self._press.x(), pos.y() - self._press.y(),
            )
            if focus != self._focus:
                self._focus = focus
                self._moved = True
                self._repaint_overlay()
                self.panned.emit(*focus)
            event.accept()
            return
        if self._interactive and self.can_pan and self._in_photo(pos):
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._press is not None and event.button() == Qt.MouseButton.LeftButton:
            self._press = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if self._moved:
                self.pan_finished.emit(*self._focus)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        delta = event.angleDelta().y()
        if self._interactive and self.can_pan and delta and self._in_photo(event.position()):
            self.zoom_step.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

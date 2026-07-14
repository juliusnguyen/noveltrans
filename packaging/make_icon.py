"""Render a 1024×1024 app icon PNG (rounded accent tile + a book glyph).

Run headless: QT_QPA_PLATFORM=offscreen python packaging/make_icon.py <out.png>
The Makefile then turns the PNG into NovelTrans.icns via sips + iconutil.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication

SIZE = 1024


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "NovelTrans.png"
    QApplication(sys.argv)

    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # rounded tile with a top-to-bottom accent gradient
    grad = QLinearGradient(0, 0, 0, SIZE)
    grad.setColorAt(0.0, QColor("#5590ff"))
    grad.setColorAt(1.0, QColor("#2f5fd0"))
    margin = 80
    radius = 220
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(margin, margin, SIZE - 2 * margin, SIZE - 2 * margin), radius, radius)

    # book glyph, white serif
    font = QFont("Songti SC", 300)
    font.setStyleHint(QFont.StyleHint.Serif)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("#ffffff"))
    p.drawText(img.rect(), int(Qt.AlignmentFlag.AlignCenter), "小說")

    p.end()
    if not img.save(out):
        print(f"failed to save {out}", file=sys.stderr)
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

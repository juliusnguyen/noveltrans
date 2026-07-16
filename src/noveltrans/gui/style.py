"""A cohesive dark theme for the app: Fusion style + palette + one QSS sheet.

Call apply_theme(app) once at startup. Primary call-to-action buttons opt into
the accent colour by setting the dynamic property `primary` to True.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

# palette tokens (kept in sync with the QSS below)
BG = "#1b1e24"  # window
SURFACE = "#23272f"  # cards, inputs
SURFACE_HI = "#2b303b"  # hover / raised
BORDER = "#353c49"
BORDER_HI = "#454e5e"
TEXT = "#e6ebf2"
MUTED = "#98a2b2"
ACCENT = "#3f7bef"
ACCENT_HI = "#5590ff"
ACCENT_LO = "#3468d6"

_QSS = f"""
* {{ outline: none; }}

QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

QLabel {{ background: transparent; }}
QLabel[muted="true"] {{ color: {MUTED}; }}

/* ---- tabs as a segmented control ---- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    top: -1px;
    background-color: {SURFACE};
}}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 8px 18px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-radius: 8px;
}}
QTabBar::tab:selected {{ background-color: {ACCENT}; color: #ffffff; }}
QTabBar::tab:hover:!selected {{ background-color: {SURFACE_HI}; color: {TEXT}; }}

/* ---- outer workspace tabs: browser-style, distinct from the inner step-tabs ---- */
QTabWidget#workspaceTabs::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    top: -1px;
    background-color: {SURFACE};
}}
QTabWidget#workspaceTabs > QTabBar {{ qproperty-drawBase: 0; }}
QTabWidget#workspaceTabs > QTabBar::tab {{
    background: {BG};
    color: {MUTED};
    padding: 7px 8px 7px 14px;
    margin-right: 3px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    min-width: 92px;
    max-width: 190px;
}}
QTabWidget#workspaceTabs > QTabBar::tab:selected {{
    background: {SURFACE};
    color: {TEXT};
    border-color: {BORDER_HI};
}}
QTabWidget#workspaceTabs > QTabBar::tab:hover:!selected {{
    background: {SURFACE_HI};
    color: {TEXT};
}}
/* our own per-tab close button (Qt's default icon is invisible on dark) */
QToolButton#tabCloseButton {{
    background: transparent;
    border: none;
    border-radius: 4px;
    color: {MUTED};
    font-size: 12px;
    padding: 0;
    margin-left: 2px;
    min-width: 16px;
    max-width: 16px;
    min-height: 16px;
    max-height: 16px;
}}
QToolButton#tabCloseButton:hover {{ background: #c0433f; color: #ffffff; }}

/* flat icon buttons in the outer tab-bar corners (＋ new, ⚙ settings) */
QPushButton#cornerButton {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 4px 11px;
    margin: 2px 4px;
    color: {MUTED};
    font-size: 17px;
}}
QPushButton#cornerButton:hover {{ background: {SURFACE_HI}; color: {TEXT}; }}
QPushButton#cornerButton:pressed {{ background: {BORDER}; }}

/* ---- buttons ---- */
QPushButton {{
    background-color: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 15px;
}}
QPushButton:hover {{ background-color: #323845; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background-color: #262b34; }}
QPushButton:disabled {{ color: #616a78; background-color: {SURFACE}; border-color: #2c323d; }}

QPushButton[primary="true"] {{
    background-color: {ACCENT};
    color: #ffffff;
    border: 1px solid {ACCENT};
    font-weight: 600;
}}
QPushButton[primary="true"]:hover {{ background-color: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton[primary="true"]:pressed {{ background-color: {ACCENT_LO}; }}
QPushButton[primary="true"]:disabled {{ background-color: #2b3a58; color: #8fa0c0; border-color: #2b3a58; }}

/* ---- text inputs ---- */
QLineEdit, QComboBox, QAbstractSpinBox, QPlainTextEdit, QTextEdit {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 9px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT_HI}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {MUTED}; background-color: #202329; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    padding: 4px;
}}

/* ---- spin box up/down buttons (default Fusion arrows are near-invisible here) ---- */
QAbstractSpinBox {{ padding-right: 26px; }}  /* reserve room for the two buttons */
QAbstractSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 24px;
    background-color: {SURFACE_HI};
    border-left: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    border-top-right-radius: 8px;
}}
QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 24px;
    background-color: {SURFACE_HI};
    border-left: 1px solid {BORDER};
    border-bottom-right-radius: 8px;
}}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background-color: {ACCENT};
}}
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
    background-color: {ACCENT_LO};
}}
/* arrow glyphs (image: url) are injected by apply_theme — see _icon_qss */

/* ---- tables ---- */
QTableView {{
    background-color: #1f232a;
    alternate-background-color: {SURFACE};
    gridline-color: #2c323d;
    border: 1px solid {BORDER};
    border-radius: 10px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QTableView::item {{ padding: 5px 7px; border: none; }}
QHeaderView {{ background-color: transparent; }}
QHeaderView::section {{
    background-color: #262b34;
    color: {MUTED};
    padding: 8px 8px;
    border: none;
    border-right: 1px solid #2c323d;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}
QHeaderView::section:first {{ border-top-left-radius: 10px; }}
QHeaderView::section:last {{ border-right: none; border-top-right-radius: 10px; }}
QTableCornerButton::section {{ background-color: #262b34; border: none; }}

/* ---- group boxes ---- */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 16px;
    padding: 12px 12px 10px 12px;
    background-color: {SURFACE};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {MUTED};
    font-weight: 600;
}}

/* ---- progress ---- */
QProgressBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    height: 18px;
    text-align: center;
    color: {TEXT};
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 7px; }}

/* ---- radio / check ---- */
QRadioButton, QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 17px;
    height: 17px;
    background-color: {SURFACE};
    border: 1px solid {BORDER_HI};
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT_HI}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked:hover, QRadioButton::indicator:checked:hover {{
    background-color: {ACCENT_HI};
    border-color: {ACCENT_HI};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background-color: #202329;
    border-color: {BORDER};
}}

/* ---- scrollbars ---- */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER_HI}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {BORDER_HI}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- menus / status / tooltip ---- */
QMenuBar {{ background-color: #171a1f; color: #c8d0dc; }}
QMenuBar::item {{ padding: 5px 10px; background: transparent; }}
QMenuBar::item:selected {{ background-color: {SURFACE_HI}; border-radius: 6px; }}
QMenu {{ background-color: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {ACCENT}; color: #ffffff; }}
QStatusBar {{ background-color: #171a1f; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}
QToolTip {{
    background-color: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 7px;
}}
"""


_ICON_DIR = Path(tempfile.gettempdir()) / "noveltrans-theme-icons"


def _pixmap(size: int = 16) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    return pix


def _save(pix: QPixmap, name: str) -> str:
    """Write a themed glyph to disk; return a QSS-safe (forward-slash) path."""
    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    path = _ICON_DIR / name
    pix.save(str(path))
    return path.as_posix()


def _make_icons() -> dict[str, str]:
    """Render the spinbox arrows / check / radio-dot glyphs the QSS references.

    Generated at runtime (needs a live QApplication) so no image assets have to
    ship with the frozen app, and so the colours track the palette tokens above.
    Qt's stylesheet url() has no data-URI support, hence real files.
    """
    icons: dict[str, str] = {}
    for name, up in (("arrow_up", True), ("arrow_down", False)):
        pix = _pixmap()
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(TEXT))
        cx, cy, half_w, half_h = 8.0, 8.0, 4.5, 2.6
        if up:
            tri = [
                QPointF(cx - half_w, cy + half_h),
                QPointF(cx + half_w, cy + half_h),
                QPointF(cx, cy - half_h),
            ]
        else:
            tri = [
                QPointF(cx - half_w, cy - half_h),
                QPointF(cx + half_w, cy - half_h),
                QPointF(cx, cy + half_h),
            ]
        p.drawPolygon(QPolygonF(tri))
        p.end()
        icons[name] = _save(pix, f"{name}.png")

    # white checkmark for a ticked QCheckBox (drawn on the accent fill)
    pix = _pixmap()
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#ffffff"))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(3.5, 8.4), QPointF(6.6, 11.5), QPointF(12.5, 4.8)]))
    p.end()
    icons["check"] = _save(pix, "check.png")

    # white dot for a selected QRadioButton
    pix = _pixmap()
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(8.0, 8.0), 3.1, 3.1)
    p.end()
    icons["radio_dot"] = _save(pix, "radio_dot.png")
    return icons


def _icon_qss(icons: dict[str, str]) -> str:
    """QSS fragment (appended after _QSS) that points the sub-controls at the
    generated glyphs. Later rules override, so this fills in image: url(...)."""
    return f"""
QAbstractSpinBox::up-arrow {{
    image: url({icons["arrow_up"]});
    width: 12px; height: 12px;
}}
QAbstractSpinBox::down-arrow {{
    image: url({icons["arrow_down"]});
    width: 12px; height: 12px;
}}
QAbstractSpinBox::up-arrow:off {{ image: none; }}
QAbstractSpinBox::down-arrow:off {{ image: none; }}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url({icons["check"]});
}}
QCheckBox::indicator:checked:hover {{
    background-color: {ACCENT_HI};
    border-color: {ACCENT_HI};
}}
QRadioButton::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url({icons["radio_dot"]});
}}
QRadioButton::indicator:checked:hover {{
    background-color: {ACCENT_HI};
    border-color: {ACCENT_HI};
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1f232a"))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(SURFACE_HI))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE_HI))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT_HI))
    disabled = QColor("#616a78")
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    app.setPalette(pal)

    app.setStyleSheet(_QSS + _icon_qss(_make_icons()))


def mark_primary(*buttons) -> None:
    """Give call-to-action buttons the accent colour."""
    for button in buttons:
        button.setProperty("primary", True)
        # dynamic property set before first show — no repolish needed, but be safe
        button.style().unpolish(button)
        button.style().polish(button)


def repolish(widget) -> None:
    """Re-apply the stylesheet to a widget after a dynamic property changes."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# `Qt` re-exported for callers that style ad-hoc widgets alongside the theme.
__all__ = ["apply_theme", "mark_primary", "repolish", "Qt"]

"""Dock/taskbar notifications: a red badge + attention bounce when a long-running
flow stops and needs the user (e.g. the daily read limit was hit).

`setBadgeNumber` shows a red badge on the macOS Dock icon (and the Unity launcher
count on Linux); it's a no-op where unsupported.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QWidget


def _set_badge(count: int) -> None:
    app = QApplication.instance()
    if app is not None and hasattr(app, "setBadgeNumber"):
        app.setBadgeNumber(count)


def set_dock_badge(count: int = 1) -> None:
    _set_badge(count)


def clear_dock_badge() -> None:
    _set_badge(0)


def request_attention(widget: QWidget | None = None) -> None:
    """Bounce the Dock icon until the app is brought to the front."""
    app = QApplication.instance()
    if app is not None:
        app.alert(widget, 0)  # msec=0 → keep alerting until the window activates

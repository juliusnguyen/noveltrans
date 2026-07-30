"""Show/hide the macOS Dock icon at runtime, so the app can live in the menu bar alone.

Qt has no API for this — it is `NSApplication.setActivationPolicy:`, reached here through
`ctypes` and `libobjc` rather than by adding pyobjc as a dependency for two calls.

* `Regular` (0) — Dock icon and application menu bar. The window is open.
* `Accessory` (1) — menu-bar item only, no Dock tile. Hidden to the menu bar.

**This is deliberately NOT `LSUIElement` in Info.plist.** That would remove the Dock icon
for the whole life of the app, including while the window is open; this is reversible, so
the app is an ordinary Dock app whenever you can see it.

Three things stop working while the Dock icon is gone. They are the cost of the feature,
not bugs:

* the Dock badge (there is no tile to badge),
* clicking the Dock icon to bring the window back (the menu-bar icon is the way in),
* ⌘Q (in Accessory mode the app does not own the menu bar, so the key never arrives —
  `Thoát` in the menu-bar panel is the way out).

Every function here returns False rather than raising on a non-macOS platform or if the
Objective-C call fails: losing the Dock icon must never be able to take the app down, and
a caller that cannot hide the icon should still hide the window.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

POLICY_REGULAR = 0
POLICY_ACCESSORY = 1

_cached: dict[str, object] = {}


def _nsapp():
    """The shared NSApplication, or None where there isn't one."""
    if sys.platform != "darwin":
        return None
    if "app" in _cached:
        return _cached["app"]
    _cached["app"] = None
    try:
        objc = ctypes.CDLL(ctypes.util.find_library("objc"))
        # Loading AppKit is what makes the NSApplication class exist to look up.
        ctypes.CDLL(ctypes.util.find_library("AppKit"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        cls = objc.objc_getClass(b"NSApplication")
        if not cls:
            return None
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        app = send(cls, objc.sel_registerName(b"sharedApplication"))
        if app:
            _cached["objc"] = objc
            _cached["app"] = app
    except Exception:  # noqa: BLE001 — a missing/odd runtime must not crash the app
        _cached["app"] = None
    return _cached["app"]


def _set_policy(policy: int) -> bool:
    """Set the activation policy. True once the app IS in that policy.

    `setActivationPolicy:` returns NO when the policy is already the requested one, which
    would otherwise read as a failure — so the result is judged by where we ended up, not
    by what the setter returned.
    """
    app = _nsapp()
    if not app:
        return False
    try:
        objc = _cached["objc"]
        send = objc.objc_msgSend
        send.restype = ctypes.c_bool
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(app, objc.sel_registerName(b"setActivationPolicy:"), policy)
    except Exception:  # noqa: BLE001
        return False
    return current_policy() == policy


def current_policy() -> int | None:
    """The app's activation policy, or None where it can't be read."""
    app = _nsapp()
    if not app:
        return None
    try:
        objc = _cached["objc"]
        send = objc.objc_msgSend
        send.restype = ctypes.c_long
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return int(send(app, objc.sel_registerName(b"activationPolicy")))
    except Exception:  # noqa: BLE001
        return None


def hide_dock_icon() -> bool:
    """Drop the Dock tile, keeping only the menu-bar item. False if unsupported."""
    return _set_policy(POLICY_ACCESSORY)


def show_dock_icon() -> bool:
    """Put the Dock tile back. False if unsupported."""
    return _set_policy(POLICY_REGULAR)


def available() -> bool:
    return _nsapp() is not None


def _reset_cache() -> None:
    """Tests only — forget the resolved NSApplication handle."""
    _cached.clear()

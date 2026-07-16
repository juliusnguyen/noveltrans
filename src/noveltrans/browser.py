"""Shared Playwright browser launching for the features that need a real browser.

Two features drive a browser: Discord auto-unlock (`discord_unlock.py`) and getting
past 69shuba's Cloudflare challenge (`cf_browser.py`). Both need the *same* launch
setup — real Chrome preferred, bundled Chromium as fallback, automation markers
suppressed — so it lives here rather than being copy-pasted. These args exist to keep
the browser looking like an ordinary Chrome; two copies drifting apart would mean one
caller silently losing that.

Each caller keeps its own profile directory (a Discord login has no business in a
scraping profile) and its own user-facing error strings: the app speaks Vietnamese,
this module is infrastructure, so it raises `BrowserUnavailableError` in English and
callers translate.

Playwright is an optional dependency (`pip install 'noveltrans[browser]'` then
`playwright install chromium`); it is imported lazily so the core app runs without it.
"""

from __future__ import annotations

from pathlib import Path

# Real Chrome first, Playwright's bundled Chromium (channel=None) as the fallback.
# Login gates and bot checks routinely refuse the bundled build — it's a different
# build with a headless-ish fingerprint — so drive the *installed* Chrome when there
# is one.
_BROWSER_CHANNELS = ("chrome", None)
_LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    # Bot checks react badly to navigator.webdriver; this and dropping
    # --enable-automation keep the window looking like an ordinary Chrome.
    "--disable-blink-features=AutomationControlled",
]


class BrowserUnavailableError(Exception):
    """Playwright isn't installed, or no browser could be launched.

    Callers catch this and re-raise their own user-facing error.
    """


def require_playwright():
    """Import Playwright's sync API, or raise if the optional dep is missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # optional dependency not installed
        raise BrowserUnavailableError(
            "Playwright is not installed. Run:\n"
            "  pip install 'noveltrans[browser]'\n"
            "  playwright install chromium"
        ) from exc
    return sync_playwright


def launch_persistent_context(sync_playwright, profile_dir: Path, *, headless: bool):
    """Open `profile_dir` in the user's real Chrome (bundled Chromium if not installed).

    Returns `(playwright, context)`; the caller owns both and should hand them to
    `close()` in a `finally`. Takes `sync_playwright` as an argument rather than
    importing it so tests can inject a fake.

    Raises BrowserUnavailableError if no channel could be launched, having stopped
    Playwright first so no process leaks.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    last_exc: Exception | None = None
    for channel in _BROWSER_CHANNELS:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                channel=channel,
                args=_LAUNCH_ARGS,
                ignore_default_args=["--enable-automation"],
            )
        except Exception as exc:  # channel not installed on this machine
            last_exc = exc
            continue
        return playwright, context

    playwright.stop()
    raise BrowserUnavailableError(
        "Could not launch a browser. Install Google Chrome, or run:  "
        "playwright install chromium"
    ) from last_exc


def close(context, playwright) -> None:
    """Tear down a context and its Playwright. Never raises — callers use it in
    `finally`, often on paths that are already failing."""
    for close_fn in (context.close, playwright.stop):
        try:
            close_fn()
        except Exception:
            pass

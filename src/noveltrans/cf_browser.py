"""A persistent browser session for reading 69shuba, which sits behind Cloudflare.

69shuba's operator deliberately deployed a Cloudflare challenge. This module drives a
real browser past it — not by forging or defeating anything, but because a real Chrome
is what the check is looking for. Measured facts behind that design (see
changes/015-69SHUBA-SOURCE):

  * The gate is decided on TLS/browser *fingerprint*, at connection. Real Chrome is
    passed silently — no interstitial, no checkbox, and **no cf_clearance cookie is
    issued at all**. So there is no token to harvest and replay over `requests`: the
    live browser session *is* the clearance, for every page of a batch. That's why
    this is a session and not a `solve()`.
  * Headless Chrome is fingerprinted and never clears the challenge (measured: 180s
    timeout headless vs instant headed). `headless=True` is offered but will very
    likely fail; the default is headed.

Keep the polite delay on. `HttpClient`'s throttle is bypassed entirely on this path,
so `_throttle()` here is the only thing standing between a 199-chapter download and
hammering the site flat. This is meant for personal reading volume.

**Do not parallelise 69shuba.** N concurrent sessions is N Chromes, and hammering a
CF-protected site is the fastest route to an IP block — which would kill the browser
path too, and it is the only path this site has.

Playwright is an optional dependency (`pip install 'noveltrans[browser]'` then
`playwright install chromium`); it is imported lazily so the core app runs without it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noveltrans.browser import close as _close_browser
from noveltrans.browser import launch_persistent_context, require_playwright
from noveltrans.storage.library import DEFAULT_LIBRARY_DIR

# Markers that mean "this is the interstitial, not content". Proven against the real
# 403 body saved at tests/fixtures/69shuba/challenge.html.
_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "__cf_chl",
    "Just a moment",
    "Enable JavaScript and cookies to continue",
)

# Subresources we abort to keep navigations text-only: on a 199-chapter batch this is
# most of the bytes and most of the requests. NEVER block "document", "script" or
# "xhr" — Cloudflare's own check is JS-driven and blocking those would fail the gate.
# If 403s ever start appearing, emptying this set is the first diagnostic.
_BLOCKED_TYPES = {"image", "font", "media", "stylesheet"}

# Serialises browser launches: two threads launching Chromium on one profile dir
# corrupts it. Downloads are sequential today, but the parallel direction is live.
_LAUNCH_LOCK = threading.Lock()


def profile_dir() -> Path:
    """Dedicated Chromium profile for scraping.

    Separate from `.discord-profile`: different sites, different cookie jars, and a
    throwaway Discord login has no business in a scraping profile.
    """
    return DEFAULT_LIBRARY_DIR / ".cf-profile"


def looks_like_challenge(markup: str) -> bool:
    """True if `markup` is a Cloudflare interstitial rather than page content."""
    return any(marker in markup for marker in _CHALLENGE_MARKERS)


class BrowserSessionError(Exception):
    """The session could not fetch a page. Callers translate to a user-facing error."""


class BrowserSession:
    """One persistent Chrome context, reused for an entire scan/download batch.

    Launching is lazy — constructing a session must never start Chrome, since adapters
    are built for cancelled scans and in tests. `sync_playwright` is injectable so
    tests can fake the browser entirely.
    """

    def __init__(
        self,
        profile: Path | None = None,
        *,
        headless: bool = False,
        delay_seconds: float = 1.5,
        timeout_ms: int = 60_000,
        sync_playwright=None,
    ):
        self.profile = profile or profile_dir()
        self.headless = headless
        self.delay_seconds = delay_seconds
        self.timeout_ms = timeout_ms
        self._sync_playwright = sync_playwright
        self._playwright = None
        self._context = None
        self._page = None
        self._last_request_at = 0.0

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _launch(self) -> None:
        """Start the browser and prepare the one page this session reuses."""
        sync_playwright = self._sync_playwright or require_playwright()
        with _LAUNCH_LOCK:
            self._playwright, self._context = launch_persistent_context(
                sync_playwright, self.profile, headless=self.headless
            )
        page = self._context.pages[0] if self._context.pages else self._context.new_page()
        page.set_default_timeout(self.timeout_ms)
        # One route for the session's whole life — re-registering per navigation would
        # re-pay the setup ~199 times.
        page.route("**/*", _block_heavy_subresources)
        self._page = page

    def close(self) -> None:
        """Tear the browser down. Idempotent, never raises — callers use it in
        `finally`, often on paths that are already failing."""
        if self._context is not None:
            _close_browser(self._context, self._playwright)
        self._playwright = self._context = self._page = None

    # -- fetching ----------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

    def get_html(self, url: str) -> str:
        """Navigate to `url` and return the rendered HTML.

        Raises BrowserSessionError if the browser is gone (crashed, or the user closed
        the window) or if Cloudflare returned an interstitial — handing challenge markup
        to a parser would otherwise surface as a misleading "layout may have changed".
        """
        if self._page is None:
            self._launch()

        self._throttle()
        self._last_request_at = time.monotonic()
        try:
            self._page.goto(url, wait_until="domcontentloaded")
            markup = self._page.content()
        except Exception as exc:  # closed window, crash, navigation timeout
            self.close()  # the session is dead; don't let 198 more chapters retry it
            raise BrowserSessionError(f"Browser navigation failed: {exc}") from exc

        if looks_like_challenge(markup):
            raise BrowserSessionError(f"Cloudflare returned a challenge for {url}")
        return markup


def _block_heavy_subresources(route) -> None:
    """Abort images/fonts/media/CSS; let documents, scripts and XHR through."""
    if route.request.resource_type in _BLOCKED_TYPES:
        route.abort()
    else:
        route.continue_()

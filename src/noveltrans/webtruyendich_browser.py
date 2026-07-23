"""A persistent browser session for webtruyendich.com.

webtruyendich sits behind the same kind of Cloudflare gate as 69shuba — a real
Chrome passes it on fingerprint, headless does not (see `cf_browser.py` for the
measured details). But this site needs one thing 69shuba doesn't: the chapter
text is produced on-page by an **AI translation** the reader picks from a
dropdown, so reading a chapter is not navigate→HTML — it's

    navigate → choose the model → click "Dịch lại" → wait for the streamed
    paragraphs to finish rendering → read the container.

That interaction is why this is a separate module from `cf_browser` rather than
an extension of `BrowserSession`: keeping the two apart leaves 69shuba's proven
navigate→HTML primitive untouched. Both share the launch layer in `browser.py`
(real Chrome, anti-automation args) and each keeps its own profile dir.

The wait is **hands-off**: it polls the rendered text until it stops growing and
the "AI Loading…" marker is gone. It never touches the Cloudflare Turnstile
widget the site shows while generating — if an interactive challenge ever blocked
generation, the content would simply never stabilise and the wait would time out
into a `WtdBrowserSessionError` (measured feasibility: a real-Chrome session
passes the managed challenge invisibly — see changes/029-WEBTRUYENDICH-SOURCE).

**Do not parallelise.** One Chrome, sequential reads: this is a Cloudflare- and
AI-quota-gated site, and hammering it is the fastest route to a block — which
would kill the only path this site has.

Playwright is an optional dependency (`pip install 'noveltrans[browser]'` then
`playwright install chromium`); it is imported lazily so the core app runs
without it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noveltrans.browser import close as _close_browser
from noveltrans.browser import launch_persistent_context, require_playwright
from noveltrans.storage.library import DEFAULT_LIBRARY_DIR

# Markers that mean "this is a Cloudflare interstitial, not page content".
_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "__cf_chl",
    "Just a moment",
    "Enable JavaScript and cookies to continue",
)

# Subresources we abort to keep navigations lean. NEVER block "document",
# "script" or "xhr": Cloudflare's check is JS-driven and the AI translation
# arrives over XHR — blocking either would break the page.
_BLOCKED_TYPES = {"image", "font", "media", "stylesheet"}

# Serialises browser launches: two threads launching Chromium on one profile dir
# corrupts it.
_LAUNCH_LOCK = threading.Lock()


def profile_dir() -> Path:
    """Dedicated Chromium profile for webtruyendich (its own cookie jar)."""
    return DEFAULT_LIBRARY_DIR / ".webtruyendich-profile"


def looks_like_challenge(markup: str) -> bool:
    """True if `markup` is a Cloudflare interstitial rather than page content."""
    return any(marker in markup for marker in _CHALLENGE_MARKERS)


class WtdBrowserSessionError(Exception):
    """The session could not fetch/translate a page. Callers translate to a
    user-facing error."""


class WtdBrowserSession:
    """One persistent Chrome context, reused for an entire scan/download batch.

    Launching is lazy — constructing a session must never start Chrome, since
    adapters are built for cancelled scans and in tests. `sync_playwright` is
    injectable so tests can fake the browser entirely.
    """

    def __init__(
        self,
        profile: Path | None = None,
        *,
        headless: bool = False,
        delay_seconds: float = 1.5,
        timeout_ms: int = 60_000,
        content_timeout_ms: int = 45_000,
        sync_playwright=None,
    ):
        self.profile = profile or profile_dir()
        self.headless = headless
        self.delay_seconds = delay_seconds
        self.timeout_ms = timeout_ms
        # A first-time (un-cached) AI translation streams for ~10–15s; a cached one
        # returns in ~2s. This bounds the wait before we give up on a chapter.
        self.content_timeout_ms = content_timeout_ms
        self._sync_playwright = sync_playwright
        self._playwright = None
        self._context = None
        self._page = None
        self._last_request_at = 0.0

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> "WtdBrowserSession":
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

    def _goto(self, url: str) -> None:
        if self._page is None:
            self._launch()
        self._throttle()
        self._last_request_at = time.monotonic()
        try:
            self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # closed window, crash, navigation timeout
            self.close()  # the session is dead; don't let the rest of the batch retry it
            raise WtdBrowserSessionError(f"Browser navigation failed: {exc}") from exc

    def get_html(self, url: str) -> str:
        """Navigate to `url` and return the rendered HTML (landing page / TOC).

        Raises WtdBrowserSessionError if the browser is gone or a Cloudflare
        interstitial is still up after giving the managed challenge time to clear.
        """
        self._goto(url)
        return self._content_after_challenge(url)

    def _content_after_challenge(self, url: str, wait_seconds: float = 15.0) -> str:
        """Return the page HTML, first waiting out a managed Cloudflare challenge.

        A real browser sees "Just a moment…" for a beat, then JS redirects to the
        real page. Reading `content()` the instant navigation settles can catch that
        interstitial, so poll until the challenge markers clear (or give up)."""
        page = self._page
        deadline = time.monotonic() + wait_seconds
        try:
            markup = page.content()
            while looks_like_challenge(markup) and time.monotonic() < deadline:
                time.sleep(1.0)
                markup = page.content()
        except Exception as exc:
            self.close()
            raise WtdBrowserSessionError(f"Lost the page while clearing challenge: {exc}") from exc
        if looks_like_challenge(markup):
            raise WtdBrowserSessionError(f"Cloudflare returned a challenge for {url}")
        return markup

    def read_translated_chapter(
        self,
        url: str,
        *,
        translator_select: str,
        translator_value: str,
        retranslate_button: str,
        content_selector: str,
        paragraph_selector: str,
    ) -> str:
        """Navigate, pick the AI model, trigger it, wait hands-off for the streamed
        paragraphs to stabilise, and return the content container's innerHTML.

        Never interacts with the Turnstile widget: it only observes whether the
        translated text arrives. If it doesn't within `content_timeout_ms`, raises
        WtdBrowserSessionError (the caller surfaces a clear message).

        A first-ever (un-cached) chapter is generated live and can occasionally
        stall on the first attempt; a plain reload + re-trigger clears it (measured
        — see changes/029). So one retry is built in before giving up.
        """
        last_exc: WtdBrowserSessionError | None = None
        for _attempt in range(2):
            self._goto(url)
            self._content_after_challenge(url)  # wait out a managed challenge; raises if it persists
            page = self._page
            try:
                self._trigger_translation(
                    page, translator_select, translator_value, retranslate_button
                )
                self._wait_for_stable_content(paragraph_selector, content_selector)
                return page.eval_on_selector(content_selector, "el => el.innerHTML")
            except WtdBrowserSessionError as exc:
                last_exc = exc  # transient first-generation stall → reload and retry once
        raise last_exc

    def _trigger_translation(
        self, page, translator_select: str, translator_value: str, retranslate_button: str
    ) -> None:
        """Pick the AI model and click 'Dịch lại' to start generation."""
        try:
            page.wait_for_selector(translator_select, timeout=self.timeout_ms)
            page.select_option(translator_select, translator_value)
            # Dispatch change (some handlers listen for it) and click "Dịch lại".
            page.evaluate(
                "(sel) => {"
                " const s = document.querySelector(sel);"
                " if (s) s.dispatchEvent(new Event('change', {bubbles: true}));"
                "}",
                translator_select,
            )
            _click_retranslate(page, retranslate_button)
        except WtdBrowserSessionError:
            raise
        except Exception as exc:
            raise WtdBrowserSessionError(f"Could not start AI translation: {exc}") from exc

    def _wait_for_stable_content(self, paragraph_selector: str, content_selector: str) -> None:
        """Poll the content until its text stops growing and 'AI Loading' is gone.

        Waiting for the AI-specific paragraph class (not just any <p>) avoids
        latching onto the stale default 'Vietphrase' body already in the container.
        """
        page = self._page
        deadline = time.monotonic() + self.content_timeout_ms / 1000
        try:
            remaining = max(1, int(deadline - time.monotonic()) * 1000)
            page.wait_for_selector(paragraph_selector, timeout=remaining)
        except Exception as exc:
            raise WtdBrowserSessionError(
                "AI translation did not start (no content appeared — possibly a "
                "Cloudflare challenge or an exhausted AI quota)."
            ) from exc

        last_len, stable = -1, 0
        while time.monotonic() < deadline:
            try:
                length = page.eval_on_selector(content_selector, "el => el.innerText.length")
                body = page.inner_text("body")
            except Exception as exc:
                raise WtdBrowserSessionError(f"Lost the page while reading content: {exc}") from exc
            if length > 400 and "AI Loading" not in body:
                if length == last_len:
                    stable += 1
                    if stable >= 2:  # unchanged across two polls → done streaming
                        return
                else:
                    stable = 0
                last_len = length
            time.sleep(0.4)
        raise WtdBrowserSessionError(
            "AI translation did not finish in time (content kept streaming or was "
            "blocked). Try again."
        )


def _click_retranslate(page, button_text: str) -> None:
    """Click the 'Dịch lại' button, tolerant of it being a <button> or a link."""
    try:
        page.get_by_role("button", name=button_text).first.click(timeout=5_000)
        return
    except Exception:
        pass
    clicked = page.evaluate(
        "(label) => {"
        " const re = new RegExp(label, 'i');"
        " const b = [...document.querySelectorAll('button, a')].find(x => re.test(x.textContent));"
        " if (b) { b.click(); return true; } return false;"
        "}",
        button_text,
    )
    if not clicked:
        raise WtdBrowserSessionError(f"Could not find the '{button_text}' button.")


def _block_heavy_subresources(route) -> None:
    """Abort images/fonts/media/CSS; let documents, scripts and XHR through."""
    if route.request.resource_type in _BLOCKED_TYPES:
        route.abort()
    else:
        route.continue_()

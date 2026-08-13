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
from collections.abc import Callable
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

# How far down the ranked model list to fall before giving up on a chapter. Each
# rejected model costs a full content wait, and if three in a row refuse, the site
# is having a bad time and the next chapter's retry is the better move.
_MAX_MODELS_TRIED = 3


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

    def _goto(self, url: str):
        """Navigate and return the main navigation Response (or None)."""
        if self._page is None:
            self._launch()
        self._throttle()
        self._last_request_at = time.monotonic()
        try:
            return self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # closed window, crash, navigation timeout
            self.close()  # the session is dead; don't let the rest of the batch retry it
            raise WtdBrowserSessionError(f"Browser navigation failed: {exc}") from exc

    def get_html(
        self,
        url: str,
        *,
        prefer_document: bool = False,
        scroll_item_selector: str | None = None,
    ) -> str:
        """Navigate to `url` and return its HTML.

        `prefer_document` returns the **raw navigation response body** — the
        server-rendered HTML before JavaScript mutates the DOM. The webtruyendich
        chapter list is fully server-rendered but JS then *virtualizes* the live
        DOM down to ~135 rows; the raw document holds every chapter, so this reads
        the whole list instantly with no scrolling. Falls back to the DOM path if
        the document response is a Cloudflare interstitial.

        `scroll_item_selector` is the DOM-path fallback: scroll to the bottom until
        the matching-element count stops growing before reading `page.content()`.

        Raises WtdBrowserSessionError if the browser is gone or a Cloudflare
        interstitial is still up after giving the managed challenge time to clear.
        """
        response = self._goto(url)
        if prefer_document and response is not None:
            body = None
            try:
                body = response.text()
            except Exception:
                body = None  # body already consumed / navigation raced — use the DOM
            if body and not looks_like_challenge(body):
                return body
        markup = self._content_after_challenge(url)
        if scroll_item_selector:
            markup = self._scroll_until_stable(scroll_item_selector)
        return markup

    def _scroll_until_stable(
        self,
        item_selector: str,
        *,
        settle_ms: int = 600,
        stable_rounds: int = 3,
        max_rounds: int = 300,
    ) -> str:
        """Scroll to the bottom until `item_selector`'s count stops growing, then
        return the fully-rendered HTML. `max_rounds` bounds a list that never
        settles; `stable_rounds` guards against a slow append between scrolls."""
        page = self._page
        prev, stable = -1, 0
        try:
            for _ in range(max_rounds):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(settle_ms)
                count = page.eval_on_selector_all(item_selector, "els => els.length")
                if count == prev:
                    stable += 1
                    if stable >= stable_rounds:
                        break
                else:
                    stable = 0
                prev = count
            return page.content()
        except Exception as exc:
            self.close()
            raise WtdBrowserSessionError(f"Lost the page while loading the list: {exc}") from exc

    def _content_after_challenge(self, url: str, wait_seconds: float = 15.0) -> str:
        """Return the page HTML, first waiting out a managed Cloudflare challenge.

        A real browser sees "Just a moment…" for a beat, then JS redirects to the
        real page. Reading `content()` the instant navigation settles can catch that
        interstitial — or throw "page is navigating" mid-redirect — so poll,
        tolerating both, until the challenge clears (or the deadline passes)."""
        page = self._page
        deadline = time.monotonic() + wait_seconds
        last: str | None = None
        while time.monotonic() < deadline:
            try:
                last = page.content()
            except Exception:
                # Page is mid-navigation (Cloudflare redirect / client-side routing).
                # Not fatal — wait a beat and try again.
                time.sleep(0.5)
                continue
            if not looks_like_challenge(last):
                return last
            time.sleep(1.0)
        raise WtdBrowserSessionError(f"Cloudflare returned a challenge for {url}")

    def read_translated_chapter(
        self,
        url: str,
        *,
        translator_select: str,
        rank_translators: Callable[[list[str]], list[str]],
        is_usable_output: Callable[[str], bool],
        retranslate_button: str,
        content_selector: str,
        paragraph_selector: str,
    ) -> tuple[str, str]:
        """Navigate, drive the on-page AI, wait hands-off for the streamed paragraphs
        to stabilise, and return (content container innerHTML, model that produced it).

        Both callbacks keep site judgement out of this module: `rank_translators`
        turns the dropdown's live option values into models to try (best first) and
        `is_usable_output` says whether rendered text is a real translation rather
        than a provider error body. Anything they raise propagates untouched.

        Models are tried in order because one being *listed* does not mean it is
        *answering* — an overloaded model streams an error payload instead of a
        translation, and the next one down usually works. Switching models re-uses
        the loaded page; only a stall costs a reload.

        Never interacts with the Turnstile widget: it only observes whether the
        translated text arrives. If it doesn't within `content_timeout_ms`, raises
        WtdBrowserSessionError (the caller surfaces a clear message).

        A first-ever (un-cached) chapter is generated live and can occasionally
        stall on the first attempt; a plain reload + re-trigger clears it (measured
        — see changes/029). So one retry per model is built in before moving on.
        """
        self._goto(url)
        self._content_after_challenge(url)  # wait out a managed challenge; raises if it persists
        options = self._read_translator_options(self._page, translator_select)
        # Outside the try blocks below on purpose: "no usable model" is the caller's
        # error to phrase, and it must not be swallowed into a retry.
        candidates = rank_translators(options)

        last_exc: WtdBrowserSessionError | None = None
        for position, model in enumerate(candidates[:_MAX_MODELS_TRIED]):
            # The stall retry exists for the first-ever generation of a chapter, so
            # only the first model gets it; fallbacks get one shot each. That keeps
            # the worst case (site down, every generation timing out) at four
            # content waits per chapter instead of two per model on offer.
            for attempt in range(2 if position == 0 else 1):
                if position or attempt:
                    # Clear the container first: a previous model may have left >400
                    # chars behind, which the stability wait would read as this
                    # model's finished output.
                    self._reset_for_retry(url, content_selector, reload_page=bool(attempt))
                page = self._page
                try:
                    self._trigger_translation(page, translator_select, model, retranslate_button)
                    self._wait_for_stable_content(paragraph_selector, content_selector)
                    markup = page.eval_on_selector(content_selector, "el => el.innerHTML")
                    text = page.eval_on_selector(content_selector, "el => el.innerText")
                except WtdBrowserSessionError as exc:
                    last_exc = exc  # transient first-generation stall → reload and retry once
                    continue
                if is_usable_output(text):
                    return markup, model
                # A provider error is not transient within one model — retrying the
                # same one just burns another generation. Fall through to the next.
                last_exc = WtdBrowserSessionError(
                    f"{model} returned a provider error instead of a translation"
                )
                break
        raise last_exc or WtdBrowserSessionError("No AI model produced a translation")

    def _read_translator_options(self, page, translator_select: str) -> list[str]:
        try:
            page.wait_for_selector(translator_select, timeout=self.timeout_ms)
            return page.eval_on_selector_all(
                f"{translator_select} option", "els => els.map(el => el.value)"
            )
        except Exception as exc:
            raise WtdBrowserSessionError(f"Could not read the AI model list: {exc}") from exc

    def _reset_for_retry(self, url: str, content_selector: str, *, reload_page: bool) -> None:
        """Blank the content container, reloading first when a stall needs clearing."""
        if reload_page:
            self._goto(url)
            self._content_after_challenge(url)
        try:
            self._page.eval_on_selector(content_selector, "el => { el.innerHTML = ''; }")
        except Exception:
            pass  # container not there yet; the stability wait will do the complaining

    def _trigger_translation(
        self, page, translator_select: str, translator_value: str, retranslate_button: str
    ) -> None:
        """Pick the given AI model and click 'Dịch lại' to start generation."""
        try:
            page.wait_for_selector(translator_select, timeout=self.timeout_ms)
            # The option list was read from this same page, so an exact match resolves
            # immediately; the timeout stops a vanished option blocking for a minute.
            page.select_option(translator_select, translator_value, timeout=self.timeout_ms)
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

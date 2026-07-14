"""Auto-unlock medoctruyen.vn's daily cap by running its Discord `/mochuong` command.

When medoctruyen hits its 50-chapters/day limit it hands out a single-use code and
tells the reader to type `/mochuong <code>` in the `#mở-khoá` channel of its Discord.
This module automates that step so a download batch can resume unattended.

Design choices (see changes/004-DISCORD-AUTOUNLOCK):
  * Playwright drives a *dedicated, persistent* Chromium profile — separate from the
    user's everyday browser — that a throwaway Discord account is logged into once.
    The login persists in `profile_dir()` across runs; we never touch a user token.
  * `/mochuong` is a real Discord slash command, so we type the name, wait for the
    command picker, press Enter to insert the command, type the code argument, then
    Enter to submit — mirroring a human typing in the official web client.

Playwright is an optional dependency (`pip install 'noveltrans[discord]'` then
`playwright install chromium`); it is imported lazily so the core app runs without it.

Note: automating a Discord *user account* is against Discord's ToS regardless of how
gently it's done — hence the throwaway-account guidance. Keep usage low-frequency.
"""

from __future__ import annotations

import re
from pathlib import Path

from noveltrans.storage.library import DEFAULT_LIBRARY_DIR

# The composer, the command picker option, and the "still logged out" markers. Kept
# here (not inline) so they're easy to retune if Discord's web DOM drifts.
_COMPOSER_SEL = 'div[role="textbox"][contenteditable="true"]'
_PICKER_OPTION_SEL = '[role="listbox"] [role="option"]'
_LOGGED_OUT_URL_RE = re.compile(r"/(login|register)\b")
_LOGGED_IN_URL_RE = re.compile(r"/channels/")
_CHANNEL_URL_RE = re.compile(r"^https://discord\.com/channels/(\d+|@me)/(\d+)/?$")

_PICKER_WAIT_MS = 5_000  # how long to wait for the slash-command popup to render
_TYPE_DELAY_MS = 40  # per-keystroke delay so Discord's editor keeps up


class DiscordUnlockError(Exception):
    """Auto-unlock could not be completed.

    `needs_login` marks the recoverable case where the dedicated profile has no
    valid Discord session yet — the fix is to run the one-time login, not retry.
    """

    def __init__(self, message: str, *, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def profile_dir() -> Path:
    """Dedicated Chromium profile holding the throwaway account's Discord login.

    Lives inside the library data dir (hidden) so it travels with the user's data
    and stays out of their normal browser profiles.
    """
    return DEFAULT_LIBRARY_DIR / ".discord-profile"


def valid_channel_url(url: str) -> bool:
    """True for a well-formed Discord channel link (Copy Link on #mở-khoá)."""
    return bool(_CHANNEL_URL_RE.match((url or "").strip()))


def _require_playwright():
    """Import Playwright's sync API, or raise a message that says how to install it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # optional dependency not installed
        raise DiscordUnlockError(
            "Chưa cài Playwright cho tính năng tự mở khoá. Chạy:\n"
            "  pip install 'noveltrans[discord]'\n"
            "  playwright install chromium"
        ) from exc
    return sync_playwright


def _launch_context(sync_playwright, *, headless: bool):
    """Open the persistent, dedicated-profile Chromium context."""
    profile_dir().mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir()),
        headless=headless,
        args=["--no-first-run", "--no-default-browser-check"],
    )
    return playwright, context


def open_login(timeout_ms: int = 300_000) -> None:
    """Open a visible Discord window so the throwaway account can log in once.

    Blocks (run me in a worker thread) until Discord finishes loading the logged-in
    app — either immediately (session already valid) or after the user signs in —
    then closes. The session is saved in the persistent profile for later unlocks.
    """
    sync_playwright = _require_playwright()
    playwright, context = _launch_context(sync_playwright, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://discord.com/login", wait_until="domcontentloaded")
        page.wait_for_url(_LOGGED_IN_URL_RE, timeout=timeout_ms)
    except Exception as exc:  # window closed early, timeout, etc.
        raise DiscordUnlockError(f"Đăng nhập Discord chưa hoàn tất: {exc}") from exc
    finally:
        _close(context, playwright)


def run_unlock(
    channel_url: str,
    code: str,
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
) -> None:
    """Run `/mochuong <code>` in the given Discord channel. Blocks; raise on failure.

    Raises DiscordUnlockError(needs_login=True) if the dedicated profile isn't
    logged in, so the caller can prompt the one-time login instead of retrying.
    """
    channel_url = (channel_url or "").strip()
    code = (code or "").strip()
    if not valid_channel_url(channel_url):
        raise DiscordUnlockError(
            "Link kênh Discord không hợp lệ. Vào Settings, chuột phải kênh #mở-khoá "
            "→ Copy Link (dạng https://discord.com/channels/…/…)."
        )
    if not code:
        raise DiscordUnlockError("Không tìm thấy mã mở khoá trên trang giới hạn.")

    sync_playwright = _require_playwright()
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    playwright, context = _launch_context(sync_playwright, headless=headless)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(channel_url, wait_until="domcontentloaded")

        # Discord bounces an unauthenticated profile to /login|/register.
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass
        if _LOGGED_OUT_URL_RE.search(page.url):
            raise DiscordUnlockError(
                "Profile Discord chưa đăng nhập. Vào Settings → “Đăng nhập Discord” "
                "để đăng nhập tài khoản phụ một lần.",
                needs_login=True,
            )

        composer = page.locator(_COMPOSER_SEL).last
        try:
            composer.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise DiscordUnlockError(
                "Không mở được ô nhập tin nhắn của kênh — kiểm tra lại link kênh và "
                "quyền của tài khoản phụ trong server."
            ) from exc
        composer.click()

        # Slash command: type the name, let the picker render, Enter to insert the
        # command pill (focus moves to its argument), type the code, Enter to send.
        page.keyboard.type("/mochuong", delay=_TYPE_DELAY_MS)
        try:
            page.wait_for_selector(_PICKER_OPTION_SEL, timeout=_PICKER_WAIT_MS)
        except PlaywrightTimeoutError:
            pass  # some builds auto-highlight; Enter still selects the match
        page.keyboard.press("Enter")
        page.keyboard.type(code, delay=_TYPE_DELAY_MS)
        page.keyboard.press("Enter")

        # A sent interaction empties the composer; that's our success signal (waiting
        # on a specific bot reply text is brittle across server configs).
        page.wait_for_timeout(1_500)
    finally:
        _close(context, playwright)


def _close(context, playwright) -> None:
    for close in (context.close, playwright.stop):
        try:
            close()
        except Exception:
            pass

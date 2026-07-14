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

_COMMAND = "mochuong"  # the site's unlock slash command, without the leading "/"

# Real Chrome first, Playwright's bundled Chromium (channel=None) as the fallback.
_BROWSER_CHANNELS = ("chrome", None)
_LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    # Discord's login gates react badly to navigator.webdriver; this and dropping
    # --enable-automation keep the window looking like an ordinary Chrome.
    "--disable-blink-features=AutomationControlled",
]

_PICKER_WAIT_MS = 5_000  # how long to wait for the slash-command popup to render
_TYPE_DELAY_MS = 40  # per-keystroke delay so Discord's editor keeps up
_SEND_WAIT_MS = 8_000  # how long to wait for the composer to clear after Enter
_SEND_POLL_MS = 250
# After the command is confirmed sent, keep the window open this long so the bot can
# receive and process the interaction before we tear the browser down.
_POST_SEND_WAIT_MS = 12_000


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
    """Open the dedicated profile in the user's real Chrome (bundled Chromium if not).

    Discord's login flow (captcha, new-device checks) routinely refuses Playwright's
    bundled Chromium — it's a different build with a headless-ish fingerprint — so we
    drive the *installed* Chrome instead. Same dedicated profile either way: this is
    still a throwaway account in its own user-data-dir, never the user's own browser
    session. Falls back to bundled Chromium where Chrome isn't installed.
    """
    profile_dir().mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    last_exc: Exception | None = None
    for channel in _BROWSER_CHANNELS:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir()),
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
    raise DiscordUnlockError(
        "Không mở được trình duyệt cho tính năng tự mở khoá. Cài Google Chrome, hoặc "
        "chạy:  playwright install chromium"
    ) from last_exc


def _command_offered(page) -> bool:
    """True if Discord's slash-command picker is actually offering `/mochuong`.

    Guards the send: with no picker match, Enter posts the typed text as an ordinary
    message instead of running the command.
    """
    return any(
        _COMMAND in text.lower() for text in page.locator(_PICKER_OPTION_SEL).all_inner_texts()
    )


def _wait_until_sent(page, code: str) -> bool:
    """Poll the composer until the command leaves it — Discord clears it on send.

    False means the command is still sitting in the composer after `_SEND_WAIT_MS`,
    i.e. it never went out (checking for a bot reply instead would be brittle across
    server configs, but "the composer emptied" is the same signal a human reads).
    """
    needles = (_COMMAND, code.lower())
    waited = 0
    while True:
        text = page.locator(_COMPOSER_SEL).last.inner_text().lower()
        if not any(needle in text for needle in needles):
            return True
        if waited >= _SEND_WAIT_MS:
            return False
        page.wait_for_timeout(_SEND_POLL_MS)
        waited += _SEND_POLL_MS


def _clear_composer(page) -> None:
    """Best-effort wipe of a half-typed command.

    Discord persists per-channel drafts, so leaving `/mochuong` behind would corrupt
    the next run's typing. Never raises — it runs on paths that are already failing.
    """
    try:
        page.locator(_COMPOSER_SEL).last.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
    except Exception:
        pass


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
    headless: bool = False,
    timeout_ms: int = 30_000,
) -> None:
    """Run `/mochuong <code>` in the given Discord channel. Blocks; raise on failure.

    Returning normally means the command was really submitted: the picker offered it
    and the composer emptied. Anything less raises, so callers never resume a download
    on the strength of a command that silently went nowhere.

    Raises DiscordUnlockError(needs_login=True) if the dedicated profile isn't
    logged in, so the caller can prompt the one-time login instead of retrying.

    Runs headed by default: Discord's login gate challenges this profile hard, and a
    headless session (HeadlessChrome UA, different fingerprint) invites a mid-run
    re-challenge that would read as a failure. The window is brief and the UI already
    tells the user to keep it open.
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
        # Discord persists a per-channel draft: wipe anything left from a prior run so
        # the new command can't be prepended to stale text (which would send both).
        _clear_composer(page)

        try:
            # Type the name and make sure Discord really offers the command before
            # committing: if the picker never matches (bot gone from the server, wrong
            # channel, no permission), Enter would post "/mochuong" and then the bare
            # code as two plain messages. Bail out first — the channel stays clean.
            page.keyboard.type(f"/{_COMMAND}", delay=_TYPE_DELAY_MS)
            try:
                page.wait_for_selector(_PICKER_OPTION_SEL, timeout=_PICKER_WAIT_MS)
                offered = _command_offered(page)
            except PlaywrightTimeoutError:
                offered = False
            if not offered:
                raise DiscordUnlockError(
                    "Discord không gợi ý lệnh /mochuong trong kênh này (chưa gửi gì "
                    "cả). Kiểm tra link kênh #mở-khoá và xem tài khoản phụ đã vào "
                    "server, thấy được bot chưa."
                )

            # Enter inserts the command pill (focus moves to its argument), type the
            # code, Enter submits the interaction.
            page.keyboard.press("Enter")
            page.keyboard.type(code, delay=_TYPE_DELAY_MS)
            page.keyboard.press("Enter")

            if not _wait_until_sent(page, code):
                raise DiscordUnlockError(
                    "Đã gõ /mochuong nhưng Discord không gửi lệnh đi (lệnh vẫn nằm "
                    "trong ô nhập). Thử mở khoá thủ công."
                )

            # Command is out — hold the window open so the bot can process it before
            # we close the browser (closing instantly can drop the interaction).
            page.wait_for_timeout(_POST_SEND_WAIT_MS)
        except DiscordUnlockError:
            _clear_composer(page)  # don't leave a draft to poison the next run
            raise
    finally:
        _close(context, playwright)


def _close(context, playwright) -> None:
    for close in (context.close, playwright.stop):
        try:
            close()
        except Exception:
            pass

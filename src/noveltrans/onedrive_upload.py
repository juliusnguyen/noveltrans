"""Push a whole novel project up to OneDrive by driving the OneDrive web UI.

Everything a novel produces already lives in one folder — `meta.json`, `chapters.db`,
and `exports/` with the audio, the rendered part-videos and each part's sidecars. This
module is the last mile: it mirrors that folder into `/NovelTrans/<tên truyện>/` on the
user's OneDrive so the work has a copy somewhere other than one laptop.

Design choices (see changes/051-ONEDRIVE-UPLOAD):

  * **Browser automation, not Microsoft Graph.** This was asked for explicitly, over the
    alternative. It is worth being honest about the cost in the one place someone will
    read it: Graph would give per-chunk resume (a dropped connection costs 10 MB, not a
    3 GB file), no selectors to drift, and a supported API rather than a tolerated one.
    What we get instead is no app registration and no OAuth client id baked into the
    build. The design compensates where it can — every step checks its own
    postcondition, the folder listing is the authority on "did it land", and
    `scripts/diagnose_onedrive.py` exists so selectors are observed rather than guessed.
    What it cannot compensate for is a redesign of the OneDrive web app.
  * **Its own profile**, `~/NovelTrans/.onedrive-profile`. Chromium refuses a second
    instance on one `user-data-dir`, so sharing the YouTube profile would make an
    OneDrive push and a YouTube upload mutually exclusive at the OS level — and a
    Microsoft session has no business sitting in a Google one either.
  * **One browser for the whole run.** Same reasoning as `youtube_upload.upload_batch`:
    the launch costs seconds, and a login-upload-quit cycle per file looks exactly like
    what it is.
  * **The generic helpers below are re-implemented, not imported from
    `youtube_upload`.** Only `_first_present` is genuinely service-agnostic; that
    module's `_click_by_text` is hard-wired to `ytcp-button` / `tp-yt-paper-button` and
    would match nothing here, and its `_dom_inventory` counts Studio's element families.
    Importing them would create a false dependency between two DOM modules and tempt the
    next person to "fix" one for the other. `_first_present` is the one candidate for a
    future hoist into `browser.py`, if a third service ever needs it.

**The safety calculus here is the opposite of YouTube's, and that is deliberate.** There,
`read_upload_state` reads a corrupt file as `unknown` and refuses to touch the part,
because guessing wrong publishes an episode twice to an audience. Here, re-uploading
replaces a file in the user's own private folder: the cost of guessing wrong is
bandwidth, and the cost of the *other* mistake is a file they believe is backed up and
isn't. So when in doubt, this module uploads again. `read_manifest` is where that rule is
enforced, and the block comment above it is worth reading before changing either module.

Note: automating the OneDrive web UI sits in the same grey zone as `discord_unlock` does
for Discord and `youtube_upload` does for YouTube. The target is the user's own storage,
which lowers the stakes but does not change the rule — keep runs human-paced.

Playwright is an optional dependency (`pip install 'noveltrans[browser]'` then
`playwright install chromium`); it is imported lazily so the core app runs without it.
"""

from __future__ import annotations

import json
import posixpath
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from noveltrans.browser import BrowserUnavailableError
from noveltrans.browser import close as _close
from noveltrans.browser import launch_persistent_context, require_playwright
from noveltrans.storage.library import DEFAULT_LIBRARY_DIR
from noveltrans.storage.project import DB_FILE, slugify

# -- where OneDrive lives -----------------------------------------------------
#
# OneDrive comes in two flavours and they do NOT share a URL shape:
#
#   personal  onedrive.live.com/?id=root&cid=<CID>
#   business  <tenant>-my.sharepoint.com/personal/<user>/_layouts/15/onedrive.aspx?id=…
#
# We open the flavour-neutral entry points and let the redirect decide which one this
# account is. `flavour()` then reads the answer off the landed URL — the only honest
# source, since nothing we know beforehand tells us which kind of account was signed in.

# MEASURED 2026-08-06 against a signed-in personal account. The obvious guess —
# `https://onedrive.live.com/` — is WRONG and was wrong from the start: the bare root
# redirects to the product marketing page **whether or not you are signed in**. It is not
# a file list, it never becomes one, and reading "signed out" from landing there is a
# mistake this module made until a real account proved otherwise.
#
#   onedrive.live.com/my        → the file list. 1.2 s. Title "Tệp của tôi - OneDrive".
#   onedrive.live.com/?id=root  → redirects to /my. Also fine.
#   onedrive.live.com/          → the brochure, signed in or not. Useless.
_MY_FILES_URL = "https://onedrive.live.com/my"
_ROOT_URLS = (
    _MY_FILES_URL,  # personal
    "https://www.office.com/onedrive",  # business; → <tenant>-my.sharepoint.com
)

# Signing in is a DIFFERENT URL from opening the file list, which is not obvious and cost a
# failed login to find out. MEASURED 2026-08-06, signed out, of four candidates:
#
#   onedrive.live.com/            → the product marketing page. No form.
#   onedrive.live.com/login/      → loads, shows no form.
#   login.live.com/               → loads, shows no form.
#   www.office.com/onedrive       → login.microsoftonline.com/common/oauth2/v2.0/authorize,
#                                   with a real email box. ← the only one that works
#
# `common` accepts personal Microsoft accounts as well as work/school ones, so this is the
# entry point for both flavours.
_LOGIN_URL = "https://www.office.com/onedrive"

# Microsoft bounces a signed-out profile to one of two login hosts (consumer vs work
# account), sometimes via a `/signin` hop on the way.
# MEASURED 2026-08-06: `office.com/onedrive` signed out lands on
# `login.microsoftonline.com/common/oauth2/v2.0/authorize?...`, which this matches.
_LOGGED_OUT_URL_RE = re.compile(
    r"login\.live\.com|login\.microsoftonline\.com|/signin|/login", re.IGNORECASE
)
# MEASURED 2026-08-06, and it was NOT what this module first assumed: a signed-out visit
# to `onedrive.live.com` does not go to a login page at all. It lands on the *product
# marketing site* — `www.microsoft.com/en/microsoft-365/onedrive/online-cloud-storage` —
# which carries no sign-in form for the DOM check to find and matches no login host.
# Without naming it, "you are signed out" reads as "OneDrive would not open", which sends
# the user looking for a broken selector instead of at the sign-in button.
_ANONYMOUS_URL_RE = re.compile(
    r"microsoft\.com/[^?#]*onedrive|/onedrive/online-cloud-storage", re.IGNORECASE
)
# Deliberately permissive: it only has to prove we are on a OneDrive surface, and
# `_LOGGED_OUT_URL_RE` is always checked FIRST. A tight pattern here would be a guess
# about a URL shape we have not observed, and guessing wrong means `open_login` waits
# five minutes on a page the user already signed into.
_LOGGED_IN_URL_RE = re.compile(
    r"onedrive\.live\.com|1drv\.ms|-my\.sharepoint\.com/personal/|"
    r"sharepoint\.com/.*onedrive\.aspx",
    re.IGNORECASE,
)

_PERSONAL_URL_RE = re.compile(r"onedrive\.live\.com|1drv\.ms", re.IGNORECASE)
_BUSINESS_URL_RE = re.compile(r"-my\.sharepoint\.com|sharepoint\.com", re.IGNORECASE)

FLAVOUR_PERSONAL = "personal"
FLAVOUR_BUSINESS = "business"

# -- sign-in surface (UNVERIFIED) ---------------------------------------------
#
# NONE of these have been verified against a live account — step 2 of the plan is a
# diagnose script whose whole job is to replace these with measured values, and the
# comments get updated to say MEASURED when that happens. Until then they are guesses,
# and they are written so that a miss is *visible*: `_account_name` returning "" degrades
# a confirmation message, it never lets a run proceed on a wrong assumption.
#
# Two rules, borrowed from the Studio block in `youtube_upload`:
#   1. Prefer ODSP's `data-automationid` hooks. They are wired to the React components,
#      survive redesigns better than class names, and are the SAME in every UI language.
#   2. Where only text will do, match Vietnamese *and* English — we do not control the
#      account's language and cannot assume it matches the app's.
# MEASURED 2026-08-06: the account is named on the navigation-pane toggle, whose
# aria-label is "<tên>, Ngăn dẫn hướng" (or "…, Navigation Pane" in English) — NOT on any
# of the `meControl*` hooks this first guessed, none of which exist. The trailing role
# word is stripped by `_account_name`.
_SIGNED_IN_ACCOUNT_SELS = (
    "[aria-label*='Ngăn dẫn hướng']",
    "[aria-label*='Navigation Pane']",
    "[data-automationid='meControlUserName']",
    "[aria-label*='Account manager']",
)
# What to cut off the end of that label to leave just the name.
_ACCOUNT_LABEL_SUFFIXES = (", Ngăn dẫn hướng", ", Navigation Pane")
# The sign-in form itself, as a DOM fallback for `_is_logged_out`. The URL check is the
# primary signal; this catches a login form rendered at a URL we did not anticipate.
# MEASURED 2026-08-06: all three match the real form at
# login.microsoftonline.com/common/oauth2/v2.0/authorize.
_SIGNIN_FORM_SELS = (
    "input[type='email'][name='loginfmt']",
    "#i0116",
    "form[name='f1']",
)

# -- the file-list surface (MEASURED 2026-08-06) ------------------------------
#
# Retuned against a live signed-in personal account. Almost every guess in the first
# version was wrong, and the diagnose script is what replaced them — the 044 lesson,
# applied before rather than after four failed runs.
#
# The one structural rule that survived: **prefer ODSP's `data-automationid`.** They are
# wired to the React components and are identical in every UI language, which matters
# here because the account under test renders in Vietnamese. Text ladders are a fallback
# only, and carry both languages.
#
# The failure mode this block guards against: a step that quietly does nothing produces a
# manifest recording files as uploaded when they are not. Every step therefore verifies
# its own postcondition rather than trusting a click.

# Everything hangs off one command-bar button, labelled "Create or upload". MEASURED: the
# guessed `button[name='New']` / `newCommand` / `#NewMenu` match nothing.
_NEW_MENU_SELS = ("[data-automationid='AddNew']",)
_NEW_MENU_TEXTS = ("Tạo hoặc tải lên", "Create or upload", "Mới", "New")

# Items inside that menu, all MEASURED from its own `data-automationid`s:
#   CreateFolderCommand  "Thư mục"       → the folder we create
#   uploadFile           "Tải tệp lên"   → the upload entry we use
#   uploadFolder         "Tải lên thư mục" → the one we must NEVER touch
_NEW_FOLDER_SELS = ("[data-automationid='CreateFolderCommand']",)
_NEW_FOLDER_TEXTS = ("Thư mục", "Folder")
_FOLDER_NAME_INPUT_SELS = (
    "[role='dialog'] input[type='text']",
    ".ms-Dialog input[type='text']",
    "input[aria-label='Tên']",
    "input[aria-label='Name']",
)
# **"Tạo" is a substring of "Tạo hoặc tải lên"**, the command-bar button. `has-text` is a
# substring match, so the naive ladder found that button — sitting behind the modal — and
# Playwright then waited forever for an overlay-blocked element to become clickable. The
# run hung with the dialog open and the name already typed.
#
# Two independent guards, because either alone is thin: scope to the dialog, and match the
# label EXACTLY. The dialog's primary button is the fallback for a relabelled Create.
# MEASURED — the create-folder dialog's real shape:
#
#   button.ms-Button < span.ms-Dialog-action < div.ms-Dialog-actionsRight
#     < div.ms-Dialog-actions < div.ms-Dialog-inner < div.ms-Dialog--close
#     < div.ms-Modal-scrollableContent
#
# Two traps in that one line. There is **no `[role=dialog]`** anywhere. And `.ms-Dialog`
# does not match either: the element's class token is `ms-Dialog--close`, and a CSS class
# selector matches whole tokens, not prefixes — so the obvious scope silently matches
# nothing. `[class*='ms-Dialog']` is a substring match on the attribute and survives
# whatever modifier suffix a future build adds.
#
# Kept as separate roots, NOT one comma-joined string: interpolating a comma list into
# `f"{roots} button:text-is(…)"` yields `rootA, rootB button:text-is(…)`, whose first
# branch matches a whole dialog and would click that instead of the button.
_DIALOG_ROOTS = (
    ".ms-Dialog-actions",  # the action bar that holds Tạo / Hủy bỏ
    "[class*='ms-Dialog']",
    "[class*='ms-Modal']",
    "[role='dialog']",  # not present today; harmless, and free if a rebuild adds it
)
_CREATE_TEXTS = ("Tạo", "Create")
# Positional fallback for a relabelled Create: the confirming action is the first button
# in the right-hand action group.
_CREATE_BUTTON_SELS = (
    ".ms-Dialog-actionsRight button",
    "[class*='ms-Dialog'] button[type='submit']",
)

# **There is no `input[type=file]` on the page** — MEASURED: zero, even with the menu
# open. OneDrive creates one only when "Tải tệp lên" is clicked, which is why the
# file-chooser interception is the PRIMARY path here rather than the fallback it is in
# `youtube_upload`. Confirmed live: Playwright intercepts the chooser and it reports
# `is_multiple() == True`, so a whole batch goes in one call.
_UPLOAD_FILE_SELS = ("[data-automationid='uploadFile']",)
_UPLOAD_FILE_TEXTS = ("Tải tệp lên", "Upload files", "Upload file")
# Named ONLY so it can be avoided. Handing our paths to the folder uploader looks like it
# works: Playwright does not populate `webkitRelativePath`, so every file lands flat in
# whichever folder is open and the tree is silently lost — and the manifest would then
# record the flattening as a success.
_UPLOAD_FOLDER_SEL = "[data-automationid='uploadFolder']"
_UPLOAD_DIRECTORY_INPUT_SEL = "input[type='file'][webkitdirectory]"
# Kept for the lazily-created input, on the chance a later build ships a static one.
_UPLOAD_INPUT_SELS = (
    "input[type='file'][multiple]:not([webkitdirectory])",
    "input[type='file']:not([webkitdirectory])",
)

# **There is no breadcrumb.** MEASURED: `[data-automationid='breadcrumb']`, `.ms-Breadcrumb`
# and every `nav[aria-label*=breadcrumb]` shape match nothing at all. What OneDrive does
# expose is the current folder's name, three independent ways — used in this order:
#
#   1. `document.title` → "Documents - OneDrive" / "Tệp của tôi - OneDrive". Simplest and
#      the most stable thing on the page.
#   2. `appListHeader` → "Tệp của tôi\nDocuments"; the LAST line is the current folder.
#      (`headerTitleButton` exists at the root and DISAPPEARS inside a folder, so it
#      cannot be the primary signal.)
#   3. the URL's `id=` parameter → `/personal/<cid>/Documents/Documents`.
_PAGE_TITLE_SUFFIX = " - OneDrive"
_LIST_HEADER_SEL = "[data-automationid='appListHeader']"
_FOLDER_ID_RE = re.compile(r"[?&]id=([^&]+)")

_ROW_SEL = "[role='row'], [data-automationid='DetailsRowFields']"
# MEASURED: this is the name cell. The guessed `[data-automationid='name'] button` matched
# 86 hidden elements and the fallback `[role='row']` yielded the *sharing* column, which
# is why the first live listing read "Private, Private, Private, Shared".
_ROW_NAME_SEL = "[data-automationid='field-LinkFilename']"
# Row 0 is the column header, not a file. MEASURED: the first `field-LinkFilename` reads
# "Name". Counting it as an item would have us "find" a folder called Name and, worse,
# treat the header as a delivered file during batch verification.
_COLUMN_HEADER_NAMES = ("name", "tên", "modified", "đã sửa đổi", "file size", "kích cỡ")
# MEASURED: the size column is what tells a folder from a file. A folder reads "30 mục"
# ("30 items"); a file reads a byte size like "1,2 MB". There is no folder icon, aria-role
# or class that says so any more plainly — the icon is an unlabelled `<div>` with hashed
# class names, which is exactly the kind of thing that breaks on the next redesign.
_ROW_SIZE_SEL = "[data-automationid='field-FileSizeDisplay']"

# Sorting the folder newest-first, which is what makes verification possible at all.
# MEASURED: OneDrive virtualises the grid — `exports/audio` holds 2684 files and renders
# **60 rows**. Waiting for a batch to appear in the listing therefore never finished, and
# a real 2752-file run sat on its first batch until the stall timeout.
#
# A batch is at most `MAX_BATCH_FILES` (20), so with the newest at the top everything just
# uploaded is comfortably inside the rendered rows. MEASURED menu ids:
#   sortCommand → sortDocIcon | sortLinkFilename | sortModified | sortFileSizeDisplay
#                 sortAsc | sortDesc     (each carries aria-checked)
_SORT_MENU_SEL = "[data-automationid='sortCommand']"
_SORT_MENU_TEXTS = ("Sắp xếp", "Sort")
_SORT_MODIFIED_SEL = "[data-automationid='sortModified']"
_SORT_DESC_SEL = "[data-automationid='sortDesc']"
_ITEM_COUNT_SIZE_RE = re.compile(r"^\d+\s*(mục|tệp|items?|files?)$", re.IGNORECASE)

# Progress. Advisory ONLY: it feeds the status line the user reads and the stall detector,
# and it never decides that a batch finished — the folder listing does that. This is the
# deliberate inverse of `youtube_upload.transfer_state`, where Studio's progress line was
# the only signal available. Here there is a better one, so the parsed text is demoted.
_UPLOAD_STATUS_SEL = (
    "[data-automationid='uploadStatusBar'], .od-ProgressIndicator, [role='status']"
)
# Read by `upload_status()`. "còn lại"/"remaining" counts as *still going*: a time
# estimate is something only an in-flight transfer has.
_UPLOADING_RE = re.compile(r"đang tải lên|uploading|còn lại|remaining", re.IGNORECASE)
_UPLOAD_DONE_RE = re.compile(
    r"đã tải lên|tải lên xong|tải lên hoàn tất|hoàn tất|đã xong|"
    r"uploaded|upload complete|completed|complete",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"hết dung lượng|không đủ dung lượng|đầy bộ nhớ|"
    r"storage is full|out of storage|not enough storage|quota",
    re.IGNORECASE,
)
_ITEM_COUNT_RE = re.compile(r"(\d+)\s*(mục|tệp|tập tin|items?|files?)", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d+)\s*%")

# A name collision. MEASURED, and it is **not a modal dialog** — it is a toast:
#
#   "Đã tồn tại tệp có tên này nên chúng tôi không thể tải lên notes.txt. Thêm dưới dạng
#    phiên bản mới của tệp hiện có hoặc giữ cả hai tệp."   [Thay thế] [Giữ cả hai]
#
# `[class*='ms-Dialog']`, `[class*='ms-Modal']` and `[role=dialog]` all match ZERO while
# it is showing. The first version gated `_resolve_conflicts` on finding such a container
# before it would even look for the button, so the prompt was never answered — OneDrive
# then did nothing, and the run reported the file as uploaded. **Every re-upload of an
# existing file silently kept the old bytes.**
#
# So there is no container gate: we look for the button itself. Default answer is
# Replace — we are mirroring a local tree, so the local copy is the intended truth.
# "Keep both" would leave `phan-1 1.mp4` beside `phan-1.mp4`, which is worse than either
# outcome and invisible until someone opens the folder.
_CONFLICT_TEXT_RE = re.compile(
    r"đã tồn tại tệp|không thể tải lên|already exists|couldn.t upload", re.IGNORECASE
)
# MEASURED, and the labels DIFFER by batch size:
#   one file   → "Thay thế"          / "Giữ cả hai"
#   many files → "Thay thế tất cả"   / "Giữ tất cả"
# The substring match covers both from the shorter form, which is why these stay short.
_REPLACE_TEXTS = ("Thay thế", "Replace")
# Matched so we can refuse it, never so we can click it.
_KEEP_BOTH_TEXTS = ("Giữ cả hai", "Giữ tất cả", "Keep both", "Keep all")
# How long a detected collision prompt gets to produce its button before we give up. The
# toast lands ~2.5 s after the transfer starts; being impatient here failed every batch of
# a 2752-file run.
_CONFLICT_WAIT_MS = 20_000
_CONFLICT_POLL_MS = 1_500

# How long to let a navigation settle before reading the URL. OneDrive redirects through
# a couple of hops (office.com → sharepoint, live.com → the CID form), and reading too
# early lands on an intermediate URL that matches neither pattern.
_NAV_SETTLE_MS = 3_000
# Generous, because it covers a cold profile's first load and a redirect chain. Paired
# with `wait_until="commit"` it is rarely approached — see `_open_root`.
_NAV_TIMEOUT_MS = 60_000
# How often `_wait_for_signin` looks. A human typing an email, a password and possibly a
# 2FA code is not in a hurry; polling faster would only burn CPU for five minutes.
_SIGNIN_POLL_MS = 1_500
_STEP_WAIT_MS = 30_000


class OneDriveUploadError(Exception):
    """A push could not be completed.

    `needs_login` marks the recoverable case where the dedicated profile has no valid
    Microsoft session — the fix is the one-time sign-in, not a retry. `relpath` names the
    file the run died on, so the report says which one instead of leaving the user to
    diff a three-thousand-file tree by hand.

    `fatal` means every remaining batch would fail for the same reason, so `push_project`
    stops instead of marking file after file as failed. Only two things are fatal: no
    session (implied by `needs_login`) and an exhausted quota. Everything else — a drifted
    selector, a stalled transfer, a folder that would not open — is per-batch, because the
    next batch may well be fine and half a mirror beats none.
    """

    def __init__(
        self,
        message: str,
        *,
        needs_login: bool = False,
        relpath: str = "",
        fatal: bool = False,
    ):
        super().__init__(message)
        self.needs_login = needs_login
        self.relpath = relpath
        self.fatal = fatal or needs_login


class OneDriveCancelled(Exception):
    """The user cancelled the run. Carries how many files had already gone up.

    Deliberately *not* named `UploadCancelled`: `workers.py` reaches into both this
    module and `youtube_upload`, and two identically-named exception classes in one file
    is a shadowing bug waiting to be written.
    """

    def __init__(self, message: str = "Đã huỷ tải lên OneDrive.", *, uploaded: int = 0):
        super().__init__(message)
        self.uploaded = uploaded


def profile_dir() -> Path:
    """Dedicated browser profile holding the Microsoft login.

    Lives inside the library data dir (hidden) so it travels with the user's data and
    stays out of their normal browser profiles. Separate from `.youtube-profile` for two
    reasons that both matter: Chromium will not open one `user-data-dir` twice, so a
    shared profile would serialise the two features against each other; and this holds a
    session for a different account entirely.
    """
    return DEFAULT_LIBRARY_DIR / ".onedrive-profile"


def flavour(url: str) -> str:
    """Which OneDrive this URL belongs to: "personal", "business", or "" if neither.

    Pure, so it is tested. Two things genuinely differ between the flavours and only
    two — where the root lands, and whether a path-shaped deep link resolves (business:
    yes; personal addresses items by opaque `cid`/`id` and a path link lands on "item not
    found"). Everything else is the same ODSP app, so this is consulted for those two
    questions and nothing else.

    A login URL is neither flavour: we are not on OneDrive yet, and answering "personal"
    for `login.live.com` would send the caller down a navigation path from a page that
    has no file list on it.
    """
    url = url or ""
    if _LOGGED_OUT_URL_RE.search(url):
        return ""
    # Business first: a tenant host can contain "onedrive" in the path
    # (`…/_layouts/15/onedrive.aspx`), so testing personal first would claim it.
    if _BUSINESS_URL_RE.search(url):
        return FLAVOUR_BUSINESS
    if _PERSONAL_URL_RE.search(url):
        return FLAVOUR_PERSONAL
    return ""


# -- generic page helpers -----------------------------------------------------
#
# Local copies rather than imports; see the module docstring for why.


def _first_present(page, selectors, *, timeout_ms: int = _STEP_WAIT_MS, state: str = "visible"):
    """Wait for the first element matching any of `selectors`. None if none appears.

    Accepts a tuple of selectors or one comma-joined string, because the constants above
    use both shapes — a tuple where the fallbacks are worth commenting individually, a
    joined string where they are not. Every constant lists fallbacks for exactly this
    reason: when an A/B variant renames one hook, another still matches and the run
    survives.

    A miss is a miss however it presents: Playwright's `TimeoutError` and a detached or
    otherwise unhappy locator are both "this selector didn't work, try the next one".
    Catching them together is also what keeps this callable without Playwright installed,
    which is what lets the fake-page tests run in a bare environment.
    """
    if isinstance(selectors, str):
        selectors = (selectors,)
    # Split the budget so a list of five dead selectors costs one timeout, not five.
    each = max(timeout_ms // max(len(selectors), 1), 1_000)
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state=state, timeout=each)
        except Exception:
            continue
        return locator
    return None


def _click_by_text(page, texts, *, timeout_ms: int = _STEP_WAIT_MS) -> bool:
    """Click the first button whose label matches any of `texts`. False if none matched.

    Only for controls with no stable hook. ODSP renders commands as plain `<button>` and
    as `[role=button]` menu items depending on where they sit, so both are tried; the
    Studio custom elements this mirrors do not exist on this page.
    """
    each = max(timeout_ms // max(len(texts), 1), 1_000)
    for text in texts:
        selector = (
            f'button:has-text("{text}"), [role="menuitem"]:has-text("{text}"), '
            f'[role="button"]:has-text("{text}")'
        )
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=each):
                # An explicit click timeout matters as much as the selector: an element
                # that is visible but covered by a modal keeps Playwright retrying for
                # its 30-second default, which reads to the user as a hang.
                locator.click(timeout=each)
                return True
        except Exception:
            continue
    return False


def _click_in_dialog(page, texts, *, timeout_ms: int = _STEP_WAIT_MS) -> bool:
    """Click a button INSIDE the modal dialog whose label contains one of `texts`.

    **Scope is the fix, not exactness.** Measured against the live create-folder dialog:

        button:text-is("Tạo")                    → 0   (never matches, even unscoped)
        button:has-text("Tạo")                   → 2, first is "Tạo hoặc tải lên"
        .ms-Dialog-actions button:has-text("Tạo") → 1, "Tạo"                    ✓

    The page-wide substring match is what hung the run: it found the command-bar button
    sitting *behind* the modal, and Playwright retried a blocked click until it gave up.
    Confining the search to the dialog removes the collision — inside it there is only
    Tạo and Hủy bỏ, and neither contains the other.

    `:text-is()` was the first attempt and matches nothing here, so it is not used.
    """
    each = max(timeout_ms // max(len(texts), 1), 1_000)
    for text in texts:
        selector = ", ".join(
            f'{root} {kind}:has-text("{text}")'
            for root in _DIALOG_ROOTS
            for kind in ("button", '[role="button"]')
        )
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=each):
                locator.click(timeout=each)
                return True
        except Exception:
            continue
    return False


def _click_any(page, selectors, texts=(), *, timeout_ms: int = _STEP_WAIT_MS) -> bool:
    """Click by selector first, fall back to label text. The house pattern for a button."""
    locator = _first_present(page, selectors, timeout_ms=timeout_ms)
    if locator is not None:
        try:
            locator.click()
            return True
        except Exception:
            pass
    return _click_by_text(page, texts, timeout_ms=timeout_ms) if texts else False


def _check_cancel(should_cancel, *, uploaded: int = 0) -> None:
    if should_cancel is not None and should_cancel():
        raise OneDriveCancelled(uploaded=uploaded)


def _report(on_progress, message: str) -> None:
    if on_progress is not None:
        on_progress(message)


def _dom_inventory(page, limit: int = 25) -> str:
    """The distinct `data-automationid` values on the page, most common first.

    Attached to failures so the next report *names the hooks that exist* instead of
    saying "giao diện có thể đã thay đổi". This is the ODSP analogue of the custom-element
    census `youtube_upload` runs on Studio: React does not give us custom tag names, but
    it does give us these, and they are what every selector in this module is built on.
    """
    try:
        ids = page.evaluate(
            """() => {
                const counts = {};
                for (const el of document.querySelectorAll('[data-automationid]')) {
                    const t = el.getAttribute('data-automationid');
                    if (t) counts[t] = (counts[t] || 0) + 1;
                }
                return Object.entries(counts)
                    .sort((a, b) => b[1] - a[1])
                    .map(([t, n]) => `${t}×${n}`);
            }"""
        )
    except Exception as exc:
        return f"(không đọc được cấu trúc trang: {exc!r})"
    return ", ".join(list(ids)[:limit]) or "(trang không có data-automationid nào)"


def _page_actions(page, limit: int = 30) -> str:
    """The visible clickable labels on the page, deduped.

    A click ladder fails because the label it wants isn't there — so the diagnostic that
    actually resolves it is the list of labels that ARE. `_dom_inventory` answers a
    different question (which selectors exist); this answers "what could I have clicked".
    """
    try:
        labels = page.evaluate(
            r"""() => {
                const out = [];
                const sel = 'button, a, [role=button], [role=menuitem], [role=tab]';
                const walk = (root) => {
                    for (const el of root.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        const t = (el.innerText || el.getAttribute('aria-label') || '')
                                    .trim().split('\n')[0];
                        if (t) out.push(t.slice(0, 60));
                    }
                    for (const el of root.querySelectorAll('*'))
                        if (el.shadowRoot) walk(el.shadowRoot);
                };
                walk(document);
                return [...new Set(out)];
            }"""
        )
    except Exception as exc:
        # Report the reason: a diagnostic that fails silently is worse than none, and the
        # Studio version of this did exactly that on its first real use.
        return f"(không đọc được các nút trên trang: {exc!r})"
    return " | ".join(list(labels)[:limit]) or "(trang không có nút nào)"


# -- browser plumbing ---------------------------------------------------------


def _require_playwright():
    """Import Playwright's sync API, or raise a message that says how to install it."""
    try:
        return require_playwright()
    except BrowserUnavailableError as exc:
        raise OneDriveUploadError(
            "Chưa cài Playwright cho tính năng tải lên OneDrive. Chạy:\n"
            "  pip install 'noveltrans[browser]'\n"
            "  playwright install chromium"
        ) from exc


def _launch_context(sync_playwright, *, headless: bool):
    """Open the dedicated profile in the user's real Chrome (bundled Chromium if not).

    Microsoft's sign-in throws MFA prompts and new-device checks, and reacts to a
    headless-ish fingerprint by throwing more of them — so we drive the *installed*
    Chrome, exactly as the YouTube flow does. Same dedicated profile either way.
    """
    try:
        return launch_persistent_context(sync_playwright, profile_dir(), headless=headless)
    except BrowserUnavailableError as exc:
        raise OneDriveUploadError(
            "Không mở được trình duyệt để tải lên OneDrive. Cài Google Chrome, hoặc "
            "chạy:  playwright install chromium"
        ) from exc


def clear_profile() -> bool:
    """Delete the dedicated profile, signing the app out of OneDrive. True if one existed.

    The reliable way to change account: Microsoft's own account switcher keeps the old
    session alive alongside the new one, so "signed in as the wrong account" is not
    something it reliably fixes, and its DOM is one more thing to keep working. Dropping
    the profile resets it at the cost of one sign-in.

    Only ever removes `profile_dir()` itself, and only when it looks like a browser
    profile: this deletes a directory tree, so it verifies what it is aiming at first.
    """
    import shutil

    path = profile_dir()
    if not path.is_dir():
        return False
    # A persistent Chromium profile always has these. If neither is present we are not
    # looking at what we think we are, and deleting recursively would be reckless.
    if not any((path / name).exists() for name in ("Default", "Local State")):
        raise OneDriveUploadError(
            f"Thư mục {path} không giống profile trình duyệt — không xoá để tránh mất "
            "dữ liệu. Hãy kiểm tra và xoá thủ công."
        )
    shutil.rmtree(path)
    return True


# -- session ------------------------------------------------------------------


def _is_logged_out(page) -> bool:
    """True if the page is Microsoft's sign-in rather than a OneDrive file list.

    Three signals, cheapest first:

      1. a login host in the URL — free, and cannot be A/B tested away;
      2. the anonymous product marketing page, which is where a signed-out visit to
         `onedrive.live.com` actually ends up (MEASURED — see `_ANONYMOUS_URL_RE`). You
         never reach it with a session, so landing there IS being signed out;
      3. a sign-in form in the DOM, the fallback for a login page served at a URL shape
         we did not anticipate.
    """
    url = page.url or ""
    if _LOGGED_OUT_URL_RE.search(url) or _ANONYMOUS_URL_RE.search(url):
        return True
    return _signin_form_present(page)


def _signin_form_present(page) -> bool:
    """True if Microsoft's email box is on the page."""
    return _first_present(page, _SIGNIN_FORM_SELS, timeout_ms=2_000) is not None


def _account_name(page) -> str:
    """The signed-in account's display name or email, or "" if it can't be read.

    Shown back to the user after a sign-in so they can confirm they landed on the account
    they meant — the failure this exists for is signing into the wrong one and not
    finding out until a novel is sitting in the wrong OneDrive.

    Never raises and never blocks the flow: a name we cannot read costs a confirmation
    message its detail, and nothing else. The account that matters is whichever one the
    profile holds, and that is true whether or not we can print its name.
    """
    for selector in _SIGNED_IN_ACCOUNT_SELS:
        try:
            locator = page.locator(selector).first
            for getter in (
                lambda: locator.get_attribute("aria-label", timeout=2_000),
                lambda: locator.inner_text(timeout=2_000),
                lambda: locator.get_attribute("title", timeout=2_000),
            ):
                text = (getter() or "").strip()
                if not text:
                    continue
                text = text.split("\n")[0]
                for suffix in _ACCOUNT_LABEL_SUFFIXES:
                    if text.endswith(suffix):
                        text = text[: -len(suffix)]
                        break
                text = text.strip()
                if text:
                    return text[:120]
        except Exception:
            continue
    return ""


def open_login(timeout_ms: int = 300_000, *, switch: bool = False) -> str:
    """Open a visible OneDrive window so the account can sign in once.

    Blocks (run me in a worker thread) until a OneDrive surface finishes loading — either
    immediately (session still valid) or after the user signs in — then closes. The
    session is saved in the persistent profile for later pushes. Returns the account name
    or email if it could be read, "" otherwise; either way a successful return means the
    profile holds a working session.

    `switch=True` drops the existing profile first, so the sign-in starts from Microsoft's
    account chooser. Without it an already-valid session matches immediately and the
    window closes before the user can change anything — the exact trap that someone
    signed into the wrong account falls into. (033 documented this for Google; Microsoft
    behaves the same way.)

    Headless is never an option here: Microsoft throws MFA prompts, "stay signed in?" and
    new-device checks that a human has to clear, and a headless fingerprint invites more
    of them.
    """
    if switch:
        clear_profile()
    sync_playwright = _require_playwright()
    playwright, context = _launch_context(sync_playwright, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(_LOGIN_URL, wait_until="commit", timeout=_NAV_TIMEOUT_MS)
        except Exception:
            # A slow load still navigates; the poll below is what decides.
            pass
        _wait_for_signin(page, timeout_ms=timeout_ms)
        # Success is "the session can reach a file list", not "the URL looks right".
        # Signing in through office.com can land anywhere in the Microsoft 365 shell, so
        # a URL pattern would be guessing; opening the root proves the thing we actually
        # need and raises with a real reason if it cannot.
        _open_root(page)
        _settle(page, _NAV_SETTLE_MS)
        return _account_name(page)
    except OneDriveUploadError:
        raise
    except Exception as exc:  # window closed early, timeout, etc.
        raise OneDriveUploadError(f"Đăng nhập OneDrive chưa hoàn tất: {exc}") from exc
    finally:
        _close(context, playwright)


def _wait_for_signin(page, *, timeout_ms: int) -> None:
    """Block until the sign-in is done: no login host, no email box, for two polls running.

    Two consecutive clear polls, not one — Microsoft's sign-in walks through several
    redirects (email → password → "stay signed in?"), and any gap between them briefly
    shows a page with no form on it. One clear reading would call that a finished login
    and go looking for a file list mid-flow.
    """
    waited = 0
    clear = 0
    while waited < timeout_ms:
        if _LOGGED_OUT_URL_RE.search(page.url or "") or _signin_form_present(page):
            clear = 0
        else:
            clear += 1
            if clear >= 2:
                return
        page.wait_for_timeout(_SIGNIN_POLL_MS)
        waited += _SIGNIN_POLL_MS
    raise OneDriveUploadError(
        "Hết thời gian chờ đăng nhập OneDrive — cửa sổ vẫn đang ở trang đăng nhập. "
        "Mở lại “Đăng nhập OneDrive” và đăng nhập cho xong; cửa sổ sẽ tự đóng khi vào "
        "được kho file."
    )


# -- what gets pushed (pure, tested) ------------------------------------------
#
# Everything from here down runs without a browser, without Playwright and without a
# Microsoft account. That is deliberate: the decisions that can lose a file or waste a
# night of bandwidth all live in this half, so they are the half that gets tested.

# Our own bookkeeping, and the transient files sqlite and this app leave lying around.
# Each exclusion earns its line:
_MANIFEST_FILE = ".onedrive-upload.json"
# `chapters.db-wal` / `-shm` are meaningless without the exact process that wrote them,
# and *actively harmful* next to a snapshot taken at a different instant — restoring the
# trio would replay a WAL against a database it does not belong to.
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")
# Half-written by `write_upload_state` and by our own `write_manifest`.
_TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class PayloadItem:
    """One file to push: where it is now, and where it goes in the mirrored tree."""

    path: Path  # absolute source; may point at a snapshot rather than the real file
    relpath: str  # POSIX-style, relative to the project folder
    size: int
    mtime: float

    @property
    def folder(self) -> str:
        """The POSIX folder this file lands in, "" for the project root."""
        return posixpath.dirname(self.relpath)


def _is_excluded(name: str) -> bool:
    """True for a filename we never push. Applied to files *and* directories."""
    if name.startswith("."):  # .DS_Store, our manifest, any stray dot-directory
        return True
    if name.endswith(_TMP_SUFFIX):
        return True
    return any(name.endswith(suffix) for suffix in _DB_SIDECAR_SUFFIXES)


def _effective_mtime(path: Path) -> float:
    """`path`'s mtime, but for `chapters.db` also counting its write-ahead log.

    In WAL mode sqlite writes to `chapters.db-wal` and does not necessarily touch the
    main file's mtime until a checkpoint. Reading `chapters.db`'s mtime alone therefore
    says "unchanged" about a database that gained fifty chapters this afternoon — and the
    skip rule would believe it and never push them again.
    """
    mtime = path.stat().st_mtime
    if path.name == DB_FILE:
        wal = path.with_name(path.name + "-wal")
        try:
            mtime = max(mtime, wal.stat().st_mtime)
        except OSError:
            pass
    return mtime


def collect_payload(project_path: Path) -> list[PayloadItem]:
    """Every file in the project folder that should be mirrored, sorted by relpath.

    "Everything the novel produced" — `meta.json`, `chapters.db`, and the whole `exports/`
    tree with the audio, the rendered parts and each part's sidecars. The `.upload.json`
    sidecars come too: they are part of what the project produced, though it is worth
    knowing that they carry the channel's publication record (see the plan's trade-offs).

    Deterministic order, because it decides batch composition and a preview that
    reshuffles between two runs is one nobody can compare.
    """
    project_path = Path(project_path)
    items: list[PayloadItem] = []
    for path in project_path.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(project_path)
        if any(_is_excluded(part) for part in relative.parts):
            continue
        try:
            size = path.stat().st_size
            mtime = _effective_mtime(path)
        except OSError:
            continue  # vanished mid-walk; a file we cannot stat is one we cannot send
        items.append(
            PayloadItem(path=path, relpath=relative.as_posix(), size=size, mtime=mtime)
        )
    return sorted(items, key=lambda item: item.relpath)


def snapshot_database(project_path: Path, dest_dir: Path) -> Path:
    """Write a consistent copy of `chapters.db` into `dest_dir` and return its path.

    Uses sqlite's own backup API rather than copying the file, because the database is
    open in WAL mode while the app runs. A plain copy of `chapters.db` taken mid-session
    produces a file that opens cleanly and is missing every chapter still sitting in the
    WAL — the worst kind of backup, one that looks fine. The backup API checkpoints into
    the destination, so the result is a single self-contained file with no sidecars,
    which is also exactly what we want to upload.
    """
    source = Path(project_path) / DB_FILE
    if not source.is_file():
        raise OneDriveUploadError(f"Không tìm thấy cơ sở dữ liệu truyện: {source}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / DB_FILE
    src_db = sqlite3.connect(source)
    try:
        dst_db = sqlite3.connect(dest)
        try:
            src_db.backup(dst_db)
        finally:
            dst_db.close()
    finally:
        src_db.close()
    return dest


def swap_in_database_snapshot(
    items: list[PayloadItem], snapshot: Path
) -> list[PayloadItem]:
    """Point the `chapters.db` item at `snapshot`, keeping its relpath and mtime.

    Size comes from the snapshot because that is what actually goes up. The mtime stays
    the *source's* (WAL included), because it is what the skip rule compares against next
    run — taking the snapshot's would stamp "now" on every run and re-upload the database
    every single time.
    """
    snapshot = Path(snapshot)
    size = snapshot.stat().st_size
    return [
        replace(item, path=snapshot, size=size) if item.relpath == DB_FILE else item
        for item in items
    ]


# -- naming the remote folder (pure, tested) ----------------------------------

# OneDrive rejects these outright in an item name.
_ILLEGAL_NAME_CHARS = '"*:<>?/\\|'
# Windows device names, which OneDrive inherits, plus OneDrive's own reserved forms.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", ".lock", "desktop.ini", "_vti_"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
# Well under OneDrive's 255-character segment limit, leaving room for the path beneath it.
_MAX_FOLDER_NAME = 100

ROOT_FOLDER = "NovelTrans"


def onedrive_folder_name(title: str) -> str:
    """A OneDrive-safe folder name for a novel, keeping the title readable.

    Diacritics are preserved on purpose: `slugify` would turn *Tên truyện* into
    `ten-truyen`, and "a folder named after the novel" does not mean that to the person
    looking at it in OneDrive. OneDrive accepts Unicode names perfectly well; only a
    specific set of punctuation is a problem.

    Illegal characters become spaces rather than being deleted, so `A/B` reads as `A B`
    instead of silently merging into `AB`. Falls back to `slugify` only when sanitising
    leaves nothing at all — a title made entirely of illegal characters is rare but a
    folder named `""` is not something we can create.
    """
    name = "".join(" " if ch in _ILLEGAL_NAME_CHARS else ch for ch in title or "")
    name = " ".join(name.split())  # collapse the runs the substitution just made
    # Trailing dots and spaces are stripped by the server, silently, which would make our
    # "is this folder here?" check compare against a name that no longer exists.
    name = name.rstrip(". ").strip()
    if name.lower() in _RESERVED_NAMES:
        name = f"_{name}"
    if len(name) > _MAX_FOLDER_NAME:
        name = name[:_MAX_FOLDER_NAME].rstrip(". ").strip()
    return name or slugify(title or "")


def remote_root_for(title: str, root_folder: str = "") -> str:
    """The full remote path a novel is mirrored into, e.g. `/Fox Novel/Tên truyện`.

    `root_folder` is the one destination the user picked for their whole library; each
    novel is a subfolder of it. Nested roots (`/Backup/Truyện`) are allowed, and every
    segment goes through `onedrive_folder_name` — a root typed with a colon in it would
    otherwise fail at creation time with a message about the novel rather than the root.
    """
    segments = [
        onedrive_folder_name(part)
        for part in (root_folder or ROOT_FOLDER).split("/")
        if part.strip()
    ]
    return "/" + "/".join([*segments, onedrive_folder_name(title)])


# -- batching (pure, tested) --------------------------------------------------

# One `set_input_files` call with three thousand chapter MP3s is an unbounded gamble on
# the page's own upload queue, and a failure costs the whole call. Both caps are
# deliberately conservative on first release; a folder with 2 000 files simply becomes
# 100 batches, and the worker checkpoints (pause/cancel) between them.
MAX_BATCH_FILES = 20
# The batch is also the resume granularity — there is no mid-file resume through a
# browser — so this bounds what a dropped connection costs.
MAX_BATCH_BYTES = 4 * 1024**3


def batch_payload(
    items: list[PayloadItem],
    *,
    max_files: int = MAX_BATCH_FILES,
    max_bytes: int = MAX_BATCH_BYTES,
) -> list[list[PayloadItem]]:
    """Split the payload into upload batches. Never spans folders; respects both caps.

    A batch targets exactly one destination folder, because the upload input uploads into
    whatever folder is open — files from two folders in one call would land in one folder,
    which is the same silent flattening `_UPLOAD_INPUT_SELS` guards against from the other
    direction.

    A single file larger than `max_bytes` still gets its own batch rather than being
    dropped: a 6 GB part-video is exactly the thing the user most wants backed up.
    """
    batches: list[list[PayloadItem]] = []
    for folder in dict.fromkeys(item.folder for item in items):  # first-seen order
        current: list[PayloadItem] = []
        total = 0
        for item in (i for i in items if i.folder == folder):
            too_many = len(current) >= max_files
            too_big = current and total + item.size > max_bytes
            if too_many or too_big:
                batches.append(current)
                current, total = [], 0
            current.append(item)
            total += item.size
        if current:
            batches.append(current)
    return batches


# -- the manifest (pure, tested) ----------------------------------------------
#
# One file per project rather than a sidecar per file: per-file sidecars would add
# several hundred tiny files to the very tree we are uploading, and would then need
# uploading themselves.
#
# **A corrupt manifest reads as EMPTY, and that is the opposite of what
# `youtube_upload.read_upload_state` does with a corrupt file.** Both choices are right
# for their own module and the difference is worth understanding before changing either:
#
#   YouTube   "I can't tell" must never read as "never uploaded", because acting on that
#             publishes an episode twice to an audience. Cost of the safe choice: the
#             user does one upload by hand.
#   OneDrive  "I can't tell" reads as "never uploaded", because acting on that re-sends
#             files to a private folder. Cost of the safe choice: bandwidth. The cost of
#             the *other* mistake is a file the user believes is backed up and isn't —
#             and silence about a missing backup is the expensive failure here.

STATUS_SENDING = "sending"  # written BEFORE a batch goes out
STATUS_DONE = "done"  # written after the folder listing confirmed every file

_MANIFEST_VERSION = 1


@dataclass
class Manifest:
    """A project's push record: which files are up, and where they went.

    `note` is non-empty when the on-disk file could not be read. The manifest is still
    usable (it is simply empty), but the GUI should say so rather than silently offering
    to re-upload sixty gigabytes.
    """

    remote_root: str = ""
    account: str = ""
    updated_at: str = ""
    files: dict[str, dict] = field(default_factory=dict)
    note: str = ""

    def mark_sending(self, item: PayloadItem) -> None:
        self.files[item.relpath] = {
            "size": item.size,
            "mtime": item.mtime,
            "status": STATUS_SENDING,
        }

    def mark_done(self, item: PayloadItem) -> None:
        self.files[item.relpath] = {
            "size": item.size,
            "mtime": item.mtime,
            "status": STATUS_DONE,
            "uploaded_at": _now_iso(),
        }


def manifest_path(project_path: Path) -> Path:
    return Path(project_path) / _MANIFEST_FILE


def read_manifest(project_path: Path) -> Manifest:
    """The project's push record, or an empty one if there isn't a readable one.

    Missing, corrupt, truncated, wrong-shaped and unreadable all return an empty
    manifest — see the block comment above for why that is deliberately unlike
    `read_upload_state`. Only the `note` distinguishes "never pushed" from "couldn't
    read the record", and it exists so the GUI can tell the user which they are looking
    at.
    """
    path = manifest_path(project_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Manifest()
    except OSError as exc:
        return Manifest(note=f"không đọc được trạng thái cũ: {exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return Manifest(note=f"file trạng thái hỏng: {exc}")
    if not isinstance(data, dict):
        return Manifest(note="file trạng thái sai định dạng")
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    return Manifest(
        remote_root=str(data.get("remote_root") or ""),
        account=str(data.get("account") or ""),
        updated_at=str(data.get("updated_at") or ""),
        files={k: v for k, v in files.items() if isinstance(v, dict)},
    )


def write_manifest(project_path: Path, manifest: Manifest) -> None:
    """Persist the push record atomically (temp file → `os.replace`).

    Atomic because it is written between real operations — immediately before and after a
    batch — and a torn write must not produce something a later run has to interpret.
    """
    manifest.updated_at = _now_iso()
    payload = {
        "version": _MANIFEST_VERSION,
        "remote_root": manifest.remote_root,
        "account": manifest.account,
        "updated_at": manifest.updated_at,
        "files": manifest.files,
    }
    path = manifest_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + _TMP_SUFFIX)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX and for same-directory replaces on Windows


def clear_manifest(project_path: Path) -> bool:
    """Forget the push record so the next run re-uploads everything. True if one existed.

    The escape hatch. Unlike `youtube_upload.clear_upload_state` this needs no warning
    about duplicates, because it cannot create one — the worst it can do is cost the user
    a re-upload. Saying so plainly in the confirmation is part of the design: a warning
    that isn't warranted trains people to ignore the ones that are.
    """
    path = manifest_path(project_path)
    if not path.is_file():
        return False
    path.unlink()
    return True


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# -- the skip rule (pure, tested) ---------------------------------------------

# Some filesystems (and some archive tools) round mtimes to whole or even-numbered
# seconds. Without a tolerance, a file that never changed can read as one second newer
# than the record and be re-sent on every run.
_MTIME_TOLERANCE_S = 2.0


def plan_uploads(
    items: list[PayloadItem], manifest: Manifest, *, force: bool = False
) -> tuple[list[PayloadItem], list[PayloadItem]]:
    """Split the payload into `(to_upload, to_skip)`. The brain of the feature.

    Size plus mtime, deliberately not a checksum: hashing forty gigabytes to avoid
    re-sending two hundred megabytes is the wrong trade, and the workflow this serves
    (re-render part 7, push again) is one the cheap comparison gets right.

    Anything the manifest is unsure about is uploaded. A `sending` entry means a previous
    run died mid-batch, and it is simply re-sent — there is no "dở dang" state to resolve
    and no human to ask, because replacing a file in a private folder is idempotent in a
    way a YouTube publish never is.
    """
    if force:
        return list(items), []
    to_upload: list[PayloadItem] = []
    to_skip: list[PayloadItem] = []
    for item in items:
        record = manifest.files.get(item.relpath)
        if not isinstance(record, dict) or record.get("status") != STATUS_DONE:
            to_upload.append(item)
            continue
        if record.get("size") != item.size:
            to_upload.append(item)
            continue
        try:
            recorded_mtime = float(record.get("mtime"))
        except (TypeError, ValueError):
            to_upload.append(item)  # unreadable record → re-send, per the module's rule
            continue
        if item.mtime > recorded_mtime + _MTIME_TOLERANCE_S:
            to_upload.append(item)
            continue
        to_skip.append(item)
    return to_upload, to_skip


def total_bytes(items: list[PayloadItem]) -> int:
    return sum(item.size for item in items)


def format_size(num_bytes: int) -> str:
    """Human bytes for the confirmation dialog: `4,2 GB`, `812 MB`, `0 B`.

    Vietnamese decimal comma, because this string is read by the user and every other
    number in the app uses one.
    """
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f}".replace(".", ",") + f" {unit}"
        value /= 1024
    return f"{num_bytes} B"  # unreachable; keeps the function total


# -- reading OneDrive's progress line (pure, tested) --------------------------


def upload_status(text: str) -> tuple[bool, int | None, int | None]:
    """Read OneDrive's status line: `(transfer finished?, item count, percent)`.

    **Advisory.** Unlike `youtube_upload.transfer_state`, this does NOT decide when a
    batch is done — the folder listing does, because it is the thing we actually care
    about and it cannot be A/B tested into a different wording. This feeds the status
    line the user reads and the stall detector, and it is pure and tested because a
    wrong answer here still costs either a wasted wait or a premature move on.

    Unrecognised text reads as *still going*, never as finished: erring that way costs
    time, erring the other way abandons a transfer when the browser closes.
    """
    text = (text or "").strip()
    if not text:
        return False, None, None
    match = _PERCENT_RE.search(text)
    percent = int(match.group(1)) if match else None
    match = _ITEM_COUNT_RE.search(text)
    count = int(match.group(1)) if match else None
    if _UPLOADING_RE.search(text):
        # Still moving — unless it is sitting at a full 100%.
        return (percent is not None and percent >= 100), count, percent
    if _UPLOAD_DONE_RE.search(text):
        return True, count, percent
    return False, count, percent


def is_quota_error(text: str) -> bool:
    """True if the status line is OneDrive saying the account is out of space.

    Worth its own check because it is the one failure where continuing is pointless:
    every remaining batch would fail identically, so `push_project` re-raises instead of
    marking file after file as failed.
    """
    return bool(_QUOTA_RE.search(text or ""))


# -- how long to wait (pure, tested) ------------------------------------------

# A pessimistic floor, not an estimate of the user's line. Its only job is to turn a
# batch size into a ceiling generous enough that a slow-but-working transfer is never
# killed by it — the stall detector is what actually catches a dead one.
_ASSUMED_FLOOR_BPS = 512 * 1024
# Slack on the ceiling, so it sits above the stall window (1.5x) rather than under it.
_CEILING_SLACK = 3.0
_BATCH_MIN_WAIT_MS = 10 * 60_000
_BATCH_MAX_WAIT_MS = 6 * 3600_000
# No movement — neither a changed status line nor a new file in the listing — for this
# long fails the batch. This, not the ceiling above, is the guard that fires on a dropped
# connection, and it is why the ceiling can afford to be generous.
_STALL_MS = 10 * 60_000


def stall_ms_for(largest_file_bytes: int) -> int:
    """How long to allow with no visible movement, given the biggest file in the batch.

    The stall detector's only movement signals are a changed status line and a file
    reaching its expected size. MEASURED: OneDrive's status region stays **empty** through
    an upload, so for a batch dominated by one big file there is *nothing* to see until it
    lands. A flat ten-minute window would fail a 456 MB part-video — which takes about
    nineteen minutes at the assumed floor — while it was transferring perfectly well.

    So the window scales with the largest file, and the size-derived `batch_timeout_ms`
    remains the outer ceiling. A batch of small files keeps the flat floor, which is where
    the detector is genuinely useful.
    """
    expected = int(max(largest_file_bytes, 0) / _ASSUMED_FLOOR_BPS * 1000 * 1.5)
    return max(_STALL_MS, expected)


def batch_timeout_ms(bytes_in_batch: int) -> int:
    """Ceiling for one batch, derived from its size rather than picked out of the air.

    `_CEILING_SLACK` matters: this has to stay **above** `stall_ms_for` for the same
    batch, or the ceiling fires first and the stall detector — the guard that actually
    reports a dropped connection — never gets a chance. A test pins that ordering, and
    caught it being the wrong way round.

    A 4 GB batch therefore gets several hours at the assumed floor; a handful of text
    files still gets the ten-minute minimum, because "small" says nothing about whether
    the network is having a bad afternoon.
    """
    estimate = int(max(bytes_in_batch, 0) / _ASSUMED_FLOOR_BPS * 1000 * _CEILING_SLACK)
    return max(_BATCH_MIN_WAIT_MS, min(estimate, _BATCH_MAX_WAIT_MS))


# -- navigating the file list -------------------------------------------------
#
# Every function here checks its own postcondition rather than trusting that a click
# landed. That is not defensive habit — it is the whole compensating control for choosing
# browser automation. A navigation step that quietly does nothing puts the next batch in
# the wrong folder, and the manifest would then record those files as safely mirrored.

# Let the SPA re-render between steps. OneDrive keeps long-poll connections open, so
# `networkidle` rarely fires and is not worth waiting on.
_SETTLE_MS = 1_200
# A runaway guard on reading the listing, not a real limit on folder size.
_MAX_LISTED_ROWS = 500
# How long a newly created folder gets to appear in the grid. A server round-trip plus a
# re-render; generous because being slow here costs seconds and being early fails a
# creation that actually worked.
_CREATE_WAIT_MS = 20_000
_CREATE_POLL_MS = 1_500
# How long the root page gets to settle on a final URL before we judge whether it is a
# file list or a login page. Cheap insurance against a spurious "chưa đăng nhập".
_ROOT_SETTLE_MS = 15_000
_ROOT_POLL_MS = 1_500


def _settle(page, ms: int = _SETTLE_MS) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def _names_match(a: str, b: str) -> bool:
    """Whether two item names refer to the same thing, the way OneDrive decides it.

    Case-insensitive, because OneDrive is: it preserves the case you typed but will not
    let `NovelTrans` and `noveltrans` coexist. A case-sensitive comparison here would
    make us create a second folder the server then refuses or auto-renames.
    """
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _texts_of(
    page, selector: str, limit: int = _MAX_LISTED_ROWS, *, keep_blank: bool = False
) -> list[str]:
    """First line of the visible text of every element matching `selector`. Never raises.

    `keep_blank` preserves empties so two parallel column reads stay row-aligned — see
    `_list_folders`, which zips names against sizes and would mis-pair every row after
    the first blank cell without it.
    """
    # One `evaluate` instead of one round-trip per row. Reading 60 rows × 2 columns the
    # slow way is ~120 round-trips, and `_wait_for_batch` does it every poll — measured
    # at tens of seconds per poll, which is why a batch that had already uploaded still
    # looked like it was hanging. The locator path stays as the fallback (and is what the
    # test fake exercises).
    try:
        texts = page.evaluate(
            """(args) => [...document.querySelectorAll(args.selector)]
                   .slice(0, args.limit)
                   .map(e => (e.innerText || '').trim().split('\\n')[0].trim())""",
            {"selector": selector, "limit": limit},
        )
        return [t for t in texts if t or keep_blank]
    except Exception:
        pass

    out: list[str] = []
    try:
        items = page.locator(selector)
        count = min(items.count(), limit)
    except Exception:
        return out
    for i in range(count):
        try:
            text = (items.nth(i).inner_text() or "").strip()
        except Exception:
            text = ""
        text = text.split("\n")[0].strip()
        if text or keep_blank:
            out.append(text)
    return out


def _current_folder(page) -> str:
    """The name of the folder OneDrive is showing, or "" if it cannot be read.

    The proof that a click landed — this module's replacement for the breadcrumb it
    assumed and OneDrive does not have. Three independent sources, cheapest first; see
    the constants block for what each looks like live.
    """
    try:
        title = (page.title() or "").strip()
    except Exception:
        title = ""
    if title.endswith(_PAGE_TITLE_SUFFIX):
        name = title[: -len(_PAGE_TITLE_SUFFIX)].strip()
        if name:
            return name

    # Read the header element in full rather than through `_texts_of`, which keeps only
    # the first line — here it is the LAST line that names the current folder.
    try:
        header = page.locator(_LIST_HEADER_SEL).first.inner_text() or ""
        lines = [line.strip() for line in header.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    except Exception:
        pass

    match = _FOLDER_ID_RE.search(page.url or "")
    if match:
        from urllib.parse import unquote

        segments = [s for s in unquote(match.group(1)).split("/") if s]
        if segments:
            return segments[-1]
    return ""


def _is_column_header(name: str) -> bool:
    """True for the grid's header row, which is not a file and must never count as one."""
    return (name or "").strip().casefold() in _COLUMN_HEADER_NAMES


def _list_names(page) -> list[str]:
    """The item names in the folder currently open, header row excluded.

    The authority on "did that file land" and on "does this folder already exist".

    Known limitation, still open: OneDrive virtualises long lists, so a folder with
    hundreds of items may not have every row in the DOM. An existing folder we fail to
    see leads to `_create_folder`, and that is safe — its own postcondition catches the
    auto-renamed twin OneDrive would produce and stops the run rather than building a
    duplicate tree.
    """
    for selector in (_ROW_NAME_SEL, _ROW_SEL):
        names = [n for n in _texts_of(page, selector) if not _is_column_header(n)]
        if names:
            return names
    return []


def _list_folders(page) -> list[str]:
    """Just the folders in the folder currently open, in the order shown.

    Powers the destination picker, where offering files would be noise at best — you
    cannot upload *into* a spreadsheet, and picking one would fail later with a confusing
    message about a folder that would not open.

    Names and sizes are read as two parallel column queries and zipped, rather than by
    walking each row and querying inside it. Both return one entry per row in the same
    order, and the flat form is what the fake page in the tests can model faithfully.
    """
    names = _texts_of(page, _ROW_NAME_SEL, keep_blank=True)
    sizes = _texts_of(page, _ROW_SIZE_SEL, keep_blank=True)
    if not names:
        return []
    if len(sizes) != len(names):
        # The columns disagree, so pairing them would be guesswork. Rather than silently
        # offer a wrong list, offer everything that is not the header and let the picker's
        # own navigation reject anything that is not a folder.
        return [n for n in names if n and not _is_column_header(n)]
    return [
        name
        for name, size in zip(names, sizes)
        if name and not _is_column_header(name) and _ITEM_COUNT_SIZE_RE.match(size)
    ]


def _sort_by_newest(page) -> bool:
    """Sort the open folder by Modified, newest first. False if the controls are missing.

    **This is what makes verification possible in a real folder.** OneDrive renders only
    about 60 rows however many files there are, so in `exports/audio` (2684 files) a
    just-uploaded batch is nowhere in the DOM and `_wait_for_batch` can only ever time
    out. Newest-first puts the whole batch — at most `MAX_BATCH_FILES` — in the top rows.

    Best-effort: a folder small enough to render completely does not need it, so a missing
    sort control is reported and not fatal. `_verify_batch` is still the thing that decides
    whether the files actually landed.

    Each option is skipped when `aria-checked` already says so — clicking a selected radio
    is at best wasted round-trips and at worst a toggle.
    """
    for option in (_SORT_MODIFIED_SEL, _SORT_DESC_SEL):
        try:
            if page.locator(option).first.get_attribute("aria-checked") == "true":
                continue
        except Exception:
            pass  # not rendered until the menu opens; find out by opening it
        if not _click_any(page, (_SORT_MENU_SEL,), _SORT_MENU_TEXTS, timeout_ms=10_000):
            return False
        _settle(page)
        locator = _first_present(page, (option,), timeout_ms=5_000)
        if locator is None:
            _dismiss_menu(page)
            return False
        if locator.get_attribute("aria-checked") == "true":
            _dismiss_menu(page)
            continue
        try:
            locator.click(timeout=5_000)
        except Exception:
            _dismiss_menu(page)
            return False
        _settle(page)
    return True


def _dismiss_menu(page) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    _settle(page, 500)


def _current_id(page) -> str:
    """The `?id=` path of the folder currently open, e.g. `/personal/<cid>/Documents/X`.

    "" at the root, which has no `id` at all — that is why navigation still has to click
    its way into the first folder before it can address anything by URL.
    """
    from urllib.parse import unquote

    match = _FOLDER_ID_RE.search(page.url or "")
    return unquote(match.group(1)) if match else ""


def _goto_folder_id(page, folder_id: str) -> bool:
    """Jump straight to a folder by its `?id=` path. False if we did not land in it.

    **This is what makes a big folder reachable at all.** OneDrive renders about 60 rows
    however many items a folder holds, so clicking through the listing simply cannot find
    the 30th part folder — `_enter_folder` reported "not there" for folders that plainly
    exist, which in turn made `_ensure_folder` try to *create* them. MEASURED: a deep link
    opens `exports/video/<part>` directly and lists its contents, whatever the sort order.
    """
    from urllib.parse import quote

    url = f"{_MY_FILES_URL}?id={quote(folder_id, safe='')}"
    try:
        page.goto(url, wait_until="commit", timeout=_NAV_TIMEOUT_MS)
    except Exception:
        pass  # a slow load still navigates; the landing check below decides
    _settle(page, _NAV_SETTLE_MS)
    return _names_match(_current_folder(page), folder_id.rstrip("/").rsplit("/", 1)[-1])


def _open_path(page, segments) -> bool:
    """Enter each folder in `segments` from here. False if one of them isn't there.

    Clicks only as far as it must: one click establishes where we are (the root carries no
    `id`), then the whole remaining path is one URL jump. That is both faster than walking
    and — the point — immune to the listing being virtualised.

    The browse-only sibling of `_ensure_path`: it never creates anything. A picker that
    silently created the folder you mistyped would be a poor thing to hand someone whose
    OneDrive already has years of files in it.
    """
    remaining = [s for s in segments if s]
    while remaining:
        base = _current_id(page).rstrip("/")
        if base:
            if _goto_folder_id(page, "/".join([base, *remaining])):
                return True
            # **Restore.** A failed jump leaves the browser at the root, and clicking on
            # from there searches the wrong folder — or, in `_ensure_folder`, CREATES in
            # the wrong folder. Caught by the tests before it could reach a real account.
            _goto_folder_id(page, base)
        if not _enter_folder(page, remaining[0]):
            return False
        remaining.pop(0)
    return True


def list_destination_folders(path: str = "", *, headless: bool = False) -> list[str]:
    """The subfolders of `path` on the user's OneDrive, for the destination picker.

    `path` is a POSIX-ish path like "/Fox Novel"; "" or "/" means the root. Opens a
    browser, so it belongs on a worker thread.
    """
    sync_playwright = _require_playwright()
    playwright, context = _launch_context(sync_playwright, headless=headless)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        _open_root(page)
        segments = [s for s in (path or "").split("/") if s.strip()]
        if segments and not _open_path(page, segments):
            raise OneDriveUploadError(f"Không tìm thấy thư mục “{path}” trên OneDrive.")
        return _list_folders(page)
    finally:
        _close(context, playwright)


def _open_root(page) -> str:
    """Navigate to the account's OneDrive root. Returns the flavour; raises if it can't.

    Tries the personal entry point and then the business one, because a wrong guess does
    not fail the navigation — it renders a page, and only the landed URL says which.
    """
    tried: list[str] = []
    # The marketing page is a *weak* signed-out signal and must not short-circuit the
    # loop. A work/school account has no consumer OneDrive, so `onedrive.live.com` sends
    # it to the brochure exactly as it sends an anonymous visitor there — concluding
    # "chưa đăng nhập" on the spot would tell a signed-in business user to sign in again,
    # over and over. A login host or a live email box, on the other hand, is unambiguous.
    saw_marketing = False
    for url in _ROOT_URLS:
        try:
            # `commit` rather than `domcontentloaded`. MEASURED 2026-08-06:
            # onedrive.live.com does not fire domcontentloaded within SIXTY seconds — it
            # streams a heavy marketing page — while `commit` returns in about one. We do
            # not care when its DOM settles; we care where the navigation landed, and
            # every element this module then touches has its own wait.
            page.goto(url, wait_until="commit", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            # A slow page can miss even that and STILL have navigated, so the landed URL
            # is checked below regardless. Giving up on the attempt here is what would
            # send a personal account down the business entry point for no reason.
            tried.append(f"{url} -> {exc!r}")
        # Give the SPA a moment to settle on its final URL before judging it. `commit`
        # returns as soon as the navigation starts, and a slow load can still be on an
        # intermediate URL three seconds later — which once produced a spurious "chưa
        # đăng nhập" on a session that was perfectly valid.
        waited = 0
        while True:
            _settle(page, _NAV_SETTLE_MS if not waited else _ROOT_POLL_MS)
            waited += _NAV_SETTLE_MS if waited == 0 else _ROOT_POLL_MS
            if flavour(page.url) or waited >= _ROOT_SETTLE_MS:
                break
        if _LOGGED_OUT_URL_RE.search(page.url or "") or _signin_form_present(page):
            raise _needs_login_error()
        if _ANONYMOUS_URL_RE.search(page.url or ""):
            saw_marketing = True
            tried.append(f"{url} -> (trang giới thiệu, không phải kho file)")
            continue
        kind = flavour(page.url)
        if kind:
            return kind
        tried.append(f"{url} -> {page.url}")

    if saw_marketing:
        # Every entry point tried, and the consumer one only ever offered the brochure.
        raise _needs_login_error()
    raise OneDriveUploadError(
        "Không mở được OneDrive. Đã thử:\n  " + "\n  ".join(tried) + "\n\n"
        f"Nút trên trang: {_page_actions(page)}"
    )


def _needs_login_error() -> OneDriveUploadError:
    return OneDriveUploadError(
        "Profile OneDrive chưa đăng nhập. Vào Settings → “Đăng nhập OneDrive” "
        "để đăng nhập một lần.",
        needs_login=True,
    )


def _enter_folder(page, name: str) -> bool:
    """Open the folder called `name` in the current listing. False if it isn't there.

    The False return is reserved for "no such folder", which is a normal state that
    `_ensure_folder` answers by creating it. A folder we clicked but did not end up
    inside raises instead — that is drift, and continuing would put files somewhere we
    cannot name.
    """
    if not any(_names_match(listed, name) for listed in _list_names(page)):
        return False

    # MEASURED: a **double-click** opens a folder. A single click on the name only
    # selects the row — the first live probe clicked `field-LinkFilename` and the view
    # did not move. Single click is kept as the second gesture in case a later build
    # makes the name a real link.
    tried = False
    for selector, gestures in ((_ROW_NAME_SEL, ("dblclick", "click")),
                               (_ROW_SEL, ("dblclick", "click"))):
        row = _row_named(page, selector, name)
        if row is None:
            continue
        for gesture in gestures:
            try:
                getattr(row, gesture)()
            except Exception:
                continue
            tried = True
            _settle(page, _NAV_SETTLE_MS)
            if _names_match(_current_folder(page), name):
                return True
    if not tried:
        return False

    raise OneDriveUploadError(
        f"Đã bấm vào thư mục “{name}” nhưng OneDrive không mở nó "
        f"(đang ở: {_current_folder(page) or 'không đọc được'}). "
        "Giao diện OneDrive có thể đã thay đổi — chạy scripts/diagnose_onedrive.py."
    )


def _row_named(page, selector: str, name: str):
    """The row matching `selector` whose first line of text is `name`, or None."""
    try:
        rows = page.locator(selector)
        count = min(rows.count(), _MAX_LISTED_ROWS)
    except Exception:
        return None
    for i in range(count):
        row = rows.nth(i)
        try:
            text = (row.inner_text() or "").strip().split("\n")[0]
        except Exception:
            continue
        if _names_match(text, name):
            return row
    return None


def _create_folder(page, name: str) -> None:
    """Create a folder called `name` here, and prove it exists before returning."""
    if not _click_any(page, _NEW_MENU_SELS, _NEW_MENU_TEXTS, timeout_ms=_STEP_WAIT_MS):
        raise OneDriveUploadError(
            f"Không mở được menu “Tạo hoặc tải lên” để tạo thư mục “{name}”. "
            f"Nút trên trang: {_page_actions(page)}"
        )
    _settle(page)
    if not _click_any(page, _NEW_FOLDER_SELS, _NEW_FOLDER_TEXTS, timeout_ms=_STEP_WAIT_MS):
        raise OneDriveUploadError(
            f"Mở được menu “Tạo hoặc tải lên” nhưng không thấy mục “Thư mục”. "
            f"Nút trên trang: {_page_actions(page)}"
        )
    _settle(page)

    box = _first_present(page, _FOLDER_NAME_INPUT_SELS, timeout_ms=_STEP_WAIT_MS)
    if box is None:
        raise OneDriveUploadError(
            f"Không tìm thấy ô nhập tên thư mục khi tạo “{name}”. "
            f"Component trên trang: {_dom_inventory(page)}"
        )
    try:
        box.fill(name)
    except Exception:
        # Some dialogs pre-fill "New folder"; clearing by hand is the fallback when
        # `fill` is refused.
        box.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(name)

    # Exact label inside the dialog first; the dialog's primary button second. Never a
    # page-wide substring match — see `_CREATE_TEXTS`.
    if not (
        _click_in_dialog(page, _CREATE_TEXTS, timeout_ms=_STEP_WAIT_MS)
        or _click_any(page, _CREATE_BUTTON_SELS, timeout_ms=10_000)
    ):
        raise OneDriveUploadError(
            f"Đã nhập tên “{name}” nhưng không bấm được nút “Tạo”. "
            f"Nút trên trang: {_page_actions(page)}"
        )
    _settle(page)

    # Creating is a server round-trip and the grid re-renders afterwards, so a single
    # read a second later finds an empty list and calls a successful creation a failure —
    # measured, on a folder that had in fact just been made. Poll instead.
    #
    # Landing *inside* the new folder counts too: OneDrive sometimes navigates into what
    # it just created, and then it is correctly absent from the listing we are reading.
    listed = _wait_for_folder(page, name)
    if listed is None:
        return
    # The dangerous outcome, called out by name: OneDrive answering a collision by making
    # `NovelTrans 1` instead of failing. Left alone that builds a second, divergent tree
    # nobody notices until they open the folder.
    twin = next(
        (item for item in listed if _names_match(item, f"{name} 1")),
        "",
    )
    if twin:
        raise OneDriveUploadError(
            f"OneDrive đã tạo “{twin}” thay vì “{name}” — nghĩa là thư mục cũ vẫn ở đó "
            "nhưng không đọc được từ danh sách. Dừng lại để không tạo cây thư mục trùng."
        )
    raise OneDriveUploadError(
        f"Đã tạo thư mục “{name}” nhưng nó không xuất hiện trong danh sách "
        f"({len(listed)} mục đọc được). Giao diện OneDrive có thể đã thay đổi — "
        "chạy scripts/diagnose_onedrive.py."
    )


def _wait_for_folder(page, name: str, *, timeout_ms: int = _CREATE_WAIT_MS):
    """Wait for `name` to appear here. None once it has; otherwise the last listing seen.

    Returning the listing on failure rather than a bare False is what lets the caller
    report "0 mục đọc được" or name the auto-renamed twin — the two very different
    reasons a folder can fail to show up.
    """
    waited = 0
    listed: list[str] = []
    while True:
        if _names_match(_current_folder(page), name):
            return None  # OneDrive navigated into what it just created
        listed = _list_names(page)
        if any(_names_match(item, name) for item in listed):
            return None
        if waited >= timeout_ms:
            return listed
        _settle(page, _CREATE_POLL_MS)
        waited += _CREATE_POLL_MS


def _ensure_folder(page, name: str) -> None:
    """Be inside the folder called `name`, creating it if it isn't there yet.

    The URL jump comes first, and it is the whole reason this is safe in a big folder: a
    folder outside the rendered rows used to read as absent, and this function would then
    try to *create* it. OneDrive answers that with an auto-renamed twin, which
    `_create_folder` catches — so the run stopped rather than duplicating, but it stopped
    on a folder that was there all along.
    """
    base = _current_id(page).rstrip("/")
    if base:
        if _goto_folder_id(page, f"{base}/{name}"):
            return
        # The jump failed, so we are at the root now. Go back before creating anything —
        # otherwise the novel folder lands beside `NovelTrans` instead of inside it.
        _goto_folder_id(page, base)
    if _enter_folder(page, name):
        return
    _create_folder(page, name)
    if _enter_folder(page, name):
        return
    raise OneDriveUploadError(
        f"Tạo được thư mục “{name}” nhưng không mở được nó. "
        f"Đang ở: {_current_folder(page) or 'không đọc được'}"
    )


def _ensure_path(page, segments) -> None:
    """Walk down (creating as needed) from the folder currently open.

    Segments are folder names, not a path string: the caller has already run each through
    `onedrive_folder_name`, and re-splitting a joined path here would be a second chance
    to get the escaping wrong.
    """
    for segment in segments:
        if not segment:
            continue
        _ensure_folder(page, segment)


def folder_segments(relpath: str) -> list[str]:
    """The folder names a payload item's relpath lands under, e.g. `["exports", "audio"]`.

    Empty for a file in the project root. Pure, so it is tested — it decides where every
    file goes, and a bug here mirrors the tree into the wrong shape.
    """
    folder = posixpath.dirname((relpath or "").strip("/"))
    return [part for part in folder.split("/") if part and part != "."]


# -- the transfer -------------------------------------------------------------
#
# The authority on "did this batch land" is the folder listing, not OneDrive's progress
# text. That is the deliberate inverse of `youtube_upload`, where Studio's progress line
# was the only signal there was. Here the file list answers the question directly, cannot
# be reworded by an A/B test, and is the thing we actually care about — so the status text
# is demoted to two jobs it is good at: telling the user what is happening, and proving
# that *something* is still happening.

# How long to hold the file-chooser interception open around the Upload click. Only the
# fallback path pays this.
_CHOOSER_WAIT_MS = 15_000
# The upload menu occasionally comes up in the wrong state between batches — the
# command-bar button stays toggled open, so clicking it closes the menu instead. Escaping
# and trying again fixes it; two attempts is enough and keeps a genuinely dead selector
# from being retried forever.
_MENU_ATTEMPTS = 3
_MENU_RETRY_SETTLE_MS = 2_500


class _MenuNotReady(Exception):
    """Internal: the upload menu was not usable this attempt. Retried, never surfaced."""

# The page must show *some* sign it took the files. Generous, because a big batch can sit
# on "preparing" for a while before the first byte moves.
_UPLOAD_ACCEPT_MS = 20_000
_UPLOAD_POLL_MS = 2_000
# After the status line claims the transfer finished, how long the listing gets to catch
# up before we call it a lie. The listing is the authority, but it is a *rendered* one and
# React needs a moment.
_POST_DONE_GRACE_MS = 30_000


def _renamed_twin(name: str) -> str:
    """What OneDrive calls a file it refused to overwrite: `phan-1.mp4` → `phan-1 1.mp4`.

    Matched so it can be *refused*. Left alone, a batch that silently became a set of
    ` 1` copies reads as a successful upload and gets recorded as one, while the file the
    user believes is mirrored still holds the old bytes.
    """
    stem, dot, ext = name.rpartition(".")
    return f"{stem} 1{dot}{ext}" if dot else f"{name} 1"


def _status_text(page) -> str:
    """OneDrive's progress line, or "" if it isn't showing one. Never raises."""
    try:
        return (page.locator(_UPLOAD_STATUS_SEL).first.inner_text() or "").strip()
    except Exception:
        return ""


def _resolve_conflicts(page) -> bool:
    """Answer OneDrive's name-collision dialog with Replace. True if one was showing.

    Replace, always: we are mirroring a local tree, so the local copy is the intended
    truth. "Keep both" is never clicked — it would leave `phan-1 1.mp4` beside
    `phan-1.mp4`, which is worse than either outcome and invisible until someone opens
    the folder. If the dialog is up and Replace cannot be found, that is a stop, not a
    reason to press whatever else is there.
    """
    waited = 0
    saw_prompt = False
    while True:
        # No container gate — see `_CONFLICT_TEXT_RE`. The button IS the signal.
        # `has-text` is a substring match, which is wanted here: it catches the
        # single-file "Thay thế" AND the multi-file "Thay thế tất cả".
        if _click_by_text(page, _REPLACE_TEXTS, timeout_ms=2_000):
            _settle(page)
            return True

        try:
            body = page.evaluate("() => document.body.innerText") or ""
        except Exception:
            body = ""
        if _CONFLICT_TEXT_RE.search(body):
            saw_prompt = True
        elif not saw_prompt:
            return False  # no collision at all — the common case, and it costs one look

        # A prompt is up but the button is not clickable *yet*. Wait for it.
        #
        # This is where a whole 2752-file run died: the toast lands about 2.5 s after the
        # transfer starts, the button search gave up at 3 s, and the body check then found
        # the text that had just appeared and raised — half a second before a retry would
        # have worked. Every batch failed, nothing uploaded, and the report blamed a
        # missing selector that was in fact present.
        if waited >= _CONFLICT_WAIT_MS:
            break
        _settle(page, _CONFLICT_POLL_MS)
        waited += _CONFLICT_POLL_MS

    raise OneDriveUploadError(
        "OneDrive báo trùng tên file nhưng không tìm thấy nút “Thay thế” sau "
        f"{_CONFLICT_WAIT_MS // 1000}s. Nút trên trang: {_page_actions(page)}"
    )


def _send_files(page, paths: list[Path]) -> None:
    """Hand a batch to OneDrive, then confirm OneDrive took it.

    **The file chooser is the primary path here**, which is the opposite of
    `youtube_upload._send_file` and is forced by what OneDrive actually does: MEASURED,
    there is no `input[type=file]` in the DOM at all — zero, even with the menu open. One
    is created only when "Tải tệp lên" is clicked. So there is nothing to set until we
    have already been through the menu, and the interception is what catches it.

    Confirmed live: Playwright intercepts the chooser and it reports
    `is_multiple() == True`, so a whole batch goes in one call.

    Setting an existing input is kept as the *second* path, for the lazily-created input
    left behind by an earlier batch and in case a later build ships a static one.

    Never the folder uploader (`_UPLOAD_FOLDER_SEL`) — handing it these paths looks like
    it works and silently flattens the tree, which the manifest would then record as a
    success.

    Then verify the page reacted at all. A silent no-op that sails on is how the first
    live Studio run presented, and it would present identically here.
    """
    expected = {Path(p).name: Path(p).stat().st_size for p in paths}
    files = [str(p) for p in paths]

    chooser = getattr(page, "expect_file_chooser", None)
    if chooser is not None:
        last: Exception | None = None
        for attempt in range(_MENU_ATTEMPTS):
            # Clear whatever the previous batch left open before reaching for the menu.
            # MEASURED at scale: batch 1 succeeded and batch 2 failed with "mở được menu
            # nhưng không thấy mục Tải tệp lên" — the command-bar button was still toggled
            # open from the last round, so clicking it *closed* the menu instead.
            _dismiss_menu(page)
            if attempt:
                _settle(page, _MENU_RETRY_SETTLE_MS)
            try:
                with chooser(timeout=_CHOOSER_WAIT_MS) as chooser_info:
                    if not _click_any(
                        page, _NEW_MENU_SELS, _NEW_MENU_TEXTS, timeout_ms=10_000
                    ):
                        raise _MenuNotReady("không mở được menu “Tạo hoặc tải lên”")
                    _settle(page)
                    if not _click_any(
                        page, _UPLOAD_FILE_SELS, _UPLOAD_FILE_TEXTS, timeout_ms=10_000
                    ):
                        raise _MenuNotReady("không thấy mục “Tải tệp lên” trong menu")
                chooser_info.value.set_files(files)
                break
            except _MenuNotReady as exc:
                last = exc
                continue
            except Exception as exc:
                last = exc
                continue
        else:
            raise OneDriveUploadError(
                f"Không mở được menu tải lên sau {_MENU_ATTEMPTS} lần thử ({last}). "
                f"Nút trên trang: {_page_actions(page)}"
            )
        if _upload_started(page, expected):
            return

    for selector in _UPLOAD_INPUT_SELS:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="attached", timeout=5_000)
            locator.set_input_files(files)
        except Exception:
            continue
        if _upload_started(page, expected):
            return

    raise OneDriveUploadError(
        f"Đã chọn {len(files)} file nhưng OneDrive không bắt đầu tải lên. "
        f"Component trên trang: {_dom_inventory(page)}"
    )


def _upload_started(page, expected: dict[str, int], *, timeout_ms: int = _UPLOAD_ACCEPT_MS) -> bool:
    """True once OneDrive shows any sign it took the files.

    Three signals, any of which will do: a progress line, a conflict prompt, or a file
    that has reached its expected size.

    **Not "the name is in the listing".** That was the first version and it is vacuous
    for a replacement: the old copy is already there under the same name, so it returned
    True instantly whether or not a single byte moved — which is exactly how a silently
    skipped replace came to be reported as a successful upload.
    """
    waited = 0
    while waited < timeout_ms:
        if _status_text(page):
            return True
        try:
            if _CONFLICT_TEXT_RE.search(page.evaluate("() => document.body.innerText") or ""):
                return True  # OneDrive is asking about a collision, so it took the file
        except Exception:
            pass
        if not _not_yet_landed(page, expected):
            return True
        _settle(page, _UPLOAD_POLL_MS)
        waited += _UPLOAD_POLL_MS
    return False


_SIZE_UNITS = {
    "byte": 1, "bytes": 1, "b": 1,
    "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4,
}
_REMOTE_SIZE_RE = re.compile(r"([\d.,]+)\s*(byte|bytes|B|KB|MB|GB|TB)\b", re.IGNORECASE)
# OneDrive rounds anything above a kilobyte ("8,3 KB"), so an exact match is impossible
# there. 12% is comfortably wider than one rounding step and far tighter than the
# difference between an old file and a new one.
_SIZE_TOLERANCE = 0.12


def parse_remote_size(text: str) -> int | None:
    """Bytes from OneDrive's size cell — "5 byte", "8,3 KB" — or None if it isn't one.

    A folder's cell reads "30 mục" and yields None, which is correct: a folder has no
    size to compare. Pure, so it is tested.
    """
    match = _REMOTE_SIZE_RE.search(text or "")
    if not match:
        return None
    number = match.group(1).replace(".", "").replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    return int(value * _SIZE_UNITS[match.group(2).lower()])


def _remote_sizes(page) -> dict[str, int | None]:
    """`{name: size in bytes}` for the folder currently open. None where unparseable."""
    names = _texts_of(page, _ROW_NAME_SEL, keep_blank=True)
    sizes = _texts_of(page, _ROW_SIZE_SEL, keep_blank=True)
    if len(sizes) != len(names):
        sizes = [""] * len(names)
    return {
        name: parse_remote_size(size)
        for name, size in zip(names, sizes)
        if name and not _is_column_header(name)
    }


def _size_matches(remote: int | None, expected: int) -> bool:
    """Whether the copy on OneDrive is the size we sent. Unreadable size → assume yes.

    Assuming yes when we cannot read the cell is deliberate: the size check exists to
    catch a *silent no-op*, not to invent failures out of an unparsed string.
    """
    if remote is None:
        return True
    if expected == 0:
        return remote == 0
    return abs(remote - expected) <= max(expected * _SIZE_TOLERANCE, 1)


def _not_yet_landed(page, expected: dict[str, int]) -> list[str]:
    """Which of `expected` are absent, or present at the wrong size. The real check.

    **Presence alone is not evidence when a file is being replaced** — the old copy is
    already sitting there under the same name. Checking only the name is how a run came
    to report a file uploaded while OneDrive had quietly kept the previous bytes.
    """
    sizes = _remote_sizes(page)
    out = []
    for name, size in expected.items():
        found = next((v for k, v in sizes.items() if _names_match(k, name)), ...)
        if found is ... or not _size_matches(found, size):
            out.append(name)
    return out


def _verify_batch(page, expected: dict[str, int]) -> None:
    """Prove every file landed AT THE RIGHT SIZE, and that none was auto-renamed.

    Called before the manifest records anything as `done`. Every failure it catches would
    otherwise be written down as a success — and the size check is the one that matters
    most, because a replaced file is present under its own name either way.
    """
    names = list(expected)
    listed = _list_names(page)
    twins = [
        _renamed_twin(n)
        for n in names
        if any(_names_match(_renamed_twin(n), item) for item in listed)
    ]
    if twins:
        raise OneDriveUploadError(
            f"OneDrive đã tạo bản sao ({', '.join(twins[:3])}) thay vì ghi đè. "
            "Dừng lại để không để lại file trùng trên OneDrive.",
            relpath=twins[0],
        )
    stale = _not_yet_landed(page, expected)
    if stale:
        raise OneDriveUploadError(
            f"OneDrive không nhận đủ {len(stale)} file (thiếu, hoặc bản trên OneDrive "
            f"vẫn là bản cũ): {', '.join(stale[:5])}"
            + ("…" if len(stale) > 5 else ""),
            relpath=stale[0],
        )


def _wait_for_batch(
    page,
    expected: dict[str, int],
    *,
    timeout_ms: int,
    on_progress=None,
    on_landed=None,
    should_cancel=None,
    uploaded: int = 0,
) -> None:
    """Block until every file in the batch is on OneDrive **at the size we sent**.

    `expected` is `{filename: bytes}`, not a list of names, and that is the whole point.
    A replaced file is present under its own name from the moment the batch starts, so
    "the name is in the listing" says nothing — the first version returned immediately on
    a replacement and reported success while OneDrive still held the old bytes.

    Three layers, in increasing order of authority:

      1. the status text, for the line the user reads;
      2. the stall detector — no movement, meaning neither a changed status line nor a
         file reaching its expected size, for `_STALL_MS`. **This is the guard that
         actually fires on a dropped connection**, which is why the `timeout_ms` ceiling
         can afford to be generous;
      3. the sizes in the listing, which decide the batch is done.

    Cancellation is checked every poll, and the collision prompt is answered every poll:
    it is a toast that appears seconds after the transfer starts, and leaving it
    unanswered is exactly how a replace turns into a silent no-op.
    """
    waited = 0
    last_status = ""
    last_movement = 0
    done_at: int | None = None
    seen: set[str] = set()
    stall_ms = stall_ms_for(max(expected.values(), default=0))

    while waited < timeout_ms:
        _check_cancel(should_cancel, uploaded=uploaded)
        _resolve_conflicts(page)

        status = _status_text(page)
        if is_quota_error(status):
            raise OneDriveUploadError(
                "Tài khoản OneDrive đã hết dung lượng — cần dọn bớt hoặc nâng gói rồi "
                f"chạy lại. OneDrive báo: “{status}”",
                fatal=True,  # every remaining batch would fail identically
            )
        if status and status != last_status:
            last_status = status
            last_movement = waited
            _report(on_progress, status)

        landed = set(expected) - set(_not_yet_landed(page, expected))
        if landed - seen:
            seen |= landed
            last_movement = waited
            # Report inside the batch, not just at its end. MEASURED: a batch of 20 audio
            # files takes minutes and OneDrive's own status region stays empty, so without
            # this the progress bar sits on one number long enough to look frozen — which
            # is exactly how it was reported.
            if on_landed is not None:
                on_landed(len(seen), len(expected))
        if len(seen) >= len(expected):
            _verify_batch(page, expected)
            return

        finished, _count, _percent = upload_status(status)
        if finished and done_at is None:
            done_at = waited
        if done_at is not None and waited - done_at >= _POST_DONE_GRACE_MS:
            # The status says the transfer is over and the sizes disagree. `_verify_batch`
            # raises naming the files, which is the report worth having.
            _verify_batch(page, expected)
            return

        if waited - last_movement >= stall_ms:
            missing = _not_yet_landed(page, expected)
            raise OneDriveUploadError(
                f"OneDrive ngừng tiến triển ở “{last_status or 'không có thông báo'}” "
                f"trong {stall_ms // 60_000} phút, còn thiếu {len(missing)} file. "
                "Kiểm tra mạng rồi chạy lại — những file đã lên sẽ được bỏ qua.",
                relpath=missing[0] if missing else "",
            )

        _settle(page, _UPLOAD_POLL_MS)
        waited += _UPLOAD_POLL_MS

    missing = _not_yet_landed(page, expected)
    raise OneDriveUploadError(
        f"Quá thời gian chờ OneDrive nhận {len(expected)} file "
        f"(dừng ở “{last_status or 'không có thông báo'}”, còn thiếu {len(missing)}). "
        "Chạy lại — những file đã lên sẽ được bỏ qua.",
        relpath=missing[0] if missing else "",
    )


# -- the run ------------------------------------------------------------------

# Between batches: cheap, and the main thing separating "automation" from "hammering" in
# the eyes of whatever watches for it.
_BETWEEN_BATCHES_MS = 3_000
# Grace before tearing the browser down, so the last listing render settles.
_SETTLE_BEFORE_CLOSE_MS = 3_000


@dataclass
class PushRequest:
    """One novel's push. Everything the run needs, resolvable on the GUI thread."""

    project_path: Path
    novel_title: str
    force: bool = False  # ignore the manifest and re-send everything
    # The library-wide destination the user chose, e.g. "/Fox Novel". Passed in rather
    # than read from AppConfig so this module never imports the GUI's settings.
    root_folder: str = ""


@dataclass
class PushResult:
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_sent: int = 0
    remote_root: str = ""


@dataclass
class PushPreview:
    """What a push *would* do, worked out without opening a browser.

    The confirmation dialog is built from this. It has to state a file count and a total
    size, because the difference between "12 files, 4 GB" and "3 200 files, 61 GB" is the
    difference between a coffee break and an overnight run, and nothing else on screen
    tells the user which one they are about to start.
    """

    to_upload: list[PayloadItem] = field(default_factory=list)
    to_skip: list[PayloadItem] = field(default_factory=list)
    remote_root: str = ""
    manifest_note: str = ""
    # Set when this novel is already mirrored somewhere other than where the current
    # settings would put it — a destination changed after the first push. Advisory: the
    # recorded path still wins, and this is how the user finds out why.
    root_note: str = ""

    @property
    def upload_bytes(self) -> int:
        return total_bytes(self.to_upload)

    @property
    def skip_bytes(self) -> int:
        return total_bytes(self.to_skip)


def _resolve_remote_root(manifest: Manifest, title: str, root_folder: str = "") -> str:
    """Where this novel's mirror lives — the recorded path wins over the computed one.

    Renaming a novel inside the app must not strand an already-uploaded tree in an
    orphaned folder and start a second one beside it. The first push writes the path down
    and every later push honours it, so a rename changes what the app shows and nothing
    on OneDrive.

    That applies to changing the library-wide destination too: novels already mirrored
    stay where they are, and only novels never pushed go to the new root. Moving an
    existing tree is a thing only the user can decide to do — “Quên trạng thái” is the
    lever, and `PushPreview.root_note` is how they find out there is a difference at all.
    """
    return manifest.remote_root or remote_root_for(title, root_folder)


def _prepare(request: PushRequest, scratch: Path) -> tuple[list[PayloadItem], Manifest, str]:
    """Collect the payload, snapshot the database, and read the manifest.

    Shared by `preview_push` and `push_project` so the numbers the user confirms are the
    numbers the run acts on. In particular the snapshot happens here, before
    `plan_uploads`: the size that decides "has the database changed" has to be the
    snapshot's, because the snapshot is what actually goes up.
    """
    items = collect_payload(request.project_path)
    if any(item.relpath == DB_FILE for item in items):
        items = swap_in_database_snapshot(
            items, snapshot_database(request.project_path, scratch)
        )
    manifest = read_manifest(request.project_path)
    return items, manifest, _resolve_remote_root(
        manifest, request.novel_title, request.root_folder
    )


def preview_push(request: PushRequest) -> PushPreview:
    """What this push would send and what it would skip. No browser, no network."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="noveltrans-onedrive-") as scratch:
        items, manifest, remote_root = _prepare(request, Path(scratch))
        to_upload, to_skip = plan_uploads(items, manifest, force=request.force)
        would_be = remote_root_for(request.novel_title, request.root_folder)
        return PushPreview(
            to_upload=to_upload,
            to_skip=to_skip,
            remote_root=remote_root,
            manifest_note=manifest.note,
            root_note=(
                f"Truyện này đã được sao lưu ở {remote_root}, nên vẫn tiếp tục vào đó "
                f"(cài đặt hiện tại là {would_be}). Muốn chuyển sang chỗ mới thì bấm "
                "“Quên trạng thái” rồi sao lưu lại."
                if remote_root != would_be
                else ""
            ),
        )


def push_project(
    request: PushRequest,
    *,
    headless: bool = False,
    on_progress=None,
    on_file_done=None,
    should_cancel=None,
    on_checkpoint=None,
) -> PushResult:
    """Mirror a novel's project folder into `/NovelTrans/<tên truyện>/` on OneDrive.

    One browser for the whole run — the launch costs seconds and a login-upload-quit cycle
    per batch looks exactly like what it is.

    Callbacks, all optional:
      * `on_progress(done, total, message)` — files finished, files planned, status line
      * `on_file_done(relpath, error)` — per file; `error` is "" on success
      * `should_cancel()` — polled during transfers and between batches
      * `on_checkpoint()` — called between batches, for the pause gate. Deliberately not
        called mid-transfer: pausing there would mean holding a half-sent batch open.

    **A failed batch does not end the run.** Its files are reported through
    `on_file_done` and counted in `PushResult.failed`, and the next batch is attempted —
    a drifted selector in one folder should not cost the user the other fifty-nine
    gigabytes. The two exceptions are `needs_login` and an exhausted quota, both of which
    carry `fatal` because every remaining batch would fail for the same reason.
    """
    import tempfile

    result = PushResult()
    with tempfile.TemporaryDirectory(prefix="noveltrans-onedrive-") as scratch:
        items, manifest, remote_root = _prepare(request, Path(scratch))
        result.remote_root = remote_root
        manifest.remote_root = remote_root

        to_upload, to_skip = plan_uploads(items, manifest, force=request.force)
        result.skipped = len(to_skip)
        total = len(to_upload)
        if not total:
            # Nothing to do, so nothing is launched. A no-op push must not cost the user
            # a browser window and thirty seconds.
            _progress(on_progress, 0, 0, "Không có file nào cần tải lên — đã đồng bộ.")
            return result

        batches = batch_payload(to_upload)
        _progress(
            on_progress,
            0,
            total,
            f"Chuẩn bị tải {total} file ({format_size(total_bytes(to_upload))}) "
            f"lên {remote_root}",
        )

        sync_playwright = _require_playwright()
        playwright, context = _launch_context(sync_playwright, headless=headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _open_root(page)
            manifest.account = _account_name(page)
            root_segments = [s for s in remote_root.split("/") if s]

            done = 0
            # Where the browser is sitting. `[]` is the OneDrive root — which is exactly
            # where `_open_root` just left it, so the first batch walks down without
            # paying for a second page load. `None` means "unknown", which is what an
            # error leaves behind and which always forces a fresh walk from the root.
            here: list[str] | None = []
            for batch in batches:
                _check_cancel(should_cancel, uploaded=done)
                if on_checkpoint is not None:
                    on_checkpoint()

                segments = root_segments + folder_segments(batch[0].relpath)
                expected = {item.path.name: item.size for item in batch}
                label = batch[0].folder or "thư mục gốc"
                try:
                    # Only re-walk the tree when the destination actually changes.
                    # Batches arrive grouped by folder, so consecutive ones in the same
                    # folder cost nothing — which matters when `exports/audio` is two
                    # thousand files and a hundred batches.
                    if segments != here:
                        if here != []:  # not already known to be at the root
                            _open_root(page)
                        _ensure_path(page, segments)
                        # Newest-first, or a batch in a folder of thousands is invisible
                        # to `_wait_for_batch` and the run stalls. Once per folder, not
                        # per batch — the setting sticks while we stay put.
                        if not _sort_by_newest(page):
                            _progress(
                                on_progress, done, total,
                                f"⚠️ {label}: không đổi được thứ tự sắp xếp — thư mục "
                                "nhiều file có thể bị chậm.",
                            )
                        here = segments

                    _progress(
                        on_progress, done, total, f"⬆️ {label}: {len(batch)} file"
                    )
                    for item in batch:
                        manifest.mark_sending(item)
                    write_manifest(request.project_path, manifest)

                    _send_files(page, [item.path for item in batch])
                    _wait_for_batch(
                        page,
                        expected,
                        timeout_ms=batch_timeout_ms(total_bytes(batch)),
                        on_progress=lambda msg: _progress(on_progress, done, total, msg),
                        # `done + landed` is display-only: the manifest still records a
                        # file when its batch completes, so a crash mid-batch cannot leave
                        # a file recorded as done that is not.
                        on_landed=lambda n, of: _progress(
                            on_progress, done + n, total, f"⬆️ {label}: {n}/{of} file"
                        ),
                        should_cancel=should_cancel,
                        uploaded=done,
                    )
                except OneDriveCancelled:
                    write_manifest(request.project_path, manifest)
                    raise
                except OneDriveUploadError as exc:
                    # The browser is somewhere unknown now; force a re-walk next time.
                    here = None
                    if exc.fatal:
                        write_manifest(request.project_path, manifest)
                        raise
                    result.failed += len(batch)
                    for item in batch:
                        _file_done(on_file_done, item.relpath, str(exc))
                    _progress(on_progress, done, total, f"⚠️ {label}: {exc}")
                    write_manifest(request.project_path, manifest)
                    continue

                for item in batch:
                    manifest.mark_done(item)
                    result.uploaded += 1
                    result.bytes_sent += item.size
                    done += 1
                    _file_done(on_file_done, item.relpath, "")
                write_manifest(request.project_path, manifest)
                _progress(on_progress, done, total, f"✅ {label}")
                _settle(page, _BETWEEN_BATCHES_MS)

            _settle(page, _SETTLE_BEFORE_CLOSE_MS)
            return result
        finally:
            _close(context, playwright)


def _progress(on_progress, done: int, total: int, message: str) -> None:
    if on_progress is not None:
        on_progress(done, total, message)


def _file_done(on_file_done, relpath: str, error: str) -> None:
    if on_file_done is not None:
        on_file_done(relpath, error)

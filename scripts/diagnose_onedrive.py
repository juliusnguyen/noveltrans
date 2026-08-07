"""Report what the OneDrive web UI actually contains, before any selector is believed.

Every selector in `noveltrans/onedrive_upload.py` is currently a guess. Automating an
unseen UI cost feature 044 four rounds of guessing on one Studio page; this is the
observation tool that comes *first* this time. It opens OneDrive in the app's own
profile, prints what is there, tries each control, and says which ones moved.

    python scripts/diagnose_onedrive.py                 # read-only: look, report, leave
    python scripts/diagnose_onedrive.py --upload FILE   # also prove a file can go in

**Read-only by default.** It creates nothing, uploads nothing, and clicks only menus
(which it then closes with Escape). Nothing in your OneDrive changes.

`--upload` is the one exception, and it is deliberately not automatic about *where*:
folder navigation is not written yet (step 4), so rather than guess a path or drop a
test file in your root, the script hands the browser to you, waits for you to navigate
to a folder you don't mind a test file landing in, and only then feeds the file to the
input it found. It reports whether the file appeared in the listing.

Paste the whole output — the point is the parts that failed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from noveltrans.browser import close as _close
from noveltrans.onedrive_upload import (
    _CREATE_TEXTS,
    _FOLDER_NAME_INPUT_SELS,
    _KEEP_BOTH_TEXTS,
    _NAV_SETTLE_MS,
    _NAV_TIMEOUT_MS,
    _NEW_FOLDER_TEXTS,
    _LIST_HEADER_SEL,
    _NEW_FOLDER_SELS,
    _NEW_MENU_SELS,
    _NEW_MENU_TEXTS,
    _REPLACE_TEXTS,
    _ROOT_URLS,
    _ROW_NAME_SEL,
    _ROW_SEL,
    _SIGNED_IN_ACCOUNT_SELS,
    _UPLOAD_DIRECTORY_INPUT_SEL,
    _UPLOAD_FILE_SELS,
    _UPLOAD_FILE_TEXTS,
    _UPLOAD_FOLDER_SEL,
    _UPLOAD_INPUT_SELS,
    _UPLOAD_STATUS_SEL,
    _account_name,
    _current_folder,
    _dom_inventory,
    _is_logged_out,
    _launch_context,
    _page_actions,
    _require_playwright,
    flavour,
)


def _rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def _try_selector(page, label: str, selector: str) -> int:
    """Report whether a selector matches anything, and whether it is visible."""
    try:
        locator = page.locator(selector)
        count = locator.count()
    except Exception as exc:
        print(f"  {label:<52} ERROR {exc!r}")
        return 0
    visible = ""
    if count:
        try:
            visible = "  visible" if locator.first.is_visible(timeout=1_500) else "  hidden"
        except Exception:
            visible = "  (visibility unknown)"
    print(f"  {label:<52} matches={count}{visible}")
    return count


def _try_group(page, title: str, selectors) -> int:
    """Report a whole constant tuple, and return how many of its members matched."""
    print(f"\n{title}")
    if isinstance(selectors, str):
        selectors = [s.strip() for s in selectors.split(",")]
    total = 0
    for selector in selectors:
        total += _try_selector(page, selector, selector)
    return total


def _try_texts(page, title: str, texts) -> None:
    """Report which of a text tuple is actually on the page, as a button or otherwise."""
    print(f"\n{title}")
    for text in texts:
        _try_selector(page, f':text("{text}")', f':text("{text}")')
        _try_selector(page, f'button:has-text("{text}")', f'button:has-text("{text}")')




def _listing(page, limit: int = 20) -> list[str]:
    """The names currently shown in the file list."""
    for selector in (_ROW_NAME_SEL, _ROW_SEL):
        try:
            rows = page.locator(selector)
            names = [
                (rows.nth(i).inner_text(timeout=1_500) or "").strip().split("\n")[0]
                for i in range(min(rows.count(), limit))
            ]
            names = [n for n in names if n]
            if names:
                return names
        except Exception:
            continue
    return []


def _open_menu(page, label: str, selectors, texts) -> bool:
    """Click a command-bar menu and say which handle worked. Escape closes it after."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=2_000):
                locator.click()
                page.wait_for_timeout(1_200)
                print(f"  {label}: opened via selector  {selector}")
                return True
        except Exception:
            continue
    for text in texts:
        try:
            locator = page.locator(f'button:has-text("{text}")').first
            if locator.is_visible(timeout=2_000):
                locator.click()
                page.wait_for_timeout(1_200)
                print(f'  {label}: opened via text  "{text}"')
                return True
        except Exception:
            continue
    print(f"  {label}: NOTHING MATCHED — neither {list(selectors)} nor {list(texts)}")
    return False


def _click_any_menu(page) -> None:
    """Open “Tạo hoặc tải lên” then “Tải tệp lên”, the way `_send_files` does."""
    from noveltrans.onedrive_upload import _click_any

    if not _click_any(page, _NEW_MENU_SELS, _NEW_MENU_TEXTS, timeout_ms=10_000):
        raise RuntimeError("could not open the Create-or-upload menu")
    page.wait_for_timeout(1_800)
    if not _click_any(page, _UPLOAD_FILE_SELS, _UPLOAD_FILE_TEXTS, timeout_ms=10_000):
        raise RuntimeError("menu opened but 'Tải tệp lên' was not there")


def _dismiss(page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    except Exception:
        pass


def main(upload: Path | None) -> int:
    playwright, context = _launch_context(_require_playwright(), headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15_000)

        _rule("LANDING")
        print(f"opening {_ROOT_URLS[0]}")
        # `commit`, not `domcontentloaded` — MEASURED: onedrive.live.com does not fire
        # the latter within sixty seconds. A goto that times out here is reported, not
        # fatal: the page has usually navigated anyway and where it landed is the whole
        # point of this script.
        try:
            page.goto(_ROOT_URLS[0], wait_until="commit", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            print(f"  goto did not complete: {exc!r} — carrying on with where we landed")
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        page.wait_for_timeout(_NAV_SETTLE_MS)
        print(f"landed on   {page.url}")
        print(f"flavour()   {flavour(page.url) or '(neither — see below)'}")
        logged_out = _is_logged_out(page)
        print(f"logged out? {logged_out}")
        print(f"account     {_account_name(page) or '(could not read)'}")
        if logged_out:
            print(
                "\nThis profile has no OneDrive session. Sign in first — either in the\n"
                "app (Settings → Đăng nhập OneDrive) or right here in this window — then\n"
                "re-run. Everything below would be meaningless on a login page."
            )
            page.wait_for_timeout(4_000)
            return 1

        _rule("WHERE AM I")
        print(f"  current folder: {_current_folder(page) or '(không đọc được)'}")
        names = _listing(page)
        print(f"  listing ({len(names)} rows): {', '.join(names) or '(nothing read)'}")

        _rule("CLICKABLE LABELS ON THE PAGE")
        print(" ", _page_actions(page, limit=60))

        _rule("data-automationid HOOKS PRESENT")
        print(" ", _dom_inventory(page, limit=40))

        _rule("DO MY SELECTORS MATCH? (at rest, before any menu is opened)")
        _try_group(page, "-- account", _SIGNED_IN_ACCOUNT_SELS)
        _try_group(page, "-- current folder", (_LIST_HEADER_SEL,))
        _try_group(page, "-- rows", (_ROW_SEL, _ROW_NAME_SEL))
        _try_group(page, "-- new menu", _NEW_MENU_SELS)
        _try_group(page, "-- upload item", _UPLOAD_FILE_SELS)
        _try_group(page, "-- new folder item", _NEW_FOLDER_SELS)
        _try_group(page, "-- upload status", _UPLOAD_STATUS_SEL)
        found_input = _try_group(page, "-- file input", _UPLOAD_INPUT_SELS)
        print("\n-- the directory input we must NEVER use")
        dir_input = _try_selector(
            page, _UPLOAD_DIRECTORY_INPUT_SEL, _UPLOAD_DIRECTORY_INPUT_SEL
        )
        if dir_input:
            print(
                "  ^ present, as expected. `set_input_files` on THIS one loses the folder\n"
                "    tree silently (Playwright does not set webkitRelativePath), which is\n"
                "    why every upload selector carries :not([webkitdirectory])."
            )

        _rule("THE NEW ▸ FOLDER LADDER (menus only — nothing is created)")
        if _open_menu(page, "New", _NEW_MENU_SELS, _NEW_MENU_TEXTS):
            print("  after opening:", _page_actions(page, limit=40))
            _try_texts(page, "-- 'Folder' item", _NEW_FOLDER_TEXTS)
        _dismiss(page)
        print(
            "\n  NOT clicked: Folder / Create. The name box and Create button can only be\n"
            "  observed by actually creating a folder, so those stay guesses until the\n"
            "  first live run of step 4. For reference, the guesses are:"
        )
        for selector in _FOLDER_NAME_INPUT_SELS:
            print(f"    name box  {selector}")
        print(f"    create    {list(_CREATE_TEXTS)}")

        _rule("THE “TẠO HOẶC TẢI LÊN” ▸ “TẢI TỆP LÊN” LADDER (nothing is uploaded)")
        if _open_menu(page, "Create or upload", _NEW_MENU_SELS, _NEW_MENU_TEXTS):
            print("  after opening:", _page_actions(page, limit=40))
            _try_group(page, "-- 'Tải tệp lên' item", _UPLOAD_FILE_SELS)
            _try_texts(page, "-- by label", _UPLOAD_FILE_TEXTS)
            _try_group(page, "-- 'Thư mục' item", _NEW_FOLDER_SELS)
            print("\n-- the FOLDER uploader we must never use")
            _try_selector(page, _UPLOAD_FOLDER_SEL, _UPLOAD_FOLDER_SEL)
        _dismiss(page)

        _rule("VERDICT ON THE ONE STEP WITH NO ALTERNATIVE")
        # MEASURED: there is no `input[type=file]` in the DOM until "Tải tệp lên" is
        # clicked, so the real question is not "is an input there" but "can the chooser
        # be intercepted". That is what an upload actually depends on.
        chooser_ok = False
        try:
            with page.expect_file_chooser(timeout=20_000) as info:
                _click_any_menu(page)
            print(f"  PASS — file chooser intercepted (multiple={info.value.is_multiple()}).")
            print("  Nothing was selected, so nothing was uploaded.")
            chooser_ok = True
        except Exception as exc:
            print(f"  FAIL — could not reach a file chooser: {type(exc).__name__} "
                  f"{str(exc)[:120]}")
            print(
                "  Nothing else in this module matters if a file cannot go in. Paste the\n"
                "  'CLICKABLE LABELS' and 'data-automationid' sections above."
            )
        _dismiss(page)
        found_input = found_input or chooser_ok

        _rule("COLLISION PROMPT (for reference — only appears during an upload)")
        print("  MEASURED: it is a TOAST, not a modal. [class*=ms-Dialog],")
        print("  [class*=ms-Modal] and [role=dialog] all match ZERO while it is up,")
        print("  so `_resolve_conflicts` looks for the button itself, not a container.")
        print(f"  replace texts (we click these):   {list(_REPLACE_TEXTS)}")
        print(f"  keep-both texts (we refuse these): {list(_KEEP_BOTH_TEXTS)}")

        if upload is not None:
            _rule("LIVE UPLOAD TEST")
            print(f"  file: {upload}  ({upload.stat().st_size} bytes)")
            print(
                "\n  Folder navigation is not written yet, so this script will NOT choose\n"
                "  where the file goes. In the browser window: navigate to a folder you\n"
                "  don't mind a test file landing in."
            )
            try:
                input("  Then press Enter here (or Ctrl-C to skip)... ")
            except (EOFError, KeyboardInterrupt):
                print("\n  skipped.")
                return 0
            print(f"  current folder now: {_current_folder(page)}")
            before = set(_listing(page, limit=200))

            sent = False
            for selector in _UPLOAD_INPUT_SELS:
                try:
                    locator = page.locator(selector).first
                    locator.wait_for(state="attached", timeout=5_000)
                    locator.set_input_files(str(upload))
                except Exception as exc:
                    print(f"  {selector} -> {exc!r}")
                    continue
                print(f"  fed the file to  {selector}")
                sent = True
                break
            if not sent:
                print("  FAIL — could not hand the file to any input.")
                page.wait_for_timeout(4_000)
                return 1

            # Poll the listing rather than the status bar: the listing is what
            # `_wait_for_batch` will treat as the authority, so this is the thing worth
            # proving works.
            for _ in range(30):
                page.wait_for_timeout(2_000)
                try:
                    status = (
                        page.locator(_UPLOAD_STATUS_SEL).first.inner_text(timeout=1_500) or ""
                    ).strip()
                except Exception:
                    status = ""
                if status:
                    print(f"  status: {status}")
                if upload.name in set(_listing(page, limit=200)) - before:
                    print(f"  PASS — “{upload.name}” appeared in the listing.")
                    break
            else:
                print(
                    f"  FAIL — “{upload.name}” never appeared in the listing. Either the\n"
                    "  row selectors are wrong or the upload did not happen; the browser\n"
                    "  is still open, so look."
                )

        print("\nDone. Nothing was created or deleted." if upload is None else "\nDone.")
        print("Paste everything above.")
        page.wait_for_timeout(5_000)
        return 0
    finally:
        _close(context, playwright)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--upload",
        type=Path,
        default=None,
        metavar="FILE",
        help="also prove a file can be handed to the upload input (asks you where)",
    )
    args = parser.parse_args()
    if args.upload is not None and not args.upload.is_file():
        parser.error(f"not a file: {args.upload}")
    raise SystemExit(main(args.upload))

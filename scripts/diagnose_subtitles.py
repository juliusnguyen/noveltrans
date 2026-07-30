"""Report what YouTube Studio's Languages/subtitles page actually contains.

Automating a UI that cannot be inspected has cost four rounds of guessing on this one
surface. This is the observation tool that should have come first: it opens the page in the
app's own logged-in profile, prints what is there, then tries each step and says which one
fails and what the page looked like when it did.

    python scripts/diagnose_subtitles.py <video_id>
    python scripts/diagnose_subtitles.py oH5rCEq4J2c
    python scripts/diagnose_subtitles.py https://studio.youtube.com/video/oH5rCEq4J2c/translations

Read-only apart from the language dropdown, which it opens and then leaves alone — it never
presses Confirm, so nothing about the video changes.
"""

from __future__ import annotations

import sys

from noveltrans.youtube_upload import (
    _SUB_CONFIRM_TEXTS,
    _pick_vietnamese,
    _subtitle_language_gate,
    _SUB_LANG_GATE_SEL,
    _SUB_LANG_GATE_TEXTS,
    _SUBTITLE_URLS,
    _VIETNAMESE_TEXTS,
    _dom_inventory,
    _launch_context,
    _page_actions,
    _require_playwright,
)


def _rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _try_selector(page, label: str, selector: str) -> None:
    """Report whether a selector matches anything, and whether it is visible."""
    try:
        locator = page.locator(selector)
        count = locator.count()
    except Exception as exc:
        print(f"  {label:<34} ERROR {exc!r}")
        return
    visible = ""
    if count:
        try:
            visible = "  visible" if locator.first.is_visible(timeout=1_500) else "  hidden"
        except Exception:
            visible = "  (visibility unknown)"
    print(f"  {label:<34} matches={count}{visible}")


def main(video_id: str) -> int:
    playwright, context = _launch_context(_require_playwright(), headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15_000)

        url = (
            video_id
            if video_id.startswith("http")
            else _SUBTITLE_URLS[0].format(video_id=video_id)
        )
        print(f"opening {url}")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass
        page.wait_for_timeout(2_000)
        print(f"landed on {page.url}")

        _rule("CLICKABLE LABELS ON THE PAGE")
        print(" ", _page_actions(page, limit=60))

        _rule("COMPONENTS PRESENT")
        print(" ", _dom_inventory(page, limit=40))

        _rule("DO MY SELECTORS MATCH?")
        for part in _SUB_LANG_GATE_SEL.split(","):
            _try_selector(page, part.strip(), part.strip())
        for text in _SUB_LANG_GATE_TEXTS:
            _try_selector(page, f':text("{text}")', f':text("{text}")')
        for text in _SUB_CONFIRM_TEXTS:
            _try_selector(page, f'button:has-text("{text}")', f'button:has-text("{text}")')

        _rule("WHAT THE LANGUAGE CONTROL ACTUALLY IS")
        for text in _SUB_LANG_GATE_TEXTS:
            try:
                node = page.locator(f':text("{text}")').last
                if not node.is_visible(timeout=1_500):
                    continue
                info = node.evaluate(
                    """el => {
                        const p = [];
                        let n = el;
                        for (let i = 0; i < 5 && n; i++, n = n.parentElement)
                            p.push(n.tagName.toLowerCase()
                                   + (n.id ? '#' + n.id : '')
                                   + (n.className && typeof n.className === 'string'
                                      ? '.' + n.className.trim().split(/\\s+/)[0] : ''));
                        return p.join('  <  ');
                    }"""
                )
                print(f'  :text("{text}") -> {info}')
            except Exception as exc:
                print(f'  :text("{text}") -> could not inspect: {exc!r}')

        _rule("TRYING TO OPEN THE DROPDOWN")
        opened = False
        for text in _SUB_LANG_GATE_TEXTS:
            try:
                node = page.locator(f':text("{text}")').last
                if not node.is_visible(timeout=1_500):
                    continue
                node.click()
                page.wait_for_timeout(1_500)
                print(f'  clicked :text("{text}")')
                opened = True
                break
            except Exception as exc:
                print(f'  :text("{text}") click failed: {exc!r}')
        if not opened:
            print("  nothing clickable matched the gate texts")

        _rule("AFTER THE CLICK — what appeared")
        print(" ", _page_actions(page, limit=60))
        for text in _VIETNAMESE_TEXTS:
            _try_selector(page, f':text("{text}")', f':text("{text}")')

        _rule("VERIFYING THE FIX — can _pick_vietnamese select the language?")
        if _pick_vietnamese(page):
            print("  PASS — the language was selected in the dropdown.")
            print("  (Confirm is NOT pressed here, so the video is still unchanged.)")
        else:
            print("  FAIL — could not select the language. Paste this whole output.")
        page.wait_for_timeout(1_000)
        print(f"  gate still up: {_subtitle_language_gate(page)}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        print("\nDone. Nothing was confirmed — the video is unchanged.")
        print("Paste everything above.")
        page.wait_for_timeout(4_000)
        return 0
    finally:
        from noveltrans.browser import close as _close

        _close(context, playwright)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))

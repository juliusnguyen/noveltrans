"""Solve 69shuba's Cloudflare challenge in a real browser, then capture test fixtures.

This is the BLOCKING first step of feature 015 (see changes/015-69SHUBA-SOURCE/
015.01-INITIAL-PLAN.md §2): the site's HTML has never been seen, so no adapter
selectors can be written until this runs and its output is read.

It does two jobs:

1. **Tests the hybrid design (plan §5 R1).** After the browser solves the challenge,
   this replays the harvested cf_clearance cookie + User-Agent over *plain requests*.
   If that GET returns content, the hybrid works and bulk chapter fetching can go over
   fast requests. If it returns another challenge, Cloudflare is binding clearance to
   Chrome's TLS fingerprint (which requests cannot reproduce) and the whole design must
   fall back to always-Playwright. Better to learn that here than after building an
   adapter on the assumption.

2. **Captures fixtures as raw bytes** over that requests path — deliberately NOT via
   page.content(), which returns already-decoded text and would bake a UTF-8 fixture
   that never exercises the GBK decode path we most expect to get wrong.

Everything it prints is answering the "MUST verify" checklist in plan §6.

Run:  .venv/bin/python scripts/capture_69shuba.py [book_url]

Opens a visible Chrome window; click the Cloudflare checkbox if prompted. Requests are
made from your IP — the polite delay stays on, keep this low-frequency.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

from noveltrans.browser import close, launch_persistent_context, require_playwright
from noveltrans.storage.library import DEFAULT_LIBRARY_DIR

BOOK_URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.69shuba.com/book/59024/"

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "69shuba"
PROFILE_DIR = DEFAULT_LIBRARY_DIR / ".cf-profile"

# Markers that say "this is the interstitial, not the book page".
INTERSTITIAL_MARKERS = (
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "__cf_chl",
    "Just a moment",
    "Enable JavaScript and cookies to continue",
)

CLEAR_TIMEOUT_MS = 180_000  # generous: a human may need to click a checkbox
POLL_MS = 1_000


def looks_like_interstitial(text: str) -> bool:
    return any(marker in text for marker in INTERSTITIAL_MARKERS)


def wait_for_clearance(context, page) -> bool:
    """Poll until the page stops being the challenge interstitial."""
    waited = 0
    while waited < CLEAR_TIMEOUT_MS:
        try:
            content = page.content()
        except Exception:  # mid-navigation
            content = ""
        if content and not looks_like_interstitial(content):
            return True
        page.wait_for_timeout(POLL_MS)
        waited += POLL_MS
        if waited % 10_000 == 0:
            print(f"  … still on the challenge after {waited // 1000}s")
    return False


def cookie_string(context, host_needle: str = "69shu") -> str:
    """Join the site's cookies into a Cookie header for HttpClient.set_cookies."""
    pairs = [
        f"{c['name']}={c['value']}"
        for c in context.cookies()
        if host_needle in c.get("domain", "")
    ]
    return "; ".join(pairs)


def report_bytes(label: str, response: requests.Response) -> None:
    """Print everything plan §6 asks about encoding for one response."""
    raw = response.content
    print(f"\n--- {label}: {response.url}")
    print(f"  status               : {response.status_code}")
    print(f"  cf-mitigated header  : {response.headers.get('cf-mitigated', '(absent)')}")
    print(f"  Content-Type         : {response.headers.get('Content-Type', '(absent)')}")
    print(f"  bytes                : {len(raw)}")
    print(f"  requests .encoding   : {response.encoding}")
    print(f"  .apparent_encoding   : {response.apparent_encoding}")

    head = raw[:1024].lower()
    for needle in (b"charset=utf-8", b"charset=gbk", b"charset=gb2312", b"charset=gb18030"):
        if needle in head:
            print(f"  meta declares        : {needle.decode()}")

    for codec in ("gb18030", "utf-8"):
        try:
            decoded = raw.decode(codec)
        except UnicodeDecodeError as exc:
            print(f"  decode {codec:<9}     : FAILS ({exc.reason})")
            continue
        # Mojibake check: a correct decode of a CN page has plenty of CJK.
        cjk = sum(1 for ch in decoded if "一" <= ch <= "鿿")
        print(f"  decode {codec:<9}     : ok, {cjk} CJK chars")


def save(name: str, raw: bytes) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_bytes(raw)
    print(f"  saved -> {path.relative_to(Path.cwd())} ({len(raw)} bytes)")


# Real chapter links are /txt/<book_id>/<chapter_id>. Match that shape exactly:
# a loose "/article/ in href" test picks up /modules/article/bookcase.php (the
# "add to bookcase" link), which bounces to a login page.
_CHAPTER_HREF_RE = re.compile(r"/txt/\d+/\d+")


def first_chapter_url(markup, base_url: str) -> str:
    """Find the first real chapter link so we can capture one too."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")  # bs4 sniffs the meta charset itself
    for a in soup.select("a[href]"):
        if _CHAPTER_HREF_RE.search(a["href"]):
            return urljoin(base_url, a["href"])
    return ""


def survey_links(markup, base_url: str) -> None:
    """Print the link shapes on the page — answers plan §6 items 4-7 (URL shapes)."""
    from collections import Counter
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    hrefs = [a["href"] for a in soup.select("a[href]")]
    shapes = Counter()
    for href in hrefs:
        path = urljoin(base_url, href).split("://", 1)[-1]
        parts = [p for p in path.split("/")[1:] if p]
        shape = "/".join("<num>" if p.split(".")[0].isdigit() else p for p in parts[:2])
        shapes[f"/{shape}"] += 1
    print(f"\n  link shapes on the page ({len(hrefs)} links):")
    for shape, count in shapes.most_common(12):
        print(f"    {count:>4}x  {shape}")


def save_text(name: str, markup: str) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_text(markup, encoding="utf-8")
    print(f"  saved -> {path.relative_to(Path.cwd())} ({len(markup)} chars)")


def goto_and_capture(context, page, url: str, name: str) -> str:
    """Navigate, wait out any challenge, save page.content() as a fixture."""
    page.goto(url, wait_until="domcontentloaded")
    if not wait_for_clearance(context, page):
        print(f"! {name}: stuck on the challenge, not captured.")
        return ""
    markup = page.content()
    print(f"\n  {name}  <- {url}")
    print(f"  title: {page.title()!r}")
    save_text(name, markup)
    return markup


def capture_via_browser(context, page, toc_markup: str) -> None:
    """Fallback capture when R1 fails: the browser IS the fetch path, so fixtures are
    page.content() — already-decoded UTF-8, exactly what the adapter will receive.

    The plan warned against this precisely because it hides the GBK decode path. That
    warning assumed the hybrid; with always-Playwright, Chrome owns the decoding and
    the adapter never sees bytes, so UTF-8 fixtures are now the *honest* ones.

    Three fixtures, because 69shuba splits what other sites keep on one page:
      novel.html   /book/<id>.htm  — the info page (title/author/cover/description)
      toc.html     /book/<id>/     — the chapter list
      chapter.html /txt/<id>/<cid> — one chapter body
    """
    print("\n" + "=" * 70)
    print("FALLBACK — capturing via the browser (always-Playwright path)")
    print("=" * 70)

    survey_links(toc_markup, BOOK_URL)
    save_text("toc.html", toc_markup)

    match = re.search(r"/book/(\d+)", BOOK_URL)
    if match:
        info_url = urljoin(BOOK_URL, f"/book/{match.group(1)}.htm")
        goto_and_capture(context, page, info_url, "novel.html")
    else:
        print("! Could not derive the /book/<id>.htm info URL from the book URL.")

    chapter_url = first_chapter_url(toc_markup, BOOK_URL)
    if not chapter_url:
        print("\n! No /txt/<id>/<cid> chapter link found — check the link shapes above.")
        return
    goto_and_capture(context, page, chapter_url, "chapter.html")
    print(f"\n  (chapter URL shape: {chapter_url})")


def main() -> None:
    print(f"book url    : {BOOK_URL}")
    print(f"profile dir : {PROFILE_DIR}")
    print("\nLaunching a visible browser — click the Cloudflare checkbox if it asks.\n")

    sync_playwright = require_playwright()
    playwright, context = launch_persistent_context(sync_playwright, PROFILE_DIR, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BOOK_URL, wait_until="domcontentloaded")

        if not wait_for_clearance(context, page):
            print("\n=> TIMED OUT on the Cloudflare challenge. Nothing captured.")
            return

        print("=> Challenge cleared in the browser.")
        print(f"   page title: {page.title()!r}")

        user_agent = page.evaluate("() => navigator.userAgent")
        cookies = cookie_string(context)
        names = [c.split("=", 1)[0] for c in cookies.split("; ") if c]
        print(f"\n   harvested UA     : {user_agent}")
        print(f"   harvested cookies: {len(names)} -> {', '.join(names) or '(none)'}")
        print(f"   cf_clearance     : {'YES' if 'cf_clearance' in names else 'NO'}")
        book_markup = page.content()

        # --- The R1 test: does that clearance survive outside the browser? ---
        print("\n" + "=" * 70)
        print("R1 TEST — replaying the clearance over plain requests")
        print("=" * 70)

        session = requests.Session()
        session.headers["User-Agent"] = user_agent
        if cookies:
            session.headers["Cookie"] = cookies

        book = session.get(BOOK_URL, timeout=30)
        report_bytes("BOOK PAGE (via requests)", book)

        if book.status_code == 200 and not looks_like_interstitial(book.text[:4000]):
            print("\n=> R1 PASSED: the clearance works over plain requests. Hybrid viable.")
            survey_links(book.content, BOOK_URL)
            save("novel.html", book.content)
            chapter_url = first_chapter_url(book.content, BOOK_URL)
            if chapter_url:
                chapter = session.get(chapter_url, timeout=30)
                report_bytes("CHAPTER PAGE (via requests)", chapter)
                if chapter.status_code == 200:
                    save("chapter.html", chapter.content)
            return

        print("\n=> R1 FAILED: requests is challenged even with the browser's UA/cookies.")
        if "cf_clearance" not in names:
            print("   Note: no cf_clearance cookie was issued AT ALL — real Chrome was")
            print("   passed silently on fingerprint, so there is no token to replay.")
            print("   Cloudflare is gating on TLS/browser fingerprint, which requests")
            print("   cannot reproduce. The hybrid is not fixable by copying headers.")
        print("   => Falling back to always-Playwright in _get_html (plan §1).")

        capture_via_browser(context, page, book_markup)
    finally:
        close(context, playwright)
        print("\n(browser closed)")


if __name__ == "__main__":
    main()

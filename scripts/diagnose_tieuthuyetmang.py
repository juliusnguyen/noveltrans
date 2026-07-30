"""Report what tieuthuyetmang.com actually serves, using your own logged-in session.

The observation tool, written before the interaction rather than after four rounds of
guessing — the lesson feature 044 paid for twice. Everything the adapter could work out
from the landing page is already built; what it could NOT observe is what a *reader* page
returns, because that needs a logged-in account. This answers it.

    .venv/bin/python scripts/diagnose_tieuthuyetmang.py <novel-url>
    .venv/bin/python scripts/diagnose_tieuthuyetmang.py <novel-url> --chapter <chapter-url>

Read-only: it fetches pages and prints what they contain. Nothing is written, no account
state is touched, and no locked chapter is opened by any means other than your own cookie.

Paste the whole output back. The four questions it exists to answer:

  Q1  is the reader URL really /truyen/<slug>/doc/<chapterNumber>?
  Q2  does a chapter's TEXT arrive in the flight stream, in server-rendered markup,
      or from a separate /api/ call?
  Q3  which single field differs between an unlocked chapter and a locked one?
  Q4  is "no cookie" distinguishable from "logged in but has not unlocked it"?
"""

from __future__ import annotations

import argparse
import json
import re
from urllib.parse import unquote

from noveltrans.config import AppConfig
from noveltrans.scrapers.base import HttpClient
from noveltrans.scrapers.tieuthuyetmang import (
    chapter_entries,
    chapter_url,
    find_story,
    flight_payload,
    iter_objects,
    landing_url,
    slug,
)


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def describe(markup: str) -> tuple[str, dict]:
    """(flight stream, quick census) for one fetched page."""
    stream = flight_payload(markup)
    return stream, {
        "html chars": len(markup),
        "__next_f pushes": markup.count("__next_f.push"),
        "flight chars": len(stream),
    }


def longest_string(stream: str) -> tuple[int, str]:
    """The longest JSON string value anywhere in the stream, and its length.

    This is the Q2 probe: a chapter whose text is server-side rendered into the flight
    data shows up here as a multi-thousand-character run. One that is fetched later by
    JavaScript does not.
    """
    best = ""
    for match in re.finditer(r'"', stream):
        try:
            value, _ = json.JSONDecoder().raw_decode(stream, match.start())
        except ValueError:
            continue
        if isinstance(value, str) and len(value) > len(best):
            best = value
    return len(best), best[:160]


def _top_strings(stream: str, count: int) -> list[tuple[int, str]]:
    """The longest JSON string values in the stream, longest first."""
    decoder = json.JSONDecoder()
    found: dict[str, int] = {}
    for match in re.finditer(r'"', stream):
        try:
            value, _ = decoder.raw_decode(stream, match.start())
        except ValueError:
            continue
        if isinstance(value, str) and len(value) > 40:
            found[value[:120]] = len(value)
    ranked = sorted(found.items(), key=lambda pair: -pair[1])[:count]
    return [(size, sample) for sample, size in ranked]


def _text_blocks(markup: str, count: int) -> list[tuple[int, str]]:
    """The elements holding the most text, as `tag#id.class` with their text length.

    If a chapter body is server-rendered under something other than `<article>`, it shows
    up here as an obvious outlier and names its own selector.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    blocks: list[tuple[int, str]] = []
    for el in soup.find_all(["article", "div", "section", "main", "p"]):
        text = el.get_text(" ", strip=True)
        if len(text) < 200:
            continue
        classes = el.get("class") or []
        name = el.name + (f"#{el['id']}" if el.get("id") else "")
        if classes:
            name += "." + ".".join(classes[:3])
        blocks.append((len(text), name))
    blocks.sort(key=lambda pair: -pair[0])
    return blocks[:count]


def _shape(entry: dict) -> str:
    """A chapter object with long values replaced by their length — keys are the point."""
    return json.dumps(
        {
            key: (f"<{len(value)} chars>" if isinstance(value, str) and len(value) > 60 else value)
            for key, value in entry.items()
        },
        ensure_ascii=False,
    )


def flags_in(markup: str) -> dict[str, bool]:
    return {
        name: f'"{name}":true' in markup
        for name in ("isLocked", "isFree", "isPreview", "hasAudio", "audioLocked")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Either form works, and either one alone is enough — a chapter URL contains the
    # novel slug, so `--chapter <url>` on its own is a complete command. That matters:
    # it is the exact command `parse_chapter` prints when it cannot read a chapter, and
    # a printed command that then fails on a missing argument is worse than no command.
    parser.add_argument(
        "novel", nargs="?", default="",
        help="a novel URL, e.g. https://tieuthuyetmang.com/truyen/<slug>",
    )
    parser.add_argument("--chapter", default="", help="a chapter URL copied from your browser")
    args = parser.parse_args()
    if not args.novel and not args.chapter:
        parser.error("give a novel URL, a --chapter URL, or both")
    if not args.novel:
        args.novel = args.chapter

    cookies = AppConfig().tieuthuyetmang_cookies
    print(f"stored cookie: {len(cookies)} chars", end="")
    if cookies:
        names = sorted({c.split("=", 1)[0].strip() for c in cookies.split(";") if "=" in c})
        print(f", {len(names)} cookie name(s): {', '.join(names)}")
    else:
        print("  <-- EMPTY. Paste your logged-in Cookie header in Cài đặt first.")
        print("     (The landing-page sections below still work; the chapter ones will not.)")

    client = HttpClient(delay_seconds=2.0, cookies=cookies)
    novel_slug = slug(args.novel)
    landing = landing_url(args.novel)

    rule("LANDING PAGE")
    markup = client.get_html(landing)
    stream, census = describe(markup)
    print(f"  {landing}")
    for key, value in census.items():
        print(f"  {key:<20} {value}")

    rule("Q5 — STORY ANCHORING: every story object in the stream, in order")
    print("  (the page's own story must be picked by SLUG — the first one is a decoy)")
    for at, obj in iter_objects(stream, '"chapters_count"', ("title", "chapters_count")):
        mark = "  <== the one we anchor on" if obj.get("slug") == novel_slug else ""
        print(
            f"  @{at:>6}  slug={obj.get('slug')!r:<40} "
            f"chapters_count={obj.get('chapters_count')}{mark}"
        )
    story = find_story(stream, novel_slug, landing)
    print(f"\n  anchored: {story.get('title')!r}  chapters_count={story.get('chapters_count')}")

    rule("CHAPTER CENSUS")
    entries = chapter_entries(stream)
    numbers = [entry["chapterNumber"] for entry in entries]
    locked = [entry for entry in entries if entry.get("isLocked")]
    print(f"  chapter objects      {len(entries)}")
    print(f"  numbers              {min(numbers, default=0)}..{max(numbers, default=0)}")
    print(f"  gaps                 {sorted(set(range(1, max(numbers, default=0) + 1)) - set(numbers))[:20]}")
    print(f"  locked               {len(locked)} of {len(entries)}")
    print(f"  chapters_count says  {story.get('chapters_count')}   (Q9: equal to the count above?)")
    print("\n  first three objects:")
    for entry in entries[:3]:
        print(f"    {json.dumps(entry, ensure_ascii=False)}")

    rule("Q1 — READER URL")
    print("  measured from the site's own route chunk; verifying it still holds:")
    for entry in entries[:2] + locked[:1]:
        number = entry["chapterNumber"]
        url = chapter_url(novel_slug, number)
        try:
            response = client.get(url)
            status, size = response.status_code, len(response.text)
        except Exception as exc:  # noqa: BLE001 — a probe, any failure is the answer
            status, size = f"ERROR {exc!r}", 0
        print(f"    chương {number:<4} locked={bool(entry.get('isLocked')):<5} -> {status}  {size} bytes  {url}")

    if not args.chapter and entries:
        args.chapter = chapter_url(novel_slug, entries[0]["chapterNumber"])
        print(f"\n  no --chapter given; using the first chapter: {args.chapter}")

    if args.chapter:
        rule("Q2/Q3/Q4 — WHAT A READER PAGE CONTAINS")
        pairs = [("as you (cookie sent)", client)]
        if cookies:
            pairs.append(("anonymous (no cookie)", HttpClient(delay_seconds=2.0)))
        for label, session in pairs:
            markup = session.get_html(args.chapter)
            stream, census = describe(markup)
            length, preview = longest_string(stream)
            print(f"\n  --- {label} ---")
            for key, value in census.items():
                print(f"    {key:<20} {value}")
            print(f"    flags true         {[k for k, v in flags_in(markup).items() if v]}")
            print(f"    <article> present  {'<article' in markup}")
            print(f"    <p> count          {markup.count('<p')}")
            print(f"    longest string     {length} chars")
            print(f"    ...starts          {preview!r}")
            entry = next(
                (e for e in chapter_entries(stream)
                 if e["chapterNumber"] == int(re.search(r'/doc/(\d+)', args.chapter).group(1))),
                {},
            )
            print(f"    chapter object     {_shape(entry)}")

            # The failure this section exists to diagnose: the text is in neither the
            # <article> nor the chapter object. Whatever IS on the page is then the
            # answer, so print the candidates rather than another guess.
            print("    longest strings in the stream:")
            for size, sample in _top_strings(stream, 5):
                print(f"      {size:>6}  {sample!r}")
            print("    text-densest elements:")
            for size, where in _text_blocks(markup, 5):
                print(f"      {size:>6}  {where}")
            apis = sorted(set(re.findall(r'"(/api/[^"]{0,80})"', markup)))
            print(f"    /api/ literals     {apis}")
            chunks = sorted(set(re.findall(r'"(/_next/static/chunks/app/[^"]+)"', markup)))
            print("    route chunks       (grep these for the fetch that loads the text)")
            for chunk in chunks:
                print(f"      {unquote(chunk)}")

    rule("DONE")
    print("Paste everything above. Nothing was written and no chapter was unlocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

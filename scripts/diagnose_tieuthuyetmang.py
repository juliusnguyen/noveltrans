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


# --- audio probes (Step 10 / Q6-Q9) -------------------------------------------------

_MEDIA_RE = re.compile(r'"(https?://[^"]{0,300}?\.(?:mp3|m4a|aac|ogg|opus|wav|m3u8|mpd)[^"]{0,200})"')
_REL_MEDIA_RE = re.compile(r'"(/[^"]{0,200}?\.(?:mp3|m4a|aac|ogg|opus|wav|m3u8|mpd)[^"]{0,200})"')
_SIGNED_HINTS = ("token", "expires", "expiry", "signature", "sig=", "X-Amz-", "hmac", "policy")


def audio_url(novel_slug: str, number: int) -> str:
    """The listen URL. INFERRED from the adapter docstring, not measured like `doc`.

    If every probe below 404s, that guess is the first thing to doubt — grep the route
    chunks printed above for the real one rather than trying more shapes by hand.
    """
    return f"https://tieuthuyetmang.com/truyen/{novel_slug}/nghe/{number}"


def audio_keys(entries: list[dict]) -> dict[str, list]:
    """Every key any chapter object carries whose name looks audio-ish, with its values.

    Deliberately not a fixed list of guessed names: Q6's real question is what the site
    *actually* calls it, and a key nobody guessed is exactly the finding worth having.
    """
    found: dict[str, list] = {}
    for entry in entries:
        for key, value in entry.items():
            if any(word in key.lower() for word in ("audio", "voice", "nghe", "mp3", "sound", "listen")):
                found.setdefault(key, []).append(value)
    return found


def media_candidates(markup: str, stream: str) -> list[str]:
    """Every media-looking URL on the page, absolute first. Q7's answer, or its absence."""
    urls: list[str] = []
    for text in (markup, stream):
        urls += _MEDIA_RE.findall(text)
    for text in (markup, stream):
        urls += [u for u in _REL_MEDIA_RE.findall(text) if not u.startswith("/_next/static")]
    seen: dict[str, None] = {}
    for url in urls:
        # These URLs are lifted out of JSON string literals, so the closing quote arrives
        # escaped and the capture keeps the backslash. Left on, every probe 404s against a
        # URL the site never served — strip it before the URL is ever used.
        cleaned = url.replace("\\u0026", "&").replace("\\/", "/").rstrip("\\")
        seen.setdefault(cleaned, None)
    return list(seen)


def audio_elements(markup: str) -> list[str]:
    """`<audio>` / `<source>` tags, which discovery tier 2 would key off."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    out = []
    for el in soup.find_all(["audio", "source"]):
        attrs = {k: (v if len(str(v)) < 200 else f"<{len(str(v))} chars>") for k, v in el.attrs.items()}
        out.append(f"<{el.name} {attrs}>")
    return out


def probe_media(client, url: str) -> None:
    """HEAD the media URL: format, size, and whether it is signed. Zero bytes fetched.

    Falls back to a 1-byte ranged GET where HEAD is refused — still effectively nothing,
    and this is a small paid site (see the adapter docstring on politeness).
    """
    print(f"    probing  {url[:160]}")
    signed = [hint for hint in _SIGNED_HINTS if hint.lower() in url.lower()]
    print(f"      signed-URL hints   {signed or 'none'}"
          + ("   <-- discovery and download must stay per-item, never batched" if signed else ""))
    response = None
    try:
        response = client._request("HEAD", url)
    except Exception as exc:  # noqa: BLE001 — a probe; failure is itself the answer
        print(f"      HEAD               refused ({exc!r:.120}); retrying as 1-byte GET")
        try:
            response = client.get(url, headers={"Range": "bytes=0-0"}, stream=True)
        except Exception as exc2:  # noqa: BLE001
            print(f"      GET                ERROR {exc2!r:.160}")
    if response is None:
        return
    interesting = ("content-type", "content-length", "content-range", "accept-ranges", "location")
    print(f"      status             {response.status_code}")
    for header in interesting:
        if header in response.headers:
            print(f"      {header:<18} {response.headers[header]}")

    if ".m3u8" in url.lower():
        # Q-3 in the plan's risk list: HLS is fine, HLS+DRM stops the feature dead.
        try:
            playlist = client.get_html(url)
        except Exception as exc:  # noqa: BLE001
            print(f"      playlist           ERROR {exc!r:.160}")
            return
        keys = re.findall(r"#EXT-X-KEY:[^\n]*", playlist)
        print(f"      playlist lines     {len(playlist.splitlines())}")
        print(f"      #EXT-X-KEY         {keys or 'none — plain HLS, -c copy will work'}")
        if any("SAMPLE-AES" in k or "widevine" in k.lower() or "fairplay" in k.lower() for k in keys):
            print("      *** DRM markers present — per plan §5.3 the feature STOPS here. ***")


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

        rule("Q6 — WHAT THE TOC SAYS ABOUT AUDIO")
        keys = audio_keys(entries)
        if not keys:
            print("  no chapter-object key mentions audio at all.")
            print("  => audio is NOT addressed through the TOC; plan §5.4's second reading holds")
            print("     and the 21 audio items need their own id space. Data model needs rework.")
        for key, values in keys.items():
            truthy = [v for v in values if v not in (None, False, "", 0)]
            print(f"  {key:<20} present on {len(values)}/{len(entries)} rows, truthy on {len(truthy)}")
            print(f"  {'':<20} distinct values: {sorted({repr(v)[:40] for v in values})[:8]}")
        with_audio = [e for e in entries if any(
            e.get(k) not in (None, False, "", 0) for k in keys
        )]
        print(f"\n  chapters that look audio-bearing: {len(with_audio)}"
              f"   (plan expected 21 of {len(entries)})")
        if with_audio:
            print(f"  their numbers: {[e['chapterNumber'] for e in with_audio][:30]}")

        rule("Q7/Q8/Q9 — THE LISTEN PAGE")
        targets = [e["chapterNumber"] for e in with_audio[:1]] or [entries[0]["chapterNumber"]]
        if entries and entries[0]["chapterNumber"] not in targets:
            targets.append(entries[0]["chapterNumber"])
        for number in targets[:2]:
            url = audio_url(novel_slug, number)
            sessions = [("as you (cookie sent)", client)]
            if cookies:
                sessions.append(("anonymous (no cookie)", HttpClient(delay_seconds=2.0)))
            for label, session in sessions:
                print(f"\n  --- chương {number}, {label} ---")
                print(f"      {url}")
                try:
                    markup = session.get_html(url)
                except Exception as exc:  # noqa: BLE001 — a probe; failure is the answer
                    print(f"      ERROR {exc!r:.200}")
                    continue
                stream, census = describe(markup)
                for key, value in census.items():
                    print(f"      {key:<20} {value}")
                print(f"      flags true         {[k for k, v in flags_in(markup).items() if v]}")
                print(f"      <audio> elements   {audio_elements(markup) or 'none'}")
                apis = sorted(set(re.findall(r'"(/api/[^"]{0,80})"', markup)))
                print(f"      /api/ literals     {apis}")
                found = media_candidates(markup, stream)
                print(f"      media URLs         {len(found)}")
                for candidate in found[:5]:
                    print(f"        {candidate[:180]}")
                if not found:
                    print("        none in HTML or flight stream => the URL arrives from a")
                    print("        client-side XHR; grep the route chunks above for the fetch.")
                if found and label.startswith("as you"):
                    probe_media(session, found[0])

    rule("DONE")
    print("Paste everything above. Nothing was written and no chapter was unlocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

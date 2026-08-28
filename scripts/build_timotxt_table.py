"""Recover timotxt.com's Hangul→Han substitution table by diffing repeat fetches.

**Why this exists.** timotxt serves every chapter body with roughly 2–5% of its characters
replaced by visually-similar Hangul syllables (U+AC00–U+D7A3), server-side. The substitution
is NOT a webfont trick — there is no `@font-face` anywhere on the page, so the corruption is
in the delivered text and no browser renders it correctly either. Left alone it would put
60–120 junk characters into every chapter, which the translator turns into garbage and TTS
reads aloud as Korean.

**What makes it recoverable.** The table is fixed; only *which* of its entries get applied is
randomised per response. So two fetches of the same URL return the same paragraphs, at the
same lengths, with different subsets substituted — and aligning them positionally reveals
each mapping wherever one side happens to be clean.

    fetch A:  …a놖b…      fetch B:  …a我b…      ⇒  놖 → 我

**Conflicts are the correctness check.** If any Hangul syllable ever resolved to two
different Han characters, the "fixed table" premise would be wrong and the whole approach
invalid. This script asserts zero conflicts and reports the count; a non-zero conflict count
means STOP, do not ship the table.

This is a one-off build tool, not part of the shipped code path. Its output is pasted into
`src/noveltrans/scrapers/timotxt.py` with a provenance comment. Re-run it if the live drift
test (`TestLive::test_the_deobfuscation_table_has_not_drifted`) ever starts reporting
residue.

Only character mappings are collected — no chapter prose is written anywhere.

Usage:
    .venv/bin/python scripts/build_timotxt_table.py [--chapters N] [--delay SECONDS]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter

import requests
from bs4 import BeautifulSoup

ORIGIN = "https://www.timotxt.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HANGUL = re.compile(r"[가-힣]")

# The reference novel from the feature request, plus a second one so the table can be
# confirmed site-wide rather than per-novel — if the two disagree, the whole design is wrong.
PRIMARY = "2608569069"
SECONDARY = ""  # filled by --second, optional (comma-separated for several)


def paragraphs(markup: str) -> list[str]:
    """The chapter's prose paragraphs, in order — the same extraction the adapter uses."""
    soup = BeautifulSoup(markup, "html.parser")
    container = soup.select_one("#chapterWarp div.content")
    if container is None:
        return []
    for junk in container.select("div.gadBlock, div.adUnit, ins, script, style, iframe"):
        junk.decompose()
    return [t for t in (p.get_text(strip=True) for p in container.find_all("p", recursive=False)) if t]


def chapter_count(session: requests.Session, book: str) -> int:
    """How many chapters this novel actually has, read off its /dir page.

    Never assume the reference novel's length. A shorter novel sampled on that assumption
    turns most requests into 404s, which `main` swallows — so the sample silently collapses
    to a handful of chapters and the run looks fine while contributing almost nothing.
    """
    response = session.get(f"{ORIGIN}/{book}/dir", timeout=25)
    response.raise_for_status()
    numbers = [int(n) for n in re.findall(rf"/{book}/(\d+)\.html", response.text)]
    return max(numbers) if numbers else 0


def fetch(session: requests.Session, book: str, n: int) -> list[str]:
    response = session.get(f"{ORIGIN}/{book}/{n}.html", timeout=25)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return paragraphs(response.text)


def merge(a: list[str], b: list[str], table: dict[str, str], conflicts: Counter) -> None:
    """Fold one aligned pair of fetches into `table`, counting any disagreement.

    Paragraphs whose lengths differ are skipped rather than guessed at: the substitution is
    strictly 1:1, so a length change means the two fetches are not the same text and any
    alignment would be fiction.
    """
    for para_a, para_b in zip(a, b):
        if len(para_a) != len(para_b):
            continue
        for char_a, char_b in zip(para_a, para_b):
            if char_a == char_b:
                continue
            if HANGUL.match(char_a) and not HANGUL.match(char_b):
                key, value = char_a, char_b
            elif HANGUL.match(char_b) and not HANGUL.match(char_a):
                key, value = char_b, char_a
            else:
                continue
            if table.setdefault(key, value) != value:
                conflicts[key] += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapters", type=int, default=20, help="chapters to sample (x2 fetches)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--second", default=SECONDARY,
                        help="one or more novel ids (comma-separated), to cross-check")
    parser.add_argument("--second-chapters", type=int, default=15,
                        help="chapters to sample from each cross-check novel")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = UA

    table: dict[str, str] = {}
    conflicts: Counter = Counter()
    per_novel: dict[str, dict[str, str]] = {}

    seconds = [b.strip() for b in (args.second or "").split(",") if b.strip()]
    books = [(PRIMARY, args.chapters)] + [(b, args.second_chapters) for b in seconds]
    for book, count in books:
        book_table: dict[str, str] = {}
        try:
            length = chapter_count(session, book)
        except requests.RequestException as exc:
            print(f"  {book}: cannot read /dir ({exc})", file=sys.stderr)
            continue
        if not length:
            print(f"  {book}: /dir lists no chapters", file=sys.stderr)
            continue
        # Spread the sample across the novel rather than taking a prefix: vocabulary drifts
        # between early and late chapters, and a prefix would over-sample the opening arc.
        step = max(1, length // count)
        print(f"  {book}: {length} chapters, sampling every {step}")
        for n in range(1, min(count * step, length) + 1, step):
            try:
                a = fetch(session, book, n)
                time.sleep(args.delay)
                b = fetch(session, book, n)
                time.sleep(args.delay)
            except requests.RequestException as exc:
                print(f"  ch{n}: {exc}", file=sys.stderr)
                continue
            if not a or len(a) != len(b):
                print(f"  ch{n}: skipped (paragraph count {len(a)} vs {len(b)})", file=sys.stderr)
                continue
            before = len(book_table)
            merge(a, b, book_table, conflicts)
            residue = sum(len(HANGUL.findall(p)) for p in a)
            print(f"  {book} ch{n}: {len(a)} paras, {residue} hangul, +{len(book_table)-before} new")
        per_novel[book] = book_table
        for key, value in book_table.items():
            if table.setdefault(key, value) != value:
                conflicts[key] += 1

    print(f"\n{len(table)} mappings, {len(conflicts)} conflicting keys")
    ids = list(per_novel)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            left, right = per_novel[ids[i]], per_novel[ids[j]]
            shared = set(left) & set(right)
            agree = sum(left[k] == right[k] for k in shared)
            verdict = "SITE-WIDE" if agree == len(shared) else "PER-NOVEL — STOP"
            print(f"cross-novel {ids[i]}/{ids[j]}: {len(shared)} shared keys, "
                  f"{agree} agree ({verdict})")
    if conflicts:
        print("CONFLICTS — the fixed-table premise is wrong, do not ship:", dict(conflicts))
        return 1

    print("\n# --- paste into timotxt.py ---")
    print("_SUBSTITUTIONS = {")
    for key in sorted(table):
        print(f'    "{key}": "{table[key]}",')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Debug CLI: python -m noveltrans.scrapers <novel-url> [--chapter N]

Prints metadata, chapter count and one chapter's text — handy for checking a
site adapter against the live site without the GUI.
"""

from __future__ import annotations

import argparse
import sys

from noveltrans.scrapers import adapter_for_url
from noveltrans.scrapers.base import HttpClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Novel landing-page URL")
    parser.add_argument("--chapter", type=int, default=0, help="Chapter index to fetch (0-based)")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between requests (s)")
    args = parser.parse_args()

    adapter = adapter_for_url(args.url, HttpClient(delay_seconds=args.delay))
    if adapter is None:
        print(f"No adapter matches: {args.url}", file=sys.stderr)
        return 1

    print(f"[adapter] {adapter.name} — {adapter.display_name}\n")
    meta = adapter.fetch_metadata(args.url)
    print(f"Title:  {meta.title}")
    print(f"Author: {meta.author}")
    print(f"Desc:   {meta.description[:200]}{'…' if len(meta.description) > 200 else ''}\n")

    refs = adapter.fetch_chapter_list(args.url)
    print(f"Chapters: {len(refs)}")
    print(f"  first: {refs[0].title}")
    print(f"  last:  {refs[-1].title}\n")

    ref = refs[args.chapter]
    text = adapter.fetch_chapter(ref)
    print(f"--- {ref.title} ({len(text)} chars) ---")
    print(text[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

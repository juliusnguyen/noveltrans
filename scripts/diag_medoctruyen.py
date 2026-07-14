"""Diagnose medoctruyen.vn auth: read the cookie the app saved, fetch a chapter,
and report whether the login/preview gate is still present.

Run:  .venv/bin/python scripts/diag_medoctruyen.py [chapter_url]
"""

from __future__ import annotations

import sys

from noveltrans.config import AppConfig
from noveltrans.scrapers.base import HttpClient

URL = sys.argv[1] if len(sys.argv) > 1 else "https://medoctruyen.vn/tu-bao-tien-bon/chuong-1"


def main() -> None:
    cookies = AppConfig().medoctruyen_cookies
    print(f"stored cookie: {len(cookies)} chars", end="")
    if cookies:
        names = sorted({c.split("=", 1)[0].strip() for c in cookies.split(";") if "=" in c})
        print(f", {len(names)} cookie name(s): {', '.join(names)}")
    else:
        print("  <-- EMPTY. Paste your logged-in Cookie header in Settings and save.")
        return

    client = HttpClient(delay_seconds=0, cookies=cookies)
    html = client.get_html(URL)

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    gate = soup.select_one("[data-preview-gate]") is not None
    article = soup.select_one("article")
    para = article.select("p") if article else []
    body_len = sum(len(p.get_text(strip=True)) for p in para)

    has_preview_true = '"isPreview":true' in html
    has_preview_false = '"isPreview":false' in html
    has_locked_true = '"isLocked":true' in html
    has_login_href = "previewLoginHref" in html

    print(f"\nfetched {URL}")
    print(f"  html length          : {len(html)}")
    print(f"  [data-preview-gate]  : {gate}")
    print(f'  "isPreview":true     : {has_preview_true}')
    print(f'  "isPreview":false    : {has_preview_false}')
    print(f'  "isLocked":true      : {has_locked_true}')
    print(f"  previewLoginHref str : {has_login_href}")
    print(f"  <article> paragraphs : {len(para)}")
    print(f"  body text length     : {body_len}")
    if body_len > 800 and not gate:
        print("\n=> Looks like FULL content. The cookie works.")
    else:
        print("\n=> Still gated/preview. Cookie is missing, wrong, or expired.")


if __name__ == "__main__":
    main()

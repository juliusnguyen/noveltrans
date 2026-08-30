"""The one function that turns a title into a filename-safe stem.

Lives at the top level rather than in `storage.project` because `models.NovelMeta` needs
it — `slug_name()` is what every generated filename is keyed to — and `models` is imported
*by* storage, not the other way round. `storage.project` re-exports it, so every existing
`from noveltrans.storage.project import slugify` keeps working.
"""

from __future__ import annotations

import re
import unicodedata


def slugify(text: str, max_len: int = 40) -> str:
    """ASCII-safe folder slug; CJK titles fall back to 'novel'."""
    text = text.replace("đ", "d").replace("Đ", "D")  # đ has no NFKD decomposition
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len] or "novel"

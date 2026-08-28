"""Drop the line a scraped chapter carries to advertise the site it was copied from.

Source sites water-mark their chapters with a line like
`Muốn xem thêm nhiều chương đặc sắc, xin truy cập sto9🍀.com`. It is in the Chinese source,
so the translator faithfully renders it and it lands in the middle of the novel.

Two facts drive the whole design:

* **The line arrives translated**, so its wording is model output and varies run to run.
  A literal blocklist cannot work; the predicate keys on the *shape* of the line.
* **The domain is deliberately obfuscated** (`sto9🍀.com` — an emoji standing in for a
  character) precisely to defeat naive filters. That obfuscation is turned into the
  strongest signal here: legitimate prose never writes a domain with a decorative glyph
  inside it.

The predicate is **conjunctive and domain-anchored**: a line is only ever dropped if a
domain-like token survives de-obfuscation on that line. No domain, no deletion. That single
conjunct is what makes "the filter ate my story" structurally impossible for prose that
isn't about the internet — `"Truy cập vào hệ thống, hắn thấy một dòng chữ đỏ."` carries the
strongest promo cue in the list and is still untouchable.

The risk is deliberately **asymmetric**. A miss costs one visible junk line the user can
find-and-replace; an over-deletion is silent data loss noticed chapters later. Every tuning
choice below biases to under-delete — the same reasoning `scrapers/shuba69.py` spells out
for its own stripping.

What this must not break:

* It never touches the stored original `content`, and never re-cleans an existing
  translation. It runs on fresh translator output only, at `Translator.translate_chapter`.
* It never runs on `complete()` output — tags, image prompts and shortened descriptions go
  through a different path and must keep any domain they contain.
* It never splits or merges a paragraph. Removing a line from inside one leaves one
  paragraph, not two; the engine prompts all promise "keep the paragraph breaks exactly as
  in the source", and downstream TTS chunking keys off `\\n\\n`.
* It never returns empty for non-empty input. `translate_chapter` also routes the novel
  title and description through here, and a description that is entirely an ad must not
  become "".
* It is idempotent, and returns non-ad text byte-identical.

`PROMPT_RULE` is the advisory half of the same rule, shared by the three engine prompts so
the wording cannot drift between them. Deliberately NOT used by `translators/rewrite.py`:
`check_rewrite` rejects any rewrite whose paragraph count differs from its input, so telling
the rewrite model to drop a line would fail validation and burn all three attempts.

Pure `str -> str`. No Qt, no I/O, no config.
"""

from __future__ import annotations

import re
import unicodedata

# The advisory half of this module, appended to every engine's translation prompt. Names
# the line's PURPOSE rather than saying "remove advertising" — the model can apply a
# purpose test, whereas the loose wording invites it to cut real content. The second half
# is the anti-over-deletion clamp, and it reconciles the rule with the pre-existing
# "keep the paragraph breaks exactly as in the source" instruction it sits next to.
PROMPT_RULE = (
    "The source sometimes contains a line whose only purpose is to promote the website "
    "the text was copied from — typically a domain name (often disguised with an emoji "
    "or an odd character inside it) together with wording such as \"visit …\" or \"read "
    "the latest chapters at …\". Omit any such line completely, together with its "
    "paragraph break. This is the ONLY thing you may omit: every other line is story "
    "text and must be translated in full, and every other paragraph break stays exactly "
    "as in the source. "
)

# An ad line is short by nature; a real paragraph is not. A paragraph that genuinely
# discusses a website survives on length alone, whatever else it contains.
MAX_AD_LINE_CHARS = 200

# 3+ character TLDs are safe to match generically.
_TLD3 = (
    "com|net|org|xyz|info|vip|top|club|site|online|shop|live|icu|fun|biz|pro"
    "|space|store|website|blog|link|host|press|world|life|today|news"
)
# 2-character TLDs are an explicit allowlist. A generic [a-z]{2} would match accent-folded
# Vietnamese across a missing space after a full stop — "Rồi.Mẹ" folds to "roi.me" and
# "sổ. Có" to "so.co". So `co`, `me`, `la`, `no`, `ta` are deliberately absent.
_TLD2 = "cc|tw|hk|kr|jp|cn|ru|vn|tv|io|pw|su"
# The trailing path is part of the match so the "bare domain" test below sees a full URL
# as one span. Without it, `https://sto9.com/book/13908/index.html` leaves the path behind
# as leftover text and reads as a sentence that merely mentions a domain.
_DOMAIN_RE = re.compile(
    rf"(?:[a-z0-9](?:[a-z0-9-]{{0,30}}[a-z0-9])?\.)+(?:{_TLD3}|{_TLD2})"
    r"(?![a-z0-9])(?:/\S*)?"
)

# Dropped before folding: a scheme carries no signal, and leaving it in would make a line
# that is nothing but a URL look like it has words in it (`https` is five alphanumerics).
_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)

# Separators that read as a dot. A site writing "sto9。com" is still writing a domain.
_DOT_LOOKALIKES = {ord(c): "." for c in "。．・･•∙‧⋅·﹒｡"}

# A run of 4+ isolated single characters is spaced-out text ("s t o 9 . c o m"). Ordinary
# Vietnamese never has four consecutive one-letter words.
_SPACED_RUN = re.compile(r"(?:(?<= )|^)(?:[a-z0-9.\-] ){3,}[a-z0-9.\-](?= |$)")
_SPACED_DOT = re.compile(r" \. ")

# Phrases whose only job is to send a reader somewhere. Matched accent-folded, so
# "truy cập" is covered by "truy cap".
_STRONG_CUES = (
    # Vietnamese
    "truy cap", "ghe tham", "doc tiep tai", "doc them tai", "xem tiep tai",
    "xem them tai", "chuong moi nhat", "cap nhat nhanh nhat", "cap nhat som nhat",
    "doc mien phi", "dia chi moi", "ghi nho", "goi nho", "dich boi", "dang tai tai",
    # English
    "visit", "read more at", "latest chapter", "full text at", "translated at", "bookmark",
)
# Chinese cues, matched on the raw line — an untranslated watermark keeps its original form.
_CJK_CUES = (
    "请访问", "請訪問", "更多精彩", "最新章节", "最新章節", "请记住", "請記住",
    "无弹窗", "無彈窗", "免费阅读", "免費閱讀", "全文阅读", "全文閱讀", "首发", "首發",
    "转载", "轉載", "域名", "收藏", "手机版", "手機版", "更新最快",
)
# Nouns that also appear in ordinary prose, so they only count NEAR the domain. Without
# the proximity rule, "Trang web của công ty bị hack" would be one signal away from
# deletion; with it, only "… trang web sto9.com …" qualifies.
_WEAK_CUES = ("trang web", "website", "nguon", "ban quyen", "source")
_WEAK_CUE_WINDOW = 30

# After removing the domain, a line with almost nothing left is a bare watermark.
_BARE_LEFTOVER_MAX = 4

_MULTISPACE_RE = re.compile(r" +")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _strip_marks(text: str) -> str:
    """Fold Vietnamese tone marks away. `đ` first — NFD does not decompose it."""
    text = text.replace("đ", "d").replace("Đ", "D")
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _fold(line: str) -> str:
    """The de-obfuscated view of a line, used ONLY for matching — never emitted.

    Anything that is not an ASCII letter, digit, dot or hyphen is **deleted** rather than
    replaced with a space, which is the exact inverse of `tts/clean.py`'s choice (that one
    maps a dropped symbol to a space so `A★B` does not become `AB`). Here the join is the
    whole point: it is what closes `sto9🍀.com` back up into `sto9.com`. It is safe only
    because this string never reaches the output.
    """
    folded = unicodedata.normalize("NFKC", line).casefold()  # ｓｔｏ９．ｃｏｍ -> sto9.com
    folded = _SCHEME_RE.sub("", folded)
    folded = folded.translate(_DOT_LOOKALIKES)
    folded = _strip_marks(folded)
    kept = [
        c if (c in ".-/" or (c.isascii() and c.isalnum())) else (" " if c.isspace() else "")
        for c in folded
    ]
    return _MULTISPACE_RE.sub(" ", "".join(kept)).strip()


def _detect_view(folded: str) -> str:
    """`_fold` plus the spaced-out forms closed up: `s t o 9 . c o m`, `sto9 . com`."""
    view = _SPACED_RUN.sub(lambda m: m.group(0).replace(" ", ""), folded)
    return _SPACED_DOT.sub(".", view)


def _cue_view(line: str) -> str:
    """The raw line, accent-folded and casefolded, for phrase matching."""
    return _MULTISPACE_RE.sub(" ", _strip_marks(line.casefold()))


def _is_ad_line(line: str) -> bool:
    """True when this whole line exists only to advertise the source site.

    Conjunctive: short enough, AND carries a domain, AND at least one corroborating signal.
    Dropping any one of those conjuncts is what would start eating story text.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_AD_LINE_CHARS:
        return False

    view = _detect_view(_fold(line))
    match = _DOMAIN_RE.search(view)
    if match is None:
        return False  # the load-bearing guarantee: no domain, no deletion

    domain = match.group(0)

    # 1. Obfuscation — the domain only exists after decorative characters were deleted.
    #    Legitimate prose never writes a domain with an emoji inside it.
    plain = unicodedata.normalize("NFKC", line).casefold()
    obfuscated = domain.split("/")[0] not in plain

    # 2. Promo cue. Strong phrases count anywhere; vaguer ones only near the domain.
    cues = _cue_view(line)
    promo = any(cue in cues for cue in _STRONG_CUES) or any(cue in line for cue in _CJK_CUES)
    if not promo:
        start = max(0, match.start() - _WEAK_CUE_WINDOW)
        window = view[start : match.end() + _WEAK_CUE_WINDOW]
        promo = any(cue in window for cue in _WEAK_CUES)

    # 3. Bare domain — the line is the watermark and nothing else.
    leftover = view[: match.start()] + view[match.end() :]
    bare = sum(c.isalnum() for c in leftover) < _BARE_LEFTOVER_MAX

    return obfuscated or promo or bare


def drop_site_ads(text: str) -> str:
    """Return `text` with source-site advertising lines removed.

    Paragraph-aware: a paragraph that loses every line disappears entirely rather than
    leaving a blank, and a paragraph that loses one of several lines stays ONE paragraph.
    Deliberately not `tts/clean.py`'s blank-the-line-then-collapse approach — blanking a
    line inside a paragraph would introduce a break and split it in two.
    """
    if not text or not text.strip():
        return text

    kept = []
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        lines = [line for line in paragraph.split("\n") if not _is_ad_line(line)]
        joined = "\n".join(lines).strip()
        if joined:
            kept.append(joined)

    result = "\n\n".join(kept).strip()
    # Never let a misjudgement empty a chapter, title or description — a surviving ad line
    # is a nuisance, a blanked field is data loss.
    return result if result else text

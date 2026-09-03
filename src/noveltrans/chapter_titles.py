"""Guards against a model's own words being saved as a chapter title — no Qt, no sqlite.

Feature 076. `Translator.translate_chapter` sends the chapter TITLE through the prompt
written for a chapter BODY. Handed a bare `第127章` — a chapter number with no title text —
the model reads it as the heading of a chapter whose content was omitted and replies asking
for the content. Nothing downstream can tell that reply apart from a translation, so it was
saved as `translated_title` and rendered above the (perfectly good) body.

Three predicates, in the order they should be relied on:

* `numeric_title` — a title that is only a number needs no model at all. Prevention.
* `is_implausible_title` — a translated title that cannot be a translation of its source.
  The primary detector; the thresholds are measured, see below.
* `looks_like_refusal` — phrasing, for BODIES, where no length ratio applies. Secondary.

Kept at the top level rather than under `translators/` because `storage.project` needs the
first two for its one-time repair pass and must not depend on the translator package —
the same reasoning that already places `rename.py`, `find_replace.py` and
`name_glossary.py` here.
"""

from __future__ import annotations

import re

# "Chương N" is not an invention: it is exactly what the model returned for the 131 bare
# titles that DID translate cleanly, so a locally-built or repaired title matches its
# neighbours in the same novel instead of standing out.
_NUMBER_WORD = {"vi": "Chương", "en": "Chapter"}

# `第 001 章`, with optional spaces and leading zeros, and NOTHING else but whitespace or
# punctuation. The "nothing else" is the whole point: every title in the library that
# carries real text after the number (`第001章 撿個破盆`, `第1 章 穿書畜生`, `第一章 ：獨孤九劍`)
# must fall through to the model exactly as it does today.
_BARE_NUMBER_RE = re.compile(r"^[\s\W]*第\s*0*(\d{1,6})\s*章[\s\W]*$")

# Deliberately no Chinese-numeral conversion (第四十二章 -> 42). Measured across every
# project in the library: zero bare Chinese-numeral titles exist — the novels that number
# that way always carry title text — so the converter would be code with no caller.

# --- is_implausible_title thresholds -------------------------------------------------
#
# Measured over the 4323 known-good translated titles in the library, zh -> vi:
#
#     p50 = 1.68    p90 = 3.52    p99 = 4.38    p99.9 = 5.91
#
# So 6 sits outside 4319 real examples. Re-derive from the data before nudging it.
_MAX_EXPANSION = 6

# Absolute floor, so a very short source with a legitimately chatty translation cannot trip
# the ratio alone. The shortest refusal actually observed is 97 characters; the longest
# legitimate title in the library is 107 at a ratio well under 6.
_MIN_SUSPECT_CHARS = 60

# A request for content: a verb of GIVING near "nội dung"/"văn bản". Matching an apology
# instead is disqualified by the library — `Chương 268: Xin lỗi nhé máy liên lạc tiên sinh`
# and `Chương 42: Xin lỗi, biểu tỷ` are correct translations of 對不起 and must never flag.
_GIVE_RE = re.compile(r"\b(cung cấp|gửi(?:\s+kèm)?|dán|paste|provide|share)\b", re.IGNORECASE)
_SUBJECT_RE = re.compile(r"(nội dung|văn bản|đoạn văn|nguyên văn|the text|content)", re.IGNORECASE)


def numeric_title(source: str, target: str = "vi") -> str:
    """`"Chương N"` when `source` carries a chapter number and nothing else, else `""`.

    An empty return means "this title has real text — ask the model", which is the answer
    for every title shape in the library except the 139 bare `第N章` ones that caused this
    feature. Unknown target languages also return "": inventing a heading word for a
    language we have no spelling for would be worse than translating it.
    """
    word = _NUMBER_WORD.get(target)
    if not word:
        return ""
    match = _BARE_NUMBER_RE.match(source or "")
    if not match:
        return ""
    return f"{word} {int(match.group(1))}"


def is_implausible_title(source: str, output: str) -> bool:
    """True when `output` cannot be a translation of the title `source`.

    Two independent signals, both measured against the whole library (14 flagged out of
    4331, zero false positives):

    * a newline — a title is one line, so this catches the model returning the title AND
      the body prose into the title slot;
    * an output many times longer than its source, which is what a refusal looks like:
      four source characters in, 142 out.

    Deliberately structural rather than phrase-based. `looks_like_refusal` misses two of
    the ten real refusals in the library because they are worded in ways no marker list
    anticipates ("Bạn vui lòng gửi nội dung chương 111…", "Không có nội dung văn bản nào
    được cung cấp ngoài tiêu đề…"); the length rule catches both without knowing any
    Vietnamese at all.
    """
    source = (source or "").strip()
    output = (output or "").strip()
    if not source or not output:
        return False
    if "\n" in output:
        return True
    return len(output) > _MIN_SUSPECT_CHARS and len(output) > _MAX_EXPANSION * len(source)


def looks_like_refusal(text: str) -> bool:
    """True when `text` is the model asking for content instead of translating it.

    For BODIES, where `is_implausible_title`'s length ratio has no equivalent — a chapter
    body is long enough that a refusal does not stand out by size. Requires BOTH a verb of
    giving and a word for the thing being asked for, which is what keeps a chapter whose
    prose happens to mention handing over a document from tripping it.
    """
    text = (text or "").strip()
    if not text:
        return False
    # Only the opening matters: a refusal leads with the request. Scanning the whole body
    # would eventually hit ordinary narration ("nàng gửi nội dung bức thư cho hắn").
    head = text[:400]
    return bool(_GIVE_RE.search(head) and _SUBJECT_RE.search(head))


def repaired_title(source: str, stored: str, target: str = "vi") -> str:
    """The corrected `translated_title` for a damaged row, or `""` to leave it alone.

    Shared by the storage repair and the translate-time fallback so the two cannot
    disagree about what a damaged title should become. Three cases, in order:

    * the source is a bare number -> rebuild it locally (the 10 refusals in the library);
    * the output has a newline -> keep the first line, which IS the correct title; only
      the prose appended after it is wrong (the 4 title+body leaks);
    * neither -> "" so the caller falls back to the untouched source title.
    """
    local = numeric_title(source, target)
    if local:
        return local
    first_line = (stored or "").strip().split("\n", 1)[0].strip()
    if first_line and not is_implausible_title(source, first_line):
        return first_line
    return ""

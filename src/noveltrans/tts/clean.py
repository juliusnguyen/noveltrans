"""Strip special characters from chapter text before TTS, for smoother audio.

The engine mispronounces or pauses awkwardly on emoji, decorative symbols, stray
leftover CJK, zero-width characters and markdown remnants. This removes them with a
**whitelist**: keep Latin-script letters (which is all of Vietnamese, including
precomposed tone marks like ộ / ữ and đ / Đ), digits, whitespace, and the punctuation
that carries prosody — drop everything else. A never-before-seen glyph is dropped
automatically.

Two things the keep-set must not break:
  * `split_sentences` (tts/base.py) chunks on paragraph breaks and on the sentence
    punctuation `. ! ? …` plus trailing quotes/parens. Those are all kept here, so
    cleaning can't silently break sentence splitting.
  * Vietnamese is Latin-script but NOT ASCII — its tone marks are non-ASCII letters.
    A naive `[a-zA-Z]` or strip-non-ASCII filter would gut the text; the predicate is
    Unicode-category + Latin-script aware instead.

Pure `str -> str`. Applied to the copy fed to the engine only; the stored translation
is never touched.
"""

from __future__ import annotations

import re
import unicodedata

# Punctuation kept for prosody (pauses/intonation). The sentence enders and the
# quote/paren chars here MUST stay in sync with tts/base.py's _SENTENCE_RE, which keys
# sentence splitting off them — dropping any would break chunking downstream.
_KEEP_PUNCT = frozenset('.!?…,;:"\'“”‘’«»()')

# Dash variants normalised to a plain hyphen so dialogue dashes ("— Xin chào") survive
# as a simple pause-worthy mark rather than being dropped.
_DASHES = frozenset("—–‒―−-")

_MULTISPACE_RE = re.compile(r"[^\S\n]+")  # runs of non-newline whitespace
_SPACE_AROUND_NL_RE = re.compile(r" *\n *")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _keep(ch: str) -> bool:
    """True for a character that belongs in spoken Vietnamese text."""
    if ch in _KEEP_PUNCT:
        return True
    category = unicodedata.category(ch)
    if category == "Nd":  # decimal digit
        return True
    if category[0] == "M":  # combining marks — decomposed Vietnamese tone marks
        return True
    if category[0] == "L":  # letters, but Latin script only (drops CJK / leftover Han)
        try:
            return unicodedata.name(ch).startswith("LATIN")
        except ValueError:  # unnamed letter
            return False
    return False


def clean_for_tts(text: str) -> str:
    """Return `text` with TTS-hostile characters removed, prosody preserved.

    Keeps Vietnamese/Latin letters, digits, newlines and prosody punctuation; drops
    everything else. Paragraph breaks (\\n\\n) are preserved so downstream sentence
    chunking still works; runs of spaces left by removed symbols are tidied.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\n")
        elif ch in _DASHES:
            out.append("-")
        elif ch.isspace():
            out.append(" ")
        elif _keep(ch):
            out.append(ch)
        elif unicodedata.category(ch)[0] == "C":
            # Control / format / zero-width: remove entirely. Mapping these to a space
            # (like a visible symbol) would split a word a ZWJ sat inside.
            continue
        else:
            # A dropped visible symbol becomes a space so it can't merge its neighbours
            # ("A★B" -> "A B", not "AB"); the collapse below tidies the result.
            out.append(" ")

    cleaned = "".join(out)
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    cleaned = _SPACE_AROUND_NL_RE.sub("\n", cleaned)  # trim spaces hugging newlines
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)  # cap blank runs at one blank line
    return cleaned.strip()

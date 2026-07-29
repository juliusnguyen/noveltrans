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

# Fullwidth / CJK punctuation → ASCII. The source novels are Chinese, so these leak
# through translation; without this they'd be stripped and their sentence/clause pauses
# lost — the opposite of what the cleaner is for. Mapped to the ASCII forms that are
# already in the keep-set so prosody survives.
_PUNCT_MAP = {
    "！": "!", "？": "?", "，": ",", "、": ",", "。": ".", "．": ".",
    "：": ":", "；": ";", "（": "(", "）": ")",
    "「": '"', "」": '"', "『": '"', "』": '"', "《": '"', "》": '"',
    "…": "…",  # (already kept; listed for clarity — the Chinese ellipsis …… is two of these)
}

_MULTISPACE_RE = re.compile(r"[^\S\n]+")  # runs of non-newline whitespace
_SPACE_AROUND_NL_RE = re.compile(r" *\n *")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Any letter or digit — i.e. anything an engine could actually pronounce.
_SPEAKABLE_RE = re.compile(r"[^\W\d_]|\d")


def _has_speech(line: str) -> bool:
    """True if a line has anything to say; False for pure punctuation.

    A line that is only punctuation — `“…”`, `……`, `“?”`, `- - -` — is a *beat*. Vietnamese
    web novels lean on it constantly for a silent reply in dialogue. Handed to the engine
    it comes out as a noise instead: `split_sentences` gives such a paragraph its own
    chunk, feature 028's `merge_short_chunks` glues that 3-character chunk onto the
    neighbouring sentence (it is far below `min_chunk_chars`), and the voice dutifully
    tries to pronounce it.

    Digits count as speech, so a bare chapter-number line still gets read. By the time
    this runs the keep-set has already reduced letters to Latin script, so no script
    logic is needed here.
    """
    return _SPEAKABLE_RE.search(line) is not None


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


def clean_for_tts(text: str, extra_remove: str = "") -> str:
    """Return `text` with TTS-hostile characters removed, prosody preserved.

    Keeps Vietnamese/Latin letters, digits, newlines and prosody punctuation; drops
    everything else. Paragraph breaks (\\n\\n) are preserved so downstream sentence
    chunking still works; runs of spaces left by removed symbols are tidied.

    `extra_remove` is a user-supplied string of characters to strip in ADDITION to the
    automatic cleaning — its only visible effect is on characters the whitelist would
    otherwise keep (e.g. removing "()" so parentheses aren't voiced). It's applied to
    the already-cleaned output, so the characters match what the preview shows (a
    fullwidth "！" has already become "!" by then). Whitespace in it is ignored so a
    stray space in the setting can't nuke every space.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\n")
        elif ch in _DASHES:
            out.append("-")
        elif ch in _PUNCT_MAP:
            out.append(_PUNCT_MAP[ch])
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

    drop = {ch for ch in extra_remove if not ch.isspace()}
    if drop:
        # Match visible-symbol removal above: → space, then let the tidy collapse it.
        cleaned = "".join(" " if ch in drop else ch for ch in cleaned)

    # Blank out lines with nothing to say, keeping their newlines so the blank-run collapse
    # below turns the hole into an ordinary paragraph break — which `synthesize_chapter`
    # already renders as `paragraph_gap_seconds` of real silence. The beat the author wrote
    # survives *as* silence, which is what they meant by it.
    #
    # Whole lines only: an ellipsis INSIDE a sentence ("Anh ta ngập ngừng… rồi im lặng.")
    # is prosody and must keep working. Runs after `extra_remove`, because that can empty a
    # line too — stripping "()" turns a "(…)" line into punctuation-only.
    cleaned = "\n".join(line if _has_speech(line) else "" for line in cleaned.split("\n"))

    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    cleaned = _SPACE_AROUND_NL_RE.sub("\n", cleaned)  # trim spaces hugging newlines
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)  # cap blank runs at one blank line
    return cleaned.strip()

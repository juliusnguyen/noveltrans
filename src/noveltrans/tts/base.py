"""TTS engine ABC with sentence-safe chunking for long chapters."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from noveltrans.errors import TtsError
from noveltrans.tts.clean import clean_for_tts

# sentence enders (incl. Vietnamese usage of …), keeping the delimiter attached
_SENTENCE_RE = re.compile(r"[^.!?…]*[.!?…]+[\"'”’)]*\s*|[^.!?…]+$")

# Sentence enders as they may appear in a chapter TITLE, which is inspected BEFORE
# clean_for_tts runs — so the fullwidth forms are still fullwidth here. Without them a
# title already ending in 。 would be judged unterminated and end up spelled "。." once
# the cleaner maps it to a period.
_TITLE_ENDERS = frozenset(".!?…。！？")

# Closing marks that can sit AFTER the real terminator: "…cầu phiếu tháng!!)" is already
# a finished sentence, and 214 titles in the reference library end this way.
_TITLE_CLOSERS = '"\'”’»）)】》》'

# Clause separators a full stop supersedes. A title ending "Chương 6:" should be read as
# "Chương 6." rather than "Chương 6:." — rare (8 rows in the reference library) but there
# is no reason to speak it badly.
_TITLE_CLAUSE_ENDS = ",;:-–—~"


def ensure_sentence_end(title: str) -> str:
    """Give a chapter title a terminator, so TTS pauses before the body.

    Chapter titles almost never carry punctuation — 3552 of the 4331 translated titles in
    the reference library end in a plain letter or digit. `synthesize_chapter` joins the
    title and body with a blank line, and `split_sentences` duly makes the title its own
    chunk; but `merge_short_chunks` then glues it onto the first body sentence with a bare
    SPACE, because "Chương 127" is 10 characters against a 30-character floor. The result
    is one chunk reading "Chương 127 Tống Nam Thời trầm ngâm…", spoken without a break.

    The obvious fix — keep the title as its own chunk — is not available. That merge is
    feature 028's measured fix for autoregressive drift: 80% of chunks under 10 characters
    babble, 0% at 40+. A bare "Chương 127" sits exactly in the worst band, so exempting it
    would trade this defect for that one.

    So the pause is bought with punctuation instead, which costs nothing and applies at
    every title length: the model reads a real sentence boundary and pauses on its own.
    """
    title = title.rstrip()
    if not title:
        return title
    # Peel back to the character that actually ends the sentence, remembering the closing
    # quotes/brackets so they can be put back: "…bùng nổ!!)" is already finished, and
    # "…đại chương ）" is not.
    unclosed = title.rstrip(_TITLE_CLOSERS).rstrip()
    closers = title[len(unclosed):].lstrip()
    # Then peel any dangling clause separator, because a terminator can hide behind one
    # too — "…thật bẩn a! ~~~" is a finished sentence wearing a decorative tail.
    stem = unclosed.rstrip(_TITLE_CLAUSE_ENDS).rstrip()
    if not stem:
        return title  # nothing but punctuation; there is no sentence to finish
    if stem[-1] in _TITLE_ENDERS:
        return title  # already a finished sentence
    if stem != unclosed:
        return f"{stem}.{closers}"  # replace the dangling separator, never stack on it
    return f"{title}."


def split_sentences(text: str, max_chars: int = 400) -> list[str]:
    """Split text into chunks of <= max_chars without breaking sentences.

    Paragraphs are split first (so a chunk never spans a paragraph break),
    then sentences are greedily packed. A single sentence longer than
    max_chars becomes its own (oversized) chunk.
    """
    chunks: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        current = ""
        for sentence in _SENTENCE_RE.findall(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if current and len(current) + 1 + len(sentence) > max_chars:
                chunks.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}" if current else sentence
        if current:
            chunks.append(current)
    return chunks


def merge_short_chunks(chunks: list[str], min_chars: int, max_chars: int) -> list[str]:
    """Coalesce sub-`min_chars` fragments into a neighbour, capped at `max_chars`.

    Autoregressive TTS (VieNeu) reliably garbles very short inputs — a stray
    number or a one-word line gives the model too little context, so its
    end-of-speech prediction misfires and it babbles until the token cap.
    Measured drift rate is ~80% under 10 chars and 0% at 40+ (see change 028).

    A chunk is merged into the previous one (joined with a space) when either it
    or the running chunk is below the floor and the result still fits max_chars.
    A fragment that can't merge without overflowing is left as-is — rare, and the
    alternative (an oversized chunk) has its own, worse failure mode.
    """
    if min_chars <= 0:
        return chunks
    out: list[str] = []
    for chunk in chunks:
        if (
            out
            and (len(chunk) < min_chars or len(out[-1]) < min_chars)
            and len(out[-1]) + 1 + len(chunk) <= max_chars
        ):
            out[-1] = f"{out[-1]} {chunk}"
        else:
            out.append(chunk)
    return out


class TtsEngine(ABC):
    """One text-to-speech backend. Chunks long text and concatenates audio."""

    name: str = ""
    display_name: str = ""
    max_chunk_chars: int = 400
    # Chunks shorter than this are merged into a neighbour before synthesis —
    # short fragments make autoregressive TTS drift/babble (see change 028).
    # 0 disables merging.
    min_chunk_chars: int = 30
    sample_rate: int = 48000
    paragraph_gap_seconds: float = 0.4  # silence inserted between chunks

    @abstractmethod
    def load(self) -> None:
        """Download/load the model. Called once by the worker before synthesis."""

    @abstractmethod
    def list_voices(self) -> list[tuple[str, str]]:
        """Available voices as (display label, voice id) pairs."""

    @abstractmethod
    def synthesize(self, text: str) -> "object":
        """Synthesize one chunk; returns a 1-D numpy float array of samples."""

    @abstractmethod
    def save_wav(self, samples: "object", out_path: Path) -> None:
        """Write samples to a WAV file at self.sample_rate."""

    def synthesize_chapter(
        self,
        title: str,
        body: str,
        out_path: Path,
        cancelled: Callable[[], bool] | None = None,
        clean: bool = True,
        clean_extra_remove: str = "",
        gap_seconds: float | None = None,
        volume: float = 1.0,
        cues_out: list | None = None,
    ) -> float:
        """Synthesize title + body into one WAV. Returns audio duration (s).

        With `clean` (the default), special characters are stripped from the text
        before synthesis so the audio reads smoothly (see tts/clean.py); any characters
        in `clean_extra_remove` are stripped on top of that. Only the copy fed to the
        engine is cleaned — nothing stored is touched.

        `gap_seconds` overrides the silence between chunks (None = the engine's
        `paragraph_gap_seconds` default). `volume` is a linear gain on the rendered
        audio, hard-clipped to [-1, 1] so gains > 1.0 can't wrap around into noise.

        `cues_out`, when given, is filled with one `subtitles.Cue` per chunk — the exact
        timings of what was spoken, which exist only inside this loop and cannot be
        recovered afterwards. An out-parameter rather than a changed return type because
        three callers depend on the `float`, and two of them are previews that would only
        unpack a value to discard it. **These timings are pre-speed**: a caller that
        post-processes with `apply_tempo` must rescale them (`subtitles.scale_cues`).

        Raises TtsError("đã dừng") if `cancelled()` turns true between chunks.
        """
        from noveltrans.tts.subtitles import Cue
        import numpy as np

        # The title gets a terminator first: without one the model runs it straight into
        # the body, because merge_short_chunks absorbs a short title into the first body
        # chunk with only a space between. See ensure_sentence_end.
        text = f"{ensure_sentence_end(title)}\n\n{body}" if title else body
        if clean:
            text = clean_for_tts(text, clean_extra_remove)
        chunks = split_sentences(text, self.max_chunk_chars)
        chunks = merge_short_chunks(chunks, self.min_chunk_chars, self.max_chunk_chars)
        if not chunks:
            raise TtsError("Chương không có nội dung để đọc.")

        gap_len = self.paragraph_gap_seconds if gap_seconds is None else gap_seconds
        gap = np.zeros(int(self.sample_rate * gap_len), dtype=np.float32)
        pieces: list = []
        # Counted in SAMPLES, not by summing float durations: the offsets have to be the
        # ones the concatenation actually produces, and float seconds accumulate error.
        offset = 0
        for chunk in chunks:
            if cancelled is not None and cancelled():
                raise TtsError("Đã dừng theo yêu cầu.")
            samples = np.asarray(self.synthesize(chunk), dtype=np.float32).reshape(-1)
            if pieces and gap.size:
                pieces.append(gap)
                offset += gap.size
            pieces.append(samples)
            if cues_out is not None:
                cues_out.append(
                    Cue(
                        offset / self.sample_rate,
                        (offset + samples.size) / self.sample_rate,
                        chunk,
                    )
                )
            offset += samples.size
        audio = np.concatenate(pieces)
        if volume != 1.0:
            audio = np.clip(audio * volume, -1.0, 1.0).astype(np.float32)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.save_wav(audio, out_path)
        return len(audio) / self.sample_rate

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


class TtsEngine(ABC):
    """One text-to-speech backend. Chunks long text and concatenates audio."""

    name: str = ""
    display_name: str = ""
    max_chunk_chars: int = 400
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
    ) -> float:
        """Synthesize title + body into one WAV. Returns audio duration (s).

        With `clean` (the default), special characters are stripped from the text
        before synthesis so the audio reads smoothly (see tts/clean.py); any characters
        in `clean_extra_remove` are stripped on top of that. Only the copy fed to the
        engine is cleaned — nothing stored is touched.

        `gap_seconds` overrides the silence between chunks (None = the engine's
        `paragraph_gap_seconds` default). `volume` is a linear gain on the rendered
        audio, hard-clipped to [-1, 1] so gains > 1.0 can't wrap around into noise.

        Raises TtsError("đã dừng") if `cancelled()` turns true between chunks.
        """
        import numpy as np

        text = f"{title}\n\n{body}" if title else body
        if clean:
            text = clean_for_tts(text, clean_extra_remove)
        chunks = split_sentences(text, self.max_chunk_chars)
        if not chunks:
            raise TtsError("Chương không có nội dung để đọc.")

        gap_len = self.paragraph_gap_seconds if gap_seconds is None else gap_seconds
        gap = np.zeros(int(self.sample_rate * gap_len), dtype=np.float32)
        pieces: list = []
        for chunk in chunks:
            if cancelled is not None and cancelled():
                raise TtsError("Đã dừng theo yêu cầu.")
            samples = np.asarray(self.synthesize(chunk), dtype=np.float32).reshape(-1)
            if pieces and gap.size:
                pieces.append(gap)
            pieces.append(samples)
        audio = np.concatenate(pieces)
        if volume != 1.0:
            audio = np.clip(audio * volume, -1.0, 1.0).astype(np.float32)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.save_wav(audio, out_path)
        return len(audio) / self.sample_rate

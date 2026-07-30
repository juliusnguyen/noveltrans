"""Feature 040 — subtitle cues timed from the TTS run, and the part `.srt` they build.

All pure: no engine, no ffmpeg, no project. The arithmetic here is the whole feature, and
the two ways it can silently desync (a speed post-process, and a chapter's offset inside a
part) are what most of these tests are about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from noveltrans.tts.subtitles import (
    Cue,
    build_srt,
    cues_path,
    format_srt_time,
    part_srt,
    read_cues,
    scale_cues,
    shift_cues,
    write_cues,
)


@dataclass
class _Seg:
    """Stand-in for MergeSegment — only `path` and `seconds` matter here."""

    path: Path
    seconds: float
    title: str = "Chương"


class TestFormatSrtTime:
    def test_uses_srts_comma_not_vtts_dot(self):
        """SRT parsers reject a dot. This is the one formatting detail that makes a file
        load or not."""
        assert format_srt_time(1.5) == "00:00:01,500"

    def test_pads_every_field(self):
        assert format_srt_time(0) == "00:00:00,000"
        assert format_srt_time(61.007) == "00:01:01,007"

    def test_handles_hours_because_a_part_is_three_of_them(self):
        assert format_srt_time(3 * 3600 + 36 * 60 + 39.25) == "03:36:39,250"

    def test_a_negative_clamps_to_zero_instead_of_emitting_junk(self):
        """A rounding error or a bad scale factor should put a cue at the start, not write
        a timestamp no parser accepts."""
        assert format_srt_time(-2.0) == "00:00:00,000"


class TestBuildSrt:
    def test_numbers_from_one_and_separates_with_blank_lines(self):
        srt = build_srt([Cue(0, 1.5, "Một"), Cue(1.6, 3.0, "Hai")])
        assert srt == (
            "1\n00:00:00,000 --> 00:00:01,500\nMột\n"
            "\n"
            "2\n00:00:01,600 --> 00:00:03,000\nHai\n"
        )

    def test_empty_cues_are_dropped_and_do_not_consume_a_number(self):
        """A chunk can clean down to nothing (feature 038's punctuation-only lines). An
        empty numbered block makes some players flash a blank caption box."""
        srt = build_srt([Cue(0, 1, "Một"), Cue(1, 2, "   "), Cue(2, 3, "Ba")])
        assert "\n2\n" in srt
        assert "3\n" not in srt.split("-->")[-1]
        assert srt.count("-->") == 2

    def test_no_cues_is_an_empty_document_not_a_stray_newline(self):
        assert build_srt([]) == ""

    def test_a_newline_inside_a_cue_survives_as_a_real_line_break(self):
        srt = build_srt([Cue(0, 1, "Dòng một\nDòng hai")])
        assert "Dòng một\nDòng hai\n" in srt


class TestScaleCues:
    def test_speed_up_pulls_every_cue_earlier(self):
        """**The `_apply_speed` regression.** `apply_tempo` at 1.25× makes the audio 1/1.25
        as long; unscaled cues drift further out of sync the longer the chapter runs."""
        scaled = scale_cues([Cue(100.0, 104.0, "x")], 1 / 1.25)
        assert scaled[0].start == 80.0
        assert scaled[0].end == 83.2

    def test_the_last_cue_still_ends_at_the_reported_duration(self):
        """The end-to-end property: after scaling, the cues describe the audio that
        actually exists."""
        raw_seconds, speed = 200.0, 1.25
        cues = [Cue(0, 100, "a"), Cue(100, raw_seconds, "b")]
        scaled = scale_cues(cues, (raw_seconds / speed) / raw_seconds)
        assert scaled[-1].end == raw_seconds / speed

    def test_a_factor_of_one_changes_nothing(self):
        cues = [Cue(1.0, 2.0, "x")]
        assert scale_cues(cues, 1.0) == cues


class TestShiftCues:
    def test_moves_a_chapter_into_its_place_in_the_part(self):
        shifted = shift_cues([Cue(0, 2, "x")], 630.5)
        assert (shifted[0].start, shifted[0].end) == (630.5, 632.5)


class TestCuesRoundTrip:
    def test_written_cues_read_back_identically(self, tmp_path):
        audio = tmp_path / "0001.mp3"
        cues = [Cue(0.0, 1.25, "Một"), Cue(1.35, 3.5, "Hai — có dấu")]
        write_cues(audio, cues, seconds=3.5)
        back, seconds = read_cues(audio)
        assert back == cues
        assert seconds == 3.5

    def test_the_sidecar_sits_beside_the_audio(self, tmp_path):
        assert cues_path(tmp_path / "0001.mp3") == tmp_path / "0001.cues.json"

    def test_a_missing_sidecar_reads_as_empty_not_an_error(self, tmp_path):
        """The normal case for every chapter voiced before this feature existed."""
        assert read_cues(tmp_path / "nope.mp3") == ([], 0.0)

    def test_a_corrupt_sidecar_reads_as_empty_and_never_raises(self, tmp_path):
        """A subtitle file must not be able to fail a video render."""
        audio = tmp_path / "0001.mp3"
        cues_path(audio).write_text("{not json", encoding="utf-8")
        assert read_cues(audio) == ([], 0.0)

    def test_a_partially_broken_sidecar_keeps_the_rows_it_can_parse(self, tmp_path):
        audio = tmp_path / "0001.mp3"
        cues_path(audio).write_text(
            '{"version":1,"seconds":2,"cues":[[0,1,"ok"],["bad"],[1,2,"fine"]]}',
            encoding="utf-8",
        )
        cues, _ = read_cues(audio)
        assert [c.text for c in cues] == ["ok", "fine"]

    def test_vietnamese_survives_the_json_round_trip(self, tmp_path):
        audio = tmp_path / "0001.mp3"
        write_cues(audio, [Cue(0, 1, "Chào mừng đến với phòng livestream ác mộng")], seconds=1)
        assert read_cues(audio)[0][0].text == "Chào mừng đến với phòng livestream ác mộng"


class TestPartSrt:
    def _chapter(self, tmp_path, name, cues, seconds):
        audio = tmp_path / name
        audio.write_bytes(b"audio")
        if cues:
            write_cues(audio, cues, seconds=seconds)
        return _Seg(audio, seconds)

    def test_later_chapters_are_offset_by_the_earlier_ones(self, tmp_path):
        segs = [
            self._chapter(tmp_path, "a.mp3", [Cue(0, 5, "Một")], 60.0),
            self._chapter(tmp_path, "b.mp3", [Cue(0, 5, "Hai")], 60.0),
        ]
        srt, covered, total = part_srt(segs)
        assert (covered, total) == (2, 2)
        assert "00:00:00,000 --> 00:00:05,000\nMột" in srt
        assert "00:01:00,000 --> 00:01:05,000\nHai" in srt

    def test_a_chapter_without_cues_still_advances_the_offset(self, tmp_path):
        """**What makes a partially-covered part useful instead of misleading**: the
        subtitles that do exist are in the right place."""
        segs = [
            self._chapter(tmp_path, "a.mp3", None, 60.0),  # legacy audio, no cues
            self._chapter(tmp_path, "b.mp3", [Cue(0, 5, "Hai")], 60.0),
        ]
        srt, covered, total = part_srt(segs)
        assert (covered, total) == (1, 2)
        assert "00:01:00,000 --> 00:01:05,000\nHai" in srt

    def test_a_part_with_no_cues_at_all_produces_nothing(self, tmp_path):
        segs = [self._chapter(tmp_path, "a.mp3", None, 60.0)]
        srt, covered, total = part_srt(segs)
        assert srt == ""
        assert (covered, total) == (0, 1)

    def test_cue_numbering_is_continuous_across_chapters(self, tmp_path):
        segs = [
            self._chapter(tmp_path, "a.mp3", [Cue(0, 1, "A1"), Cue(1, 2, "A2")], 10.0),
            self._chapter(tmp_path, "b.mp3", [Cue(0, 1, "B1")], 10.0),
        ]
        srt, _c, _t = part_srt(segs)
        assert srt.startswith("1\n")
        assert "\n3\n" in srt  # B1 continues the numbering, it doesn't restart


class TestExpectedGap:
    def test_speed_shortens_the_gap_in_the_finished_file(self):
        """`apply_tempo` rescales the whole chapter, silence included. Detecting with the
        unscaled value would miss every gap in a sped-up file — and speed is the setting
        most likely to be non-default."""
        from noveltrans.tts.subtitles import expected_gap

        assert expected_gap(0.4, 1.15) == pytest.approx(0.4 / 1.15)
        assert expected_gap(0.4, 1.0) == 0.4

    def test_a_zero_speed_does_not_divide_by_zero(self):
        from noveltrans.tts.subtitles import expected_gap

        assert expected_gap(0.4, 0) == 0.4


class TestChunksForText:
    def test_it_reproduces_what_synthesis_would_have_chunked(self, tmp_path):
        """The backfill's whole premise: the chunk list is deterministic, so it can be
        re-derived from the same text and settings without the engine."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from test_tts import FakeTtsEngine

        from noveltrans.tts.subtitles import chunks_for_text

        engine = FakeTtsEngine()
        body = "Cau mot rat dai va day du.\n\nCau hai cung dai nhu vay."
        engine.synthesize_chapter("", body, tmp_path / "x.wav")
        assert chunks_for_text(
            "", body, clean=True, extra_remove="",
            max_chars=engine.max_chunk_chars, min_chars=engine.min_chunk_chars,
        ) == engine.chunks


class TestBackfillRefusesRatherThanGuesses:
    """The safety argument for the whole feature: cues are returned only when the silence
    pattern in the file matches the text we think produced it."""

    # Each paragraph must clear the 30-char merge floor, or `merge_short_chunks` fuses
    # them into ONE chunk and every count assertion below becomes vacuously true.
    TWO_CHUNKS = (
        "Cau mot nay du dai de dung mot minh khong bi gop.\n\n"
        "Cau hai cung du dai de dung mot minh khong bi gop."
    )

    def test_the_fixture_really_makes_two_chunks(self):
        """Guards every count assertion in this class."""
        from noveltrans.tts.subtitles import chunks_for_text

        assert len(chunks_for_text(
            "", self.TWO_CHUNKS, clean=True, extra_remove="", max_chars=120, min_chars=30
        )) == 2

    def _fake_silences(self, monkeypatch, gaps):
        """Patch the calibrating finder: it returns the gaps only when the count matches,
        so a test supplying the wrong number gets `[]`, exactly as the real one does."""
        import noveltrans.tts.subtitles as mod

        monkeypatch.setattr(
            mod, "find_chunk_gaps",
            lambda p, *, want, gap: gaps if len(gaps) == want else [],
        )

    def test_a_single_chunk_needs_no_gaps_at_all(self, tmp_path, monkeypatch):
        from noveltrans.tts.subtitles import backfill_cues

        self._fake_silences(monkeypatch, [])
        cues = backfill_cues(tmp_path / "a.wav", "", "Một câu.", duration=5.0, gap_seconds=0.4)
        assert len(cues) == 1
        assert (cues[0].start, cues[0].end) == (0.0, 5.0)

    def test_too_few_gaps_returns_none(self, tmp_path, monkeypatch):
        """Fewer gaps than expected means the audio doesn't match the text — a chapter
        edited after voicing, or a settings change. Skipping beats confidently wrong."""
        from noveltrans.tts.subtitles import backfill_cues

        body = "\n\n".join(f"Cau so {i} nay du dai de dung mot minh khong bi gop." for i in range(4))
        self._fake_silences(monkeypatch, [(5.0, 5.4)])  # 1 gap, 3 expected
        assert backfill_cues(
            tmp_path / "a.wav", "", body, duration=40.0, gap_seconds=0.4,
            max_chars=120, min_chars=30,
        ) is None

    def test_too_many_gaps_returns_none(self, tmp_path, monkeypatch):
        from noveltrans.tts.subtitles import backfill_cues

        body = self.TWO_CHUNKS
        self._fake_silences(monkeypatch, [(3.0, 3.4), (6.0, 6.4), (9.0, 9.4)])
        assert backfill_cues(
            tmp_path / "a.wav", "", body, duration=20.0, gap_seconds=0.4,
            max_chars=120, min_chars=30,
        ) is None

    def test_gap_length_is_not_used_as_a_filter(self, tmp_path, monkeypatch):
        """Measured on real audio: `silencedetect` reports a region slightly INSIDE the
        gap, because speech trails off around it. A real 0.348 s gap is never reported as
        0.348 s, so filtering by length rejected every one of them — 76 expected, 7 kept.
        Length must not gate the result; the count does."""
        from noveltrans.tts.subtitles import backfill_cues

        # a "gap" nothing like the nominal 0.4 s — still accepted, because the count fits
        self._fake_silences(monkeypatch, [(6.0, 6.05)])
        cues = backfill_cues(
            tmp_path / "a.wav", "", self.TWO_CHUNKS, duration=13.0, gap_seconds=0.4,
            max_chars=120, min_chars=30,
        )
        assert cues is not None and len(cues) == 2

    def test_matching_gaps_produce_cues_bounded_by_them(self, tmp_path, monkeypatch):
        from noveltrans.tts.subtitles import backfill_cues

        body = self.TWO_CHUNKS
        self._fake_silences(monkeypatch, [(6.0, 6.4)])
        cues = backfill_cues(
            tmp_path / "a.wav", "", body, duration=13.0, gap_seconds=0.4,
            max_chars=120, min_chars=30,
        )
        assert [(c.start, c.end) for c in cues] == [(0.0, 6.0), (6.4, 13.0)]

    def test_a_zero_duration_chapter_returns_none(self, tmp_path, monkeypatch):
        from noveltrans.tts.subtitles import backfill_cues

        self._fake_silences(monkeypatch, [])
        assert backfill_cues(tmp_path / "a.wav", "", "Một câu.", duration=0.0,
                             gap_seconds=0.4) is None

    def test_an_empty_chapter_returns_none(self, tmp_path, monkeypatch):
        from noveltrans.tts.subtitles import backfill_cues

        self._fake_silences(monkeypatch, [])
        assert backfill_cues(tmp_path / "a.wav", "", "   ", duration=10.0,
                             gap_seconds=0.4) is None


class TestDetectSilences:
    def test_missing_ffmpeg_reads_as_no_silences_not_a_crash(self, tmp_path, monkeypatch):
        import subprocess

        import noveltrans.tts.subtitles as mod

        def boom(*a, **k):
            raise FileNotFoundError("no ffmpeg")

        monkeypatch.setattr(subprocess, "run", boom)
        assert mod.detect_silences(tmp_path / "a.wav", min_seconds=0.3) == []

    def test_it_parses_ffmpegs_stderr_format(self, tmp_path, monkeypatch):
        import subprocess

        import noveltrans.tts.subtitles as mod

        class _R:
            stderr = (
                "[silencedetect @ 0x1] silence_start: 3.2\n"
                "[silencedetect @ 0x1] silence_end: 3.6 | silence_duration: 0.4\n"
                "[silencedetect @ 0x1] silence_start: 10.5\n"
                "[silencedetect @ 0x1] silence_end: 10.9 | silence_duration: 0.4\n"
            )

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        assert mod.detect_silences(tmp_path / "a.wav", min_seconds=0.3) == [
            (3.2, 3.6), (10.5, 10.9)
        ]


class TestThresholdCalibration:
    """The count expected from the text is used to CHOOSE the silence threshold, not only
    to check the answer. Measured on a real chapter with known cues:

        -50 dB -> 98 silences (catches quiet speech)   -70 dB -> 76  EXACT
        -60 dB -> 77 silences                          -80 dB -> 76  EXACT
    """

    def test_it_takes_the_first_threshold_that_yields_the_expected_count(self, monkeypatch):
        import noveltrans.tts.subtitles as mod

        by_db = {-90: [], -80: [(1, 1.3), (2, 2.3)], -70: [(1, 1.3)], -60: [], -50: []}
        tried: list = []

        def fake(path, *, min_seconds, noise_db=-80):
            tried.append(noise_db)
            return by_db[noise_db]

        monkeypatch.setattr(mod, "detect_silences", fake)
        assert mod.find_chunk_gaps("a.wav", want=2, gap=0.35) == [(1, 1.3), (2, 2.3)]
        assert tried == [-90, -80]  # stopped as soon as the count matched

    def test_no_threshold_matching_the_count_returns_nothing(self, monkeypatch):
        """Refusal, not a best guess — the chapter is skipped and reported."""
        import noveltrans.tts.subtitles as mod

        monkeypatch.setattr(
            mod, "detect_silences", lambda p, *, min_seconds, noise_db=-80: [(1, 2)] * 9
        )
        assert mod.find_chunk_gaps("a.wav", want=2, gap=0.35) == []

    def test_the_quietest_threshold_is_tried_first(self):
        """Digital silence is -inf dB; quiet speech is not. Starting loud would match
        speech pauses on a chapter where the quiet threshold would have been exact."""
        from noveltrans.tts.subtitles import _SILENCE_THRESHOLDS_DB

        assert list(_SILENCE_THRESHOLDS_DB) == sorted(_SILENCE_THRESHOLDS_DB)

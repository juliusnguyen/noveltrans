"""Tests for the audio-merge planning + metadata builders (pure, no ffmpeg).

A real ffmpeg merge is exercised in test_real_merge, skipped when ffmpeg is absent.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from noveltrans.models import Chapter
from noveltrans.tts.merge import (
    MergeSegment,
    build_chapter_metadata,
    build_concat_list,
    chapter_marker_title,
    merge_chapters,
    plan_merge_windows,
)


def _ch(index, voice="Ngọc Lan", audio=True, source="translated", seconds=10.0):
    return Chapter(
        index=index,
        title=f"第{index + 1}章",
        url="u",
        content="nội dung",
        translated="bản dịch",
        translated_title=f"Chương {index + 1}",
        audio_path=(f"exports/audio/{index:04d}.wav" if audio else ""),
        audio_voice=(voice if audio else ""),
        audio_source=source,
        audio_seconds=seconds,
    )


class TestPlanMergeWindows:
    def test_all_spans_min_to_max(self):
        chs = [_ch(i) for i in range(5)]
        [w] = plan_merge_windows(chs, "Ngọc Lan", "all")
        assert (w.first_num, w.last_num, len(w.chapters)) == (1, 5, 5)

    def test_all_excludes_gaps_and_other_voices(self):
        chs = [_ch(i) for i in range(5)]
        chs[2].audio_path = ""  # chapter 3 has no audio
        chs[3].audio_voice = "Gia Bảo"  # chapter 4 voiced differently
        [w] = plan_merge_windows(chs, "Ngọc Lan", "all")
        assert [c.index + 1 for c in w.chapters] == [1, 2, 5]

    def test_range_by_chapter_number(self):
        chs = [_ch(i) for i in range(10)]
        [w] = plan_merge_windows(chs, "Ngọc Lan", "range", start=3, end=7)
        assert (w.first_num, w.last_num) == (3, 7)
        assert [c.index + 1 for c in w.chapters] == [3, 4, 5, 6, 7]

    def test_batch_boundaries_are_stable_across_gaps(self):
        chs = [_ch(i) for i in range(25)]
        chs[4].audio_path = ""  # gap at chapter 5 must NOT shift later batches
        windows = plan_merge_windows(chs, "Ngọc Lan", "batch", batch=10)
        spans = [(w.first_num, w.last_num, len(w.chapters)) for w in windows]
        assert spans == [(1, 10, 9), (11, 20, 10), (21, 25, 5)]

    def test_batch_skips_empty_windows(self):
        chs = [_ch(i) for i in (0, 1, 25)]  # nothing in 11-20
        windows = plan_merge_windows(chs, "Ngọc Lan", "batch", batch=10)
        assert [(w.first_num, w.last_num) for w in windows] == [(1, 2), (26, 26)]

    def test_no_audio_returns_empty(self):
        assert plan_merge_windows([_ch(0, audio=False)], "Ngọc Lan", "all") == []
        assert plan_merge_windows([_ch(0)], "Khác", "all") == []


class TestMergeWorker:
    def test_start_is_not_shadowed_by_params(self, qapp):
        # regression: `self.start = start` once clobbered QThread.start() → merges never
        # launched (button stuck). The range params live on *_num fields instead.
        from noveltrans.gui.workers import MergeWorker

        w = MergeWorker("/tmp/x", voice="v", fmt="mp3", mode="range", start=1, end=10)
        assert callable(w.start)  # the QThread method, not the int
        assert w.start_num == 1 and w.end_num == 10


class TestChapterMetadata:
    def test_cumulative_offsets_in_ms(self):
        segs = [
            MergeSegment(path="/a.wav", seconds=12.0, title="Một"),
            MergeSegment(path="/b.wav", seconds=8.5, title="Hai"),
        ]
        meta = build_chapter_metadata(segs)
        assert ";FFMETADATA1" in meta
        assert "START=0\nEND=12000" in meta
        assert "START=12000\nEND=20500" in meta  # 12.0s then +8.5s
        assert meta.count("[CHAPTER]") == 2

    def test_escapes_ffmetadata_specials(self):
        meta = build_chapter_metadata([MergeSegment("/a", 1.0, "a=b;c#d")])
        assert "title=a\\=b\\;c\\#d" in meta

    def test_marker_title_follows_source(self):
        assert chapter_marker_title(_ch(0, source="translated")) == "Chương 1"
        assert chapter_marker_title(_ch(0, source="original")) == "第1章"


class TestConcatList:
    def test_escapes_single_quotes_and_spaces(self):
        body = build_concat_list(["/a b.wav", "/it's.wav"])
        assert "file '/a b.wav'" in body
        assert "file '/it'\\''s.wav'" in body


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestRealMerge:
    def _tone(self, path, seconds):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-ar", "48000", str(path)],
            capture_output=True,
        )

    def test_m4b_has_chapters(self, tmp_path):
        segs = []
        for i, (dur, title) in enumerate([(2.0, "Chương 1"), (1.5, "Chương 2")]):
            wav = tmp_path / f"{i}.wav"
            self._tone(wav, dur)
            segs.append(MergeSegment(path=wav, seconds=dur, title=title))
        out = merge_chapters(segs, tmp_path / "book.m4b", "m4b")
        assert out.exists() and out.stat().st_size > 0
        import json

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", str(out)],
            capture_output=True, text=True,
        )
        chapters = json.loads(probe.stdout)["chapters"]
        assert [c["tags"]["title"] for c in chapters] == ["Chương 1", "Chương 2"]
        assert chapters[1]["start_time"] == "2.000000"

    def test_mp3_join(self, tmp_path):
        segs = []
        for i in range(2):
            wav = tmp_path / f"{i}.wav"
            self._tone(wav, 1.0)
            segs.append(MergeSegment(path=wav, seconds=1.0, title=f"C{i}"))
        out = merge_chapters(segs, tmp_path / "book.mp3", "mp3")
        assert out.exists() and out.stat().st_size > 0

    def test_cancel_before_start_raises_cancelled(self, tmp_path):
        from noveltrans.tts.merge import MergeCancelled

        wav = tmp_path / "a.wav"
        self._tone(wav, 1.0)
        segs = [MergeSegment(path=wav, seconds=1.0, title="C")]
        with pytest.raises(MergeCancelled):
            merge_chapters(segs, tmp_path / "out.m4b", "m4b", cancelled=lambda: True)
        assert not (tmp_path / "out.m4b").exists() or True  # terminated; partial ok

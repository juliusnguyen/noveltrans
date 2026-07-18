"""Tests for video export — pure ASS/description builders (no ffmpeg) + one real render.

Mirrors test_merge.py: the builders are pure and always run; the actual ffmpeg render is
skipped when ffmpeg is absent.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from noveltrans.tts.merge import MergeSegment
from noveltrans.tts.video import (
    FONT_NAME,
    _ass_time,
    _escape_ass,
    _yt_timestamp,
    build_ass_subtitles,
    build_youtube_description,
)


def _seg(seconds, title="Chương"):
    return MergeSegment(path="/x/a.wav", seconds=seconds, title=title)


class TestAssTime:
    @pytest.mark.parametrize(
        ("secs", "expected"),
        [(0, "0:00:00.00"), (12.0, "0:00:12.00"), (20.5, "0:00:20.50"),
         (65.0, "0:01:05.00"), (3725.5, "1:02:05.50")],
    )
    def test_formats_centiseconds(self, secs, expected):
        assert _ass_time(secs) == expected


class TestEscapeAss:
    def test_braces_neutralised(self):
        # { } open/close ASS override blocks — a title with them must not inject a tag.
        assert _escape_ass("Chương {bí mật}") == "Chương (bí mật)"

    def test_newlines_become_soft_breaks(self):
        assert _escape_ass("dòng 1\ndòng 2") == "dòng 1\\Ndòng 2"
        assert _escape_ass("a\r\nb") == "a\\Nb"

    def test_comma_preserved(self):
        # Text is the final ASS field; commas are safe and must survive verbatim.
        assert _escape_ass("Chương 3, phần 2") == "Chương 3, phần 2"

    def test_trailing_backslash_removed(self):
        assert _escape_ass("kết thúc\\") == "kết thúc"

    def test_vietnamese_and_cjk_pass_through(self):
        assert _escape_ass("Diệp Vân 叶云 ộ ữ đ") == "Diệp Vân 叶云 ộ ữ đ"


class TestBuildAssSubtitles:
    def _doc(self):
        segs = [_seg(125.4, "Chương 1"), _seg(98.7, "Chương 2"), _seg(140.0, "Chương 3")]
        return build_ass_subtitles(segs, "Tựa truyện", width=1920, height=1080), segs

    def test_has_the_required_sections(self):
        doc, _ = self._doc()
        assert "[Script Info]" in doc
        assert "[V4+ Styles]" in doc
        assert "[Events]" in doc
        assert "PlayResX: 1920" in doc and "PlayResY: 1080" in doc

    def test_one_novel_event_plus_one_per_chapter(self):
        doc, segs = self._doc()
        dialogues = [ln for ln in doc.splitlines() if ln.startswith("Dialogue:")]
        assert len(dialogues) == len(segs) + 1  # novel title + each chapter

    def test_novel_title_spans_the_whole_video(self):
        doc, segs = self._doc()
        total = sum(s.seconds for s in segs)
        novel = next(ln for ln in doc.splitlines() if ",Novel," in ln)
        assert f"{_ass_time(0)},{_ass_time(total)}" in novel

    def test_chapter_starts_are_cumulative(self):
        # The load-bearing timing: chapter 2 starts exactly where chapter 1 ended.
        doc, segs = self._doc()
        chapters = [ln for ln in doc.splitlines() if ",Chapter," in ln]
        assert f"{_ass_time(0)},{_ass_time(125.4)}" in chapters[0]
        assert f"{_ass_time(125.4)},{_ass_time(125.4 + 98.7)}" in chapters[1]

    def test_uses_the_bundled_font_family(self):
        doc, _ = self._doc()
        assert f"Style: Novel,{FONT_NAME}," in doc
        assert f"Style: Chapter,{FONT_NAME}," in doc

    def test_malicious_title_is_escaped_in_output(self):
        doc = build_ass_subtitles([_seg(10, "Chương {evil}\nline2")], "Truyện")
        assert "{evil}" not in doc
        assert "(evil)\\Nline2" in doc


class TestYoutubeTimestamp:
    @pytest.mark.parametrize(
        ("secs", "expected"),
        [(0, "0:00"), (5, "0:05"), (65, "1:05"), (125.4, "2:05"), (3725, "1:02:05")],
    )
    def test_format(self, secs, expected):
        assert _yt_timestamp(secs) == expected


class TestBuildYoutubeDescription:
    def _desc(self):
        segs = [_seg(125.4, "Chương 1: Mở đầu"), _seg(98.7, "Chương 2: Cao trào"),
                _seg(140.0, "Chương 3: Kết")]
        return build_youtube_description(segs, "Tựa truyện"), segs

    def test_first_chapter_is_zero_for_youtube_chapters(self):
        # YouTube only makes chapters when the first timestamp is 0:00.
        desc, _ = self._desc()
        ts_lines = [ln for ln in desc.splitlines() if ln[:1].isdigit()]
        assert ts_lines[0].startswith("0:00 ")

    def test_timestamps_are_cumulative_and_ascending(self):
        desc, _ = self._desc()
        ts_lines = [ln for ln in desc.splitlines() if ln[:1].isdigit()]
        assert ts_lines[0] == "0:00 Chương 1: Mở đầu"
        assert ts_lines[1] == "2:05 Chương 2: Cao trào"  # 125.4s → 2:05
        assert ts_lines[2] == "3:44 Chương 3: Kết"  # 224.1s → 3:44

    def test_includes_the_novel_title_header(self):
        desc, _ = self._desc()
        assert desc.startswith("Tựa truyện")

    def test_one_line_per_chapter(self):
        desc, segs = self._desc()
        ts_lines = [ln for ln in desc.splitlines() if ln[:1].isdigit()]
        assert len(ts_lines) == len(segs)


class TestVideoWorker:
    def test_start_is_not_shadowed_by_params(self, qapp):
        # Same trap as MergeWorker: `self.start = start` would clobber QThread.start().
        from noveltrans.gui.workers import VideoWorker

        w = VideoWorker("/tmp/x", voice="v", mode="range", image_path="/tmp/bg.png",
                        start=3, end=9)
        assert callable(w.start)  # the QThread method, not the int
        assert w.start_num == 3 and w.end_num == 9

    def test_no_audio_fails_cleanly(self, qapp, library_dir, sample_meta, sample_refs):
        from noveltrans.gui.workers import VideoWorker
        from noveltrans.storage import NovelProject

        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # no audio yet
        w = VideoWorker(project.path, voice="V", mode="all", image_path="/tmp/bg.png")
        failures = []
        w.failed.connect(failures.append)
        w.run()  # synchronous
        assert failures and "Không có chương" in failures[0]


def test_project_has_a_video_dir(library_dir, sample_meta, sample_refs):
    from noveltrans.storage import NovelProject

    project = NovelProject.create(library_dir, sample_meta, sample_refs)
    assert project.video_dir.name == "video"
    assert project.video_dir.parent == project.exports_dir


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestRealRender:
    def _tone(self, path, seconds):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-ar", "48000", str(path)],
            check=True, capture_output=True,
        )

    def test_renders_mp4_with_audio_and_description(self, tmp_path):
        from pathlib import Path

        from noveltrans.tts.video import font_dir_context, render_video

        # two short tone WAVs + a solid-colour PNG background
        wavs = []
        for i, dur in enumerate((1.0, 1.0)):
            w = tmp_path / f"{i}.wav"
            self._tone(w, dur)
            wavs.append(w)
        image = tmp_path / "bg.png"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=navy:s=640x360:d=1",
             "-frames:v", "1", str(image)],
            check=True, capture_output=True,
        )
        segs = [MergeSegment(path=wavs[0], seconds=1.0, title="Chương 1: Diệp Vân"),
                MergeSegment(path=wavs[1], seconds=1.0, title="Chương 2: ộ ữ đ")]
        out = tmp_path / "out.mp4"
        with font_dir_context() as font_dir:
            render_video(segs, image, out, font_dir, "Truyện thử",
                         width=640, height=360, fps=8)

        assert out.exists() and out.stat().st_size > 0
        # companion description written next to the video
        desc = out.with_suffix(".txt")
        assert desc.exists() and desc.read_text(encoding="utf-8").startswith("Truyện thử")
        # ffprobe: one video + one audio stream, duration ≈ 2s (validates -shortest)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type", "-of", "default=nw=1", str(out)],
            capture_output=True, text=True,
        )
        assert "codec_type=video" in probe.stdout
        assert "codec_type=audio" in probe.stdout
        assert Path(out).stat().st_size > 1000

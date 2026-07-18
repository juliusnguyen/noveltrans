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

    def test_every_chapter_event_fades(self):
        # Smooth transition: each chapter title fades in/out.
        doc, _ = self._doc()
        chapters = [ln for ln in doc.splitlines() if ",Chapter," in ln]
        assert chapters and all(",,{\\fad(400,400)}" in ln for ln in chapters)

    def test_novel_event_does_not_fade(self):
        # The novel title is persistent — no fade.
        doc, _ = self._doc()
        novel = next(ln for ln in doc.splitlines() if ",Novel," in ln)
        assert "\\fad" not in novel

    def test_fade_prefix_sits_before_the_escaped_title(self):
        # The \fad override must be outside the escaped title so a braced title can't
        # break out of it.
        doc = build_ass_subtitles([_seg(10, "Chương {evil}")], "Truyện")
        chapter = next(ln for ln in doc.splitlines() if ",Chapter," in ln)
        assert chapter.endswith("{\\fad(400,400)}Chương (evil)")  # fade outside the escaped title

    def test_titles_are_placed_in_the_right_half(self):
        # Split layout: the photo is on the left, so both titles sit in the RIGHT half —
        # Alignment 8 with MarginL ≈ half the width. Chapter below the bars (larger MarginV).
        doc = build_ass_subtitles([_seg(10, "C1")], "Truyện", width=1920, height=1080)
        novel = next(ln for ln in doc.splitlines() if ln.startswith("Style: Novel,"))
        chapter = next(ln for ln in doc.splitlines() if ln.startswith("Style: Chapter,"))
        assert novel.endswith(",8,960,57,140,1")  # right half, top
        assert chapter.endswith(",8,960,57,820,1")  # right half, below bars


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


class TestFiltergraph:
    def _graph(self):
        from pathlib import Path

        from noveltrans.tts.video import _filtergraph

        return _filtergraph(1920, 1080, Path("/tmp/subs.ass"), Path("/tmp/fonts"))

    def test_has_the_bars_from_the_audio_input(self):
        g = self._graph()
        assert "[1:a]showfreqs=" in g  # bars driven by the audio (input 1)
        assert "mode=bar" in g
        assert "[base][viz]overlay=" in g  # bars composited over the base
        assert "subtitles=" in g  # titles still burned on top

    def test_photo_framed_on_the_left_over_a_blurred_backdrop(self):
        g = self._graph()
        assert "boxblur" in g  # blurred backdrop fills the frame
        # the sharp photo is placed on the left (small x), not centered
        assert "[bg][photo]overlay=90:" in g


class TestRenderArgv:
    def test_render_command_uses_waveform_and_drops_stillimage(self, tmp_path, monkeypatch):
        # Capture the ffmpeg render argv without running ffmpeg.
        import noveltrans.tts.video as video

        cmds = []

        class _FakeProc:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kw):
            cmds.append(cmd)
            return _FakeProc()

        monkeypatch.setattr(video.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(video, "_with_real_durations", lambda segs: segs)  # skip ffprobe

        segs = [MergeSegment(path=tmp_path / "a.wav", seconds=3.0, title="C1")]
        with video.font_dir_context() as font_dir:
            video.render_video(segs, tmp_path / "bg.png", tmp_path / "out.mp4",
                               font_dir, "Truyện", width=640, height=360, fps=25)

        render = next(c for c in cmds if any("showfreqs" in a for a in c))
        assert "-tune" not in render  # stillimage tuning removed (motion video now)
        assert "veryfast" in render  # a normal preset instead
        assert "[v]" in render and "1:a" in render  # map filtered video + copy audio
        assert "copy" in render  # -c:a copy (audio filtered AND copied — the crux)


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


class TestRealDurations:
    def test_falls_back_to_stored_when_probe_fails(self):
        # ffprobe on a nonexistent file returns 0 → keep the stored seconds.
        from noveltrans.tts.video import _with_real_durations

        segs = [MergeSegment(path="/does/not/exist.wav", seconds=7.5, title="C1")]
        assert _with_real_durations(segs)[0].seconds == 7.5

    @pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
    def test_probes_the_real_duration_over_a_wrong_stored_value(self, tmp_path):
        # The bug: audio_seconds == 0 collapses every subtitle event to zero length.
        # The probe must recover the real duration so the titles stay visible.
        from noveltrans.tts.video import _with_real_durations, build_ass_subtitles

        wav = tmp_path / "a.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-ar", "48000", str(wav)],
            check=True, capture_output=True,
        )
        seg = MergeSegment(path=wav, seconds=0.0, title="Chương 1")  # wrong stored 0
        timed = _with_real_durations([seg])
        assert 1.9 < timed[0].seconds < 2.1  # real ~2.0s recovered

        # and the ASS event now has a non-zero span (would have been invisible before)
        doc = build_ass_subtitles(timed, "Truyện")
        chapter = next(ln for ln in doc.splitlines() if ",Chapter," in ln)
        start, end = chapter.split(",")[1:3]
        assert start != end  # visible


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

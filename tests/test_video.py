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

    def test_titles_are_placed_in_the_right_column(self):
        # 'Now playing' block: both titles sit in the right column (photo is on the left)
        # via Alignment 8 + PlayerLayout margins — novel above, chapter just below it.
        from noveltrans.tts.player_skin import PlayerLayout

        lay = PlayerLayout.of(1920, 1080)
        doc = build_ass_subtitles([_seg(10, "C1")], "Truyện", width=1920, height=1080)
        novel = next(ln for ln in doc.splitlines() if ln.startswith("Style: Novel,"))
        chapter = next(ln for ln in doc.splitlines() if ln.startswith("Style: Chapter,"))
        assert novel.endswith(f",8,{lay.text_margin_l},{lay.text_margin_r},{lay.novel_margin_v},1")
        assert chapter.endswith(f",8,{lay.text_margin_l},{lay.text_margin_r},{lay.chapter_margin_v},1")
        assert lay.novel_margin_v < lay.chapter_margin_v  # novel above the chapter line


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

        return _filtergraph(1920, 1080, Path("/tmp/subs.ass"), Path("/tmp/fonts"), 100.0)

    def test_has_the_bars_from_the_audio_input(self):
        g = self._graph()
        assert "[1:a]showfreqs=" in g  # bars driven by the audio (input 1)
        assert "mode=bar" in g
        assert "[s1][viz]overlay=" in g  # bars composited over the spun-vinyl base
        assert "subtitles=" in g  # titles still burned on top

    def test_bars_are_in_the_right_column_and_purple(self):
        # The skin is pre-baked (no photo/blur here); the bars sit in the right column
        # where the old progress bar was, in a purple that reads over the light skin.
        from noveltrans.tts.player_skin import PlayerLayout

        lay = PlayerLayout.of(1920, 1080)
        g = self._graph()
        assert "boxblur" not in g  # the backdrop is baked into the skin, not done here
        assert f"showfreqs=s={lay.bars_w}x{lay.bars_h}" in g
        assert "colors=0x8a52c8" in g  # purple, visible on the pastel skin
        assert f"[s1][viz]overlay={lay.bars_x}:{lay.bars_y}" in g

    def test_vinyl_spins_in_place_over_the_skin(self):
        # The vinyl (input 2) rotates by an angle growing with time, overlaid at its box.
        from noveltrans.tts.player_skin import PlayerLayout

        lay = PlayerLayout.of(1920, 1080)
        g = self._graph()
        assert "[2:v]format=rgba,rotate=a='2*PI*t/" in g  # spins with playback time
        assert "fillcolor=none" in g and "ow=iw:oh=ih" in g  # transparent, same frame
        assert f"[0:v][vin]overlay={lay.vinyl_x}:{lay.vinyl_y}" in g

    def test_knob_slides_along_the_track_with_progress(self):
        # The playhead (input 3) x is a linear function of t/total across the track.
        from noveltrans.tts.player_skin import PlayerLayout

        lay = PlayerLayout.of(1920, 1080)
        g = self._graph()
        assert f"[s2][3:v]overlay=x='{lay.track_x}+(t/100.0)*{lay.track_w}-" in g
        assert f":y={lay.track_y - lay.knob_half}" in g

    def test_zero_total_does_not_divide_by_zero(self):
        # An empty/zero-duration render must still build a valid knob expression.
        from pathlib import Path

        from noveltrans.tts.video import _filtergraph

        g = _filtergraph(1920, 1080, Path("/tmp/s.ass"), Path("/tmp/f"), 0.0)
        assert "(t/0)" not in g  # guarded against a zero divide


class TestRenderArgv:
    def test_render_command_uses_bars_and_drops_stillimage(self, tmp_path, monkeypatch):
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
        # audio concat has its own subprocess/pipe dance (tested separately); stub it here
        # so this test isolates the render argv.
        monkeypatch.setattr(video, "_concat_audio", lambda *a, **k: None)

        segs = [MergeSegment(path=tmp_path / "a.wav", seconds=3.0, title="C1")]
        with video.font_dir_context() as font_dir:
            video.render_video(segs, tmp_path / "bg.png", tmp_path / "out.mp4",
                               font_dir, "Truyện", width=640, height=360, fps=25)

        render = next(c for c in cmds if any("showfreqs" in a for a in c))
        assert "-tune" not in render  # stillimage tuning removed (motion video now)
        assert "veryfast" in render  # a normal preset instead
        assert "[v]" in render and "1:a" in render  # map filtered video + copy audio
        assert "copy" in render  # -c:a copy (audio filtered AND copied — the crux)
        assert any("rotate=a=" in a for a in render)  # the vinyl spins
        assert render.count("-loop") == 3  # skin + vinyl + knob are looped stills

    def test_audio_concat_avoids_the_concat_demuxer(self, tmp_path, monkeypatch):
        # Regression guard for the >12.4h truncation bug: the concat demuxer overflows a
        # 32-bit timestamp counter (2**31 / 48000Hz), silently cutting long "toàn bộ"
        # videos. render_video must NOT shell out to `-f concat` for the audio.
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
        monkeypatch.setattr(video, "_with_real_durations", lambda segs: segs)
        monkeypatch.setattr(video, "_concat_audio", lambda *a, **k: None)

        segs = [MergeSegment(path=tmp_path / "a.wav", seconds=3.0, title="C1")]
        with video.font_dir_context() as font_dir:
            video.render_video(segs, tmp_path / "bg.png", tmp_path / "out.mp4",
                               font_dir, "Truyện", width=640, height=360, fps=25)

        # exact-token membership (not substring — the tmp_path name contains "concat")
        assert not any("concat" in cmd for cmd in cmds)  # no `-f concat` demuxer arg


class TestPlayerLayout:
    def test_scales_with_output_size(self):
        # Geometry is proportional to width/height, so any resolution stays laid out.
        from noveltrans.tts.player_skin import PlayerLayout

        small = PlayerLayout.of(960, 540)
        big = PlayerLayout.of(1920, 1080)
        assert big.photo_h == small.photo_h * 2
        assert big.vinyl_size == small.vinyl_size * 2
        assert big.chapter_font_px == small.chapter_font_px * 2
        assert abs(big.bars_w - small.bars_w * 2) <= 1  # proportional (rounding aside)

    def test_elements_are_stacked_in_the_right_column(self):
        from noveltrans.tts.player_skin import PlayerLayout

        lay = PlayerLayout.of(1920, 1080)
        assert lay.photo_x + lay.photo_w < lay.width * 0.5  # photo stays in the left half
        assert lay.bars_x > lay.width * 0.5  # bars in the right column now, not full-width
        # top-to-bottom on the right: chapter title, then bars, then the progress track
        assert lay.chapter_margin_v < lay.bars_y < lay.track_y
        assert lay.knob_half > lay.knob_r  # the knob PNG has room for its ring


class TestPlayerSkin:
    def test_builds_a_png_of_the_requested_size(self, tmp_path):
        from PIL import Image

        from noveltrans.tts.player_skin import build_player_skin

        # a small real photo to frame on the left
        photo = tmp_path / "p.png"
        Image.new("RGB", (400, 300), (200, 120, 60)).save(photo)
        out = tmp_path / "skin.png"
        build_player_skin(photo, out, width=640, height=360)
        assert out.exists()
        with Image.open(out) as im:
            assert im.size == (640, 360)

    def test_unreadable_photo_still_produces_a_skin(self, tmp_path):
        # A missing/corrupt image must not crash the render — a placeholder card is drawn.
        from PIL import Image

        from noveltrans.tts.player_skin import build_player_skin

        out = tmp_path / "skin.png"
        build_player_skin(tmp_path / "nope.png", out, width=640, height=360)
        assert out.exists()
        with Image.open(out) as im:
            assert im.size == (640, 360)

    def test_vinyl_is_a_square_rgba_disc_with_a_label(self, tmp_path):
        # ffmpeg rotates this in place, so it must be a square, transparent-cornered PNG.
        from PIL import Image

        from noveltrans.tts.player_skin import build_vinyl

        logo = tmp_path / "logo.png"
        Image.new("RGB", (300, 300), (180, 120, 60)).save(logo)
        out = tmp_path / "vinyl.png"
        build_vinyl(logo, out, size=200)
        with Image.open(out) as im:
            assert im.size == (200, 200)
            assert im.mode == "RGBA"
            assert im.getpixel((0, 0))[3] == 0  # corner is transparent (outside the disc)
            assert im.getpixel((100, 100))[3] == 255  # centre (label) is opaque

    def test_vinyl_survives_an_unreadable_logo(self, tmp_path):
        from PIL import Image

        from noveltrans.tts.player_skin import build_vinyl

        out = tmp_path / "vinyl.png"
        build_vinyl(tmp_path / "missing.png", out, size=160)  # no logo → plain label
        with Image.open(out) as im:
            assert im.size == (160, 160)

    def test_knob_png_matches_the_layout_offset(self, tmp_path):
        # The filtergraph centres the knob by subtracting knob_half, so the PNG side must
        # be exactly 2*knob_half — otherwise the playhead would sit off the track.
        from PIL import Image

        from noveltrans.tts.player_skin import PlayerLayout, build_knob

        lay = PlayerLayout.of(1920, 1080)
        out = tmp_path / "knob.png"
        build_knob(out, radius=lay.knob_r)
        with Image.open(out) as im:
            assert im.size == (lay.knob_half * 2, lay.knob_half * 2)


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
class TestConcatAudio:
    """The audio concat that replaced the (32-bit-overflowing) concat demuxer."""

    def _tone(self, path, seconds, freq=440, extra=()):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
             "-ar", "48000", *extra, str(path)],
            check=True, capture_output=True,
        )

    def _dur(self, path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nokey=1", str(path)],
            capture_output=True, text=True,
        ).stdout.strip()
        return float(out)

    def test_concatenates_mixed_formats_to_the_summed_duration(self, tmp_path):
        # Decodes each file independently (mixed wav/mp3, mono/stereo) and stitches the raw
        # PCM, so the output length is the sum — this is the path that, unlike `-f concat`,
        # does not truncate past ~12.4h.
        from noveltrans.tts.video import _concat_audio

        a = tmp_path / "a.wav"
        b = tmp_path / "b.mp3"
        c = tmp_path / "c.wav"
        self._tone(a, 1.0, 300)
        self._tone(b, 0.5, 500)
        self._tone(c, 0.8, 700, extra=("-ac", "2"))  # stereo → normalised to mono
        out = tmp_path / "audio.m4a"
        _concat_audio([a, b, c], out, tmp_path / "err.txt", None, __import__("time").monotonic() + 60)
        assert out.exists()
        assert abs(self._dur(out) - 2.3) < 0.3  # 1.0 + 0.5 + 0.8, within codec padding

    def test_raises_when_a_chapter_cannot_be_decoded(self, tmp_path):
        from noveltrans.errors import TtsError
        from noveltrans.tts.video import _concat_audio

        good = tmp_path / "a.wav"
        self._tone(good, 0.5)
        bad = tmp_path / "broken.wav"
        bad.write_bytes(b"not audio at all")
        with pytest.raises(TtsError):
            _concat_audio([good, bad], tmp_path / "o.m4a", tmp_path / "e.txt",
                          None, __import__("time").monotonic() + 60)


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

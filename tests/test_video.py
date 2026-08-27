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

    def test_auto_wrap_is_enabled_so_long_titles_dont_overflow(self):
        # WrapStyle 0 lets libass break a long chapter title within the right-column margins
        # instead of running off-frame / over the left photo.
        doc, _ = self._doc()
        assert "WrapStyle: 0" in doc

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

    def test_default_uses_dark_text_on_light(self):
        # No chosen background → the original dark-on-light palette, no outline.
        doc, _ = self._doc()
        assert "&H00A06B8A" in doc  # muted grey-purple novel line
        assert "&H00502A55" in doc  # dark purple chapter line
        novel = next(ln for ln in doc.splitlines() if ln.startswith("Style: Novel,"))
        assert ",1,0,1,8," in novel  # BorderStyle=1, Outline=0, Shadow=1, Align=8

    def test_dark_background_flips_titles_to_light_text_with_outline(self):
        doc = build_ass_subtitles([_seg(10, "C1")], "Truyện", bg_color=(20, 24, 40))
        assert "&H00A06B8A" not in doc  # not the light-backdrop palette
        novel = next(ln for ln in doc.splitlines() if ln.startswith("Style: Novel,"))
        chapter = next(ln for ln in doc.splitlines() if ln.startswith("Style: Chapter,"))
        assert ",1,2,1,8," in novel    # a dark outline (width 2) added for legibility
        assert ",1,2,1,8," in chapter

    def test_light_custom_background_keeps_dark_text(self):
        # A light chosen colour still reads best with the dark palette.
        doc = build_ass_subtitles([_seg(10, "C1")], "Truyện", bg_color=(240, 235, 210))
        assert "&H00A06B8A" in doc

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

    def test_is_capped_to_the_youtube_limit(self):
        """Feature 065 — it writes the same `.txt` the rich builder does, and is what an
        upload reads if the metadata pass fails after a successful render."""
        from noveltrans.tts.description import (
            YOUTUBE_DESCRIPTION_CHAR_LIMIT,
            description_length,
        )

        segs = [_seg(300.0, f"Chương {i}: {'ả' * 45}") for i in range(400)]
        desc = build_youtube_description(segs, "Tựa truyện")
        assert description_length(desc) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT
        assert "Mục lục chương:" in desc


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

    def test_static_vinyl_skips_the_rotate(self):
        # The "fastest" preset drops the per-frame rotate: the disc is overlaid statically.
        from pathlib import Path

        from noveltrans.tts.player_skin import PlayerLayout
        from noveltrans.tts.video import _filtergraph

        lay = PlayerLayout.of(1920, 1080)
        g = _filtergraph(1920, 1080, Path("/tmp/s.ass"), Path("/tmp/f"), 100.0, spin_vinyl=False)
        assert "rotate=" not in g  # no per-frame rotate → much faster encode
        assert f"[0:v][2:v]overlay={lay.vinyl_x}:{lay.vinyl_y}" in g  # static overlay
        assert "showfreqs=" in g and "subtitles=" in g  # bars + titles still there

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


class TestVideoPresets:
    def test_presets_cover_the_three_speed_tiers(self):
        from noveltrans.tts.video import VIDEO_QUALITY_PRESETS

        high = VIDEO_QUALITY_PRESETS["high"]
        fast = VIDEO_QUALITY_PRESETS["fast"]
        fastest = VIDEO_QUALITY_PRESETS["fastest"]
        assert (high["width"], high["height"], high["spin_vinyl"]) == (1920, 1080, True)
        assert (fast["width"], fast["height"]) == (1280, 720)  # 720p, still spinning
        assert fast["spin_vinyl"] is True
        # fastest trades the most for speed: 720p, lower fps, static disc
        assert fastest["height"] == 720 and fastest["fps"] < high["fps"]
        assert fastest["spin_vinyl"] is False
        # the estimate speeds ascend high < fast < fastest (each tier is faster)
        assert high["speed"] < fast["speed"] < fastest["speed"]

    def test_high_static_is_1080p_without_a_spinning_disc(self):
        from noveltrans.tts.video import VIDEO_QUALITY_PRESETS

        hs = VIDEO_QUALITY_PRESETS["high_static"]
        high = VIDEO_QUALITY_PRESETS["high"]
        # same full resolution as "high" but no rotate → faster than "high", slower than "fast"
        assert (hs["width"], hs["height"]) == (1920, 1080)
        assert hs["spin_vinyl"] is False
        assert high["speed"] < hs["speed"] < VIDEO_QUALITY_PRESETS["fast"]["speed"]

    def test_unknown_preset_falls_back_to_high(self):
        from noveltrans.tts.video import VIDEO_QUALITY_PRESETS, video_preset

        assert video_preset("nope") == VIDEO_QUALITY_PRESETS["high"]
        assert video_preset("fast") == VIDEO_QUALITY_PRESETS["fast"]
        assert video_preset("high_static") == VIDEO_QUALITY_PRESETS["high_static"]


class TestVideoFonts:
    def test_registry_shape_and_default(self):
        from noveltrans.tts.video import (
            DEFAULT_VIDEO_FONT,
            FONT_NAME,
            VIDEO_FONTS,
            video_font,
        )

        assert DEFAULT_VIDEO_FONT in VIDEO_FONTS
        for spec in VIDEO_FONTS.values():
            assert {"label", "file", "family"} <= spec.keys()
        # the default font's family is the original bundled family → default behaviour kept
        assert VIDEO_FONTS[DEFAULT_VIDEO_FONT]["family"] == FONT_NAME
        assert video_font("nope") == VIDEO_FONTS[DEFAULT_VIDEO_FONT]  # unknown → default
        assert video_font("lora")["family"] == "Lora"

    def test_every_font_file_is_bundled(self):
        from importlib import resources

        from noveltrans.tts.video import VIDEO_FONTS

        assets = resources.files("noveltrans.tts").joinpath("assets")
        for spec in VIDEO_FONTS.values():
            assert assets.joinpath(spec["file"]).is_file(), spec["file"]

    @pytest.mark.parametrize("key", [
        "noto_sans", "be_vietnam", "nunito", "montserrat", "roboto", "museomoderno",
        "lora", "noto_serif", "playfair", "pacifico", "dancing_script", "sedgwick_ave",
        "amatic_sc",
    ])
    def test_font_family_matches_and_covers_vietnamese(self, key):
        # libass resolves a style by FAMILY name inside fontsdir, so the registry family must
        # equal the TTF's own family — and every Vietnamese diacritic must render (no tofu).
        from importlib import resources

        from PIL import ImageFont

        from noveltrans.tts.video import VIDEO_FONTS

        spec = VIDEO_FONTS[key]
        with resources.as_file(
            resources.files("noveltrans.tts").joinpath("assets", spec["file"])
        ) as path:
            font = ImageFont.truetype(str(path), 40)
            assert font.getname()[0] == spec["family"]  # family-name contract
            for ch in "ệữỗậọằẽỹđĐơư":  # precomposed Vietnamese incl. đ Đ ơ ư
                assert font.getmask(ch).getbbox() is not None, f"{spec['family']} missing {ch!r}"

    def test_build_ass_uses_the_given_family(self):
        doc = build_ass_subtitles([_seg(10, "C1")], "Truyện", font_name="Lora")
        assert "Style: Novel,Lora," in doc
        assert "Style: Chapter,Lora," in doc


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

    def test_render_threads_the_chosen_font(self, tmp_path, monkeypatch):
        # The selected font must reach build_ass_subtitles (it used to be dropped there).
        import noveltrans.tts.video as video

        captured = {}
        real = video.build_ass_subtitles

        def spy(segments, novel_title, **kw):
            captured["font_name"] = kw.get("font_name")
            return real(segments, novel_title, **kw)

        class _FakeProc:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(video.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
        monkeypatch.setattr(video, "_with_real_durations", lambda segs: segs)
        monkeypatch.setattr(video, "_concat_audio", lambda *a, **k: None)
        monkeypatch.setattr(video, "build_ass_subtitles", spy)

        segs = [MergeSegment(path=tmp_path / "a.wav", seconds=3.0, title="C1")]
        with video.font_dir_context() as font_dir:
            video.render_video(segs, tmp_path / "bg.png", tmp_path / "out.mp4",
                               font_dir, "Truyện", width=640, height=360, font_name="Lora")
        assert captured["font_name"] == "Lora"


class TestPreviewFrame:
    def test_preview_argv_is_a_single_still_grab(self, tmp_path, monkeypatch):
        # One ffmpeg call: a synthetic audio drives the bars, a single frame is grabbed, and
        # the font dir is passed — and it must NOT concat audio (no chapter files needed).
        import noveltrans.tts.video as video

        cmds = []

        class _FakeProc:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(video.subprocess, "Popen", lambda cmd, **kw: cmds.append(cmd) or _FakeProc())

        with video.font_dir_context() as font_dir:
            video.render_preview_frame(
                tmp_path / "bg.png", tmp_path / "out.png", font_dir, "Truyện",
                "Chương 1: mẫu", width=320, height=180,
            )
        assert len(cmds) == 1  # a single ffmpeg call, no audio concat
        cmd = cmds[0]
        assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"
        assert any("anoisesrc" in a for a in cmd)  # synthetic audio → lively bars
        assert "[v]" in cmd and any("fontsdir=" in a for a in cmd)
        assert not any("concat" in a for a in cmd)

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_renders_a_real_preview_png_without_any_audio(self, tmp_path):
        # The whole point: a preview with NO chapter audio, purely for design.
        from PIL import Image

        from noveltrans.tts.video import font_dir_context, render_preview_frame

        bg = tmp_path / "bg.png"
        Image.new("RGB", (400, 300), (120, 90, 160)).save(bg)
        out = tmp_path / "preview.png"
        with font_dir_context() as font_dir:
            render_preview_frame(bg, out, font_dir, "Tựa truyện", "Chương 1: thử",
                                 width=640, height=360, font_name="Lora")
        assert out.exists()
        with Image.open(out) as im:
            assert im.size == (640, 360)


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

    def test_custom_bg_color_changes_the_backdrop(self, tmp_path):
        # A chosen background color must actually change the rendered gradient.
        from PIL import Image

        from noveltrans.tts.player_skin import build_player_skin

        default_out = tmp_path / "default.png"
        color_out = tmp_path / "color.png"
        build_player_skin(tmp_path / "nope.png", default_out, width=320, height=180)
        build_player_skin(
            tmp_path / "nope.png", color_out, width=320, height=180, bg_color=(20, 120, 90)
        )
        with Image.open(default_out) as a, Image.open(color_out) as b:
            # sample a top-right point that sits over the gradient, not the framed photo
            assert a.getpixel((300, 10)) != b.getpixel((300, 10))

    def test_hex_to_rgb_parses_and_rejects(self):
        from noveltrans.tts.player_skin import hex_to_rgb

        assert hex_to_rgb("#1e785a") == (30, 120, 90)
        assert hex_to_rgb("1e785a") == (30, 120, 90)
        assert hex_to_rgb("#fff") == (255, 255, 255)
        assert hex_to_rgb("") is None
        assert hex_to_rgb("not-a-color") is None
        assert hex_to_rgb("#12345") is None

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


class TestVideoPartName:
    def test_windowed_part_name(self):
        from noveltrans.tts.video import video_part_name

        assert video_part_name("my-slug", 1, 10) == "my-slug-0001-0010.mp4"
        assert video_part_name("my-slug", 21, 30) == "my-slug-0021-0030.mp4"

    def test_whole_novel_name(self):
        from noveltrans.tts.video import video_part_name

        assert video_part_name("my-slug", 1, 199, whole_novel=True) == "my-slug.mp4"

    def test_part_dir_name_is_the_stem(self):
        from noveltrans.tts.video import video_part_dir_name

        # each part's subfolder is its file name without the .mp4
        assert video_part_dir_name("my-slug", 1, 10) == "my-slug-0001-0010"
        assert video_part_dir_name("my-slug", 1, 199, whole_novel=True) == "my-slug"

    def test_edition_slug_is_the_identity_for_chapter_audio(self):
        """Feature 067's "no migration" guarantee at its lowest level: chapter audio's
        names are byte-for-byte what they were, so nothing already on disk moves."""
        from noveltrans.tts.video import edition_slug

        assert edition_slug("my-slug") == "my-slug"
        assert edition_slug("my-slug", source_audio=False) == "my-slug"
        assert edition_slug("my-slug", source_audio=True) == "my-slug-nguon"

    def test_the_source_edition_gets_its_own_slug(self):
        from noveltrans.tts.video import video_part_name

        assert (
            video_part_name("my-slug", 1, 10, source_audio=True)
            == "my-slug-nguon-0001-0010.mp4"
        )

    def test_the_source_whole_novel_name_is_namespaced_too(self):
        """`{slug}.mp4` carries no range suffix, so it collided just as squarely."""
        from noveltrans.tts.video import video_part_name

        assert (
            video_part_name("my-slug", 1, 199, whole_novel=True, source_audio=True)
            == "my-slug-nguon.mp4"
        )

    def test_part_dir_name_is_the_stem_for_the_source_edition_too(self):
        from noveltrans.tts.video import video_part_dir_name

        assert (
            video_part_dir_name("my-slug", 1, 10, source_audio=True)
            == "my-slug-nguon-0001-0010"
        )
        assert (
            video_part_dir_name("my-slug", 1, 199, whole_novel=True, source_audio=True)
            == "my-slug-nguon"
        )

    def test_the_two_editions_can_never_produce_the_same_name(self):
        """The feature, stated once: every sidecar — thumbnail, description, "đã tạo" tick,
        and the `.upload.json` holding a live YouTube video id — is keyed off this name."""
        from noveltrans.tts.video import video_part_name

        spans = [(1, 1), (1, 10), (21, 30), (1, 199)]
        chapter, source = set(), set()
        for first, last in spans:
            for whole in (False, True):
                chapter.add(video_part_name("my-slug", first, last, whole_novel=whole))
                source.add(
                    video_part_name(
                        "my-slug", first, last, whole_novel=whole, source_audio=True
                    )
                )
        assert chapter and source
        assert chapter & source == set()


def _chapter(i, *, voiced=True, voice="V"):
    from noveltrans.models import Chapter

    return Chapter(
        index=i, title=f"Chương {i + 1}", url="u",
        audio_path=f"{i}.mp3" if voiced else "", audio_voice=voice if voiced else "",
    )


class TestPlanLockedVideoWindows:
    """Feature 058 follow-up: a batch part committed with fewer than a full batch of
    chapters (rendered, or manually ticked "đã tạo") must stay that size — new chapters
    start the *next* part instead of retroactively growing an already-"đã tạo" one."""

    def test_matches_the_plain_grid_when_nothing_is_committed(self):
        from noveltrans.tts.merge import part_number, plan_merge_windows
        from noveltrans.tts.video import plan_locked_video_windows

        chapters = [_chapter(i) for i in range(25)]
        plain = plan_merge_windows(chapters, "V", "batch", batch=10)
        locked = plan_locked_video_windows(chapters, "V", 10, {})

        assert [w.first_num for w in plain] == [w.first_num for _, w in locked]
        assert [w.last_num for w in plain] == [w.last_num for _, w in locked]
        assert [pn for pn, _ in locked] == [
            part_number(w.first_num, 10) for w in plain
        ]

    def test_a_committed_partial_window_stays_frozen(self):
        from noveltrans.tts.video import plan_locked_video_windows

        # 98 chapters; part 10 (chương 91-100) only has 8 voiced so far, and got committed
        chapters = [_chapter(i) for i in range(98)]
        locked = plan_locked_video_windows(chapters, "V", 10, {91: 98})
        part10 = locked[-1]
        assert part10 == (10, part10[1])
        assert (part10[1].first_num, part10[1].last_num) == (91, 98)
        assert len(part10[1].chapters) == 8

    def test_new_chapters_start_the_next_part_not_fill_the_locked_one(self):
        from noveltrans.tts.video import plan_locked_video_windows

        # 2 more chapters arrive (99, 100) — part 10 must NOT grow to (91, 100)
        chapters = [_chapter(i) for i in range(100)]
        locked = plan_locked_video_windows(chapters, "V", 10, {91: 98})
        part10, part11 = locked[-2], locked[-1]
        assert (part10[0], part10[1].first_num, part10[1].last_num) == (10, 91, 98)
        assert (part11[0], part11[1].first_num, part11[1].last_num) == (11, 99, 100)

    def test_a_full_new_batch_after_the_lock_gets_its_own_part(self):
        from noveltrans.tts.video import plan_locked_video_windows

        chapters = [_chapter(i) for i in range(115)]
        locked = plan_locked_video_windows(chapters, "V", 10, {91: 98})
        numbered = {pn: (w.first_num, w.last_num, len(w.chapters)) for pn, w in locked}
        assert numbered[10] == (91, 98, 8)  # still frozen
        assert numbered[11] == (99, 108, 10)  # a fresh full batch, not chương 101-110
        assert numbered[12] == (109, 115, 7)  # tail, not yet committed

    def test_a_mid_sequence_gap_still_does_not_shift_later_numbers(self):
        """Preserves merge.part_number's existing guarantee: a window with zero currently
        available chapters is omitted from the result but still consumes its part number."""
        from noveltrans.tts.video import plan_locked_video_windows

        # chapters 11-20 (part 2) have no audio at all yet
        chapters = [_chapter(i) for i in range(10)] + [
            _chapter(i, voiced=False) for i in range(10, 20)
        ] + [_chapter(i) for i in range(20, 30)]
        locked = plan_locked_video_windows(chapters, "V", 10, {})
        assert [pn for pn, _ in locked] == [1, 3]  # part 2 omitted, not renumbered to 2


class TestDiscoverCommittedVideoWindows:
    def test_empty_when_the_directory_does_not_exist(self, tmp_path):
        from noveltrans.tts.video import discover_committed_video_windows

        assert discover_committed_video_windows(tmp_path / "nope", "slug") == {}

    def test_a_rendered_part_folder_is_committed(self, tmp_path):
        from noveltrans.tts.video import discover_committed_video_windows

        folder = tmp_path / "slug-0091-0098"
        folder.mkdir()
        (folder / "slug-0091-0098.mp4").write_bytes(b"fake mp4")
        assert discover_committed_video_windows(tmp_path, "slug") == {91: 98}

    def test_a_manual_override_with_no_file_is_also_committed(self, tmp_path):
        from noveltrans.tts.video import discover_committed_video_windows
        from noveltrans.video_state import set_created_override

        folder = tmp_path / "slug-0091-0098"
        folder.mkdir()
        set_created_override(folder / "slug-0091-0098.mp4", True, file_exists=False)
        assert discover_committed_video_windows(tmp_path, "slug") == {91: 98}

    def test_an_untouched_folder_is_not_committed(self, tmp_path):
        """No render, no manual tick — just a stray folder — must not lock anything."""
        from noveltrans.tts.video import discover_committed_video_windows

        (tmp_path / "slug-0091-0098").mkdir()
        assert discover_committed_video_windows(tmp_path, "slug") == {}

    def test_folders_from_a_different_slug_are_ignored(self, tmp_path):
        from noveltrans.tts.video import discover_committed_video_windows

        folder = tmp_path / "other-slug-0091-0098"
        folder.mkdir()
        (folder / "other-slug-0091-0098.mp4").write_bytes(b"fake mp4")
        assert discover_committed_video_windows(tmp_path, "slug") == {}

    def test_a_committed_chapter_part_is_not_a_source_commit(self, tmp_path):
        """Feature 067: the two editions are separate namespaces, so a rendered chapter
        part cannot freeze the source edition's grid (its numbers are release ordinals)."""
        from noveltrans.tts.video import discover_committed_video_windows

        folder = tmp_path / "slug-0091-0098"
        folder.mkdir()
        (folder / "slug-0091-0098.mp4").write_bytes(b"fake mp4")
        assert discover_committed_video_windows(tmp_path, "slug") == {91: 98}
        assert discover_committed_video_windows(tmp_path, "slug", source_audio=True) == {}

    def test_a_committed_source_part_is_not_a_chapter_commit(self, tmp_path):
        from noveltrans.tts.video import discover_committed_video_windows

        folder = tmp_path / "slug-nguon-0001-0002"
        folder.mkdir()
        (folder / "slug-nguon-0001-0002.mp4").write_bytes(b"fake mp4")
        assert discover_committed_video_windows(tmp_path, "slug", source_audio=True) == {1: 2}
        assert discover_committed_video_windows(tmp_path, "slug") == {}


class TestIterRenderedPartDirs:
    """Feature 065 — the shared folder scan behind commit discovery and the description
    resync. The two callers want different things from it (a manual "đã tạo" tick counts
    for one, a real file for the other), so it yields folders and lets them decide."""

    def _dirs(self, tmp_path, slug="slug"):
        from noveltrans.tts.video import iter_rendered_part_dirs

        return [(d.name, a, b) for d, a, b in iter_rendered_part_dirs(tmp_path, slug)]

    def test_nothing_when_the_directory_does_not_exist(self, tmp_path):
        assert self._dirs(tmp_path / "nope") == []

    def test_yields_every_per_part_subfolder(self, tmp_path):
        (tmp_path / "slug-0001-0010").mkdir()
        (tmp_path / "slug-0011-0020").mkdir()
        assert self._dirs(tmp_path) == [
            ("slug-0001-0010", 1, 10),
            ("slug-0011-0020", 11, 20),
        ]

    def test_yields_a_folder_with_no_render_in_it(self, tmp_path):
        # unlike discover_committed_video_windows, which filters on effective_created
        (tmp_path / "slug-0001-0010").mkdir()
        assert self._dirs(tmp_path) == [("slug-0001-0010", 1, 10)]

    def test_ignores_folders_of_other_novels(self, tmp_path):
        (tmp_path / "other-slug-0001-0010").mkdir()
        assert self._dirs(tmp_path) == []

    def test_ignores_names_that_are_not_a_span(self, tmp_path):
        (tmp_path / "slug-notaspan").mkdir()
        (tmp_path / "slug").mkdir()  # the whole-novel folder has no span suffix
        assert self._dirs(tmp_path) == []

    def test_ignores_loose_files(self, tmp_path):
        (tmp_path / "slug-0001-0010.mp4").write_bytes(b"x")
        assert self._dirs(tmp_path) == []

    def test_source_part_folders_are_invisible_to_the_chapter_scan(self, tmp_path):
        """Feature 067. No extra filtering does this — the source folder's remainder after
        the `slug-` prefix is `nguon-0001-0010`, which is not digits-dash-digits."""
        (tmp_path / "slug-nguon-0001-0010").mkdir()
        (tmp_path / "slug-nguon").mkdir()  # the source whole-novel folder
        assert self._dirs(tmp_path) == []

    def test_the_source_scan_sees_only_source_folders(self, tmp_path):
        from noveltrans.tts.video import iter_rendered_part_dirs

        (tmp_path / "slug-0001-0010").mkdir()
        (tmp_path / "slug-nguon-0011-0020").mkdir()
        found = [
            (d.name, a, b)
            for d, a, b in iter_rendered_part_dirs(tmp_path, "slug", source_audio=True)
        ]
        assert found == [("slug-nguon-0011-0020", 11, 20)]

    def test_a_novel_whose_slug_ends_in_the_marker_still_partitions(self, tmp_path):
        """The pathological case: the extra segment leaves the remainder non-numeric either
        way, so neither scan can stray into the other's folders."""
        from noveltrans.tts.video import iter_rendered_part_dirs

        (tmp_path / "slug-nguon-0001-0010").mkdir()  # source part of novel "slug"
        assert self._dirs(tmp_path, slug="slug") == []
        chapter_of_nguon_novel = [
            d.name for d, _a, _b in iter_rendered_part_dirs(tmp_path, "slug-nguon")
        ]
        assert chapter_of_nguon_novel == ["slug-nguon-0001-0010"]


class TestVideoWorker:
    def test_skip_existing_param_is_carried(self, qapp):
        from noveltrans.gui.workers import VideoWorker

        w = VideoWorker("/tmp/x", voice="v", mode="batch", image_path="/tmp/bg.png",
                        batch=10, skip_existing=True)
        assert w.skip_existing is True

    def test_start_is_not_shadowed_by_params(self, qapp):
        # Same trap as MergeWorker: `self.start = start` would clobber QThread.start().
        from noveltrans.gui.workers import VideoWorker

        w = VideoWorker("/tmp/x", voice="v", mode="range", image_path="/tmp/bg.png",
                        start=3, end=9, font="Lora")
        assert callable(w.start)  # the QThread method, not the int
        assert w.start_num == 3 and w.end_num == 9
        assert w.font == "Lora"  # the chosen title font is carried to render_video

    def test_preview_worker_carries_its_params(self, qapp):
        from noveltrans.gui.workers import VideoPreviewWorker

        w = VideoPreviewWorker("/tmp/bg.png", "Tựa", "Chương 1", width=1280, height=720,
                               spin_vinyl=False, font="Nunito")
        assert callable(w.start) and w.font == "Nunito"
        assert (w.width, w.height, w.spin_vinyl) == (1280, 720, False)

    def test_no_audio_fails_cleanly(self, qapp, library_dir, sample_meta, sample_refs):
        from noveltrans.gui.workers import VideoWorker
        from noveltrans.storage import NovelProject

        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # no audio yet
        w = VideoWorker(project.path, voice="V", mode="all", image_path="/tmp/bg.png")
        failures = []
        w.failed.connect(failures.append)
        w.run()  # synchronous
        assert failures and "Không có chương" in failures[0]

    def test_skip_existing_also_skips_a_manually_marked_created_part(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        """Feature 058 follow-up: a part manually ticked "đã tạo" (no .mp4 yet) must be
        skipped by `skip_existing`, the same as one whose file actually exists — not just
        rendered files."""
        from pathlib import Path

        from noveltrans.gui.workers import VideoWorker
        from noveltrans.storage import NovelProject
        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name
        from noveltrans.video_state import set_created_override

        project = NovelProject.create(library_dir, sample_meta, sample_refs)  # 5 chapters
        for i in range(4):
            rel = f"exports/audio/{i}.mp3"
            project.save_audio(i, rel, "V", 1.0)
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"fake audio")
        path = project.path
        project.close()

        project = NovelProject.open(path)
        slug = slugify(project.meta.translated_title or project.meta.title)
        name1 = video_part_name(slug, 1, 2, whole_novel=False)  # first window: chương 1-2
        out1 = project.video_dir / Path(name1).stem / name1
        set_created_override(out1, True, file_exists=False)  # marked "đã tạo" by hand
        project.close()

        rendered = []

        def _fake_render_video(segments, image_path, out_path, *a, **k):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake mp4")
            rendered.append(out_path)

        monkeypatch.setattr("noveltrans.tts.video.render_video", _fake_render_video)

        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        w = VideoWorker(path, voice="V", mode="batch", image_path=str(image), batch=2,
                        skip_existing=True)
        finished = []
        w.finished_ok.connect(finished.append)
        w.run()  # synchronous

        assert out1 not in rendered  # skipped: manually marked, even with no file on disk
        assert len(rendered) == 1  # only the second window (chương 3-4) actually rendered
        assert finished == [1]

    def test_batch_run_locks_a_committed_partial_part_and_titles_the_next_one_correctly(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """End-to-end version of feature 058's follow-up: part 10 was already rendered
        with only 8/10 chapters; 2 more chapters arrive. A "Tạo video" batch run must skip
        part 10 untouched and render exactly one new part — 11 (chương 99-100) — titled
        "Phần 11", not silently absorbed into part 10 or mistitled "Phần 1"/"Phần 10"."""
        from pathlib import Path

        from noveltrans.gui.workers import VideoWorker
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name
        from noveltrans.video_state import set_created_override

        refs = [
            ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
            for i in range(98)
        ]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(98):
            rel = f"exports/audio/{i}.mp3"
            project.save_audio(i, rel, "V", 1.0)
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"fake audio")
        path = project.path
        slug = slugify(project.meta.translated_title or project.meta.title)

        # parts 1-9 (chương 1-90) already committed — marked "đã tạo" by hand, no need for
        # real files, since only their commit STATUS matters for this scenario
        for first in range(1, 90, 10):
            name = video_part_name(slug, first, first + 9, whole_novel=False)
            out = project.video_dir / Path(name).stem / name
            set_created_override(out, True, file_exists=False)
        # part 10 (chương 91-98, 8 chapters) already committed by a real render
        name10 = video_part_name(slug, 91, 98, whole_novel=False)
        out10 = project.video_dir / Path(name10).stem / name10
        out10.parent.mkdir(parents=True, exist_ok=True)
        out10.write_bytes(b"already rendered")
        (out10.parent / (out10.stem + ".title.txt")).write_text(
            "Truyện - Phần 10\n", encoding="utf-8"
        )

        # 2 more chapters arrive and get voiced
        project.replace_toc(
            refs + [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                    for i in range(98, 100)]
        )
        for i in range(98, 100):
            rel = f"exports/audio/{i}.mp3"
            project.save_audio(i, rel, "V", 1.0)
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"fake audio")
        project.close()

        rendered = []

        def _fake_render_video(segments, image_path, out_path, *a, **k):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake mp4")
            rendered.append(out_path)

        monkeypatch.setattr("noveltrans.tts.video.render_video", _fake_render_video)

        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        w = VideoWorker(path, voice="V", mode="batch", image_path=str(image), batch=10,
                        skip_existing=True)
        finished = []
        w.finished_ok.connect(finished.append)
        w.run()  # synchronous

        assert out10 not in rendered  # part 10 stays exactly as committed — untouched
        assert len(rendered) == 1  # exactly one new part rendered: 99-100
        name11 = video_part_name(slug, 99, 100, whole_novel=False)
        out11 = project.video_dir / Path(name11).stem / name11
        assert rendered == [out11]
        title11 = (out11.parent / (out11.stem + ".title.txt")).read_text(encoding="utf-8")
        assert "Phần 11" in title11  # not "Phần 1" (no batch grid) or "Phần 10" (collision)
        assert finished == [1]

    def test_skip_existing_false_ignores_locked_windows_and_uses_the_fresh_grid(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """"Tạo lại tất cả video" (skip_existing=False) is an explicit full rebuild — it
        must NOT stay pinned to an earlier partial commit like the incremental batch run
        does; part 2 should render with all currently-available chapters."""
        from pathlib import Path

        from noveltrans.gui.workers import VideoWorker
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name
        from noveltrans.video_state import set_created_override

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(3)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(3):
            rel = f"exports/audio/{i}.mp3"
            project.save_audio(i, rel, "V", 1.0)
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"fake audio")
        path = project.path
        slug = slugify(project.meta.translated_title or project.meta.title)

        # part 2 (chương 3, 1/2 chapters) committed by hand
        name2 = video_part_name(slug, 3, 3, whole_novel=False)
        set_created_override(project.video_dir / Path(name2).stem / name2, True, file_exists=False)

        # chương 4 arrives
        project.replace_toc(refs + [ChapterRef(index=3, title="第4章", url="https://x/4")])
        rel = "exports/audio/3.mp3"
        project.save_audio(3, rel, "V", 1.0)
        (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (project.path / rel).write_bytes(b"fake audio")
        project.close()

        rendered = []

        def _fake_render_video(segments, image_path, out_path, *a, **k):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake mp4")
            rendered.append(out_path)

        monkeypatch.setattr("noveltrans.tts.video.render_video", _fake_render_video)

        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        w = VideoWorker(path, voice="V", mode="batch", image_path=str(image), batch=2,
                        skip_existing=False)  # redo-all
        finished = []
        w.finished_ok.connect(finished.append)
        w.run()  # synchronous

        # fresh grid: part 1 (1-2), part 2 (3-4) — NOT the locked (3-3)/(4-4) split
        name_full_part2 = video_part_name(slug, 3, 4, whole_novel=False)
        out_full_part2 = project.video_dir / Path(name_full_part2).stem / name_full_part2
        assert out_full_part2 in rendered
        assert len(rendered) == 2
        assert finished == [2]

    def test_redo_all_still_honors_a_manual_split_boundary(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """Unlike an auto-discovered "đã tạo" commit, a manual split (e.g. to stay under
        YouTube's 12h cap) must survive "Tạo lại tất cả video" too — redo-all changes
        visual settings, not the chaptering plan, and silently re-merging the split would
        reintroduce the exact policy violation it exists to avoid."""
        from pathlib import Path

        from noveltrans.gui.workers import VideoWorker
        from noveltrans.models import ChapterRef
        from noveltrans.storage import NovelProject
        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name
        from noveltrans.video_windows import split_window

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(4)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(4):
            rel = f"exports/audio/{i}.mp3"
            project.save_audio(i, rel, "V", 1.0)
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"fake audio")
        path = project.path
        slug = slugify(project.meta.translated_title or project.meta.title)
        split_window(path, 1, 4, 1)  # batch=4 would be one part; split off the last chương
        project.close()

        rendered = []

        def _fake_render_video(segments, image_path, out_path, *a, **k):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake mp4")
            rendered.append(out_path)

        monkeypatch.setattr("noveltrans.tts.video.render_video", _fake_render_video)

        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        w = VideoWorker(path, voice="V", mode="batch", image_path=str(image), batch=4,
                        skip_existing=False)  # redo-all
        w.run()  # synchronous

        name_a = video_part_name(slug, 1, 3, whole_novel=False)
        name_b = video_part_name(slug, 4, 4, whole_novel=False)
        out_a = project.video_dir / Path(name_a).stem / name_a
        out_b = project.video_dir / Path(name_b).stem / name_b
        assert set(rendered) == {out_a, out_b}  # the split held — not one 4-chapter part

    def test_explicit_windows_renders_exactly_those_with_correct_titles(
        self, qapp, tmp_path, library_dir, sample_meta, monkeypatch
    ):
        """A multi-select "Tạo video" (right-click 2+ chosen rows) passes an explicit
        window list + part-number map, bypassing mode-based planning entirely — only
        those windows render, skipping any others that happen to be pending too, and each
        gets its true (non-grid) part number/title."""
        from pathlib import Path

        from noveltrans.gui.workers import VideoWorker
        from noveltrans.storage import NovelProject
        from noveltrans.storage.project import slugify
        from noveltrans.models import ChapterRef
        from noveltrans.tts.merge import MergeWindow
        from noveltrans.tts.video import video_part_name

        refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}")
                for i in range(30)]
        project = NovelProject.create(library_dir, sample_meta, refs)
        for i in range(30):
            rel = f"exports/audio/{i}.mp3"
            project.save_audio(i, rel, "V", 1.0)
            (project.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (project.path / rel).write_bytes(b"fake audio")
        chapters = project.chapters()  # re-fetch with audio_path populated
        path = project.path
        slug = slugify(project.meta.translated_title or project.meta.title)
        project.close()

        # rows 1 (chương 1-10) and 3 (chương 21-30) selected — row 2 (11-20) skipped
        window_1 = MergeWindow(1, 10, [c for c in chapters if c.index < 10])
        window_3 = MergeWindow(21, 30, [c for c in chapters if c.index >= 20])

        rendered = []

        def _fake_render_video(segments, image_path, out_path, *a, **k):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"fake mp4")
            rendered.append(out_path)

        monkeypatch.setattr("noveltrans.tts.video.render_video", _fake_render_video)

        image = tmp_path / "bg.png"
        image.write_bytes(b"fake")
        w = VideoWorker(
            path, voice="V", mode="batch", image_path=str(image), batch=10,
            skip_existing=True,
            explicit_windows=[window_1, window_3],
            explicit_part_numbers={1: 1, 21: 3},
        )
        w.run()  # synchronous

        name1 = video_part_name(slug, 1, 10, whole_novel=False)
        name3 = video_part_name(slug, 21, 30, whole_novel=False)
        out1 = project.video_dir / Path(name1).stem / name1
        out3 = project.video_dir / Path(name3).stem / name3
        assert set(rendered) == {out1, out3}  # NOT the middle window (11-20)
        title1 = (out1.parent / (out1.stem + ".title.txt")).read_text(encoding="utf-8")
        title3 = (out3.parent / (out3.stem + ".title.txt")).read_text(encoding="utf-8")
        assert "Phần 1" in title1
        assert "Phần 3" in title3  # not "Phần 2" from grid arithmetic on first_num=21


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


class TestBurnedNarrationSubtitles:
    """Feature 041 — the optional narration layer burned into the video.

    The title block (Novel/Chapter styles) is the player-skin identity and must be
    completely unaffected when the option is off.
    """

    def _segments(self, tmp_path, n=2, seconds=60.0):
        from noveltrans.tts.video import MergeSegment

        out = []
        for i in range(n):
            p = tmp_path / f"{i:04d}.mp3"
            p.write_bytes(b"audio")
            out.append(MergeSegment(p, seconds, f"Chương {i + 1}"))
        return out

    def _cue(self, start, end, text):
        from noveltrans.tts.subtitles import Cue

        return Cue(start, end, text)

    def test_no_narration_is_byte_identical_to_before(self, tmp_path):
        """The no-surprise guarantee: the option cannot change a video nobody asked to
        change."""
        from noveltrans.tts.video import build_ass_subtitles

        segs = self._segments(tmp_path)
        assert build_ass_subtitles(segs, "Tên") == build_ass_subtitles(
            segs, "Tên", narration=None
        )

    def test_an_empty_narration_list_adds_nothing(self, tmp_path):
        from noveltrans.tts.video import build_ass_subtitles

        segs = self._segments(tmp_path)
        assert build_ass_subtitles(segs, "Tên", narration=[]) == build_ass_subtitles(
            segs, "Tên"
        )

    def test_each_cue_becomes_a_sub_event_at_its_own_time(self, tmp_path):
        from noveltrans.tts.video import build_ass_subtitles

        ass = build_ass_subtitles(
            self._segments(tmp_path),
            "Tên",
            narration=[self._cue(1.5, 3.0, "Hắn bước tới.")],
        )
        assert "Sub,,0,0,0,,Hắn bước tới." in ass
        assert "0:00:01.50,0:00:03.00,Sub" in ass

    def test_the_title_events_survive_alongside_the_narration(self, tmp_path):
        """Both layers coexist — narration is added, nothing is replaced."""
        from noveltrans.tts.video import build_ass_subtitles

        ass = build_ass_subtitles(
            self._segments(tmp_path), "Tên", narration=[self._cue(0, 1, "Lời đọc")]
        )
        assert ",Novel,," in ass and ",Chapter,," in ass and ",Sub,," in ass

    def test_cue_text_is_escaped_like_every_other_event(self, tmp_path):
        """Braces open an ASS override block and a newline ends the event — narration is
        arbitrary novel text, so it is exactly as dangerous as a chapter title."""
        from noveltrans.tts.video import build_ass_subtitles

        ass = build_ass_subtitles(
            self._segments(tmp_path),
            "Tên",
            narration=[self._cue(0, 1, "A {\\b1} B\nC")],
        )
        assert "Sub,,0,0,0,,A (\\b1) B\\NC" in ass

    def test_an_empty_cue_produces_no_event(self, tmp_path):
        """A chunk can clean down to nothing — feature 038's punctuation-only lines."""
        from noveltrans.tts.video import build_ass_subtitles

        ass = build_ass_subtitles(
            self._segments(tmp_path), "Tên", narration=[self._cue(0, 1, "   ")]
        )
        assert ",Sub,," not in ass

    def test_the_sub_style_sits_at_the_bottom_centre(self, tmp_path):
        from noveltrans.tts.video import build_ass_subtitles

        ass = build_ass_subtitles(self._segments(tmp_path), "Tên", narration=[])
        style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Sub,"))
        # ASS style fields: ..., BorderStyle, Outline, Shadow, Alignment, L, R, V, Enc
        assert style.split(",")[18] == "2"  # 2 = bottom-centre

    def test_the_narration_clears_the_progress_track_at_any_resolution(self):
        """Asserted against PlayerLayout rather than a 1080p number, so a future layout
        change surfaces here instead of on a rendered video."""
        from noveltrans.tts.player_skin import PlayerLayout
        from noveltrans.tts.video import sub_font_px, sub_margin_v

        for w, h in ((1920, 1080), (1280, 720)):
            lay = PlayerLayout.of(w, h)
            top_of_three_lines = h - sub_margin_v(h) - 3 * sub_font_px(h)
            assert top_of_three_lines > lay.track_y, f"{w}x{h} collides with the track"

    def test_the_narration_does_not_reach_up_into_the_photo(self):
        from noveltrans.tts.player_skin import PlayerLayout
        from noveltrans.tts.video import sub_font_px, sub_margin_v

        lay = PlayerLayout.of(1920, 1080)
        top_of_three_lines = 1080 - sub_margin_v(1080) - 3 * sub_font_px(1080)
        assert top_of_three_lines > lay.photo_y + lay.photo_h


class TestBurnedSubtitlesShareTheSidecarTimings:
    """One definition of where a chapter starts, so a burned line can never disagree with
    the .srt line."""

    def test_part_cues_and_part_srt_agree(self, tmp_path):
        from noveltrans.tts.subtitles import Cue, part_cues, part_srt, write_cues
        from noveltrans.tts.video import MergeSegment

        segs = []
        for i in range(2):
            p = tmp_path / f"{i:04d}.mp3"
            p.write_bytes(b"audio")
            write_cues(p, [Cue(0, 5, f"Câu {i}")], seconds=60.0)
            segs.append(MergeSegment(p, 60.0, f"Chương {i}"))

        cues, covered, total = part_cues(segs)
        srt, srt_covered, srt_total = part_srt(segs)
        assert (covered, total) == (srt_covered, srt_total) == (2, 2)
        assert len(cues) == srt.count("-->")
        # the second chapter's cue is offset by the first chapter's duration in both
        assert cues[1].start == 60.0
        assert "00:01:00,000" in srt

"""Feature 116: choose which part of the background image shows in the small photo.

Covers the crop math (`player_skin.cover_crop` / `pan_photo_focus`), the per-novel
settings, the pass-through to both renderers, and the preview window's drag controls.
"""

from __future__ import annotations

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QMouseEvent

from noveltrans.config import AppConfig
from noveltrans.storage import NovelProject
from noveltrans.tts.player_skin import (
    DEFAULT_PHOTO_FOCUS,
    DEFAULT_PHOTO_ZOOM,
    MAX_PHOTO_ZOOM,
    _cover_fit,
    clamp_photo_focus,
    clamp_photo_zoom,
    cover_crop,
    pan_photo_focus,
)


def _config(tmp_path):
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.library_dir = tmp_path / "library"
    return config


def _old_cover_fit(photo, w, h):
    """The centre crop every render used before feature 116, kept to prove nothing moved."""
    scale = max(w / photo.width, h / photo.height)
    photo = photo.resize((max(1, round(photo.width * scale)), max(1, round(photo.height * scale))))
    left = (photo.width - w) // 2
    top = (photo.height - h) // 2
    return photo.crop((left, top, left + w, top + h))


def _gradient(w, h):
    img = Image.new("RGB", (w, h))
    img.putdata([(x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 90)
                 for y in range(h) for x in range(w)])
    return img


def _split(w=400, h=225):
    """Left half red, right half blue."""
    img = Image.new("RGB", (w, h), (220, 20, 20))
    img.paste((20, 20, 220), (w // 2, 0, w, h))
    return img


# ------------------------------------------------------------------ crop math


class TestCoverCrop:
    @pytest.mark.parametrize("src,slot", [
        ((400, 225), (138, 107)),   # odd slack sideways
        ((401, 300), (100, 100)),
        ((300, 401), (100, 100)),   # tall
        ((640, 360), (276, 213)),
    ])
    def test_defaults_match_the_old_centre_crop_pixel_for_pixel(self, src, slot):
        photo = _gradient(*src)
        assert _cover_fit(photo, *slot).tobytes() == _old_cover_fit(photo, *slot).tobytes()

    def test_focus_edges_reach_the_image_edges(self):
        c0 = cover_crop(400, 225, 100, 100, focus=(0, 0))
        c1 = cover_crop(400, 225, 100, 100, focus=(1, 1))
        assert (c0.left, c0.top) == (0, 0)
        assert c1.left == c1.scaled_w - 100
        assert c1.top == c1.scaled_h - 100

    @pytest.mark.parametrize("src", [(4000, 100), (100, 4000), (1, 1), (3, 2)])
    @pytest.mark.parametrize("focus", [(0, 0), (0.5, 0.5), (1, 1), (0.3, 0.9)])
    @pytest.mark.parametrize("zoom", [1.0, 2.0, MAX_PHOTO_ZOOM])
    def test_crop_stays_inside_the_image_and_is_exactly_the_slot(self, src, focus, zoom):
        w, h = 826, 637
        c = cover_crop(*src, w, h, focus=focus, zoom=zoom)
        assert 0 <= c.left <= c.scaled_w - w
        assert 0 <= c.top <= c.scaled_h - h
        assert _cover_fit(Image.new("RGB", src), w, h, focus=focus, zoom=zoom).size == (w, h)

    def test_zoom_gives_a_wide_image_room_to_move_vertically(self):
        # A 16:9 image covering a wider-than-tall slot fills its height exactly at 100%.
        flat = cover_crop(1600, 900, 826, 637)
        assert flat.scaled_h - 637 <= 1
        zoomed = cover_crop(1600, 900, 826, 637, zoom=2.0)
        assert zoomed.scaled_h - 637 > 500
        assert zoomed.top > 0

    def test_focus_changes_what_the_photo_shows(self):
        photo = _split()
        left = _cover_fit(photo, 100, 100, focus=(0, 0.5))
        right = _cover_fit(photo, 100, 100, focus=(1, 0.5))
        assert left.getpixel((50, 50))[0] > 200   # red
        assert right.getpixel((50, 50))[2] > 200  # blue

    @pytest.mark.parametrize("value,expected", [
        ((-1, 2), (0.0, 1.0)),
        ([0.25, 0.75], (0.25, 0.75)),
        ("abc", DEFAULT_PHOTO_FOCUS),
        (None, DEFAULT_PHOTO_FOCUS),
        ((1, 2, 3), DEFAULT_PHOTO_FOCUS),
        ((float("nan"), 0.2), DEFAULT_PHOTO_FOCUS),
        (("0.1", "0.2"), (0.1, 0.2)),
    ])
    def test_clamp_photo_focus(self, value, expected):
        assert clamp_photo_focus(value) == expected

    @pytest.mark.parametrize("value,expected", [
        (0.1, 1.0), (10, MAX_PHOTO_ZOOM), (1.5, 1.5), ("x", DEFAULT_PHOTO_ZOOM),
        (None, DEFAULT_PHOTO_ZOOM), (float("inf"), DEFAULT_PHOTO_ZOOM),
    ])
    def test_clamp_photo_zoom(self, value, expected):
        assert clamp_photo_zoom(value) == expected


class TestPanPhotoFocus:
    def test_the_picture_follows_the_hand(self):
        # dragging right reveals more of the left side → fx drops
        fx, fy = pan_photo_focus((1600, 900), (100, 100), (0.5, 0.5), 1.0, 20, 0)
        assert fx < 0.5 and fy == 0.5

    def test_an_axis_with_no_spare_image_does_not_move(self):
        # 16:9 into a 16:9 slot at 100%: no slack anywhere
        assert pan_photo_focus((1600, 900), (160, 90), (0.3, 0.7), 1.0, 50, 50) == (0.3, 0.7)

    def test_result_is_clamped(self):
        assert pan_photo_focus((1600, 900), (100, 100), (0.5, 0.5), 1.0, -10_000, 0) == (1.0, 0.5)

    def test_scale_invariant(self):
        a = pan_photo_focus((1600, 900), (100, 80), (0.5, 0.5), 2.0, 13, -7)
        b = pan_photo_focus((1600, 900), (300, 240), (0.5, 0.5), 2.0, 39, -21)
        assert a == pytest.approx(b)

    def test_degenerate_sizes_keep_the_focus(self):
        assert pan_photo_focus((0, 0), (100, 100), (0.2, 0.4), 1.0, 10, 10) == (0.2, 0.4)


class TestPlayerSkinFraming:
    def test_default_skin_is_unchanged_and_focus_moves_the_photo(self, tmp_path):
        from noveltrans.tts.player_skin import PlayerLayout, build_player_skin

        src = tmp_path / "split.png"
        _split(1600, 400).save(src)
        default = tmp_path / "d.png"
        explicit = tmp_path / "e.png"
        left = tmp_path / "l.png"
        right = tmp_path / "r.png"
        build_player_skin(src, default, width=640, height=360)
        build_player_skin(src, explicit, width=640, height=360,
                          photo_focus=DEFAULT_PHOTO_FOCUS, photo_zoom=DEFAULT_PHOTO_ZOOM)
        build_player_skin(src, left, width=640, height=360, photo_focus=(0, 0.5))
        build_player_skin(src, right, width=640, height=360, photo_focus=(1, 0.5))
        lay = PlayerLayout.of(640, 360)
        centre = (lay.photo_x + lay.photo_w // 2, lay.photo_y + lay.photo_h // 2)
        with Image.open(default) as d, Image.open(explicit) as e, \
                Image.open(left) as lt, Image.open(right) as rt:
            assert d.tobytes() == e.tobytes()
            assert lt.getpixel(centre)[0] > 200
            assert rt.getpixel(centre)[2] > 200


# ------------------------------------------------------------- renderers


class TestRendererPassThrough:
    def _record_skin(self, monkeypatch):
        from noveltrans.tts import video

        seen = {}

        class _Stop(Exception):
            pass

        def fake(*_a, **kw):
            seen.update(kw)
            raise _Stop()

        monkeypatch.setattr(video, "build_player_skin", fake)
        return seen, _Stop

    def test_preview_frame_forwards_the_framing(self, tmp_path, monkeypatch):
        from noveltrans.tts.video import font_dir_context, render_preview_frame

        seen, stop = self._record_skin(monkeypatch)
        with font_dir_context() as font_dir, pytest.raises(stop):
            render_preview_frame(tmp_path / "bg.png", tmp_path / "o.png", font_dir, "T", "C",
                                 width=640, height=360, photo_focus=(0.1, 0.9), photo_zoom=2.0)
        assert (seen["photo_focus"], seen["photo_zoom"]) == ((0.1, 0.9), 2.0)

    def test_render_video_forwards_the_framing(self, tmp_path, monkeypatch):
        from noveltrans.tts.video import MergeSegment, font_dir_context, render_video

        seen, stop = self._record_skin(monkeypatch)
        segments = [MergeSegment(path="", seconds=5.0, title="Chương 1")]
        with font_dir_context() as font_dir, pytest.raises(stop):
            render_video(segments, tmp_path / "bg.png", tmp_path / "o.mp4", font_dir, "T",
                         width=640, height=360, photo_focus=(0.8, 0.2), photo_zoom=1.5)
        assert (seen["photo_focus"], seen["photo_zoom"]) == ((0.8, 0.2), 1.5)

    def test_preview_worker_clamps_and_forwards(self, qapp, tmp_path, monkeypatch):
        from noveltrans.gui.workers import VideoPreviewWorker
        from noveltrans.tts import video

        seen = {}
        monkeypatch.setattr(video, "render_preview_frame", lambda *_a, **kw: seen.update(kw))
        w = VideoPreviewWorker(tmp_path / "bg.png", "T", "C", photo_focus=(5, -1), photo_zoom=9)
        w.run()
        assert (seen["photo_focus"], seen["photo_zoom"]) == ((1.0, 0.0), MAX_PHOTO_ZOOM)
        w = VideoPreviewWorker(tmp_path / "bg.png", "T", "C")
        w.run()
        assert (seen["photo_focus"], seen["photo_zoom"]) == (DEFAULT_PHOTO_FOCUS, DEFAULT_PHOTO_ZOOM)

    def test_video_worker_keeps_the_framing(self, qapp):
        from noveltrans.gui.workers import VideoWorker

        w = VideoWorker("/tmp/p", voice="V", mode="all", image_path="/tmp/bg.png",
                        photo_focus=(0.2, 0.3), photo_zoom=1.7)
        assert (w.photo_focus, w.photo_zoom) == ((0.2, 0.3), 1.7)


# ------------------------------------------------------------- settings


class TestFramingSettings:
    def test_keys_are_per_novel_with_centre_defaults(self, tmp_path):
        from noveltrans import video_settings

        assert "video_photo_focus" in video_settings.IDENTITY_KEYS
        assert "video_photo_zoom" in video_settings.IDENTITY_KEYS
        resolved = video_settings.effective({}, _config(tmp_path))
        assert resolved["video_photo_focus"] == DEFAULT_PHOTO_FOCUS
        assert resolved["video_photo_zoom"] == DEFAULT_PHOTO_ZOOM

    @pytest.mark.parametrize("stored,focus,zoom", [
        ({"video_photo_focus": [0.25, 0.75], "video_photo_zoom": 1.5}, (0.25, 0.75), 1.5),
        ({"video_photo_focus": [5, -1], "video_photo_zoom": 99}, (1.0, 0.0), MAX_PHOTO_ZOOM),
        ({"video_photo_focus": "abc", "video_photo_zoom": None}, DEFAULT_PHOTO_FOCUS, 1.0),
        ({"video_photo_focus": None, "video_photo_zoom": 0}, DEFAULT_PHOTO_FOCUS, 1.0),
    ])
    def test_stored_values_are_normalised_and_clamped(self, tmp_path, stored, focus, zoom):
        from noveltrans import video_settings

        resolved = video_settings.effective(stored, _config(tmp_path))
        assert resolved["video_photo_focus"] == focus
        assert resolved["video_photo_zoom"] == zoom

    def test_config_properties_clamp(self, tmp_path):
        config = _config(tmp_path)
        assert config.video_photo_focus == DEFAULT_PHOTO_FOCUS
        assert config.video_photo_zoom == DEFAULT_PHOTO_ZOOM
        config.video_photo_focus = (2, -3)
        config.video_photo_zoom = 50
        assert config.video_photo_focus == (1.0, 0.0)
        assert config.video_photo_zoom == MAX_PHOTO_ZOOM
        config._s.setValue("video_photo_zoom", "garbage")
        assert config.video_photo_zoom == DEFAULT_PHOTO_ZOOM


# ------------------------------------------------------------- preview window


def _png(tmp_path, size=(640, 360)):
    png = tmp_path / "prev.png"
    Image.new("RGB", size, (120, 90, 160)).save(png)
    return png


def _bg(tmp_path):
    bg = tmp_path / "bg.png"
    _split(1600, 900).save(bg)
    return bg


def _mouse(label, kind, pos, button=Qt.MouseButton.LeftButton):
    pt = QPointF(*pos)
    buttons = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    event = QMouseEvent(kind, pt, label.mapToGlobal(QPoint(int(pt.x()), int(pt.y()))).toPointF(),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    {
        QMouseEvent.Type.MouseButtonPress: label.mousePressEvent,
        QMouseEvent.Type.MouseMove: label.mouseMoveEvent,
        QMouseEvent.Type.MouseButtonRelease: label.mouseReleaseEvent,
    }[kind](event)


class TestPhotoPanLabel:
    def _label(self, tmp_path, zoom=1.0):
        from noveltrans.gui.preview_photo_pan import PhotoPanLabel

        label = PhotoPanLabel()
        label.set_frame(str(_png(tmp_path)), str(_bg(tmp_path)), DEFAULT_PHOTO_FOCUS, zoom)
        return label

    def _drag(self, label, start, end):
        _mouse(label, QMouseEvent.Type.MouseButtonPress, start)
        _mouse(label, QMouseEvent.Type.MouseMove, end)
        _mouse(label, QMouseEvent.Type.MouseButtonRelease, end)

    def test_frame_is_shown_at_native_size_and_the_photo_box_is_known(self, qapp, tmp_path):
        label = self._label(tmp_path)
        assert (label.width(), label.height()) == (640, 360)
        assert label.can_pan

    def test_dragging_inside_the_photo_reports_the_new_focus_once(self, qapp, tmp_path):
        label = self._label(tmp_path)
        live, done = [], []
        label.panned.connect(lambda *f: live.append(f))
        label.pan_finished.connect(lambda *f: done.append(f))
        x, y, w, h, _r = label._rect
        cx, cy = x + w / 2, y + h / 2
        self._drag(label, (cx, cy), (cx + 30, cy))
        assert live and len(done) == 1
        assert done[0][0] < 0.5 and done[0][1] == 0.5

    def test_a_press_outside_the_photo_does_nothing(self, qapp, tmp_path):
        label = self._label(tmp_path)
        done = []
        label.pan_finished.connect(lambda *f: done.append(f))
        self._drag(label, (630, 350), (500, 300))
        assert done == []

    def test_no_drag_while_a_render_is_running(self, qapp, tmp_path):
        label = self._label(tmp_path)
        label.set_interactive(False)
        done = []
        label.pan_finished.connect(lambda *f: done.append(f))
        x, y, w, h, _r = label._rect
        self._drag(label, (x + w / 2, y + h / 2), (x + w / 2 + 40, y + h / 2))
        assert done == []

    def test_unreadable_background_disables_panning(self, qapp, tmp_path):
        from noveltrans.gui.preview_photo_pan import PhotoPanLabel

        label = PhotoPanLabel()
        label.set_frame(str(_png(tmp_path)), str(tmp_path / "missing.png"), DEFAULT_PHOTO_FOCUS, 1.0)
        assert not label.can_pan
        assert not label.pixmap().isNull()

    def test_overlay_only_changes_the_photo_box(self, tmp_path):
        from noveltrans.gui.preview_photo_pan import compose_pan_overlay, photo_rect

        base = Image.new("RGB", (640, 360), (10, 200, 10))
        rect = photo_rect((640, 360), (640, 360))
        out = compose_pan_overlay(base, _split(1600, 900), rect, (0, 0.5), 1.0)
        assert out.size == base.size
        x, y, w, h, _r = rect
        assert out.getpixel((x + w // 2, y + h // 2))[:3] != (10, 200, 10)
        assert out.getpixel((x + w + 20, y + h // 2))[:3] == (10, 200, 10)


class TestPreviewWindowFraming:
    def _tab(self, tmp_path, library_dir, sample_meta, sample_refs, settings=None):
        from noveltrans.gui.tab_video import VideoTab

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for i in range(3):
            project.save_audio(i, f"exports/audio/{i}.mp3", "V", 60.0)
        project.save_video_settings({"video_image_path": str(_bg(tmp_path)), **(settings or {})})
        path = project.path
        project.close()
        tab = VideoTab(_config(tmp_path))
        tab.voice_combo.addItem("V", "V")
        tab.voice_combo.setCurrentIndex(tab.voice_combo.findData("V"))
        tab._on_project_selected(str(path))
        return tab, path

    def _stored(self, path):
        project = NovelProject.open(str(path))
        try:
            return dict(project.meta.video_settings)
        finally:
            project.close()

    def _open_preview(self, tab, tmp_path):
        tab._preview_image_path = tab._video_settings["video_image_path"]
        tab._on_preview_ready(str(_png(tmp_path)))

    def test_preview_window_has_the_framing_controls(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.gui.preview_photo_pan import PhotoPanLabel

        tab, _path = self._tab(tmp_path, library_dir, sample_meta, sample_refs,
                               {"video_photo_zoom": 1.5})
        self._open_preview(tab, tmp_path)
        assert isinstance(tab._preview_label, PhotoPanLabel)
        assert tab._preview_label.can_pan
        assert tab._preview_zoom_spin.value() == 150
        assert tab._preview_zoom_spin in tab._preview_controls
        tab.shutdown()
        assert tab._preview_zoom_spin is None

    def test_drag_release_saves_to_the_novel_and_rerenders(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab, path = self._tab(tmp_path, library_dir, sample_meta, sample_refs)
        self._open_preview(tab, tmp_path)
        calls = []
        monkeypatch.setattr(tab, "_start_preview", lambda: calls.append(1))
        tab._preview_label.panned.emit(0.3, 0.5)  # live drag: nothing saved yet
        assert "video_photo_focus" not in self._stored(path) or \
            tuple(self._stored(path)["video_photo_focus"]) == DEFAULT_PHOTO_FOCUS
        tab._preview_label.pan_finished.emit(0.1, 0.9)
        assert tuple(self._stored(path)["video_photo_focus"]) == (0.1, 0.9)
        assert calls == [1]
        tab.shutdown()

    def test_zoom_and_recentre(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        tab, path = self._tab(tmp_path, library_dir, sample_meta, sample_refs,
                              {"video_photo_focus": [0.2, 0.2]})
        self._open_preview(tab, tmp_path)
        calls = []
        monkeypatch.setattr(tab, "_start_preview", lambda: calls.append(1))
        tab._preview_zoom_spin.setValue(200)
        assert self._stored(path)["video_photo_zoom"] == 2.0
        tab._preview_label.zoom_step.emit(1)
        assert tab._preview_zoom_spin.value() == 210
        tab._reset_preview_photo_framing()
        stored = self._stored(path)
        assert tuple(stored["video_photo_focus"]) == DEFAULT_PHOTO_FOCUS
        assert stored["video_photo_zoom"] == DEFAULT_PHOTO_ZOOM
        assert tab._preview_zoom_spin.value() == 100
        assert len(calls) == 3
        tab.shutdown()

    def test_picking_a_new_background_resets_the_framing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from noveltrans.gui import tab_video as tab_module

        tab, path = self._tab(tmp_path, library_dir, sample_meta, sample_refs,
                              {"video_photo_focus": [0.1, 0.1], "video_photo_zoom": 2.5})
        other = tmp_path / "other.png"
        _split().save(other)
        monkeypatch.setattr(tab_module.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *_a, **_k: (str(other), "")))
        tab._pick_video_image()
        stored = self._stored(path)
        assert tuple(stored["video_photo_focus"]) == DEFAULT_PHOTO_FOCUS
        assert stored["video_photo_zoom"] == DEFAULT_PHOTO_ZOOM
        tab.shutdown()

    def test_repicking_the_same_background_keeps_the_framing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from noveltrans.gui import tab_video as tab_module

        tab, path = self._tab(tmp_path, library_dir, sample_meta, sample_refs,
                              {"video_photo_focus": [0.1, 0.1]})
        same = tab._video_settings["video_image_path"]
        monkeypatch.setattr(tab_module.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *_a, **_k: (same, "")))
        tab._pick_video_image()
        assert tuple(self._stored(path)["video_photo_focus"]) == (0.1, 0.1)
        tab.shutdown()

    def test_both_workers_get_the_novels_framing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs, monkeypatch
    ):
        from noveltrans.gui import tab_video as tab_module

        tab, _path = self._tab(tmp_path, library_dir, sample_meta, sample_refs,
                               {"video_photo_focus": [0.7, 0.3], "video_photo_zoom": 1.8})
        tab.tags_edit.setPlainText("a, b")
        built = {}

        class _Sig:
            def connect(self, *_a):
                pass

        class _FakeWorker:
            def __init__(self, *a, **kw):
                built[type(self).__name__] = kw
                self.progress = self.file_done = self.finished_ok = self.failed = _Sig()
                self.done = _Sig()

            def start(self):
                pass

            def isRunning(self):
                return False

        class VideoWorker(_FakeWorker):
            pass

        class VideoPreviewWorker(_FakeWorker):
            pass

        monkeypatch.setattr(tab_module, "VideoWorker", VideoWorker)
        monkeypatch.setattr(tab_module, "VideoPreviewWorker", VideoPreviewWorker)
        monkeypatch.setattr(tab_module, "track_worker", lambda *_a: None)
        tab._start_preview()
        tab._launch_video()
        for name in ("VideoWorker", "VideoPreviewWorker"):
            assert built[name]["photo_focus"] == (0.7, 0.3)
            assert built[name]["photo_zoom"] == 1.8
        tab._video_worker = None
        tab._preview_worker = None
        tab.shutdown()

    def test_each_novel_keeps_its_own_framing(
        self, qapp, tmp_path, library_dir, sample_meta, sample_refs
    ):
        from dataclasses import replace

        tab, path_a = self._tab(tmp_path, library_dir, sample_meta, sample_refs,
                                {"video_photo_zoom": 2.0})
        meta_b = replace(sample_meta, url="https://example.com/novel/456", title="Other B",
                         video_settings={})
        project_b = NovelProject.create(library_dir, meta_b, sample_refs)
        path_b = project_b.path
        project_b.close()
        self._open_preview(tab, tmp_path)
        tab._on_project_selected(str(path_b))
        assert tab._video_settings["video_photo_zoom"] == DEFAULT_PHOTO_ZOOM
        assert tab._preview_zoom_spin.value() == 100
        tab._on_project_selected(str(path_a))
        assert tab._preview_zoom_spin.value() == 200
        tab.shutdown()

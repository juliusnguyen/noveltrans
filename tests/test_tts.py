import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from noveltrans.errors import TtsError
from noveltrans.tts import get_tts_engine
from noveltrans.tts.base import TtsEngine, split_sentences


class TestSplitSentences:
    def test_empty(self):
        assert split_sentences("") == []
        assert split_sentences("\n\n  \n\n") == []

    def test_short_paragraph_single_chunk(self):
        assert split_sentences("Một câu. Hai câu.", 100) == ["Một câu. Hai câu."]

    def test_packs_sentences_up_to_limit(self):
        text = "Câu một dài dài. Câu hai dài dài. Câu ba dài dài."
        chunks = split_sentences(text, 35)
        assert chunks == ["Câu một dài dài. Câu hai dài dài.", "Câu ba dài dài."]

    def test_never_spans_paragraph_break(self):
        chunks = split_sentences("Đoạn một.\n\nĐoạn hai.", 100)
        assert chunks == ["Đoạn một.", "Đoạn hai."]

    def test_oversized_sentence_is_own_chunk(self):
        long_sentence = "x" * 500 + "."
        chunks = split_sentences(f"Ngắn. {long_sentence} Ngắn nữa.", 100)
        assert chunks == ["Ngắn.", long_sentence, "Ngắn nữa."]

    def test_rejoin_preserves_text(self):
        text = "Anh nói. Cô cười! Họ đi về…\n\nHôm sau trời mưa."
        chunks = split_sentences(text, 20)
        assert " ".join(chunks).replace(" ", "") == text.replace("\n\n", "").replace(" ", "")


class FakeTtsEngine(TtsEngine):
    """1000 Hz engine: 1 sample per character, for deterministic duration math."""

    name = "fake"
    sample_rate = 1000
    max_chunk_chars = 30
    paragraph_gap_seconds = 0.1

    def __init__(self):
        self.loaded = False
        self.chunks: list[str] = []
        self.saved: list[Path] = []

    def load(self) -> None:
        self.loaded = True

    def list_voices(self) -> list[tuple[str, str]]:
        return [("Giọng A — test", "Giọng A")]

    def synthesize(self, text: str):
        self.chunks.append(text)
        return np.ones(len(text), dtype=np.float32)

    def save_wav(self, samples, out_path: Path) -> None:
        out_path.write_bytes(b"RIFF" + bytes(int(abs(s)) for s in samples[:4]))
        self.saved.append(out_path)
        self.last_samples = np.asarray(samples)  # captured so gain/gap are assertable


class TestSynthesizeChapter:
    def test_chunks_and_duration(self, tmp_path):
        engine = FakeTtsEngine()
        out = tmp_path / "audio" / "0001-test.wav"
        text = "a" * 25 + ". " + "b" * 25 + "."
        seconds = engine.synthesize_chapter("Tiêu đề", text, out)
        assert out.exists()
        assert len(engine.chunks) == 3  # title + 2 sentence chunks
        total_samples = sum(len(c) for c in engine.chunks)
        gaps = 2 * int(1000 * 0.1)
        assert seconds == pytest.approx((total_samples + gaps) / 1000)

    def test_empty_chapter_raises(self, tmp_path):
        engine = FakeTtsEngine()
        with pytest.raises(TtsError, match="không có nội dung"):
            engine.synthesize_chapter("", "  \n\n ", tmp_path / "x.wav")

    def test_cancel_between_chunks(self, tmp_path):
        engine = FakeTtsEngine()
        calls = iter([False, True])
        with pytest.raises(TtsError, match="dừng"):
            engine.synthesize_chapter(
                "", "một. hai. ba." * 20, tmp_path / "x.wav", cancelled=lambda: next(calls)
            )
        assert engine.saved == []

    def test_clean_strips_specials_before_synthesis(self, tmp_path):
        # clean=True (default): the engine never sees the emoji/CJK.
        engine = FakeTtsEngine()
        engine.synthesize_chapter("★ Tiêu đề ★", "Nội dung 😀 中文 ở đây.", tmp_path / "x.wav")
        seen = " ".join(engine.chunks)
        assert "★" not in seen and "😀" not in seen and "中" not in seen
        assert "Tiêu đề" in seen and "Nội dung" in seen  # Vietnamese survived

    def test_clean_false_passes_text_through_untouched(self, tmp_path):
        # The escape hatch: nothing is stripped when cleaning is off.
        engine = FakeTtsEngine()
        engine.synthesize_chapter("", "Nội dung ★ 😀 nguyên vẹn.", tmp_path / "x.wav", clean=False)
        assert "★" in " ".join(engine.chunks)

    def test_chapter_that_cleans_to_empty_raises_gracefully(self, tmp_path):
        # A chapter of only symbols cleans to "" — must hit the existing empty-content
        # TtsError, not crash on an empty concatenate.
        engine = FakeTtsEngine()
        with pytest.raises(TtsError, match="không có nội dung"):
            engine.synthesize_chapter("", "★☆※ 😀 【】", tmp_path / "x.wav")
        assert engine.saved == []


class TestGapAndVolume:
    # FakeTtsEngine is 1000 Hz, 1 sample/char, so duration math is exact and gain is
    # visible in the captured samples — no model needed.
    def _text(self):
        # Two sentences each over max_chunk_chars (30) so they land in separate chunks
        # → exactly one gap between them.
        return "a" * 25 + ". " + "b" * 25 + "."

    def test_gap_seconds_overrides_the_default_and_moves_duration(self, tmp_path):
        engine = FakeTtsEngine()
        # two chunks → one gap between them. default gap 0.1s = 100 samples.
        default = engine.synthesize_chapter("", self._text(), tmp_path / "d.wav")
        wide = engine.synthesize_chapter("", self._text(), tmp_path / "w.wav", gap_seconds=0.3)
        # 0.3s gap = 300 samples vs 100 → +200 samples = +0.2s
        assert wide == pytest.approx(default + 0.2)

    def test_zero_gap_inserts_no_silence(self, tmp_path):
        engine = FakeTtsEngine()
        default = engine.synthesize_chapter("", self._text(), tmp_path / "d.wav")
        none = engine.synthesize_chapter("", self._text(), tmp_path / "z.wav", gap_seconds=0.0)
        # gap_seconds=0 drops the 100-sample (0.1s) default pad between the two chunks
        assert none == pytest.approx(default - 0.1)

    def test_volume_scales_and_default_is_untouched(self, tmp_path):
        engine = FakeTtsEngine()
        engine.synthesize_chapter("", "aaaa.", tmp_path / "half.wav", volume=0.5)
        assert engine.last_samples.max() == pytest.approx(0.5)  # ones → 0.5

        engine.synthesize_chapter("", "aaaa.", tmp_path / "one.wav")  # default 1.0
        assert engine.last_samples.max() == pytest.approx(1.0)

    def test_volume_above_one_is_hard_clipped(self, tmp_path):
        engine = FakeTtsEngine()
        engine.synthesize_chapter("", "aaaa.", tmp_path / "loud.wav", volume=2.0)
        # ones × 2 = 2.0, clipped back to 1.0 — no wraparound distortion
        assert engine.last_samples.max() == pytest.approx(1.0)


class TestRegistry:
    def test_unknown_engine(self):
        with pytest.raises(TtsError, match="Unknown TTS engine"):
            get_tts_engine("espeak")

    def test_vieneu_engine_constructed_without_package(self):
        # constructing is lazy; only load() needs the vieneu package
        engine = get_tts_engine("vieneu", voice="Xuân Vĩnh")
        assert engine.name == "vieneu"
        assert engine.voice == "Xuân Vĩnh"

    def test_missing_package_raises_install_hint(self):
        engine = get_tts_engine("vieneu")
        with patch.dict(sys.modules, {"vieneu": None}):
            with pytest.raises(TtsError, match="Chưa cài VieNeu-TTS"):
                engine.load()


class TestVieneuEngine:
    def _engine_with_mock(self, voice="", presets=None, default_voice=None, temperature=None):
        mock_tts = MagicMock()
        if presets is not None:
            mock_tts.list_preset_voices.return_value = presets
        if default_voice is not None:
            mock_tts._default_voice = default_voice
        mock_module = MagicMock()
        mock_module.Vieneu.return_value = mock_tts
        engine = get_tts_engine("vieneu", voice=voice, temperature=temperature)
        with patch.dict(sys.modules, {"vieneu": mock_module}):
            engine.load()
        return engine, mock_tts

    def test_synthesize_passes_voice(self):
        engine, mock_tts = self._engine_with_mock(voice="Xuân Vĩnh")
        mock_tts.infer.return_value = np.zeros(10)
        engine.synthesize("xin chào")
        mock_tts.infer.assert_called_once_with("xin chào", voice="Xuân Vĩnh")

    def test_synthesize_default_voice(self):
        engine, mock_tts = self._engine_with_mock()
        mock_tts.infer.return_value = np.zeros(10)
        engine.synthesize("xin chào")
        mock_tts.infer.assert_called_once_with("xin chào")

    def test_synthesize_passes_temperature_when_set(self):
        engine, mock_tts = self._engine_with_mock(voice="Xuân Vĩnh", temperature=0.7)
        mock_tts.infer.return_value = np.zeros(10)
        engine.synthesize("xin chào")
        mock_tts.infer.assert_called_once_with("xin chào", voice="Xuân Vĩnh", temperature=0.7)

    def test_synthesize_omits_temperature_when_unset(self):
        # Parity: an unset temperature must add NO kwarg, so infer uses the model default.
        engine, mock_tts = self._engine_with_mock(temperature=None)
        mock_tts.infer.return_value = np.zeros(10)
        engine.synthesize("xin chào")
        mock_tts.infer.assert_called_once_with("xin chào")

    def test_voices_before_load_are_presets(self):
        engine = get_tts_engine("vieneu")
        assert ("Ngọc Linh — Nữ · Bắc · Phong cách kể chuyện", "Ngọc Linh") in engine.list_voices()

    def test_voices_from_loaded_model(self):
        engine, mock_tts = self._engine_with_mock()
        mock_tts.list_preset_voices.return_value = [("Giọng X — mô tả", "id-x")]
        assert engine.list_voices() == [("Giọng X — mô tả", "id-x")]

    def test_infer_failure_wrapped(self):
        engine, mock_tts = self._engine_with_mock()
        mock_tts.infer.side_effect = RuntimeError("onnx boom")
        with pytest.raises(TtsError, match="onnx boom"):
            engine.synthesize("xin chào")

    def test_load_resolves_unknown_voice_to_default(self):
        presets = [(f"{n} — mô tả", n) for n in ("Minh Đức", "Phạm Tuyên", "Ngọc Linh")]
        engine, mock_tts = self._engine_with_mock(
            voice="Ngọc Lan", presets=presets, default_voice="Phạm Tuyên"
        )
        assert engine.voice == "Phạm Tuyên"  # model default, not the stale name
        assert engine.voice_notice  # non-empty substitution notice
        mock_tts.infer.return_value = np.zeros(10)
        engine.synthesize("xin chào")  # must not raise on the (resolved) voice
        mock_tts.infer.assert_called_once_with("xin chào", voice="Phạm Tuyên")

    def test_load_resolves_unknown_voice_to_first_when_no_default(self):
        presets = [(f"{n} — mô tả", n) for n in ("Minh Đức", "Phạm Tuyên")]
        engine, _ = self._engine_with_mock(
            voice="Ngọc Lan", presets=presets, default_voice="Không Tồn Tại"
        )
        assert engine.voice == "Minh Đức"  # first available, since default is invalid too
        assert engine.voice_notice

    def test_load_keeps_valid_voice(self):
        presets = [(f"{n} — mô tả", n) for n in ("Minh Đức", "Ngọc Linh")]
        engine, _ = self._engine_with_mock(
            voice="Ngọc Linh", presets=presets, default_voice="Minh Đức"
        )
        assert engine.voice == "Ngọc Linh"
        assert engine.voice_notice == ""

    def test_load_survives_model_voice_list_error(self):
        # If the model's voice list errors, list_voices() falls back to PRESET_VOICES,
        # so load() must not raise and self.voice must end up a real preset id.
        from noveltrans.tts.vieneu import PRESET_VOICES

        mock_tts = MagicMock()
        mock_tts.list_preset_voices.side_effect = RuntimeError("boom")
        mock_module = MagicMock()
        mock_module.Vieneu.return_value = mock_tts
        engine = get_tts_engine("vieneu", voice="Ngọc Lan")
        with patch.dict(sys.modules, {"vieneu": mock_module}):
            engine.load()  # must not raise
        assert engine.voice in {vid for _, vid in PRESET_VOICES}

    def test_default_voice_constant_is_a_preset(self):
        from noveltrans.config import DEFAULT_TTS_VOICE
        from noveltrans.tts.vieneu import PRESET_VOICES

        assert DEFAULT_TTS_VOICE in {vid for _, vid in PRESET_VOICES}


class TestAudioWorker:
    def _project(self, library_dir, sample_meta, sample_refs, translated=(0, 1)):
        from noveltrans.storage import NovelProject

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for idx in translated:
            project.save_content(idx, "原文")
            project.save_translation(idx, f"Chương {idx + 1}", "bản dịch dài.", "vi")
        return project

    def _run_worker(self, project, engine, **kwargs):
        from noveltrans.gui.workers import AudioWorker

        results = {"done": [], "errors": [], "failed": [], "finished": None}
        worker = AudioWorker(project.path, voice="Ngọc Lan", **kwargs)
        worker.chapter_done.connect(results["done"].append)
        worker.chapter_error.connect(lambda i, m: results["errors"].append((i, m)))
        worker.failed.connect(results["failed"].append)
        worker.finished_ok.connect(lambda ok, err: results.__setitem__("finished", (ok, err)))
        with patch("noveltrans.tts.get_tts_engine", return_value=engine):
            worker.run()  # synchronous — no thread needed for tests
        return worker, results

    def test_generates_pending_and_resumes(self, library_dir, sample_meta, sample_refs):
        engine = FakeTtsEngine()
        project = self._project(library_dir, sample_meta, sample_refs)
        _, results = self._run_worker(project, engine)
        assert results["finished"] == (2, 0)
        assert results["done"] == [0, 1]
        chapter = project.chapter(0)
        assert chapter.audio_path.startswith("exports/audio/0001-")
        assert chapter.audio_path.endswith("-ngoc-lan.wav")  # voice in filename
        assert (project.path / chapter.audio_path).exists()
        assert chapter.audio_voice == "Ngọc Lan"
        # second run: nothing pending
        engine2 = FakeTtsEngine()
        _, results2 = self._run_worker(project, engine2)
        assert results2["finished"] == (0, 0)
        assert engine2.chunks == []

    def test_indices_regenerates_specific_chapter(
        self, library_dir, sample_meta, sample_refs
    ):
        engine = FakeTtsEngine()
        project = self._project(library_dir, sample_meta, sample_refs)
        project.save_audio(0, "exports/audio/old.wav", "Cũ", 1.0)
        _, results = self._run_worker(project, engine, indices=[0])
        assert results["finished"] == (1, 0)
        assert project.chapter(0).audio_voice == "Ngọc Lan"

    def test_voice_change_regenerates_all_and_drops_stale_file(
        self, library_dir, sample_meta, sample_refs
    ):
        engine = FakeTtsEngine()
        project = self._project(library_dir, sample_meta, sample_refs)
        stale = project.path / "exports/audio/0001-old-format.mp3"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"ID3")
        project.save_audio(0, "exports/audio/0001-old-format.mp3", "Giọng Cũ", 1.0)
        project.save_audio(1, "exports/audio/0002-x.wav", "Giọng Cũ", 1.0)
        # worker runs with the default test voice "Ngọc Lan" — both chapters re-pend
        _, results = self._run_worker(project, engine)
        assert results["finished"] == (2, 0)
        assert project.chapter(0).audio_voice == "Ngọc Lan"
        assert project.chapter(0).audio_path.endswith("-ngoc-lan.wav")
        assert not stale.exists()  # old differently-named file cleaned up

    def test_engine_error_marks_chapter_and_continues(
        self, library_dir, sample_meta, sample_refs
    ):
        engine = FakeTtsEngine()
        original = engine.synthesize
        engine.synthesize = lambda text: (_ for _ in ()).throw(TtsError("hỏng")) \
            if "Chương 1" in text else original(text)
        project = self._project(library_dir, sample_meta, sample_refs)
        _, results = self._run_worker(project, engine)
        assert results["finished"] == (1, 1)
        assert project.chapter(0).audio_error == "hỏng"
        assert project.chapter(1).has_audio

    def test_load_failure_emits_failed(self, library_dir, sample_meta, sample_refs):
        engine = FakeTtsEngine()
        engine.load = lambda: (_ for _ in ()).throw(TtsError("Chưa cài VieNeu-TTS"))
        project = self._project(library_dir, sample_meta, sample_refs)
        _, results = self._run_worker(project, engine)
        assert results["failed"] == ["Chưa cài VieNeu-TTS"]
        assert results["finished"] is None

    def test_cancel_before_second_chapter(self, library_dir, sample_meta, sample_refs):
        from noveltrans.gui.workers import AudioWorker

        engine = FakeTtsEngine()
        project = self._project(library_dir, sample_meta, sample_refs)
        worker = AudioWorker(project.path, voice="Ngọc Lan")
        finished = []
        worker.finished_ok.connect(lambda ok, err: finished.append((ok, err)))
        worker.chapter_done.connect(lambda _idx: worker.cancel())
        with patch("noveltrans.tts.get_tts_engine", return_value=engine):
            worker.run()
        assert finished == [(1, 0)]
        assert not project.chapter(1).has_audio

    def test_workers_one_loads_engine_once(self, library_dir, sample_meta, sample_refs):
        # workers=1 (default) must take the sequential path: only the probe engine
        # is built — no wasted second ~334 MB load.
        project = self._project(library_dir, sample_meta, sample_refs)
        factory = MagicMock(side_effect=lambda *a, **k: FakeTtsEngine())
        from noveltrans.gui.workers import AudioWorker

        worker = AudioWorker(project.path, voice="Ngọc Lan")
        with patch("noveltrans.tts.get_tts_engine", factory):
            worker.run()
        assert factory.call_count == 1


class TestAudioWorkerParallel:
    """workers > 1: a fresh FakeTtsEngine per pool thread (via a factory), so each
    thread mutates only its own chunk/save lists — assert on final DB state."""

    def _project(self, library_dir, sample_meta, sample_refs, translated=(0, 1, 2, 3)):
        from noveltrans.storage import NovelProject

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        for idx in translated:
            project.save_content(idx, "原文")
            project.save_translation(idx, f"Chương {idx + 1}", "bản dịch dài.", "vi")
        return project

    def _run(self, project, factory, *, workers=3, **kwargs):
        from noveltrans.gui.workers import AudioWorker

        results = {"done": [], "errors": [], "failed": [], "finished": None}
        worker = AudioWorker(project.path, voice="Ngọc Lan", workers=workers, **kwargs)
        worker.chapter_done.connect(results["done"].append)
        worker.chapter_error.connect(lambda i, m: results["errors"].append((i, m)))
        worker.failed.connect(results["failed"].append)
        worker.finished_ok.connect(lambda ok, err: results.__setitem__("finished", (ok, err)))
        with patch("noveltrans.tts.get_tts_engine", factory):
            worker.run()
        return results

    def test_generates_all_pending(self, library_dir, sample_meta, sample_refs):
        project = self._project(library_dir, sample_meta, sample_refs)
        factory = MagicMock(side_effect=lambda *a, **k: FakeTtsEngine())
        results = self._run(project, factory, workers=3)
        assert results["finished"] == (4, 0)
        assert sorted(results["done"]) == [0, 1, 2, 3]
        for idx in range(4):
            chapter = project.chapter(idx)
            assert chapter.has_audio
            assert chapter.audio_voice == "Ngọc Lan"
            assert chapter.audio_path.endswith("-ngoc-lan.wav")
            assert (project.path / chapter.audio_path).exists()

    def test_continues_on_error(self, library_dir, sample_meta, sample_refs):
        project = self._project(library_dir, sample_meta, sample_refs)

        def make(*a, **k):
            engine = FakeTtsEngine()
            original = engine.synthesize
            engine.synthesize = lambda text: (_ for _ in ()).throw(TtsError("hỏng")) \
                if "Chương 1" in text else original(text)
            return engine

        results = self._run(project, MagicMock(side_effect=make), workers=3)
        assert results["finished"] == (3, 1)
        assert project.chapter(0).audio_error == "hỏng"
        for idx in (1, 2, 3):
            assert project.chapter(idx).has_audio

    def test_stale_file_cleanup(self, library_dir, sample_meta, sample_refs):
        project = self._project(library_dir, sample_meta, sample_refs, translated=(0, 1))
        stale = project.path / "exports/audio/0001-old-format.mp3"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"ID3")
        project.save_audio(0, "exports/audio/0001-old-format.mp3", "Giọng Cũ", 1.0)
        project.save_audio(1, "exports/audio/0002-x.wav", "Giọng Cũ", 1.0)
        factory = MagicMock(side_effect=lambda *a, **k: FakeTtsEngine())
        results = self._run(project, factory, workers=2)
        assert results["finished"] == (2, 0)
        assert project.chapter(0).audio_path.endswith("-ngoc-lan.wav")
        assert not stale.exists()  # old differently-named file cleaned up

    def test_engine_count_capped_by_chapters(self, library_dir, sample_meta, sample_refs):
        # workers=5 but only 2 chapters → never more than 2 engines load
        # (probe + at most one extra), i.e. min(workers, #chapters).
        project = self._project(library_dir, sample_meta, sample_refs, translated=(0, 1))
        factory = MagicMock(side_effect=lambda *a, **k: FakeTtsEngine())
        results = self._run(project, factory, workers=5)
        assert results["finished"] == (2, 0)
        assert factory.call_count <= 2


class TestConfigWorkers:
    def _config(self, tmp_path):
        from PySide6.QtCore import QSettings

        from noveltrans.config import AppConfig

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        return config

    def test_default_is_one(self, tmp_path):
        assert self._config(tmp_path).tts_workers == 1

    def test_clamps_below_one(self, tmp_path):
        config = self._config(tmp_path)
        config.tts_workers = 0
        assert config.tts_workers == 1
        config.tts_workers = -3
        assert config.tts_workers == 1

    def test_roundtrips_valid_value(self, tmp_path):
        config = self._config(tmp_path)
        config.tts_workers = 4
        assert config.tts_workers == 4


class TestConfigTtsAdjust:
    def _config(self, tmp_path):
        from PySide6.QtCore import QSettings

        from noveltrans.config import AppConfig

        config = AppConfig()
        config._s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        return config

    def test_defaults_reproduce_current_behaviour(self, tmp_path):
        c = self._config(tmp_path)
        assert (c.tts_gap_seconds, c.tts_speed, c.tts_volume, c.tts_temperature) == (
            0.4, 1.0, 1.0, 0.0
        )

    def test_each_knob_clamps_at_both_bounds(self, tmp_path):
        c = self._config(tmp_path)
        for name, lo, hi in [
            ("tts_gap_seconds", 0.0, 2.0),
            ("tts_speed", 0.5, 2.0),
            ("tts_volume", 0.1, 3.0),
            ("tts_temperature", 0.0, 1.5),
        ]:
            setattr(c, name, 999)
            assert getattr(c, name) == hi, name
            setattr(c, name, -999)
            assert getattr(c, name) == lo, name

    def test_roundtrips_valid_values(self, tmp_path):
        c = self._config(tmp_path)
        c.tts_gap_seconds, c.tts_speed, c.tts_volume, c.tts_temperature = 0.6, 1.25, 1.5, 0.7
        assert (c.tts_gap_seconds, c.tts_speed, c.tts_volume, c.tts_temperature) == (
            0.6, 1.25, 1.5, 0.7
        )


class TestConvert:
    def test_convert_replaces_wav_with_mp3(self, tmp_path):
        from noveltrans.tts.convert import convert_to_mp3

        wav = tmp_path / "0001-test.wav"
        wav.write_bytes(b"RIFF")

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"ID3")
            return MagicMock(returncode=0, stderr="")

        with patch("noveltrans.tts.convert.subprocess.run", side_effect=fake_run):
            mp3 = convert_to_mp3(wav)
        assert mp3 == tmp_path / "0001-test.mp3"
        assert mp3.exists()
        assert not wav.exists()  # intermediate deleted

    def test_ffmpeg_error_keeps_wav(self, tmp_path):
        from noveltrans.tts.convert import convert_to_mp3

        wav = tmp_path / "x.wav"
        wav.write_bytes(b"RIFF")
        with patch(
            "noveltrans.tts.convert.subprocess.run",
            return_value=MagicMock(returncode=1, stderr="codec boom"),
        ):
            with pytest.raises(TtsError, match="codec boom"):
                convert_to_mp3(wav)
        assert wav.exists()

    def test_worker_mp3_format(self, library_dir, sample_meta, sample_refs):
        from noveltrans.storage import NovelProject

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "Chương 1", "bản dịch.", "vi")

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"ID3")
            return MagicMock(returncode=0, stderr="")

        from noveltrans.gui.workers import AudioWorker

        worker = AudioWorker(project.path, voice="Ngọc Lan", out_format="mp3")
        with (
            patch("noveltrans.tts.get_tts_engine", return_value=FakeTtsEngine()),
            patch("noveltrans.tts.convert.subprocess.run", side_effect=fake_run),
        ):
            worker.run()
        chapter = project.chapter(0)
        assert chapter.audio_path.endswith(".mp3")
        assert (project.path / chapter.audio_path).exists()

    def _project_with_symbols(self, library_dir, sample_meta, sample_refs):
        from noveltrans.storage import NovelProject

        project = NovelProject.create(library_dir, sample_meta, sample_refs)
        project.save_content(0, "原文")
        project.save_translation(0, "★ Chương 1 ★", "Nội dung 😀 中文 ở đây.", "vi")
        return project

    def test_worker_clean_text_true_strips_before_the_engine(
        self, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.gui.workers import AudioWorker

        project = self._project_with_symbols(library_dir, sample_meta, sample_refs)
        engine = FakeTtsEngine()
        worker = AudioWorker(project.path, voice="Ngọc Lan", clean_text=True)
        with patch("noveltrans.tts.get_tts_engine", return_value=engine):
            worker.run()
        seen = " ".join(engine.chunks)
        assert "★" not in seen and "😀" not in seen and "中" not in seen
        assert "Chương 1" in seen  # Vietnamese survived

    def test_worker_clean_text_false_leaves_specials_for_the_engine(
        self, library_dir, sample_meta, sample_refs
    ):
        from noveltrans.gui.workers import AudioWorker

        project = self._project_with_symbols(library_dir, sample_meta, sample_refs)
        engine = FakeTtsEngine()
        worker = AudioWorker(project.path, voice="Ngọc Lan", clean_text=False)
        with patch("noveltrans.tts.get_tts_engine", return_value=engine):
            worker.run()
        assert "★" in " ".join(engine.chunks)  # the toggle really reached the engine


@pytest.mark.live
class TestVieneuLive:
    def test_synthesize_one_sentence(self, tmp_path):
        engine = get_tts_engine("vieneu")
        engine.load()
        seconds = engine.synthesize_chapter(
            "", "Xin chào, đây là bản đọc thử.", tmp_path / "live.wav"
        )
        assert (tmp_path / "live.wav").stat().st_size > 10_000
        assert seconds > 0.5

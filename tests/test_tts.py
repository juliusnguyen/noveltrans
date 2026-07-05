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
        out_path.write_bytes(b"RIFF" + bytes(int(s) for s in samples[:4]))
        self.saved.append(out_path)


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


class TestRegistry:
    def test_unknown_engine(self):
        with pytest.raises(TtsError, match="Unknown TTS engine"):
            get_tts_engine("espeak")

    def test_vieneu_engine_constructed_without_package(self):
        # constructing is lazy; only load() needs the vieneu package
        engine = get_tts_engine("vieneu", voice="Ngọc Lan")
        assert engine.name == "vieneu"
        assert engine.voice == "Ngọc Lan"

    def test_missing_package_raises_install_hint(self):
        engine = get_tts_engine("vieneu")
        with patch.dict(sys.modules, {"vieneu": None}):
            with pytest.raises(TtsError, match="Chưa cài VieNeu-TTS"):
                engine.load()


class TestVieneuEngine:
    def _engine_with_mock(self, voice=""):
        mock_tts = MagicMock()
        mock_module = MagicMock()
        mock_module.Vieneu.return_value = mock_tts
        engine = get_tts_engine("vieneu", voice=voice)
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

    def test_voices_before_load_are_presets(self):
        engine = get_tts_engine("vieneu")
        assert ("Ngọc Lan — nữ, giọng dịu dàng", "Ngọc Lan") in engine.list_voices()

    def test_voices_from_loaded_model(self):
        engine, mock_tts = self._engine_with_mock()
        mock_tts.list_preset_voices.return_value = [("Giọng X — mô tả", "id-x")]
        assert engine.list_voices() == [("Giọng X — mô tả", "id-x")]

    def test_infer_failure_wrapped(self):
        engine, mock_tts = self._engine_with_mock()
        mock_tts.infer.side_effect = RuntimeError("onnx boom")
        with pytest.raises(TtsError, match="onnx boom"):
            engine.synthesize("xin chào")


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

"""Feature 084 — what QC writes to the database, and what it must never write.

Two load-bearing tests, both named so a reviewer meets them first:

* `test_qc_off_translates_exactly_as_before` — the backward-compatibility guarantee;
* `test_a_scan_never_changes_a_single_byte_of_a_translation` — a quality SCAN forms an
  opinion, and an opinion must not be able to damage the text it is about.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from noveltrans.errors import NovelTransError, TranslateError
from noveltrans.gui.workers import (
    QcEngineSpec,
    QcScanWorker,
    QcSettings,
    TranslateWorker,
    chapters_to_qc,
    split_retranslation,
)
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject
from noveltrans.translators.base import Translator

SOURCE = "他看了她一眼然后转身离开，心中涌起一阵难以言喻的悲伤。" * 12
GOOD_VI = (
    "Hắn nhìn nàng một cái rồi quay đầu bỏ đi, trong lòng dâng lên một nỗi buồn khó tả. "
) * 12
ENGLISH = (
    "He looked at her once and then turned away, with a sadness in his heart that he "
    "could not put into words at all. "
) * 12
# `translate_chapter` strips the joined body before saving, so this is what lands in the DB.
STORED_GOOD = GOOD_VI.strip()
STORED_ENGLISH = ENGLISH.strip()


class _FakeEngine(Translator):
    """An engine whose reply is scripted, recording exactly how it was called.

    `translate` takes `retry_hint` keyword-only and defaults it, like every real LLM
    engine — so a worker that passes it when it should not shows up as a recorded hint.
    """

    name = "fake"
    display_name = "Fake"
    max_chunk_chars = 100_000  # one chunk per chapter keeps the call log readable
    supports_completion = True
    supports_retry_hint = True

    def __init__(self, *bodies: str):
        self.bodies = list(bodies) or [GOOD_VI]
        self.calls: list[tuple[str, str]] = []  # (text, retry_hint)

    def translate(self, text, source="zh", target="vi", *, retry_hint="") -> str:
        self.calls.append((text, retry_hint))
        # The worker also translates the novel's title/description through this same
        # method. Only calls carrying an actual CHAPTER body advance the script, or the
        # front matter would silently eat the first scripted reply.
        if not text.startswith(SOURCE[:20]):
            return "Chương"
        return self.bodies[min(len(self.body_calls) - 1, len(self.bodies) - 1)]

    @property
    def body_calls(self) -> list:
        """Only the calls that translated a chapter body, in order."""
        return [c for c in self.calls if c[0].startswith(SOURCE[:20])]

    def complete(self, prompt: str) -> str:
        return "OK"


def _project(library_dir: Path, *, translated: str = "") -> Path:
    """Two chapters with Chinese source; optionally already carrying `translated`."""
    meta = NovelMeta(url="https://x/truyen", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i + 1}") for i in range(2)]
    project = NovelProject.create(library_dir, meta, refs)
    for idx in range(2):
        project.save_content(idx, SOURCE)
        if translated:
            project.save_translation(idx, f"Chương {idx + 1}", translated, "vi", "CLI (agy)")
    path = project.path
    project.close()
    return path


def _use(monkeypatch, engine) -> None:
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)


def _settings(judge=None, attempts: int = 2) -> QcSettings:
    return QcSettings(chain=[QcEngineSpec("fake", attempts=attempts)], judge=judge)


@pytest.fixture
def opened():
    handles: list[NovelProject] = []

    def _open(path: Path) -> NovelProject:
        project = NovelProject.open(path)
        handles.append(project)
        return project

    yield _open
    for project in handles:
        project.close()


class TestTranslateWorkerWithoutQc:
    def test_qc_off_translates_exactly_as_before(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """THE backward-compatibility test. With `qc=None` the engine sees one call per
        chunk with NO retry hint, and no verdict is written — the path the app has always
        taken, unchanged."""
        path = _project(library_dir)
        engine = _FakeEngine(ENGLISH)  # deliberately bad: nothing must notice
        _use(monkeypatch, engine)
        worker = TranslateWorker(path, "fake", "vi")
        worker.run()

        project = opened(path)
        assert project.chapter(0).translated == STORED_ENGLISH  # saved as-is, as always
        assert project.chapter(0).qc_status == ""  # never checked, not "failed"
        assert all(hint == "" for _text, hint in engine.calls)  # no hint ever passed


class TestTranslateWorkerWithQc:
    def test_a_bad_chapter_is_retried_with_the_reason_named(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        engine = _FakeEngine(ENGLISH, GOOD_VI)
        _use(monkeypatch, engine)
        worker = TranslateWorker(path, "fake", "vi", qc=_settings())
        worker.run()

        project = opened(path)
        assert project.chapter(0).translated == STORED_GOOD
        assert project.chapter(0).qc_ok
        hints = [hint for _text, hint in engine.body_calls]
        assert hints[0] == ""  # the first attempt is what the app would have done anyway
        assert "TIẾNG VIỆT" in hints[1]  # …and the retry was told what went wrong

    def test_an_unfixable_chapter_keeps_its_best_text_and_is_marked(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """A fresh translation has no alternative text, so the least-bad attempt is kept
        AND the row is marked so the user can find it."""
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(ENGLISH))
        worker = TranslateWorker(path, "fake", "vi", qc=_settings())
        worker.run()

        chapter = opened(path).chapter(0)
        assert chapter.translated == STORED_ENGLISH  # kept, not thrown away
        assert chapter.qc_failed
        assert "tiếng Anh" in chapter.qc_reason
        assert chapter.error == chapter.qc_reason  # …and visible in the table's Lỗi column
        assert chapter.qc_attempts == 2

    def test_the_verdict_matches_the_text_that_was_saved(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(GOOD_VI))
        TranslateWorker(path, "fake", "vi", qc=_settings()).run()

        chapter = opened(path).chapter(0)
        assert not chapter.qc_is_stale  # the fingerprint is of what actually got stored

    def test_an_unbuildable_chain_reports_instead_of_translating_blindly(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """Every engine in the chain unusable is fatal — translating with no check at all
        would silently ignore what the user switched on."""
        path = _project(library_dir)
        _use(monkeypatch, _FakeEngine(GOOD_VI))  # the tab's own engine is fine…
        monkeypatch.setattr(  # …but nothing in the chain can be built
            QcEngineSpec, "build",
            lambda self, *a, **k: (_ for _ in ()).throw(NovelTransError("chưa có API key")),
        )
        failures: list[str] = []
        worker = TranslateWorker(path, "fake", "vi", qc=_settings())
        worker.failed.connect(failures.append)
        worker.run()
        assert failures and "kiểm tra chất lượng" in failures[0].lower()
        assert opened(path).chapter(0).translated == ""  # nothing written blindly

    def test_one_dead_engine_in_the_chain_is_skipped_not_fatal(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """A chain is meant to be configured with engines you have not set up yet."""
        path = _project(library_dir)
        good = _FakeEngine(GOOD_VI)
        _use(monkeypatch, good)
        real_build = QcEngineSpec.build

        def build(self, *args, **kwargs):
            if self.engine_name == "claude":
                raise NovelTransError("chưa có API key")
            return real_build(self, *args, **kwargs)

        monkeypatch.setattr(QcEngineSpec, "build", build)
        settings = QcSettings(
            chain=[QcEngineSpec("claude"), QcEngineSpec("fake")], judge=None
        )
        TranslateWorker(path, "fake", "vi", qc=settings).run()
        assert opened(path).chapter(0).qc_ok

    def test_a_vietnamese_source_novel_is_never_judged(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """`_run_identity` copies the original text verbatim. Judging that would flag a
        whole novel, and "re-translating" a copy is meaningless."""
        meta = NovelMeta(url="https://x/vn", site="x", title="Truyện", source_lang="vi")
        refs = [ChapterRef(index=0, title="Chương 1", url="https://x/1")]
        project = NovelProject.create(library_dir, meta, refs)
        project.save_content(0, "Nội dung tiếng Việt sẵn có.")
        path = project.path
        project.close()

        TranslateWorker(path, "fake", "vi", qc=_settings()).run()
        chapter = opened(path).chapter(0)
        assert chapter.translated == "Nội dung tiếng Việt sẵn có."
        assert chapter.qc_status == ""  # untouched by QC entirely


class TestQcScanWorker:
    def test_a_scan_never_changes_a_single_byte_of_a_translation(
        self, qapp, library_dir, opened
    ):
        """The other hard rule: a scan reads and forms an opinion. Nothing else."""
        path = _project(library_dir, translated=ENGLISH)
        worker = QcScanWorker(path, _settings())
        worker.run()

        project = opened(path)
        assert project.chapter(0).translated == ENGLISH  # byte for byte, trailing space and all
        assert project.chapter(0).qc_failed  # …and now carries an opinion about it
        assert project.chapter(0).error == ""  # a scan does not redden the row

    def test_it_reports_each_verdict_for_the_result_view(self, qapp, library_dir):
        path = _project(library_dir, translated=ENGLISH)
        verdicts: list[tuple] = []
        worker = QcScanWorker(path, _settings())
        worker.verdict.connect(lambda idx, code, reason: verdicts.append((idx, code)))
        worker.run()
        assert [idx for idx, _code in verdicts] == [0, 1]
        assert all(code == "not_vietnamese" for _idx, code in verdicts)

    def test_a_good_novel_produces_no_failures(self, qapp, library_dir, opened):
        path = _project(library_dir, translated=GOOD_VI)
        results: list[tuple] = []
        worker = QcScanWorker(path, _settings())
        worker.finished_ok.connect(lambda checked, failed: results.append((checked, failed)))
        worker.run()
        assert results == [(2, 0)]
        assert opened(path).chapter(0).qc_ok

    def test_a_second_scan_skips_what_it_already_judged(self, qapp, library_dir, opened):
        path = _project(library_dir, translated=GOOD_VI)
        QcScanWorker(path, _settings()).run()
        again: list[tuple] = []
        worker = QcScanWorker(path, _settings())
        worker.finished_ok.connect(lambda checked, failed: again.append((checked, failed)))
        worker.run()
        assert again == [(0, 0)]  # nothing left to do → an interrupted scan resumes free

    def test_editing_a_chapter_makes_its_verdict_stale_again(self, qapp, library_dir, opened):
        path = _project(library_dir, translated=GOOD_VI)
        QcScanWorker(path, _settings()).run()
        project = opened(path)
        project.edit_translation(0, text=ENGLISH)
        assert chapters_to_qc(project, "vi") and chapters_to_qc(project, "vi")[0].index == 0

    def test_the_limit_is_the_dialogs_dry_run(self, qapp, library_dir):
        path = _project(library_dir, translated=GOOD_VI)
        results: list[tuple] = []
        worker = QcScanWorker(path, _settings(), limit=1)
        worker.finished_ok.connect(lambda checked, failed: results.append((checked, failed)))
        worker.run()
        assert results == [(1, 0)]

    def test_a_broken_judge_does_not_fail_chapters(self, qapp, library_dir, monkeypatch, opened):
        """An engine outage must not turn into a library-wide re-translation."""
        class _DeadJudge(_FakeEngine):
            def complete(self, prompt: str) -> str:
                raise TranslateError("hết quota")

        _use(monkeypatch, _DeadJudge())
        path = _project(library_dir, translated=GOOD_VI)
        worker = QcScanWorker(path, _settings(judge=QcEngineSpec("fake")))
        worker.run()
        assert opened(path).chapter(0).qc_ok

    def test_a_judge_that_cannot_be_built_degrades_to_the_cheap_checks(
        self, qapp, library_dir, monkeypatch, opened
    ):
        monkeypatch.setattr(
            "noveltrans.translators.get_translator",
            lambda *a, **k: (_ for _ in ()).throw(NovelTransError("chưa có API key")),
        )
        path = _project(library_dir, translated=ENGLISH)
        worker = QcScanWorker(path, _settings(judge=QcEngineSpec("claude")))
        worker.run()
        assert opened(path).chapter(0).qc_failed  # heuristics still caught the English


class TestChaptersToQc:
    def test_only_translated_chapters_are_eligible(self, library_dir, opened):
        path = _project(library_dir)  # downloaded, never translated
        assert chapters_to_qc(opened(path), "vi") == []

    def test_explicit_indices_win_over_the_resume_query(self, library_dir, opened):
        path = _project(library_dir, translated=GOOD_VI)
        project = opened(path)
        QcScanWorker(path, _settings()).run()
        # Already judged, so the resume query is empty — but a right-click on the row is
        # an explicit "check this one again".
        assert chapters_to_qc(project, "vi") == []
        assert [c.index for c in chapters_to_qc(project, "vi", indices=[1])] == [1]


class _TitleEngine(_FakeEngine):
    """Scripted TITLE replies on top of `_FakeEngine`'s body script. No retry sleep."""

    retry_delay = 0.0

    def __init__(self, *titles: str, bodies: tuple[str, ...] = (GOOD_VI,)):
        super().__init__(*bodies)
        self.titles = list(titles)
        self.title_calls = 0

    def translate(self, text, source="zh", target="vi", *, retry_hint="") -> str:
        if text.startswith(SOURCE[:20]) or text != CHINESE_TITLE:
            return super().translate(text, source, target, retry_hint=retry_hint)
        self.calls.append((text, retry_hint))
        self.title_calls += 1
        return self.titles[min(self.title_calls - 1, len(self.titles) - 1)]


CHINESE_TITLE = "第1章 夜雨孤灯"


def _title_failed_project(library_dir: Path) -> Path:
    """One chapter with a good body saved under its untranslated title, flagged by QC."""
    meta = NovelMeta(url="https://x/t", site="x", title="Truyện", source_lang="zh")
    project = NovelProject.create(
        library_dir, meta, [ChapterRef(index=0, title=CHINESE_TITLE, url="https://x/1")]
    )
    project.save_content(0, SOURCE)
    project.save_translation(0, CHINESE_TITLE, STORED_GOOD, "vi", "CLI (agy)")
    project.mark_qc_failed(
        0, "title_untranslated", "tiêu đề còn chữ Hán", project.chapter(0).qc_fingerprint(), 2
    )
    path = project.path
    project.close()
    return path


class TestTitleOnlyRetranslation:
    """Feature 094 — a chapter whose only failure is its title keeps its body."""

    def test_only_the_title_is_sent_and_the_body_is_kept(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _title_failed_project(library_dir)
        engine = _TitleEngine("Chương 1: Đèn cô độc đêm mưa")
        _use(monkeypatch, engine)
        TranslateWorker(path, "fake", "vi", indices=[0], qc=_settings(), title_only={0}).run()

        chapter = opened(path).chapter(0)
        assert engine.body_calls == []  # THE point: not one body token spent
        assert chapter.translated_title == "Chương 1: Đèn cô độc đêm mưa"
        assert chapter.translated == STORED_GOOD  # byte for byte
        assert chapter.translator == "CLI (agy)"  # the body is still that engine's work
        assert chapter.qc_ok and not chapter.qc_is_stale
        assert chapter.error == "" and chapter.status == "translated"

    def test_a_title_that_stays_chinese_keeps_the_old_one_and_stays_marked(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _title_failed_project(library_dir)
        _use(monkeypatch, _TitleEngine(CHINESE_TITLE))
        errors: list = []
        worker = TranslateWorker(path, "fake", "vi", indices=[0], qc=_settings(), title_only={0})
        worker.chapter_error.connect(lambda idx, msg: errors.append(idx))
        worker.run()

        chapter = opened(path).chapter(0)
        assert chapter.translated == STORED_GOOD
        assert chapter.qc_failed and chapter.qc_code == "title_untranslated"
        assert chapter.qc_attempts == 2  # the chain's budget, spent and recorded
        assert errors == [0]

    def test_the_body_gets_the_judge_it_never_had(
        self, qapp, library_dir, monkeypatch, opened
    ):
        """A title failure stopped the scan before the judge read the body."""

        class _HarshJudge(_TitleEngine):
            def complete(self, prompt: str) -> str:
                return "LOI: han_viet — trật tự từ kiểu tiếng Trung"

        path = _title_failed_project(library_dir)
        _use(monkeypatch, _HarshJudge("Chương 1: Đèn cô độc đêm mưa"))
        TranslateWorker(
            path, "fake", "vi", indices=[0], qc=_settings(judge=QcEngineSpec("fake")),
            title_only={0},
        ).run()

        chapter = opened(path).chapter(0)
        assert chapter.translated_title == "Chương 1: Đèn cô độc đêm mưa"  # the fix stays
        assert chapter.qc_code == "han_viet"  # …and the body's real verdict surfaces

    def test_with_qc_off_the_tabs_engine_fixes_the_title(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _title_failed_project(library_dir)
        engine = _TitleEngine("Chương 1: Đèn cô độc đêm mưa")
        _use(monkeypatch, engine)
        TranslateWorker(path, "fake", "vi", indices=[0], title_only={0}).run()

        chapter = opened(path).chapter(0)
        assert engine.body_calls == []
        assert chapter.translated_title == "Chương 1: Đèn cô độc đêm mưa"
        assert chapter.qc_ok

    def test_a_chapter_without_a_body_is_translated_in_full(
        self, qapp, library_dir, monkeypatch, opened
    ):
        path = _title_failed_project(library_dir)
        project = opened(path)
        project.clear_translations([0])
        engine = _TitleEngine("Chương 1: Đèn cô độc đêm mưa")
        _use(monkeypatch, engine)
        TranslateWorker(path, "fake", "vi", indices=[0], qc=_settings(), title_only={0}).run()
        assert len(engine.body_calls) == 1


class TestSplitRetranslation:
    def test_only_a_title_failure_with_a_body_is_title_only(self, library_dir, opened):
        path = _title_failed_project(library_dir)
        project = opened(path)
        title_chapter = project.chapter(0)
        body_failure = replace(title_chapter, index=1, qc_code="not_vietnamese")
        no_body = replace(title_chapter, index=2, translated="")
        assert split_retranslation([title_chapter, body_failure, no_body]) == ([0], [1, 2])

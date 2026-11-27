"""Feature 113, phase 3 — checking chapters against the address table during a QC scan.

Load-bearing tests, named so a reviewer meets them first:

* `test_an_unparseable_or_failing_judge_never_fails_a_chapter` — same contract as the
  style judge: a missing opinion must not become a re-translation;
* `test_a_chapter_without_table_characters_costs_no_call`;
* `test_drift_is_fixed_in_place_and_the_stale_verdict_cleared`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from noveltrans.address_table import AddressEntry, AddressTable, write_address
from noveltrans.config import AppConfig
from noveltrans.gui.workers import (
    AddressFixWorker,
    QcEngineSpec,
    QcScanWorker,
    QcSettings,
    qc_settings_from_config,
)
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject
from noveltrans.translators.base import Translator
from noveltrans.translators.qc import (
    QC_ADDRESS_DRIFT,
    QC_LABELS,
    QC_SEVERITY,
    judge_address,
    parse_address_judge_reply,
    retry_hint_for,
    QcVerdict,
)

A, B = "Lâm Phong", "Tô Tuyết"
TABLE = AddressTable(entries=(AddressEntry(A, B, "tớ", "cậu"),))
DRIFTED = (
    "Lâm Phong quay sang Tô Tuyết, giọng trầm xuống như sắp nổi giận đến nơi rồi.\n\n"
    "“Tao bảo mày rồi, đừng đi một mình ra ngoài vào lúc trời tối như thế này nữa.”\n\n"
    "Tô Tuyết cúi đầu, không nói gì, chỉ lặng lẽ siết chặt vạt áo trong tay mình."
)
FIXED = DRIFTED.replace("Tao bảo mày", "Tớ bảo cậu")
NO_PAIR = "Trời mưa suốt đêm, gió lạnh thổi qua những mái nhà cũ trong con ngõ nhỏ. " * 4


class TestJudge:
    def test_code_is_registered(self):
        assert QC_LABELS[QC_ADDRESS_DRIFT]
        assert QC_ADDRESS_DRIFT in QC_SEVERITY
        assert "BẢNG XƯNG HÔ" in retry_hint_for(QcVerdict(QC_ADDRESS_DRIFT))

    def test_fail_line_is_read(self):
        verdict = parse_address_judge_reply(
            "LOI: address_drift — Lâm Phong → Tô Tuyết: dùng tao/mày thay vì tớ/cậu"
        )
        assert verdict.code == QC_ADDRESS_DRIFT and "tao/mày" in verdict.reason

    def test_an_unparseable_or_failing_judge_never_fails_a_chapter(self):
        assert parse_address_judge_reply("Tôi nghĩ là có vấn đề…").ok
        assert parse_address_judge_reply("").ok
        assert judge_address(lambda _p: 1 / 0, DRIFTED, "BẢNG").ok

    def test_no_block_means_no_call(self):
        assert judge_address(lambda _p: 1 / 0 or "LOI: x", DRIFTED, "").ok


class _Judge(Translator):
    """Fails any chapter whose dialogue still says 'Tao bảo mày'."""

    name = "fake"
    supports_completion = True

    def __init__(self):
        self.prompts: list[str] = []

    def translate(self, text, source="zh", target="vi"):
        return text

    def complete(self, prompt):
        self.prompts.append(prompt)
        if "Sửa CÁCH XƯNG HÔ" in prompt:  # the fix prompt carries the table too — first
            return prompt.split("---\n", 1)[1].replace("Tao bảo mày", "Tớ bảo cậu")
        if "BẢNG XƯNG HÔ" in prompt and "Tao bảo mày" in prompt:
            return "LOI: address_drift — Lâm Phong → Tô Tuyết: dùng tao/mày thay vì tớ/cậu"
        return "OK"


def _project(library_dir: Path, bodies: list[str]) -> Path:
    meta = NovelMeta(url="https://x/t", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"u{i}") for i in range(len(bodies))]
    project = NovelProject.create(library_dir, meta, refs)
    for i, body in enumerate(bodies):
        project.save_content(i, "原文" * 40)
        project.save_translation(i, f"Chương {i + 1}", body, "vi", "Google")
    path = project.path
    project.close()
    return path


@pytest.fixture
def judge(monkeypatch):
    engine = _Judge()
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)
    return engine


def _scan(path, address: bool = True):
    spec = QcEngineSpec("fake")
    QcScanWorker(
        path, QcSettings(chain=[spec], address_judge=spec if address else None), force=True
    ).run()


def _scan_pending(path):
    spec = QcEngineSpec("fake")
    QcScanWorker(path, QcSettings(chain=[spec], address_judge=spec)).run()


class TestScan:
    def test_drift_is_flagged(self, qapp, library_dir, judge):
        path = _project(library_dir, [DRIFTED, FIXED])
        write_address(path, TABLE)
        _scan(path)
        project = NovelProject.open(path)
        try:
            assert project.chapter(0).qc_code == QC_ADDRESS_DRIFT
            assert project.chapter(1).qc_ok
        finally:
            project.close()

    def test_a_chapter_without_table_characters_costs_no_call(self, qapp, library_dir, judge):
        path = _project(library_dir, [NO_PAIR])
        write_address(path, TABLE)
        _scan(path)
        assert judge.prompts == []

    def test_a_general_note_alone_costs_no_call(self, qapp, library_dir, judge):
        path = _project(library_dir, [DRIFTED])
        write_address(path, AddressTable(general_note="cổ trang: ta/ngươi"))
        _scan(path)
        assert not any("BẢNG XƯNG HÔ" in p for p in judge.prompts)

    def test_chapters_that_passed_before_the_table_are_checked_for_pronouns(
        self, qapp, library_dir, judge
    ):
        path = _project(library_dir, [DRIFTED, NO_PAIR])
        _scan(path)  # no table yet: both pass
        write_address(path, TABLE)
        judge.prompts.clear()
        _scan_pending(path)  # the default scope, not "all"
        project = NovelProject.open(path)
        try:
            assert project.chapter(0).qc_code == QC_ADDRESS_DRIFT
            assert project.chapter(1).qc_ok  # no table character: not re-checked
        finally:
            project.close()
        # only the pronoun check was paid for, and only for the chapter a row covers
        assert len(judge.prompts) == 1 and "BẢNG XƯNG HÔ" in judge.prompts[0]

    def test_a_second_scan_costs_nothing_until_the_covering_row_changes(
        self, qapp, library_dir, judge
    ):
        path = _project(library_dir, [FIXED])
        write_address(path, TABLE)
        _scan_pending(path)
        judge.prompts.clear()
        _scan_pending(path)
        assert judge.prompts == []
        write_address(path, AddressTable(entries=(AddressEntry(A, B, "tôi", "cậu"),)))
        _scan_pending(path)
        assert len(judge.prompts) == 1

    def test_off_or_no_table_means_no_call(self, qapp, library_dir, judge):
        path = _project(library_dir, [DRIFTED])
        _scan(path)  # on, but the novel has no table
        write_address(path, TABLE)
        _scan(path, address=False)  # table, but switched off
        assert judge.prompts == []


def test_drift_is_fixed_in_place_and_the_stale_verdict_cleared(qapp, library_dir, judge):
    path = _project(library_dir, [DRIFTED])
    write_address(path, TABLE)
    _scan(path)
    AddressFixWorker(path, "fake", [0]).run()
    project = NovelProject.open(path)
    try:
        chapter = project.chapter(0)
        assert chapter.translated == FIXED
        assert chapter.qc_status == "" and chapter.qc_code == ""  # unchecked, not "OK"
        assert project.qc_failures() == []
    finally:
        project.close()


class TestSettings:
    @pytest.fixture
    def config(self, tmp_path):
        config = AppConfig()
        config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
        return config

    def test_off_by_default(self, config):
        assert not config.qc_check_address
        assert qc_settings_from_config(config, QcEngineSpec("cli")).address_judge is None

    def test_independent_of_the_style_judge(self, config):
        config.qc_use_llm_judge = False
        config.qc_check_address = True
        settings = qc_settings_from_config(config, QcEngineSpec("cli"))
        assert settings.judge is None and settings.address_judge is not None

    def test_dialog_offers_it_only_with_a_table(self, qapp, library_dir, config):
        from noveltrans.gui.qc_dialog import QcDialog

        path = _project(library_dir, [DRIFTED])
        project = NovelProject.open(path)
        try:
            assert not QcDialog(project, config).address_check.isEnabled()
            write_address(path, TABLE)
            dialog = QcDialog(project, config)
            assert dialog.address_check.isEnabled()
            dialog.address_check.setChecked(True)
            dialog._remember_choices()
            assert config.qc_check_address
        finally:
            project.close()

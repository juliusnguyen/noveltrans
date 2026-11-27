"""Feature 113, phase 2 — drafting an address table, and fixing chapters to match it.

Load-bearing tests, named so a reviewer meets them first:

* `test_a_fix_that_touches_anything_but_pronouns_is_rejected` — the validator is what
  makes an in-place AI edit of a finished translation safe at all;
* `test_a_failed_fix_keeps_the_text_and_does_not_queue_a_retranslation`;
* `test_a_scan_never_changes_a_row_the_user_already_has`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.address_table import (
    NARRATOR,
    AddressEntry,
    AddressTable,
    merge_extracted,
    render_prompt_block,
    write_address,
)
from noveltrans.errors import TranslateError
from noveltrans.gui.workers import AddressFixWorker, AddressScanWorker, sample_chapters
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject
from noveltrans.translators.address import (
    Finding,
    address_vocab,
    aggregate,
    check_address_fix,
    dialogue_excerpt,
    fix_chapter,
    parse_extract_reply,
)
import unicodedata

from noveltrans.translators.address import build_extract_prompt
from noveltrans.translators.base import Translator
from noveltrans.translators.qc import QC_ADDRESS_DRIFT
from noveltrans.translators.rewrite import build_rewrite_prompt

A, B = "Lâm Phong", "Tô Tuyết"
ROW = AddressEntry(A, B, "tớ", "cậu")
VOCAB = address_vocab([ROW])

DRIFTED = (
    "Lâm Phong quay sang Tô Tuyết.\n\n"
    "“Tao bảo mày rồi, đừng đi một mình.”\n\n"
    "Tô Tuyết cúi đầu, không nói gì."
)
FIXED = (
    "Lâm Phong quay sang Tô Tuyết.\n\n"
    "“Tớ bảo cậu rồi, đừng đi một mình.”\n\n"
    "Tô Tuyết cúi đầu, không nói gì."
)


# --------------------------------------------------------------------------- extraction


class TestExtraction:
    def test_excerpt_keeps_dialogue_and_the_line_before_it(self):
        text = "Trời mưa.\n\nGió thổi.\n\nLâm Phong quay sang.\n\n“Đi thôi.”\n\nHết."
        assert dialogue_excerpt(text) == "Lâm Phong quay sang.\n\n“Đi thôi.”"

    def test_excerpt_without_dialogue_falls_back_to_the_start(self):
        assert dialogue_excerpt("Một.\n\nHai.", limit=6) == "Một."

    def test_parse_reply_tolerates_noise(self):
        reply = (
            "Đây là kết quả:\n"
            "người nói | người nghe | xưng | gọi | số lần\n"
            "- Lâm Phong | Tô Tuyết | tớ | cậu | 4\n"
            "Tô Tuyết|Lâm Phong|'tớ'|“cậu”\n"
            "người kể | Lâm Phong | - | hắn | 3\n"
            "Lâm Phong | Lâm Phong | tôi | tôi | 1\n"
            "rác | chỉ hai cột\n"
            "A | B | - | - | 2\n"
        )
        assert parse_extract_reply(reply) == [
            Finding(A, B, "tớ", "cậu", 4),
            Finding(B, A, "tớ", "cậu", 1),
            Finding(NARRATOR, A, "", "hắn", 3),
        ]

    def test_parse_reply_reads_a_markdown_table(self):
        reply = (
            "| người nói | người nghe | xưng | gọi | số lần |\n"
            "|---|:---:|---|---|---|\n"
            "| Lâm Phong | Tô Tuyết | tớ | cậu | 4 |\n"
            "| Tô Tuyết | Lâm Phong | tớ | cậu | nhiều |\n"
        )
        assert parse_extract_reply(reply) == [
            Finding(A, B, "tớ", "cậu", 4),
            Finding(B, A, "tớ", "cậu", 1),  # a worded count counts once, not in the term
        ]

    def test_aggregate_picks_the_majority_and_reports_the_rest(self):
        proposed, conflicts = aggregate({
            1: [Finding(A, B, "tớ", "cậu", 3)],
            2: [Finding(A, B, "Tớ", "cậu", 1)],
            5: [Finding(A, B, "tao", "mày", 9)],
            7: [Finding(B, A, "tớ", "cậu")],
        })
        assert proposed[0] == AddressEntry(A, B, "tớ", "cậu", origin="auto")
        (conflict,) = conflicts
        assert conflict.minority_chapters == [5]  # more chapters beat more occurrences
        assert "tao/mày (chương 5)" in conflict.describe()

    def test_a_pair_the_table_has_is_measured_against_the_table(self):
        # The table says tớ/cậu; most sampled chapters drifted to tao/mày. The drifted
        # ones are what to fix — not the correct minority.
        proposed, conflicts = aggregate(
            {
                1: [Finding(A, B, "tao", "mày")],
                2: [Finding(A, B, "tao", "mày")],
                3: [Finding(A, B, "tớ", "cậu")],
            },
            existing=[ROW],
        )
        (conflict,) = conflicts
        assert conflict.minority_chapters == [1, 2]
        assert "lệch bảng" in conflict.describe()

    def test_agreeing_chapters_that_all_disagree_with_the_table_still_conflict(self):
        _, conflicts = aggregate({4: [Finding(A, B, "tao", "mày")]}, existing=[ROW])
        assert conflicts[0].minority_chapters == [4]
        _, conflicts = aggregate({4: [Finding(A, B, "Tớ", "cậu")]}, existing=[ROW])
        assert conflicts == []  # matches the table: nothing to report

    def test_the_row_in_force_at_each_chapter_decides(self):
        later = AddressEntry(A, B, "anh", "em", from_chapter=10)
        _, conflicts = aggregate(
            {2: [Finding(A, B, "tớ", "cậu")], 12: [Finding(A, B, "tớ", "cậu")]},
            existing=[ROW, later],
        )
        assert conflicts[0].minority_chapters == [12]

    def test_the_prompt_lists_the_names_in_the_excerpt_most_frequent_first(self):
        names = [f"Tên {i:03d}" for i in range(200)] + ["Vương Lâm"]
        prompt = build_extract_prompt("Vương Lâm nói. Vương Lâm cười. Tên 005 đi.", names)
        known = prompt.split("các tên đã biết: ", 1)[1].split(")", 1)[0]
        assert known == "Vương Lâm, Tên 005"

    def test_a_scan_never_changes_a_row_the_user_already_has(self):
        mine = AddressEntry(A, B, "anh", "em", from_chapter=40)
        merged = merge_extracted([mine], [ROW, AddressEntry(B, A, "em", "anh")])
        assert merged == [mine, AddressEntry(B, A, "em", "anh")]

    def test_sample_takes_the_start_then_spreads(self):
        picked = sample_chapters(list(range(100)), 6)
        assert picked[:3] == [0, 1, 2] and len(picked) == 6 and picked[-1] > 60
        assert sample_chapters([1, 2], 20) == [1, 2]


# --------------------------------------------------------------------------- fix


class TestValidator:
    def test_pronoun_swaps_pass(self):
        assert check_address_fix(DRIFTED, FIXED, VOCAB) == ""

    def test_a_fix_that_touches_anything_but_pronouns_is_rejected(self):
        reworded = FIXED.replace("đừng đi một mình", "đừng có đi một mình")
        assert "không phải xưng hô" in check_address_fix(DRIFTED, reworded, VOCAB)
        # a swap that uses only address words can still mangle a name that contains one
        assert "tên riêng" in check_address_fix(
            "“Lão Lý gọi mày.”", "“Ông Lý gọi cậu.”", VOCAB, ("Lão Lý",)
        )
        assert "không phải xưng hô" in check_address_fix(DRIFTED, FIXED.replace(",", ""), VOCAB)

    def test_everyday_words_that_double_as_pronouns_are_not_swappable(self):
        assert "không phải xưng hô" in check_address_fix(
            "Hắn có ba người con.", "Hắn có bố người con.", VOCAB
        )
        assert "không phải xưng hô" in check_address_fix(
            "Hắn có ba người con.", "Hắn có ba con.", VOCAB
        )
        assert "không phải xưng hô" in check_address_fix(
            "“Cái này của tớ.”", "“Cái đó của tớ.”", VOCAB
        )
        # a modifier alone is not an address change; with a pronoun it is
        assert "không phải xưng hô" in check_address_fix("Các chương.", "Chương.", VOCAB)
        assert check_address_fix("“Anh ta đi rồi.”", "“Hắn đi rồi.”", VOCAB) == ""
        assert check_address_fix("“Cô ấy cười.”", "“Nàng cười.”", VOCAB) == ""

    def test_swapping_or_dropping_pronouns_is_rejected(self):
        vocab = address_vocab([])
        assert "đảo chỗ" in check_address_fix("“Hắn đánh nàng.”", "“Nàng đánh hắn.”", vocab)
        assert "thêm hoặc bỏ" in check_address_fix("“Rồi tôi đi.”", "“Rồi đi.”", vocab)
        assert "thêm hoặc bỏ" in check_address_fix("“Rồi đi.”", "“Rồi tôi đi.”", vocab)
        # the same word in DIFFERENT sentences is two separate swaps, not a move
        assert check_address_fix(
            "“Tao đi. Mày ở.”", "“Mày đi. Tớ ở.”", vocab
        ) == ""

    def test_decomposed_text_is_compared_as_precomposed(self):
        nfd = unicodedata.normalize("NFD", DRIFTED)
        assert check_address_fix(nfd, FIXED, VOCAB, (A, B)) == ""

    def test_a_novel_that_uses_an_everyday_word_as_a_pronoun_lists_it(self):
        vocab = address_vocab([AddressEntry(A, B, "con", "ba")])
        assert check_address_fix("“Bố ơi, tôi về.”", "“Ba ơi, con về.”", vocab) == ""

    def test_a_multi_word_term_is_one_unit(self):
        # "công", "tử" on their own are everyday syllables — only the whole term may change
        assert check_address_fix("“Công tử đi đâu?”", "“Thiếu gia đi đâu?”", VOCAB) == ""
        assert "không phải xưng hô" in check_address_fix(
            "“Công việc xong rồi.”", "“Công tử xong rồi.”", VOCAB
        )

    def test_paragraphs_and_length_are_guarded(self):
        assert "số đoạn" in check_address_fix(DRIFTED, FIXED.replace("\n\n", " ", 1), VOCAB)
        assert "rỗng" in check_address_fix(DRIFTED, "", VOCAB)

    def test_table_terms_extend_the_vocabulary(self):
        vocab = address_vocab([AddressEntry(A, B, "bổn tọa", "tiểu tử")])
        assert check_address_fix("“Ta tha ngươi.”", "“Bổn tọa tha tiểu tử.”", vocab) == ""


class TestFixChapter:
    def test_good_reply_is_used(self):
        assert fix_chapter(lambda _p: FIXED, DRIFTED, [ROW]) == FIXED

    def test_retries_then_raises(self):
        prompts: list[str] = []

        def send(prompt):
            prompts.append(prompt)
            return FIXED.replace("một mình", "một thân")

        with pytest.raises(TranslateError):
            fix_chapter(send, DRIFTED, [ROW])
        assert len(prompts) == 3 and "lần trước bạn đã sai" in prompts[-1]

    def test_chunks_without_dialogue_are_not_sent(self):
        calls: list[str] = []
        body = "Trời mưa to.\n\nGió thổi."
        assert fix_chapter(lambda p: calls.append(p) or "x", body, [ROW]) == body
        assert calls == []

    def test_no_rows_means_no_call(self):
        assert fix_chapter(lambda _p: 1 / 0, DRIFTED, []) == DRIFTED


class TestRewritePrompt:
    def test_the_fix_prompt_keeps_pairs_outside_the_table(self, monkeypatch):
        sent = []
        fix_chapter(lambda prompt: sent.append(prompt) or FIXED, DRIFTED, [ROW])
        assert "GIỮ NGUYÊN xưng hô như bản hiện tại" in sent[0]
        assert "chọn xưng hô theo ngữ cảnh" not in sent[0]

    def test_rule_2_is_kept_without_a_table(self):
        assert "GIỮ NGUYÊN đại từ xưng hô" in build_rewrite_prompt("x")

    def test_rule_2_follows_the_table_when_there_is_one(self):
        block = render_prompt_block([ROW])
        prompt = build_rewrite_prompt("x", address_block=block)
        assert "GIỮ NGUYÊN đại từ xưng hô" not in prompt
        assert "XƯNG HÔ theo đúng BẢNG XƯNG HÔ" in prompt and block in prompt
        assert "GIỮ NGUYÊN đại từ xưng hô" in build_rewrite_prompt(
            "x", is_title=True, address_block=block
        )


# --------------------------------------------------------------------------- workers


class _Llm(Translator):
    name = "fake"
    supports_completion = True

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def translate(self, text, source="zh", target="vi"):
        return text

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


def _project(library_dir: Path, bodies: list[str]) -> Path:
    meta = NovelMeta(url="https://x/t", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"u{i}") for i in range(len(bodies))]
    project = NovelProject.create(library_dir, meta, refs)
    for i, body in enumerate(bodies):
        project.save_content(i, "原文")
        project.save_translation(i, f"Chương {i + 1}", body, "vi", "Google")
    write_address(project.path, AddressTable(entries=(ROW,)))
    path = project.path
    project.close()
    return path


def _use(monkeypatch, engine):
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)


class TestFixWorker:
    def test_fixes_saves_and_can_be_undone(self, qapp, library_dir, monkeypatch):
        path = _project(library_dir, [DRIFTED, "Trời mưa.", FIXED])
        _use(monkeypatch, _Llm(FIXED))
        worker = AddressFixWorker(path, "fake", [0, 1, 2])
        results = []
        worker.finished_ok.connect(lambda *r: results.append(r))
        worker.run()
        assert results == [(1, 2, 0)]  # ch.2 has no pair, ch.3 came back unchanged
        project = NovelProject.open(path)
        try:
            assert project.chapter(0).translated == FIXED
            assert project.chapter(0).is_address_fixed
            # its own undo copy: not mistaken for a style rewrite, so later rewrite runs
            # still pick it up
            assert not project.chapter(0).is_rewritten
            assert 0 in {c.index for c in project.pending_rewrite("vi")}
            assert not project.chapter(2).is_address_fixed  # unchanged → not written
            assert project.restore_address_fix(0) == 1
            assert project.chapter(0).translated == DRIFTED
        finally:
            project.close()

    def test_a_failed_fix_keeps_the_text_and_does_not_queue_a_retranslation(
        self, qapp, library_dir, monkeypatch
    ):
        path = _project(library_dir, [DRIFTED])
        _use(monkeypatch, _Llm("Một câu hoàn toàn khác.\n\nHai.\n\nBa."))
        errors = []
        worker = AddressFixWorker(path, "fake", [0])
        worker.chapter_error.connect(lambda i, m: errors.append(i))
        worker.run()
        project = NovelProject.open(path)
        try:
            assert project.chapter(0).translated == DRIFTED
            assert errors == [0]
            assert project.pending_translation("vi") == []
        finally:
            project.close()


    def test_a_reply_that_only_drops_trailing_spaces_is_not_a_fix(
        self, qapp, library_dir, monkeypatch
    ):
        spaced = FIXED.replace("rồi, đừng đi một mình.”", "rồi, đừng đi một mình.”  ")
        path = _project(library_dir, [spaced])
        _use(monkeypatch, _Llm(FIXED))
        results = []
        worker = AddressFixWorker(path, "fake", [0])
        worker.finished_ok.connect(lambda *r: results.append(r))
        worker.run()
        assert results == [(0, 1, 0)]
        project = NovelProject.open(path)
        try:
            assert not project.chapter(0).is_address_fixed
        finally:
            project.close()

    def test_undoing_a_fix_keeps_an_earlier_style_rewrite(
        self, qapp, library_dir, monkeypatch
    ):
        path = _project(library_dir, [DRIFTED])
        project = NovelProject.open(path)
        try:
            project.save_rewrite(0, "Chương 1", DRIFTED.replace("cúi đầu", "cúi gằm"))
        finally:
            project.close()
        restyled = DRIFTED.replace("cúi đầu", "cúi gằm")
        _use(monkeypatch, _Llm(restyled.replace("Tao bảo mày", "Tớ bảo cậu")))
        AddressFixWorker(path, "fake", [0]).run()
        project = NovelProject.open(path)
        try:
            assert project.restore_address_fix(0) == 1
            chapter = project.chapter(0)
            assert chapter.translated == restyled  # the rewrite survives
            assert chapter.is_rewritten and not chapter.is_address_fixed
        finally:
            project.close()

    def test_an_unchanged_drift_chapter_leaves_the_failure_list(
        self, qapp, library_dir, monkeypatch
    ):
        path = _project(library_dir, [FIXED])
        project = NovelProject.open(path)
        try:
            project.save_qc_verdict(0, "fail", QC_ADDRESS_DRIFT, "tao/mày", "")
        finally:
            project.close()
        _use(monkeypatch, _Llm(FIXED))  # nothing left to change
        AddressFixWorker(path, "fake", [0]).run()
        project = NovelProject.open(path)
        try:
            assert project.chapter(0).qc_code == ""
            assert project.qc_failures() == []
        finally:
            project.close()


class TestScanWorker:
    def test_emits_proposals_and_conflicts(self, qapp, library_dir, monkeypatch):
        path = _project(library_dir, [DRIFTED, FIXED, FIXED])
        replies = iter([
            "Lâm Phong | Tô Tuyết | tao | mày | 1",
            "Lâm Phong | Tô Tuyết | tớ | cậu | 1",
            "Lâm Phong | Tô Tuyết | tớ | cậu | 1",
        ])
        _use(monkeypatch, _Llm(lambda _p: next(replies)))
        got = []
        worker = AddressScanWorker(path, "fake", sample_size=10)
        worker.scanned.connect(lambda p, c: got.append((p, c)))
        worker.run()
        ((proposed, conflicts),) = got
        assert (proposed[0].self_term, proposed[0].other_term) == ("tớ", "cậu")
        assert conflicts[0].minority_chapters == [1]

    def test_a_stopped_scan_keeps_what_it_found_so_far(self, qapp, library_dir, monkeypatch):
        path = _project(library_dir, [DRIFTED, FIXED, FIXED])
        worker = AddressScanWorker(path, "fake", sample_size=10)

        def reply(_prompt):
            worker.cancel()  # Dừng pressed while the first chapter is being read
            return "Lâm Phong | Tô Tuyết | tớ | cậu | 1"

        _use(monkeypatch, _Llm(reply))
        stopped, scanned = [], []
        worker.stopped.connect(lambda *a: stopped.append(a))
        worker.scanned.connect(lambda *a: scanned.append(a))
        worker.run()
        ((proposed, conflicts, done, total),) = stopped
        assert (done, total) == (1, 3) and scanned == [] and conflicts == []
        assert (proposed[0].self_term, proposed[0].other_term) == ("tớ", "cậu")

    def test_google_is_refused(self, qapp, library_dir, monkeypatch):
        path = _project(library_dir, [DRIFTED])
        engine = _Llm("")
        engine.supports_completion = False
        _use(monkeypatch, engine)
        failed = []
        worker = AddressScanWorker(path, "google")
        worker.failed.connect(failed.append)
        worker.run()
        assert failed

    def test_prefer_source_scans_original_text_even_when_translated(
        self, qapp, library_dir, monkeypatch
    ):
        from noveltrans.name_glossary import NameEntry, write_names

        path = _project(library_dir, [DRIFTED])
        project = NovelProject.open(path)
        try:
            project.save_content(0, "林风看着苏雪说道：“你好。”")
            write_names(path, [NameEntry(source="林风", reading=A), NameEntry(source="苏雪", reading=B)])
        finally:
            project.close()
        engine = _Llm("Lâm Phong | Tô Tuyết | tớ | cậu | 1")
        _use(monkeypatch, engine)
        got = []
        worker = AddressScanWorker(path, "fake", sample_size=5, prefer_source=True)
        worker.scanned.connect(lambda p, c: got.append((p, c)))
        worker.run()
        assert len(engine.prompts) == 1
        assert "Đoạn truyện tiếng Trung" in engine.prompts[0]
        assert "林风" not in engine.prompts[0] and A in engine.prompts[0]
        assert (got[0][0][0].self_term, got[0][0][0].other_term) == ("tớ", "cậu")


# --------------------------------------------------------------------------- dialog


class TestDialogPhase2:
    @pytest.fixture
    def dialog(self, qapp, library_dir):
        from noveltrans.gui.address_table_dialog import AddressTableDialog

        path = _project(library_dir, [DRIFTED, FIXED, "Không ai nói gì."])
        project = NovelProject.open(path)
        yield AddressTableDialog(project)
        project.close()

    def test_scan_adds_only_new_pairs_and_lists_conflicts(self, dialog):
        from noveltrans.translators.address import Conflict

        dialog.apply_scan(
            [AddressEntry(A, B, "tao", "mày"), AddressEntry(B, A, "tớ", "cậu")],
            [Conflict(A, B, [("tớ", "cậu", [2]), ("tao", "mày", [1])])],
        )
        rows = dialog._rows()
        assert rows[0] == ROW  # the user's row is untouched
        assert len(rows) == 2  # the A→B proposal lost to the user's own row
        assert (rows[1].speaker, rows[1].self_term, rows[1].other_term) == (B, "tớ", "cậu")
        assert dialog.conflict_list.count() == 1
        assert dialog.affected_chapters() == [0]  # chapter 1 used the minority form

    @pytest.mark.parametrize("engine, offered", [("google", False), ("cli", True)])
    def test_retranslate_is_offered_only_with_an_engine_that_reads_the_table(
        self, qapp, library_dir, monkeypatch, engine, offered
    ):
        from PySide6.QtWidgets import QMessageBox

        from noveltrans.gui.address_table_dialog import AddressTableDialog

        path = _project(library_dir, [DRIFTED])
        project = NovelProject.open(path)
        try:
            dialog = AddressTableDialog(project, engine=engine)
            seen = []
            monkeypatch.setattr(
                QMessageBox, "exec", lambda box: seen.extend(b.text() for b in box.buttons())
            )
            dialog._offer_repair([0])
            assert ("Dịch lại" in seen) is offered
        finally:
            project.close()

    def test_a_stopped_scan_shows_its_partial_findings(self, dialog):
        dialog._on_scan_stopped([AddressEntry(B, A, "tớ", "cậu")], [], 2, 5)
        assert len(dialog._rows()) == 2
        assert dialog.scan_status.text().startswith("Đã dừng sau 2/5 chương.")
        dialog._on_scan_stopped([], [], 0, 5)
        assert "chưa dò xong chương nào" in dialog.scan_status.text()

    def test_a_changed_row_marks_the_chapters_it_applies_to(self, dialog, monkeypatch):
        from noveltrans.gui.address_table_dialog import _COL_OTHER

        offered = []
        monkeypatch.setattr(dialog, "_offer_repair", offered.append)
        dialog.table.item(0, _COL_OTHER).setText("bạn")
        dialog._save()
        assert offered == [[0, 1]]  # both chapters with the pair; not the one without

    def test_saving_with_nothing_changed_offers_nothing(self, dialog, monkeypatch):
        offered = []
        monkeypatch.setattr(dialog, "_offer_repair", offered.append)
        dialog._save()
        assert offered == []

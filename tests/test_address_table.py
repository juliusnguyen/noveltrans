"""Feature 113 — the address table (bảng xưng hô) and how it reaches the engines.

Load-bearing tests, named so a reviewer meets them first:

* `test_no_table_means_the_exact_same_prompt` — a novel without a table must be translated
  with the prompt it always had, on every LLM engine;
* `test_the_block_reaches_every_body_chunk_but_not_the_title`;
* `test_google_never_receives_the_context`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.address_table import (
    MAX_PROMPT_ROWS,
    NARRATOR,
    AddressEntry,
    AddressTable,
    address_path,
    prompt_for_chapter,
    read_address,
    render_prompt_block,
    reverse_of,
    rows_for_chapter,
    write_address,
)
from noveltrans.gui.workers import QcEngineSpec, QcSettings, TranslateWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.name_glossary import NameEntry, write_names
from noveltrans.storage import NovelProject
from noveltrans.translators.base import Translator

A, B, C = "Lâm Phong", "Tô Tuyết", "Trương Tam"


def _row(speaker=A, listener=B, self_term="tớ", other_term="cậu", **kw) -> AddressEntry:
    return AddressEntry(speaker, listener, self_term, other_term, **kw)


# --------------------------------------------------------------------------- storage


class TestStorage:
    def test_round_trip(self, tmp_path):
        table = AddressTable(
            general_note="Truyện học đường",
            entries=(_row(note="bạn học"), _row(NARRATOR, A, "", "hắn"), _row(from_chapter=50)),
        )
        write_address(tmp_path, table)
        assert read_address(tmp_path) == table
        assert not address_path(tmp_path).with_suffix(".json.tmp").exists()

    @pytest.mark.parametrize("content", [None, "", "{not json", "[]", '{"entries": 5}'])
    def test_a_missing_or_broken_file_is_an_empty_table(self, tmp_path, content):
        if content is not None:
            address_path(tmp_path).write_text(content, encoding="utf-8")
        table = read_address(tmp_path)
        assert table.entries == () and table.empty

    def test_garbage_rows_are_skipped_not_fatal(self, tmp_path):
        address_path(tmp_path).write_text(
            '{"entries": [5, {"speaker": "", "listener": "x"}, '
            '{"speaker": "A", "listener": "B", "from_chapter": "abc", "self_term": "tôi"}]}',
            encoding="utf-8",
        )
        (entry,) = read_address(tmp_path).entries
        assert (entry.speaker, entry.listener, entry.from_chapter) == ("A", "B", 1)

    @pytest.mark.parametrize(
        "raw, enabled",
        [('"false"', False), ('"0"', False), ('"no"', False), ("false", False), ("0", False),
         ('"true"', True), ("true", True), ("1", True), ('""', True), ("null", True)],
    )
    def test_a_hand_edited_enabled_flag_means_what_it_says(self, tmp_path, raw, enabled):
        address_path(tmp_path).write_text(
            '{"entries": [{"speaker": "A", "listener": "B", "self_term": "tôi", '
            f'"enabled": {raw}}}]}}',
            encoding="utf-8",
        )
        (entry,) = read_address(tmp_path).entries
        assert entry.enabled is enabled


# --------------------------------------------------------------------------- matching


def test_a_pass_over_an_existing_translation_keeps_pairs_outside_the_table():
    rows = [AddressEntry("A", "B", "tớ", "cậu")]
    assert "chọn xưng hô theo ngữ cảnh" in render_prompt_block(rows)
    block = render_prompt_block(rows, for_translation=False)
    assert "chọn xưng hô theo ngữ cảnh" not in block
    assert "GIỮ NGUYÊN xưng hô như bản hiện tại" in block


def test_names_match_decomposed_text():
    import unicodedata

    table = AddressTable(entries=(AddressEntry("Tô Tuyết", "Lâm Phong", "tớ", "cậu"),))
    text = unicodedata.normalize("NFD", "Tô Tuyết nhìn Lâm Phong.")
    assert rows_for_chapter(table, 1, text)


class TestRowsForChapter:
    TEXT = f"{A}看着{B}说道。{C}在旁边。"

    def test_only_pairs_present_in_the_chapter(self):
        table = AddressTable(entries=(_row(), _row(A, "Người Vắng Mặt")))
        assert rows_for_chapter(table, 1, self.TEXT) == [_row()]

    def test_from_chapter_picks_the_latest_row_not_after_this_chapter(self):
        early = _row()
        mid = _row(A, B, "anh", "em", from_chapter=50)
        late = _row(A, B, "ta", "ngươi", from_chapter=120)
        table = AddressTable(entries=(early, mid, late))
        assert rows_for_chapter(table, 49, self.TEXT) == [early]
        assert rows_for_chapter(table, 50, self.TEXT) == [mid]
        assert rows_for_chapter(table, 200, self.TEXT) == [late]

    def test_narration_rows_need_only_the_character(self):
        narration = _row(NARRATOR, C, "", "gã")
        table = AddressTable(entries=(narration, _row(NARRATOR, "Vắng", "", "y")))
        assert rows_for_chapter(table, 1, self.TEXT) == [narration]

    def test_disabled_and_empty_rows_are_ignored(self):
        table = AddressTable(entries=(_row(enabled=False), _row(A, C, "", "")))
        assert rows_for_chapter(table, 1, self.TEXT) == []

    def test_capped(self):
        names = [f"Nhân Vật {i}" for i in range(MAX_PROMPT_ROWS + 10)]
        table = AddressTable(entries=tuple(_row(A, n) for n in names))
        assert len(rows_for_chapter(table, 1, A + "".join(names))) == MAX_PROMPT_ROWS


class TestPrompt:
    def test_nothing_to_say_is_empty(self):
        assert render_prompt_block([], "") == ""
        assert prompt_for_chapter(AddressTable(), 1, "x") == ""

    def test_block_names_both_terms_and_the_note(self):
        block = render_prompt_block([_row(note="bạn thân"), _row(NARRATOR, A, "", "hắn")], "Hiện đại")
        assert "Lâm Phong nói với Tô Tuyết: xưng 'tớ', gọi 'cậu' (bạn thân)" in block
        assert "Lời kể gọi Lâm Phong là 'hắn'" in block
        assert "Ghi chú chung: Hiện đại" in block

    def test_general_note_alone_still_reaches_the_engine(self):
        assert "Hiện đại" in prompt_for_chapter(AddressTable(general_note="Hiện đại"), 1, "x")

    def test_reverse_swaps_the_terms(self):
        reverse = reverse_of(_row(self_term="anh", other_term="em", from_chapter=7))
        assert (reverse.speaker, reverse.listener) == (B, A)
        assert (reverse.self_term, reverse.other_term, reverse.from_chapter) == ("em", "anh", 7)


# --------------------------------------------------------------------------- engines


class TestEnginePrompts:
    @pytest.fixture
    def captured(self, monkeypatch):
        """What each LLM engine would send, without any network or subprocess."""
        from noveltrans.translators.claude import ClaudeTranslator
        from noveltrans.translators.cli_agent import CliAgentTranslator
        from noveltrans.translators.lmstudio import LmStudioTranslator

        sent: list[tuple[str, str]] = []

        def record(self, prompt, *, system="", **_):
            sent.append((system, prompt))
            return "Bản dịch"

        monkeypatch.setattr(ClaudeTranslator, "complete", record)
        monkeypatch.setattr(LmStudioTranslator, "complete", record)
        monkeypatch.setattr(CliAgentTranslator, "complete", lambda self, p: record(self, p))
        engines = [
            ClaudeTranslator(api_key="k"),
            LmStudioTranslator(model="m"),
            CliAgentTranslator(command="agy -p"),
        ]
        return engines, sent

    def test_no_table_means_the_exact_same_prompt(self, captured):
        engines, sent = captured
        for engine in engines:
            sent.clear()
            engine.translate("你好", context="")
            without = list(sent)
            sent.clear()
            engine.translate("你好")
            assert sent == without
            assert "BẢNG XƯNG HÔ" not in "".join(sent[0])

    def test_the_block_reaches_every_body_chunk_but_not_the_title(self, captured):
        engines, sent = captured
        block = render_prompt_block([_row()])
        for engine in engines:
            engine.max_chunk_chars = 10
            sent.clear()
            engine.translate_chapter("标题文字", "第一段内容很长。\n\n第二段内容很长。", context=block)
            title_call, *body_calls = sent
            assert block not in "".join(title_call)
            assert len(body_calls) == 2
            assert all(block in "".join(call) for call in body_calls)

    def test_google_never_receives_the_context(self):
        class NoContext(Translator):
            name = "google"

            def translate(self, text, source="zh", target="vi"):  # no **kwargs on purpose
                return "Xin chào"

        assert NoContext().translate_chapter("", "你好", context="BẢNG") == ("", "Xin chào")


# --------------------------------------------------------------------------- worker


class _Recorder(Translator):
    name = "fake"
    display_name = "Fake"
    max_chunk_chars = 100_000
    supports_completion = True
    supports_retry_hint = True
    supports_context = True

    def __init__(self):
        self.contexts: list[tuple[str, str]] = []  # (text, context)

    def translate(self, text, source="zh", target="vi", *, retry_hint="", context=""):
        self.contexts.append((text, context))
        return "Lâm Phong nhìn Tô Tuyết rồi nói, giọng nhẹ nhàng như gió thoảng qua. " * 6

    def complete(self, prompt):
        return "OK"


def _project(library_dir: Path) -> Path:
    meta = NovelMeta(url="https://x/t", site="x", title="Truyện", source_lang="zh")
    refs = [ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://x/{i}") for i in range(2)]
    project = NovelProject.create(library_dir, meta, refs)
    project.save_content(0, "林风看着苏雪说道：你好。" * 5)
    project.save_content(1, "风吹过山谷，什么人也没有。" * 5)
    write_names(project.path, [
        NameEntry(source="林风", reading=A, auto=A, edited=True),
        NameEntry(source="苏雪", reading=B, auto=B, edited=True),
    ])
    path = project.path
    project.close()
    return path


@pytest.mark.parametrize("qc", [False, True])
def test_worker_sends_each_chapter_only_its_own_rows(qapp, library_dir, monkeypatch, qc):
    path = _project(library_dir)
    write_address(path, AddressTable(entries=(_row(),)))
    engine = _Recorder()
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)
    settings = QcSettings(chain=[QcEngineSpec("fake", attempts=1)]) if qc else None
    TranslateWorker(path, "fake", "vi", qc=settings).run()

    by_text = {text: context for text, context in engine.contexts}
    ch0 = next(c for t, c in by_text.items() if t.startswith(A))
    ch1 = next(c for t, c in by_text.items() if t.startswith("风吹过"))
    assert "xưng 'tớ', gọi 'cậu'" in ch0  # names substituted first, so the row matched
    assert ch1 == ""  # nobody from the table is in chapter 2
    assert all(c == "" for t, c in by_text.items() if t.startswith("第"))  # titles: never


def test_worker_without_a_table_passes_no_context(qapp, library_dir, monkeypatch):
    path = _project(library_dir)
    engine = _Recorder()
    monkeypatch.setattr("noveltrans.translators.get_translator", lambda *a, **k: engine)
    TranslateWorker(path, "fake", "vi").run()
    assert engine.contexts and all(c == "" for _t, c in engine.contexts)


# --------------------------------------------------------------------------- dialog


class TestDialog:
    @pytest.fixture
    def dialog(self, qapp, library_dir):
        from noveltrans.gui.address_table_dialog import AddressTableDialog

        path = _project(library_dir)
        project = NovelProject.open(path)
        write_address(path, AddressTable(entries=(_row(self_term="anh", other_term="em"),)))
        yield AddressTableDialog(project)
        project.close()

    def test_loads_and_saves(self, dialog):
        from noveltrans.gui.address_table_dialog import _COL_NOTE

        dialog.table.item(0, _COL_NOTE).setText("người yêu")
        dialog.general_note.setPlainText("Hiện đại")
        dialog._save()
        table = read_address(dialog.project.path)
        assert table.general_note == "Hiện đại"
        assert table.entries[0].note == "người yêu"

    def test_reverse_row_is_prefilled(self, dialog):
        dialog.table.setCurrentCell(0, 0)
        dialog._add_reverse()
        assert dialog._rows()[1] == AddressEntry(B, A, "em", "anh")

    def test_unknown_names_are_flagged(self, dialog):
        from noveltrans.gui.address_table_dialog import _COL_LISTENER, _COL_SPEAKER

        assert dialog.table.item(0, _COL_SPEAKER).toolTip() == ""
        dialog.table.item(0, _COL_LISTENER).setText("Ai Đó")
        assert "⚠️" in dialog.table.item(0, _COL_LISTENER).toolTip()

    def test_google_warning(self, qapp, library_dir):
        from noveltrans.gui.address_table_dialog import AddressTableDialog

        project = NovelProject.open(_project(library_dir))
        try:
            assert AddressTableDialog(project, engine="claude").engine_warning.isHidden()
            assert not AddressTableDialog(project, engine="google").engine_warning.isHidden()
        finally:
            project.close()

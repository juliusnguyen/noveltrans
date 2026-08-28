"""Feature 072 — the name glossary now runs for every engine, not only Google.

The reported bug: a character's name came out spelled two different ways in different
chapters. The prompts do ask each engine to translate names consistently, but that
instruction only has scope over one request — and a novel is hundreds of them. Substituting
the name into the source before the engine sees it is what actually makes it consistent, and
until this feature that only happened for Google.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.gui.workers import TranslateWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.name_glossary import NameEntry, names_path, read_names, write_names
from noveltrans.storage import NovelProject

NAME = "夏檸七"
READING = "Hạ Ninh Thất"


class _RecordingTranslator:
    """Echoes its input back and remembers exactly what it was handed."""

    def __init__(self, seen: list[str]):
        self._seen = seen

    def translate_chapter(self, title, content, source="zh", target="vi"):
        self._seen.append(content)
        return title, content


def _zh_project(library_dir: Path, bodies: list[str]) -> Path:
    meta = NovelMeta(
        url="https://fake.test/book/1", site="fake", title="T", source_lang="zh"
    )
    refs = [
        ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://fake.test/{i}")
        for i in range(len(bodies))
    ]
    project = NovelProject.create(library_dir, meta, refs)
    for i, body in enumerate(bodies):
        project.save_content(i, body)
    path = project.path
    project.close()
    return path


class _Seen(list):
    """The bodies handed to the engine. The novel's title/description go through the same
    call, so tests ask about the whole batch rather than a fixed index."""

    @property
    def text(self) -> str:
        return "\n".join(self)


@pytest.fixture
def seen(monkeypatch):
    """Capture the text each engine actually receives."""
    captured = _Seen()
    monkeypatch.setattr(
        "noveltrans.translators.get_translator",
        lambda *a, **k: _RecordingTranslator(captured),
    )
    return captured


def _run(path: Path, engine: str = "cli", target: str = "vi") -> None:
    TranslateWorker(path, engine_name=engine, target_lang=target).run()


class TestUngating:
    def test_an_llm_engine_now_gets_the_substituted_name(self, qapp, library_dir, seen):
        """Fails before feature 072: the glossary was gated to Google, so `cli` saw 夏檸七."""
        path = _zh_project(library_dir, [f"{NAME}走進院子。"])
        write_names(path, [NameEntry(source=NAME, reading=READING)])

        _run(path, engine="cli")

        assert seen, "the engine was never called"
        assert READING in seen.text
        assert NAME not in seen.text

    def test_google_still_substitutes(self, qapp, library_dir, seen):
        """No regression to the path that already worked."""
        path = _zh_project(library_dir, [f"{NAME}走進院子。"])
        write_names(path, [NameEntry(source=NAME, reading=READING)])

        _run(path, engine="google")

        assert READING in seen.text

    def test_an_english_target_is_left_alone(self, qapp, library_dir, seen):
        """Hán-Việt readings are Vietnamese; injecting them into a zh→en run is nonsense."""
        path = _zh_project(library_dir, [f"{NAME}走進院子。"])
        write_names(path, [NameEntry(source=NAME, reading=READING)])

        _run(path, engine="cli", target="en")

        assert NAME in seen.text
        assert READING not in seen.text

    def test_a_disabled_entry_is_not_substituted(self, qapp, library_dir, seen):
        path = _zh_project(library_dir, [f"{NAME}走進院子。"])
        write_names(path, [NameEntry(source=NAME, reading=READING, enabled=False)])

        _run(path, engine="cli")

        assert NAME in seen.text


class TestCrossChapterConsistency:
    def test_the_same_name_is_spelled_the_same_in_every_chapter(
        self, qapp, library_dir, seen
    ):
        """**The regression for this report.** Two chapters, one name, one spelling."""
        path = _zh_project(
            library_dir, [f"{NAME}走進院子。", f"風吹過，{NAME}抬起頭。"]
        )
        write_names(path, [NameEntry(source=NAME, reading=READING)])

        _run(path, engine="cli")

        project = NovelProject.open(path)
        try:
            spellings = {
                c.translated.count(READING) > 0 for c in project.chapters() if c.translated
            }
        finally:
            project.close()
        assert spellings == {True}, "a chapter came out with a different spelling"
        assert all(NAME not in body for body in seen)


class TestFirstRun:
    def test_a_novel_with_no_list_gets_one_built_and_saved(
        self, qapp, library_dir, seen, monkeypatch
    ):
        """The dialog is the correction path, not the activation path: a user who never
        opens it must still get consistent names."""
        monkeypatch.setattr(
            "noveltrans.name_glossary.build_from_project",
            lambda project: [NameEntry(source=NAME, reading=READING, auto=READING)],
        )
        path = _zh_project(library_dir, [f"{NAME}走進院子。"])
        assert not names_path(path).exists()

        _run(path, engine="cli")

        assert names_path(path).exists(), "the detected list was not persisted"
        assert [e.source for e in read_names(path)] == [NAME]
        assert READING in seen.text

    def test_an_existing_list_is_not_rebuilt(self, qapp, library_dir, seen, monkeypatch):
        """Reading the file is the point — rebuilding would be slow and non-deterministic."""
        called = {"n": 0}
        monkeypatch.setattr(
            "noveltrans.name_glossary.build_from_project",
            lambda project: called.__setitem__("n", called["n"] + 1) or [],
        )
        path = _zh_project(library_dir, [f"{NAME}走進院子。"])
        write_names(path, [NameEntry(source=NAME, reading=READING)])

        _run(path, engine="cli")

        assert called["n"] == 0

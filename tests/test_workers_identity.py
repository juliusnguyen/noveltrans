"""TranslateWorker identity/passthrough path for same-language sources."""

from pathlib import Path

from noveltrans.errors import NovelTransError
from noveltrans.gui.workers import TranslateWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject


def _vi_project(library_dir: Path) -> NovelProject:
    meta = NovelMeta(
        url="https://medoctruyen.vn/truyen-thu",
        site="medoctruyen",
        title="Truyện Thử",
        description="Mô tả gốc.",
        source_lang="vi",
    )
    refs = [
        ChapterRef(index=i, title=f"Chương {i + 1}", url=f"https://x/chuong-{i + 1}")
        for i in range(3)
    ]
    return NovelProject.create(library_dir, meta, refs)


def test_identity_translation_copies_original(qapp, library_dir):
    project = _vi_project(library_dir)
    project.save_content(0, "Nội dung chương một.")
    project.save_content(1, "Nội dung chương hai.")
    path = project.path
    project.close()

    worker = TranslateWorker(path, engine_name="google", target_lang="vi")
    worker.run()  # synchronous — no event loop needed, workers never touch widgets

    reopened = NovelProject.open(path)
    try:
        c0 = reopened.chapter(0)
        c1 = reopened.chapter(1)
        # original text copied verbatim into `translated` (no engine ran)
        assert c0.translated == "Nội dung chương một."
        assert c0.translated_title == "Chương 1"
        assert c0.target_lang == "vi"
        assert c0.translator == "(nguyên bản)"
        assert c1.is_translated
        # untouched chapter (no content) stays pending
        assert not reopened.chapter(2).is_translated
        # meta translation populated for export front matter
        assert reopened.meta.translated_lang == "vi"
    finally:
        reopened.close()


def test_identity_skipped_when_target_differs(qapp, library_dir, monkeypatch):
    """vi source with an en target must NOT take the passthrough branch — it runs
    a real vi->en translation instead."""
    project = _vi_project(library_dir)
    project.save_content(0, "Nội dung.")
    path = project.path
    project.close()

    took_identity = {"yes": False}
    monkeypatch.setattr(
        TranslateWorker,
        "_run_identity",
        lambda self, proj, pending: took_identity.__setitem__("yes", True),
    )
    # Stop the real engine path early; we only assert which branch was chosen.
    monkeypatch.setattr(
        "noveltrans.translators.get_translator",
        lambda *a, **k: (_ for _ in ()).throw(NovelTransError("no engine")),
    )

    worker = TranslateWorker(path, engine_name="google", target_lang="en")
    worker.run()

    assert took_identity["yes"] is False


def test_a_self_written_vi_novel_becomes_voiceable_either_way(qapp, library_dir):
    """The Tab 4 "works with either radio button" claim, for a hand-written novel.

    A local Vietnamese novel gets its text via edit_content, never save_content. Running
    "Dịch tất cả" sang Tiếng Việt takes the identity path, which both fills `translated`
    AND `meta.translated_title` — the value the video slug is keyed to.
    """
    from noveltrans.storage import Library

    project = Library(library_dir).create_local_project(
        NovelMeta(url="", site="", title="Truyện Của Tôi", source_lang="vi")
    )
    project.add_chapters(["Chương 1", "Chương 2"])
    project.edit_content(0, "Nội dung tôi tự viết.")
    path = project.path
    # Bản gốc works before any translation exists at all.
    assert [c.index for c in project.pending_audio("v1", use_translation=False)] == [0]
    project.close()

    TranslateWorker(path, engine_name="google", target_lang="vi").run()

    reopened = NovelProject.open(path)
    try:
        assert reopened.chapter(0).translated == "Nội dung tôi tự viết."
        assert reopened.chapter(0).translator == "(nguyên bản)"
        assert reopened.meta.translated_title == "Truyện Của Tôi"
        # …and now Bản dịch works too
        assert [c.index for c in reopened.pending_audio("v1", use_translation=True)] == [0]
    finally:
        reopened.close()

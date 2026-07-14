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

"""ScanWorker must find an existing project by the adapter's canonical URL too.

`Library.find_by_url` is exact string equality, and some adapters canonicalise: bookqq
folds `/book-read/<id>/<n>` to the detail page, giatocvuongtai normalises its slug. So a
project stores the canonical URL in meta.json while the user may well re-scan by pasting
a *chapter* address — the normal case, not an exotic one.

Looking up only the pasted URL missed that project, fell through to `create_project`, and
`NovelProject.create` overwrites meta.json wholesale — discarding `translated_title`,
`tags`, `thumbnail_prompt` and `video_settings`, i.e. everything `refresh_meta` exists to
protect. Chapter content survived, so the loss was quiet.
"""

from __future__ import annotations

import pytest

from noveltrans.gui.workers import ScanWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers.base import SiteAdapter
from noveltrans.storage import NovelProject

CANONICAL = "https://fake.test/book-detail/58625737"
CHAPTER = "https://fake.test/book-read/58625737/12"


class _CanonicalisingAdapter(SiteAdapter):
    """Returns the same canonical `meta.url` whichever form it is handed."""

    name = "fake"
    display_name = "Fake"
    url_patterns = [r"fake\.test"]

    def fetch_metadata(self, url: str) -> NovelMeta:
        return NovelMeta(url=CANONICAL, site=self.name, title="书名", source_lang="zh")

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        return [
            ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://fake.test/book-read/1/{i + 1}")
            for i in range(3)
        ]

    def fetch_chapter(self, ref: ChapterRef) -> str:
        return "nội dung"


@pytest.fixture
def canonicalising(monkeypatch):
    adapter = _CanonicalisingAdapter(None)
    monkeypatch.setattr("noveltrans.gui.workers.adapter_for_url", lambda *_a, **_k: adapter)
    return adapter


def _scan(url: str, library_dir) -> str:
    scanned: list[tuple] = []
    worker = ScanWorker(url, library_dir, delay=0)
    worker.scanned.connect(lambda *a: scanned.append(a))
    worker.run()
    assert scanned, "scan did not succeed"
    return scanned[0][0]


class TestScanByCanonicalUrl:
    def test_rescanning_by_chapter_url_reuses_the_existing_project(
        self, qapp, library_dir, canonicalising
    ):
        first = _scan(CANONICAL, library_dir)
        second = _scan(CHAPTER, library_dir)
        assert second == first

    def test_it_does_not_clobber_metadata_the_user_earned(
        self, qapp, library_dir, canonicalising
    ):
        """The whole point: `create_project` would rewrite meta.json from scratch."""
        path = _scan(CANONICAL, library_dir)
        project = NovelProject.open(path)
        project.save_meta_translation("Tên Đã Dịch", "Mô tả đã dịch", "vi", "Tác giả")
        project.save_tags("truyện hay, tiên hiệp")
        project.close()

        _scan(CHAPTER, library_dir)

        project = NovelProject.open(path)
        try:
            assert project.meta.translated_title == "Tên Đã Dịch"
            assert project.meta.tags == "truyện hay, tiên hiệp"
        finally:
            project.close()

    def test_downloaded_chapters_survive_the_rescan(self, qapp, library_dir, canonicalising):
        path = _scan(CANONICAL, library_dir)
        project = NovelProject.open(path)
        project.save_content(0, "原文一")
        project.close()

        _scan(CHAPTER, library_dir)

        project = NovelProject.open(path)
        try:
            assert project.chapter(0).content == "原文一"
        finally:
            project.close()

    def test_a_genuinely_new_novel_still_creates_a_project(self, qapp, library_dir):
        """The lookup must not adopt an unrelated project — find_by_url returns None for
        a URL nothing matches, and `or` then falls through to create as before."""

        class _Other(_CanonicalisingAdapter):
            def fetch_metadata(self, url: str) -> NovelMeta:
                return NovelMeta(url=url, site=self.name, title="别的书", source_lang="zh")

        import noveltrans.gui.workers as w

        other = _Other(None)
        original = w.adapter_for_url
        w.adapter_for_url = lambda *_a, **_k: other
        try:
            first = _scan("https://fake.test/book-detail/1", library_dir)
            second = _scan("https://fake.test/book-detail/2", library_dir)
        finally:
            w.adapter_for_url = original
        assert first != second


class TestScanSto9ByEveryPasteForm:
    """sto9 folds three paste forms to one canonical URL (see scrapers/sto9.py).

    Recorded here rather than in the adapter's own module because the property being
    pinned belongs to `ScanWorker`: the adapter canonicalises correctly on its own, but
    only the `find_by_url(pasted) or find_by_url(meta.url)` lookup turns that into "one
    project per novel" instead of one project per URL shape the user happened to copy.
    """

    BID = "13908"
    FORMS = (
        f"https://sto9.com/book/{BID}.html",
        f"https://sto9.com/book/{BID}/index.html",
        f"https://sto9.com/txt/{BID}/7671958.html",
    )

    @pytest.fixture
    def sto9_like(self, monkeypatch):
        from noveltrans.scrapers.sto9 import read_url

        class _Sto9Shaped(_CanonicalisingAdapter):
            def fetch_metadata(self, url: str) -> NovelMeta:
                return NovelMeta(url=read_url(url), site="sto9", title="劍影孤舟", source_lang="zh")

        adapter = _Sto9Shaped(None)
        monkeypatch.setattr("noveltrans.gui.workers.adapter_for_url", lambda *_a, **_k: adapter)
        return adapter

    def test_all_three_paste_forms_land_on_one_project(self, qapp, library_dir, sto9_like):
        paths = {_scan(url, library_dir) for url in self.FORMS}
        assert len(paths) == 1

    def test_rescanning_by_a_chapter_url_keeps_what_the_user_earned(
        self, qapp, library_dir, sto9_like
    ):
        path = _scan(self.FORMS[1], library_dir)  # the form the site itself links to
        project = NovelProject.open(path)
        project.save_meta_translation("Kiếm Ảnh Cô Chu", "Mô tả đã dịch", "vi", "Vô Danh Thị")
        project.close()

        _scan(self.FORMS[2], library_dir)

        project = NovelProject.open(path)
        try:
            assert project.meta.translated_title == "Kiếm Ảnh Cô Chu"
        finally:
            project.close()

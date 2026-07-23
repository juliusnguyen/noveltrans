"""Workers must release their adapter, on every path.

Most adapters hold nothing and inherit a no-op close(). 69shuba holds a browser for
the life of a scan/download, so a missed teardown is an orphan Chrome window the user
has to force-quit — and the paths that leak are the failure paths, which is what these
cover.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noveltrans.errors import ScrapeError
from noveltrans.gui.workers import DownloadWorker, ScanWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers.base import SiteAdapter
from noveltrans.storage import NovelProject

URL = "https://fake.test/book/1/"


class _FakeAdapter(SiteAdapter):
    """Records close() calls; optionally blows up mid-fetch."""

    name = "fake"
    display_name = "Fake"
    url_patterns = [r"fake\.test"]

    def __init__(self, client=None, *, fail: bool = False):
        super().__init__(client)
        self.closed = 0
        self.fail = fail

    def fetch_metadata(self, url: str) -> NovelMeta:
        self._status("đang mở trình duyệt…")
        if self.fail:
            raise ScrapeError("boom", url)
        return NovelMeta(url=url, site=self.name, title="Tựa đề")

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        return [ChapterRef(index=0, title="C1", url="https://fake.test/txt/1/1")]

    def fetch_chapter(self, ref: ChapterRef) -> str:
        self._status("đang mở trình duyệt…")
        if self.fail:
            raise ScrapeError("boom", ref.url)
        return "nội dung"

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def fake_adapter(monkeypatch):
    def install(**kw) -> _FakeAdapter:
        adapter = _FakeAdapter(**kw)
        monkeypatch.setattr(
            "noveltrans.gui.workers.adapter_for_url", lambda *_a, **_k: adapter
        )
        return adapter

    return install


def test_default_close_is_a_noop_for_adapters_that_hold_nothing():
    # The four existing adapters inherit this; it must never raise.
    from noveltrans.scrapers.xbanxia import XbanxiaAdapter
    from noveltrans.scrapers.base import HttpClient

    XbanxiaAdapter(HttpClient(delay_seconds=0)).close()


class TestScanWorker:
    def test_closes_the_adapter_on_success(self, qapp, library_dir, fake_adapter):
        adapter = fake_adapter()
        scanned: list[tuple] = []
        worker = ScanWorker(URL, library_dir, delay=0)
        worker.scanned.connect(lambda *a: scanned.append(a))
        worker.run()
        assert scanned  # it really did succeed
        assert adapter.closed == 1

    def test_closes_the_adapter_when_the_scan_fails(self, qapp, library_dir, fake_adapter):
        # The leak path that matters: an exception mid-scan must still tear the
        # browser down.
        adapter = fake_adapter(fail=True)
        failures: list[str] = []
        worker = ScanWorker(URL, library_dir, delay=0)
        worker.failed.connect(failures.append)
        worker.run()
        assert failures  # it really did fail
        assert adapter.closed == 1

    def test_does_not_crash_when_no_adapter_matches(self, qapp, library_dir, monkeypatch):
        # adapter is None → nothing to close, and no AttributeError.
        monkeypatch.setattr("noveltrans.gui.workers.adapter_for_url", lambda *_a, **_k: None)
        failures: list[str] = []
        worker = ScanWorker("https://unsupported.example/x", library_dir, delay=0)
        worker.failed.connect(failures.append)
        worker.run()
        assert failures and "Chưa hỗ trợ" in failures[0]


def _project(library_dir: Path, n: int = 1) -> Path:
    meta = NovelMeta(url=URL, site="fake", title="Tựa đề")
    refs = [
        ChapterRef(index=i, title=f"C{i + 1}", url=f"https://fake.test/txt/1/{i + 1}")
        for i in range(n)
    ]
    project = NovelProject.create(library_dir, meta, refs)
    path = project.path
    project.close()
    return path


class TestDownloadWorker:
    def test_closes_the_adapter_after_the_batch(self, qapp, library_dir, fake_adapter):
        adapter = fake_adapter()
        DownloadWorker(_project(library_dir), delay=0).run()
        assert adapter.closed == 1

    def test_closes_the_adapter_when_a_chapter_fails(self, qapp, library_dir, fake_adapter):
        adapter = fake_adapter(fail=True)
        DownloadWorker(_project(library_dir), delay=0).run()
        assert adapter.closed == 1

    def test_closes_the_adapter_when_cancelled(self, qapp, library_dir, fake_adapter):
        adapter = fake_adapter()
        worker = DownloadWorker(_project(library_dir), delay=0)
        worker.cancel()  # breaks out of the loop before any chapter
        worker.run()
        assert adapter.closed == 1

    def test_pre_translated_source_lands_content_as_translation(
        self, qapp, library_dir, fake_adapter
    ):
        # A source like webtruyendich serves a finished translation. The worker must
        # store it as `translated` (skipping our own translators) AND as `content`
        # (else pending_download re-queues it forever).
        adapter = fake_adapter()
        adapter.content_is_translated = True
        adapter.translated_lang = "vi"
        adapter.translator_label = "webtruyendich (Gemini Flash 3.5)"

        path = _project(library_dir)
        DownloadWorker(path, delay=0).run()

        reopened = NovelProject.open(path)
        try:
            c0 = reopened.chapter(0)
            assert c0.content == "nội dung"  # written so it isn't re-downloaded
            assert c0.translated == "nội dung"  # landed as the translation
            assert c0.translated_title == "C1"
            assert c0.target_lang == "vi"
            assert c0.translator == "webtruyendich (Gemini Flash 3.5)"
            assert c0.is_translated
            assert reopened.pending_download() == []  # not re-queued
        finally:
            reopened.close()

    def test_ordinary_source_only_sets_content(self, qapp, library_dir, fake_adapter):
        # The default path is unchanged: content set, translation left for later.
        fake_adapter()  # content_is_translated defaults to False
        path = _project(library_dir)
        DownloadWorker(path, delay=0).run()

        reopened = NovelProject.open(path)
        try:
            c0 = reopened.chapter(0)
            assert c0.content == "nội dung"
            assert c0.translated == ""
            assert not c0.is_translated
        finally:
            reopened.close()

    def test_download_from_a_start_index_only(self, qapp, library_dir, fake_adapter):
        fake_adapter()
        path = _project(library_dir, n=5)
        DownloadWorker(path, delay=0, start_index=2).run()  # "from chapter 3"

        reopened = NovelProject.open(path)
        try:
            got = [reopened.chapter(i).content for i in range(5)]
            assert got == ["", "", "nội dung", "nội dung", "nội dung"]
        finally:
            reopened.close()

    def test_download_a_range_skips_already_downloaded(self, qapp, library_dir, fake_adapter):
        fake_adapter()
        path = _project(library_dir, n=5)
        pre = NovelProject.open(path)
        pre.save_content(2, "cũ")  # already downloaded, inside the range
        pre.close()

        DownloadWorker(path, delay=0, start_index=1, end_index=3).run()

        reopened = NovelProject.open(path)
        try:
            got = [reopened.chapter(i).content for i in range(5)]
            # idx 1 & 3 fetched; idx 2 left untouched (not re-fetched); 0 & 4 out of range
            assert got == ["", "nội dung", "cũ", "nội dung", ""]
        finally:
            reopened.close()

    def test_force_redownloads_even_downloaded_chapters(self, qapp, library_dir, fake_adapter):
        fake_adapter()
        path = _project(library_dir, n=5)
        pre = NovelProject.open(path)
        pre.save_content(2, "cũ")  # already downloaded
        pre.close()

        # "Chỉ tải lại chương 3" → force a single-chapter refresh
        DownloadWorker(path, delay=0, start_index=2, end_index=2, force=True).run()

        reopened = NovelProject.open(path)
        try:
            assert reopened.chapter(2).content == "nội dung"  # overwritten
            assert reopened.chapter(1).content == ""  # neighbours untouched
            assert reopened.chapter(3).content == ""
        finally:
            reopened.close()


class TestStatusPlumbing:
    def test_adapter_status_is_a_noop_without_a_listener(self):
        # Adapters run outside the GUI too (scripts, tests) — _status must not care.
        _FakeAdapter().fetch_metadata(URL)  # on_status is None; must not raise

    def test_scan_worker_forwards_adapter_status(self, qapp, library_dir, fake_adapter):
        # A Chrome window appearing during a scan needs explaining; ScanWorker had no
        # progress signal at all before this.
        adapter = fake_adapter()
        messages: list[str] = []
        worker = ScanWorker(URL, library_dir, delay=0)
        worker.progress.connect(messages.append)
        worker.run()
        assert adapter.on_status is not None
        assert "đang mở trình duyệt…" in messages

    def test_download_worker_forwards_adapter_status_with_position(
        self, qapp, library_dir, fake_adapter
    ):
        fake_adapter()
        seen: list[tuple[int, int, str]] = []
        worker = DownloadWorker(_project(library_dir), delay=0)
        worker.progress.connect(lambda d, t, m: seen.append((d, t, m)))
        worker.run()
        status = [row for row in seen if row[2] == "đang mở trình duyệt…"]
        assert status, "adapter status never reached the progress signal"
        # Carries the real batch position, not a placeholder.
        assert status[0][0] == 0 and status[0][1] == 1

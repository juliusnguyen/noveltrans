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
        if self.fail:
            raise ScrapeError("boom", url)
        return NovelMeta(url=url, site=self.name, title="Tựa đề")

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        return [ChapterRef(index=0, title="C1", url="https://fake.test/txt/1/1")]

    def fetch_chapter(self, ref: ChapterRef) -> str:
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


def _project(library_dir: Path) -> Path:
    meta = NovelMeta(url=URL, site="fake", title="Tựa đề")
    refs = [ChapterRef(index=0, title="C1", url="https://fake.test/txt/1/1")]
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

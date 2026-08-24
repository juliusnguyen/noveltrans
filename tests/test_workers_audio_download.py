"""Feature 059 — AudioDownloadWorker, which fetches the site's own audio edition.

Driven through the REAL TieuthuyetmangAdapter over `responses`, not a stub, so the URL
arithmetic (release number -> /nghe/N -> media URL) is exercised rather than assumed.

The central invariant here is the one 059.07 established: a release is NOT a chapter.
Downloads land in `source_audio` and must never touch a chapter row.
"""

from pathlib import Path

import responses

from noveltrans.gui.workers import AudioDownloadWorker
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage import NovelProject

from conftest import load_fixture

NOVEL_URL = "https://tieuthuyetmang.com/truyen/truyen-thu-nghiem"
MEDIA = "https://img.tieuthuyetmang.com/audios/01TESTSTORY000000000000000/"
BODY = b"ID3" + b"\x00" * 2045


def fixture(name: str) -> str:
    return load_fixture("tieuthuyetmang", name)


def make_project(library_dir: Path, n: int = 6) -> Path:
    meta = NovelMeta(url=NOVEL_URL, site="tieuthuyetmang", title="Truyện Thử Nghiệm")
    refs = [
        ChapterRef(index=i, title=f"Chương {i + 1}", url=f"{NOVEL_URL}/doc/{i + 1}")
        for i in range(n)
    ]
    project = NovelProject.create(library_dir, meta, refs)
    path = project.path
    project.close()
    return path


def serve(numbers=(1, 2, 3, 4, 5, 6), page="audio_ready.html", media=".mp3"):
    """Landing page + one listen page per release + the media bytes."""
    responses.get(NOVEL_URL, body=fixture("landing.html"))
    for n in numbers:
        responses.get(f"{NOVEL_URL}/nghe/{n}", body=fixture(page))
    responses.get(MEDIA + f"01TESTAUDIO0000000000000A{media}", body=BODY)
    responses.get(MEDIA + f"01TESTAUDIO0000000000000B{media}", body=BODY)


def run(path: Path, **kw) -> tuple[list, list, list]:
    worker = AudioDownloadWorker(path, delay=0, **kw)
    # The 2s floor is a politeness rule for the live site, pinned separately below.
    worker.delay = 0
    ok, errors, failures = [], [], []
    worker.item_done.connect(ok.append)
    worker.item_error.connect(lambda n, m: errors.append((n, m)))
    worker.failed.connect(failures.append)
    worker.run()
    return ok, errors, failures


def releases(path: Path):
    project = NovelProject.open(path)
    try:
        return project.source_audio()
    finally:
        project.close()


class TestAudioDownloadWorker:
    @responses.activate
    def test_downloads_every_release(self, qapp, library_dir):
        path = make_project(library_dir)
        serve()
        ok, errors, failures = run(path)
        assert not failures and not errors
        assert len(ok) == 6

    @responses.activate
    def test_the_audio_lands_in_source_audio_not_on_a_chapter(self, qapp, library_dir):
        """The whole point of 059.07: a release covers a chapter RANGE and belongs to a
        different edition, so it must not appear as any chapter's narration."""
        path = make_project(library_dir)
        serve()
        run(path)
        project = NovelProject.open(path)
        try:
            assert all(not c.audio_path for c in project.chapters())
            stored = project.source_audio()
            assert [r.number for r in stored if r.has_audio] == [1, 2, 3, 4, 5, 6]
            assert (path / stored[0].path).exists()
        finally:
            project.close()

    @responses.activate
    def test_the_saved_file_keeps_the_servers_extension(self, qapp, library_dir):
        """This site publishes some volumes as AAC and some as MP3."""
        path = make_project(library_dir)
        serve(page="audio_aac.html", media=".aac")
        run(path)
        assert releases(path)[0].path.endswith(".aac")

    @responses.activate
    def test_the_release_list_is_recorded_even_before_anything_downloads(
        self, qapp, library_dir
    ):
        path = make_project(library_dir)
        responses.get(NOVEL_URL, body=fixture("landing.html"))
        for n in (1, 2, 3, 4, 5, 6):
            responses.get(f"{NOVEL_URL}/nghe/{n}", body=fixture("audio_vip.html"))
        run(path)
        stored = releases(path)
        assert len(stored) == 6
        assert [r.ord for r in stored] == [1, 2, 3, 4, 5, 6]

    @responses.activate
    def test_only_the_requested_releases_are_fetched(self, qapp, library_dir):
        path = make_project(library_dir)
        serve()
        ok, _, _ = run(path, numbers=[3])
        assert ok == [3]

    @responses.activate
    def test_one_unavailable_release_does_not_abandon_the_batch(self, qapp, library_dir):
        path = make_project(library_dir)
        responses.get(NOVEL_URL, body=fixture("landing.html"))
        responses.get(f"{NOVEL_URL}/nghe/1", body=fixture("audio_pending.html"))
        for n in (2, 3, 4, 5, 6):
            responses.get(f"{NOVEL_URL}/nghe/{n}", body=fixture("audio_ready.html"))
        responses.get(MEDIA + "01TESTAUDIO0000000000000A.mp3", body=BODY)
        ok, errors, failures = run(path)
        assert not failures
        assert len(errors) == 1 and errors[0][0] == 1
        assert len(ok) == 5

    @responses.activate
    def test_the_error_is_recorded_on_the_release(self, qapp, library_dir):
        path = make_project(library_dir)
        responses.get(NOVEL_URL, body=fixture("landing.html"))
        for n in (1, 2, 3, 4, 5, 6):
            responses.get(f"{NOVEL_URL}/nghe/{n}", body=fixture("audio_vip.html"))
        ok, errors, _ = run(path)
        assert not ok and len(errors) == 6
        assert releases(path)[0].error

    @responses.activate
    def test_cancelling_stops_the_batch(self, qapp, library_dir):
        path = make_project(library_dir)
        serve()
        worker = AudioDownloadWorker(path, delay=0)
        worker.delay = 0
        worker.cancel()
        ok: list[int] = []
        worker.item_done.connect(ok.append)
        worker.run()
        assert not ok

    def test_an_unsupported_source_is_refused_politely(self, qapp, library_dir):
        meta = NovelMeta(url="https://unsupported.example/x", site="nope", title="T")
        project = NovelProject.create(library_dir, meta, [])
        path = project.path
        project.close()
        _, _, failures = run(path)
        assert failures and "không hỗ trợ" in failures[0].lower()

    def test_the_politeness_delay_cannot_be_lowered_below_two_seconds(self, qapp, library_dir):
        """The adapter's docstring asks for >=2s against this small paid site."""
        worker = AudioDownloadWorker(make_project(library_dir), delay=0.0)
        assert worker.delay == 2.0


class TestSkipsWhatIsAlreadyDownloaded:
    """A release is 50-200 MB and its bytes never change; fetching it twice is pure waste."""

    @responses.activate
    def test_a_downloaded_release_is_not_fetched_again(self, qapp, library_dir):
        path = make_project(library_dir)
        serve()
        run(path)  # first pass fetches all six
        ok, errors, _ = run(path)  # second pass should find nothing to do
        assert not ok and not errors

    @responses.activate
    def test_the_number_skipped_is_counted_not_silently_dropped(self, qapp, library_dir):
        path = make_project(library_dir)
        serve()
        run(path)
        worker = AudioDownloadWorker(path, delay=0)
        worker.delay = 0
        worker.run()
        assert worker.skipped == 6

    @responses.activate
    def test_force_re_fetches_a_release_that_already_has_audio(self, qapp, library_dir):
        """What the per-row "⬇️ Tải lại" button asks for."""
        path = make_project(library_dir)
        serve()
        run(path)
        ok, _, _ = run(path, numbers=[1], skip_downloaded=False)
        assert ok == [1]

    @responses.activate
    def test_a_release_whose_file_vanished_is_fetched_again(self, qapp, library_dir):
        """The row says downloaded but the file is gone — skipping would leave the project
        permanently missing audio it believes it has."""
        path = make_project(library_dir)
        serve()
        run(path)
        (path / releases(path)[0].path).unlink()
        ok, _, _ = run(path)
        assert ok == [1]

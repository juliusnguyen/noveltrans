"""Feature 059 — downloading narration published by the source site.

The sizes drive the design and so they drive these tests: the reference novel's audio is
1.7 GB across 21 files, the largest single file 203 MB. Nothing may buffer a whole file in
RAM, and a transfer that stops early must never be left looking complete — feature 059's
guards then protect the truncated file from ever being replaced.
"""

import pytest
import responses

from noveltrans.errors import TtsError
from noveltrans.tts.convert import DownloadCancelled, download_media

URL = "https://img.tieuthuyetmang.com/audios/01STORY/01AUDIO.mp3"
BODY = b"ID3" + b"\x00" * 4093  # 4096 bytes, so several 256 KiB-capped reads is still one


class TestDownloadMedia:
    @responses.activate
    def test_writes_the_body_to_the_destination(self, tmp_path):
        responses.get(URL, body=BODY)
        out = download_media(URL, tmp_path / "a.mp3")
        assert out.read_bytes() == BODY

    @responses.activate
    def test_leaves_no_part_file_behind_on_success(self, tmp_path):
        responses.get(URL, body=BODY)
        download_media(URL, tmp_path / "a.mp3")
        assert not (tmp_path / "a.mp3.part").exists()

    @responses.activate
    def test_creates_the_destination_directory(self, tmp_path):
        responses.get(URL, body=BODY)
        out = download_media(URL, tmp_path / "audio" / "a.mp3")
        assert out.exists()

    @responses.activate
    def test_sends_the_cookie_when_one_is_given(self, tmp_path):
        responses.get(URL, body=BODY)
        download_media(URL, tmp_path / "a.mp3", cookies="session=abc")
        assert responses.calls[0].request.headers["Cookie"] == "session=abc"

    @responses.activate
    def test_reports_progress_as_bytes_land(self, tmp_path):
        responses.get(URL, body=BODY, headers={"content-length": str(len(BODY))})
        seen: list[tuple[int, int]] = []
        download_media(URL, tmp_path / "a.mp3", on_progress=lambda d, t: seen.append((d, t)))
        assert seen and seen[-1] == (len(BODY), len(BODY))

    @responses.activate
    def test_a_short_body_is_an_error_not_a_finished_file(self, tmp_path):
        """The server promised more than it sent. Renaming this into place would create a
        truncated file that the downloaded-audio guards then refuse to overwrite."""
        responses.get(URL, body=BODY, headers={"content-length": str(len(BODY) + 999)})
        with pytest.raises(TtsError):
            download_media(URL, tmp_path / "a.mp3")
        assert not (tmp_path / "a.mp3").exists()
        assert (tmp_path / "a.mp3.part").exists(), "keep the bytes so a retry resumes"

    @responses.activate
    def test_a_dropped_connection_reports_the_network_not_the_disk(self, tmp_path):
        """`requests.RequestException` subclasses `IOError`, so an `except OSError` placed
        first turns every network failure into "could not write file"."""
        responses.get(URL, body=BODY, headers={"content-length": str(len(BODY) + 999)})
        with pytest.raises(TtsError, match="Mất kết nối"):
            download_media(URL, tmp_path / "a.mp3")

    @responses.activate
    def test_an_empty_body_is_an_error(self, tmp_path):
        responses.get(URL, body=b"")
        with pytest.raises(TtsError):
            download_media(URL, tmp_path / "a.mp3")
        assert not (tmp_path / "a.mp3").exists()

    @responses.activate
    def test_an_http_error_is_reported_as_a_tts_error(self, tmp_path):
        responses.get(URL, status=403)
        with pytest.raises(TtsError):
            download_media(URL, tmp_path / "a.mp3")

    def test_hls_is_refused_rather_than_half_supported(self, tmp_path):
        with pytest.raises(TtsError, match="HLS"):
            download_media(URL.replace(".mp3", ".m3u8"), tmp_path / "a.m3u8")


class TestResume:
    @responses.activate
    def test_requests_a_range_when_a_part_file_exists(self, tmp_path):
        (tmp_path / "a.mp3.part").write_bytes(BODY[:1000])
        responses.get(URL, body=BODY[1000:], status=206)
        download_media(URL, tmp_path / "a.mp3")
        assert responses.calls[0].request.headers["Range"] == "bytes=1000-"

    @responses.activate
    def test_appends_the_remainder_to_the_partial_file(self, tmp_path):
        (tmp_path / "a.mp3.part").write_bytes(BODY[:1000])
        responses.get(URL, body=BODY[1000:], status=206)
        assert download_media(URL, tmp_path / "a.mp3").read_bytes() == BODY

    @responses.activate
    def test_a_server_that_ignores_range_restarts_instead_of_appending(self, tmp_path):
        """200 to a Range request means the whole file is coming. Appending it to what is
        already on disk would silently concatenate the first bytes twice."""
        (tmp_path / "a.mp3.part").write_bytes(BODY[:1000])
        responses.get(URL, body=BODY, status=200)
        assert download_media(URL, tmp_path / "a.mp3").read_bytes() == BODY

    @responses.activate
    def test_no_range_header_when_there_is_nothing_to_resume(self, tmp_path):
        responses.get(URL, body=BODY)
        download_media(URL, tmp_path / "a.mp3")
        assert "Range" not in responses.calls[0].request.headers


class TestCancel:
    @responses.activate
    def test_cancelling_raises_and_keeps_the_part_file_for_next_time(self, tmp_path):
        responses.get(URL, body=BODY)
        with pytest.raises(DownloadCancelled):
            download_media(URL, tmp_path / "a.mp3", cancelled=lambda: True)
        assert not (tmp_path / "a.mp3").exists()
        assert (tmp_path / "a.mp3.part").exists()

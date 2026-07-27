"""Tests for the browser-free parts of the YouTube upload module.

The Playwright automation itself needs a live, logged-in YouTube channel and can't run
in CI, so we cover what can go wrong without a browser — which is most of what matters:

  * the upload-state machine, whose whole job is to never publish the same part twice;
  * the schedule arithmetic and the locale-specific date/time strings typed into
    Studio's pickers (a mis-parsed date publishes a part *now* instead of in three
    weeks — the most damaging silent failure this feature has);
  * the guards that reject a bad request before any browser is launched.

Same shape as `test_discord_unlock.py`, including the fake-Playwright injection.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from noveltrans.youtube_upload import (
    STATE_COMMITTED,
    STATE_DRAFT,
    STATE_PUBLISHED,
    STATE_STARTED,
    STATE_UNKNOWN,
    UploadRequest,
    YouTubeUploadError,
    _CONFIRM_DIALOG_SEL,
    _DIALOG_SEL,
    _DONE_SEL,
    _TITLE_SEL,
    _format_date,
    _format_time,
    is_published,
    is_uploadable,
    needs_attention,
    profile_dir,
    read_upload_state,
    schedule_times,
    upload_state_path,
    validate_schedule_start,
    write_upload_state,
)


@pytest.fixture
def part(tmp_path: Path) -> Path:
    """A stand-in for a rendered part: `<dir>/<stem>/<stem>.mp4`, as feature 026 lays out."""
    folder = tmp_path / "truyen-0001-0010"
    folder.mkdir()
    video = folder / "truyen-0001-0010.mp4"
    video.write_bytes(b"not really an mp4")
    return video


def test_profile_dir_is_dedicated_and_separate_from_the_others():
    """The YouTube session must never share a profile with Discord or the CF solver."""
    from noveltrans.discord_unlock import profile_dir as discord_profile

    assert profile_dir().name == ".youtube-profile"
    assert profile_dir() != discord_profile()


class TestUploadStateRoundTrip:
    def test_sidecar_sits_beside_the_video(self, part):
        assert upload_state_path(part) == part.parent / "truyen-0001-0010.upload.json"

    def test_unattempted_part_reads_empty(self, part):
        assert read_upload_state(part) == {}
        assert is_uploadable(part)
        assert not is_published(part)
        assert not needs_attention(part)

    def test_write_then_read(self, part):
        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        state = read_upload_state(part)
        assert state["status"] == STATE_PUBLISHED
        assert state["video_id"] == "dQw4w9WgXcQ"

    def test_write_merges_rather_than_overwrites(self, part):
        """A later `published` write must keep the video_id an earlier `draft` recorded."""
        write_upload_state(part, status=STATE_DRAFT, video_id="dQw4w9WgXcQ")
        write_upload_state(part, status=STATE_PUBLISHED, published_at="2026-07-27")
        state = read_upload_state(part)
        assert state["video_id"] == "dQw4w9WgXcQ"  # survived
        assert state["status"] == STATE_PUBLISHED

    def test_write_leaves_no_temp_file_behind(self, part):
        write_upload_state(part, status=STATE_STARTED)
        leftovers = [p.name for p in part.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []


class TestCorruptStateIsNeverMistakenForUnattempted:
    """The single most dangerous confusion in the module.

    `{}` means "safe to upload". A truncated JSON file is precisely the situation where
    that is least true, so it must read as `unknown` and route into the same
    needs-a-human path as an interrupted attempt.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            '{"status": "publis',  # truncated mid-write
            "",  # zero-length: the classic torn write
            "not json at all",
            "[1, 2, 3]",  # valid JSON, wrong shape
            "null",
        ],
    )
    def test_unreadable_state_reads_as_unknown(self, part, raw):
        upload_state_path(part).write_text(raw, encoding="utf-8")
        assert read_upload_state(part).get("status") == STATE_UNKNOWN
        assert not is_uploadable(part)
        assert needs_attention(part)
        assert not is_published(part)


class TestUploadableTruthTable:
    """Only a part with no record at all may be uploaded automatically.

    This test *is* the never-double-publish guarantee: erring toward "skip" costs the
    user a manual upload, erring the other way publishes an episode twice in public.
    """

    @pytest.mark.parametrize(
        "status,uploadable,attention,published",
        [
            (STATE_STARTED, False, True, False),
            (STATE_DRAFT, False, True, False),
            (STATE_COMMITTED, False, True, False),
            (STATE_PUBLISHED, False, False, True),
            (STATE_UNKNOWN, False, True, False),
        ],
    )
    def test_states(self, part, status, uploadable, attention, published):
        write_upload_state(part, status=status)
        assert is_uploadable(part) is uploadable
        assert needs_attention(part) is attention
        assert is_published(part) is published

    def test_committed_is_not_treated_as_retryable(self, part):
        """`committed` means the publish click may already have landed.

        It must never look like a fresh part — that's the crash window where we cannot
        tell whether the video went live, so a human has to check.
        """
        write_upload_state(part, status=STATE_COMMITTED, video_id="dQw4w9WgXcQ")
        assert not is_uploadable(part)
        assert needs_attention(part)


class TestClearUploadState:
    """The escape hatch out of the states the app refuses to touch on its own."""

    def test_clearing_makes_a_stuck_part_uploadable_again(self, part):
        from noveltrans.youtube_upload import clear_upload_state

        write_upload_state(part, status=STATE_STARTED)
        assert not is_uploadable(part)
        assert clear_upload_state(part) is True
        assert is_uploadable(part)
        assert read_upload_state(part) == {}

    def test_clearing_nothing_is_not_an_error(self, part):
        from noveltrans.youtube_upload import clear_upload_state

        assert clear_upload_state(part) is False

    @pytest.mark.parametrize(
        "status,video_id,expected",
        [
            # No id → the run died before the file was ever sent; nothing is on YouTube.
            (STATE_STARTED, "", False),
            (STATE_UNKNOWN, "", False),
            # An id means a real video exists — re-uploading would duplicate it.
            (STATE_DRAFT, "dQw4w9WgXcQ", True),
            (STATE_COMMITTED, "dQw4w9WgXcQ", True),
            # Already published isn't a "draft" needing rescue; it's a settled outcome.
            (STATE_PUBLISHED, "dQw4w9WgXcQ", False),
        ],
    )
    def test_has_remote_draft_distinguishes_the_two_warnings(
        self, part, status, video_id, expected
    ):
        from noveltrans.youtube_upload import has_remote_draft

        write_upload_state(part, status=status, video_id=video_id)
        assert has_remote_draft(part) is expected


class TestScheduleTimes:
    def test_empty(self):
        assert schedule_times(datetime(2026, 8, 1, 20, 0), 0) == []

    def test_daily_series(self):
        times = schedule_times(datetime(2026, 8, 1, 20, 0), 3, 1)
        assert times == [
            datetime(2026, 8, 1, 20, 0),
            datetime(2026, 8, 2, 20, 0),
            datetime(2026, 8, 3, 20, 0),
        ]

    def test_spacing_zero_puts_every_part_at_the_same_instant(self):
        times = schedule_times(datetime(2026, 8, 1, 20, 0), 4, 0)
        assert len(set(times)) == 1

    def test_wall_clock_hour_is_preserved_across_a_month_boundary(self):
        """The user picks "20:00 every day" and expects 20:00 to stick."""
        times = schedule_times(datetime(2026, 8, 30, 20, 0), 4, 2)
        assert [t.day for t in times] == [30, 1, 3, 5]
        assert {(t.hour, t.minute) for t in times} == {(20, 0)}

    @pytest.mark.parametrize("bad", [-1])
    def test_rejects_negative_inputs(self, bad):
        with pytest.raises(ValueError):
            schedule_times(datetime(2026, 8, 1, 20, 0), bad)
        with pytest.raises(ValueError):
            schedule_times(datetime(2026, 8, 1, 20, 0), 3, bad)


class TestValidateScheduleStart:
    def test_rejects_the_past(self):
        now = datetime(2026, 7, 27, 12, 0)
        with pytest.raises(YouTubeUploadError, match="quá khứ"):
            validate_schedule_start(now - timedelta(hours=1), now=now)

    def test_rejects_too_soon(self):
        """A multi-GB part is still uploading five minutes from now."""
        now = datetime(2026, 7, 27, 12, 0)
        with pytest.raises(YouTubeUploadError, match="quá gần"):
            validate_schedule_start(now + timedelta(minutes=5), now=now)

    def test_accepts_tomorrow(self):
        now = datetime(2026, 7, 27, 12, 0)
        validate_schedule_start(now + timedelta(days=1), now=now)  # does not raise


class TestStudioDateTimeStrings:
    """Studio parses what it renders, so the strings must match the UI language.

    Regression-proofs the "part published now instead of in three weeks" bug.
    """

    def test_vietnamese(self):
        when = datetime(2026, 8, 1, 20, 0)
        assert _format_date(when, vietnamese=True) == "1 thg 8, 2026"
        assert _format_time(when, vietnamese=True) == "20:00"

    def test_english(self):
        when = datetime(2026, 8, 1, 20, 0)
        assert _format_date(when, vietnamese=False) == "Aug 1, 2026"
        assert _format_time(when, vietnamese=False) == "8:00 PM"

    @pytest.mark.parametrize(
        "hour,expected",
        [(0, "12:30 AM"), (9, "9:30 AM"), (12, "12:30 PM"), (13, "1:30 PM"), (23, "11:30 PM")],
    )
    def test_english_twelve_hour_edges(self, hour, expected):
        """Midnight and noon are where 12-hour formatting usually goes wrong."""
        assert _format_time(datetime(2026, 8, 1, hour, 30), vietnamese=False) == expected


class TestRequestValidation:
    """Every one of these must be caught before a browser is launched."""

    def _request(self, part, **kw):
        base = dict(video=part, title="Tên truyện - Phần 1")
        base.update(kw)
        return UploadRequest(**base)

    def test_accepts_a_good_request(self, part):
        self._request(part).validate()

    def test_rejects_a_missing_video(self, tmp_path):
        with pytest.raises(YouTubeUploadError, match="Không tìm thấy file video"):
            self._request(tmp_path / "nope.mp4").validate()

    def test_rejects_an_empty_title(self, part):
        with pytest.raises(YouTubeUploadError, match="thiếu tiêu đề"):
            self._request(part, title="   ").validate()

    def test_rejects_an_unknown_visibility(self, part):
        with pytest.raises(YouTubeUploadError, match="không hợp lệ"):
            self._request(part, visibility="everyone").validate()

    def test_rejects_schedule_without_a_time(self, part):
        with pytest.raises(YouTubeUploadError, match="chưa có thời điểm"):
            self._request(part, visibility="schedule").validate()

    def test_rejects_a_missing_thumbnail(self, part):
        """Silently uploading without the cover the user made is worse than saying so."""
        with pytest.raises(YouTubeUploadError, match="Không tìm thấy ảnh bìa"):
            self._request(part, thumbnail=part.parent / "gone.jpg").validate()


class TestUploadOneGuards:
    """`upload_one` must refuse a part that could duplicate a video — before it touches
    the page. Passing `page=None` proves it: any locator access would raise
    AttributeError instead of the error we assert on."""

    def test_already_published_is_skipped_not_re_uploaded(self, part):
        from noveltrans.youtube_upload import upload_one

        write_upload_state(
            part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ", url="https://youtu.be/dQw4w9WgXcQ"
        )
        result = upload_one(None, UploadRequest(video=part, title="Phần 1"))
        assert result.skipped
        assert result.video_id == "dQw4w9WgXcQ"

    @pytest.mark.parametrize("status", [STATE_STARTED, STATE_DRAFT, STATE_COMMITTED, STATE_UNKNOWN])
    def test_interrupted_attempts_raise_instead_of_retrying(self, part, status):
        from noveltrans.youtube_upload import upload_one

        write_upload_state(part, status=status)
        with pytest.raises(YouTubeUploadError, match="gián đoạn"):
            upload_one(None, UploadRequest(video=part, title="Phần 1"))

    def test_a_bad_request_raises_before_touching_the_page(self, tmp_path):
        from noveltrans.youtube_upload import upload_one

        with pytest.raises(YouTubeUploadError):
            upload_one(None, UploadRequest(video=tmp_path / "nope.mp4", title="Phần 1"))


class _FakeUploadPage:
    """A Studio upload dialog: only `accepting` selectors expose a usable file input.

    Models the live failure — the dialog is open, but the selector we reach for isn't
    there, so `set_input_files` never happens and the page sits on the drag-and-drop pane.
    """

    def __init__(self, *, accepting=(), progresses=True, visible=()):
        self.accepting = set(accepting)
        self.progresses = progresses
        self.visible = set(visible)
        self.sent: list = []
        self.tried: list = []

    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def wait_for(self, state=None, timeout=None):
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                page.tried.append(selector)
                if selector in page.accepting or selector in page.visible:
                    return
                # The real thing raises Playwright's TimeoutError, and `_first_present`
                # catches only that — a plain RuntimeError here would test nothing.
                raise PlaywrightTimeoutError(f"not found: {selector}")

            def set_input_files(self, path):
                if selector not in page.accepting:
                    raise RuntimeError(f"not a file input: {selector}")
                page.sent.append((selector, path))
                if page.progresses:
                    page.visible.add(_TITLE_SEL)

        return _Loc()


class TestSendFile:
    """The one step with no alternative: if the file never goes in, nothing else matters."""

    def test_uses_the_first_selector_that_works(self, part):
        from noveltrans.youtube_upload import _FILE_INPUT_SELS, _send_file

        page = _FakeUploadPage(accepting={_FILE_INPUT_SELS[0]})
        _send_file(page, part)
        assert page.sent == [(_FILE_INPUT_SELS[0], str(part))]

    def test_falls_through_to_the_plain_html_selector(self, part):
        """Regression: the narrow `ytcp-*`-scoped selectors had drifted and matched
        nothing, stalling the live run on an empty upload dialog. Plain
        `input[type=file]` cannot be renamed away, so it must still be reached."""
        from noveltrans.youtube_upload import _FILE_INPUT_SELS, _send_file

        page = _FakeUploadPage(accepting={"input[type='file']"})
        _send_file(page, part)
        assert page.sent == [("input[type='file']", str(part))]
        assert page.tried[:2] == list(_FILE_INPUT_SELS[:2])  # the narrow ones tried first

    def test_raises_when_studio_never_leaves_the_drop_pane(self, part):
        """Setting the input silently doing nothing must fail *here*, not ten steps later
        with a confusing message about the title box."""
        from noveltrans.youtube_upload import _FILE_INPUT_SELS, _send_file

        page = _FakeUploadPage(accepting={_FILE_INPUT_SELS[0]}, progresses=False)
        page.expect_file_chooser = None  # no fallback available on this fake
        with pytest.raises(YouTubeUploadError):
            _send_file(page, part)

    def test_no_file_input_at_all_is_a_clear_error(self, part):
        from noveltrans.youtube_upload import _send_file

        page = _FakeUploadPage(accepting=set())
        page.expect_file_chooser = None
        with pytest.raises(YouTubeUploadError, match="YouTube Studio"):
            _send_file(page, part)
        assert page.sent == []


class _FakeStudioPage:
    """Studio's upload dialog, faithful to the trait that broke two live runs.

    `ytcp-uploads-dialog` is *attached but never visible* — it's a zero-size wrapper
    around an overlay, so Playwright reports it hidden while the user is looking right
    at it. Anything waiting for it to become visible waits forever.
    """

    def __init__(self, *, step="SELECT_FILES", attached=True, visible=(), confirm=False):
        self.step = step
        self.attached = attached
        self.visible = set(visible)
        self.confirm = confirm
        self.clicked: list = []
        self.waits: list = []

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def get_attribute(self, name, timeout=None):
                if selector == _DIALOG_SEL and name == "workflow-step":
                    return page.step
                return None

            def wait_for(self, state=None, timeout=None):
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                page.waits.append((selector, state))
                if selector == _DIALOG_SEL:
                    ok = {
                        "attached": page.attached,
                        "detached": not page.attached,
                        "visible": False,  # the trait under test: never visible
                        "hidden": True,
                    }[state]
                elif selector == _CONFIRM_DIALOG_SEL:
                    ok = page.confirm
                else:
                    ok = selector in page.visible
                if not ok:
                    raise PlaywrightTimeoutError(f"{selector} not {state}")

            def click(self):
                page.clicked.append(selector)

            def is_enabled(self, timeout=None):
                return selector in page.visible

        return _Loc()


class TestTransferState:
    """The gate that decides when a part's bytes are safely on YouTube.

    Getting this wrong is the most expensive failure in the feature: a false "finished"
    publishes and moves on while the transfer is still running, so a 31-part batch queues
    every upload at once and then kills them all when the browser closes. That is exactly
    what happened when the gate was "the publish button is enabled" — Studio does not
    disable it during a transfer, so it read finished immediately, every time.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Uploading 17%...",
            "Đang tải lên 17%…",
            "Uploading 99%",
            "Đang tải lên 1%",
            "Uploading 0%",
        ],
    )
    def test_still_uploading(self, text):
        from noveltrans.youtube_upload import transfer_state

        finished, _ = transfer_state(text)
        assert finished is False

    @pytest.mark.parametrize(
        "text",
        [
            "Upload complete. Now processing...",
            "Đã tải lên xong. Đang xử lý…",
            "Processing up to HD",
            "Đang xử lý HD",
            "Checks complete. No issues found.",
            "Kiểm tra xong, không có vấn đề",
            "Uploaded",
        ],
    )
    def test_transfer_finished(self, text):
        """Processing and checks both happen *after* the bytes are in — safe to move on."""
        from noveltrans.youtube_upload import transfer_state

        finished, _ = transfer_state(text)
        assert finished is True

    def test_a_full_hundred_percent_counts_as_finished(self):
        from noveltrans.youtube_upload import transfer_state

        assert transfer_state("Uploading 100%")[0] is True

    def test_percent_is_reported_for_the_progress_line(self):
        from noveltrans.youtube_upload import transfer_state

        assert transfer_state("Uploading 42%...")[1] == 42
        assert transfer_state("Processing")[1] is None

    @pytest.mark.parametrize("text", ["", "   ", "Something unfamiliar"])
    def test_unrecognised_text_is_not_treated_as_finished(self, text):
        """Erring this way costs time; erring the other way abandons an upload."""
        from noveltrans.youtube_upload import transfer_state

        assert transfer_state(text)[0] is False


class _FakeProgressPage:
    """Emits a scripted sequence of progress lines, the last repeating forever."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.polls = 0

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def inner_text(self, timeout=None):
                index = min(page.polls, len(page.texts) - 1)
                page.polls += 1
                return page.texts[index]

            def get_attribute(self, name, timeout=None):
                return "DETAILS"

        return _Loc()


class TestWaitForBytesUploaded:
    def test_returns_only_after_the_transfer_completes(self):
        from noveltrans.youtube_upload import _wait_for_bytes_uploaded

        page = _FakeProgressPage(
            ["Uploading 10%", "Uploading 55%", "Uploading 99%", "Upload complete. Processing…"]
        )
        seen: list = []
        _wait_for_bytes_uploaded(
            page, on_progress=seen.append, should_cancel=None, video_id="abc"
        )
        assert page.polls >= 4  # it really waited rather than returning on the first look
        assert any("99%" in s for s in seen)  # and reported progress along the way

    def test_never_returns_while_still_uploading(self, monkeypatch):
        """A batch that returns early is what queues 31 uploads and then kills them."""
        import noveltrans.youtube_upload as yt

        monkeypatch.setattr(yt, "_UPLOAD_WAIT_MS", 10_000)
        monkeypatch.setattr(yt, "_UPLOAD_POLL_MS", 1_000)
        page = _FakeProgressPage(["Uploading 17%"])
        with pytest.raises(YouTubeUploadError, match="Quá thời gian"):
            yt._wait_for_bytes_uploaded(
                page, on_progress=None, should_cancel=None, video_id="abc"
            )

    def test_missing_progress_line_stops_rather_than_guessing(self, monkeypatch):
        """Publishing without ever reading progress would abandon the transfer."""
        import noveltrans.youtube_upload as yt

        monkeypatch.setattr(yt, "_UPLOAD_WAIT_MS", 5_000)
        monkeypatch.setattr(yt, "_UPLOAD_POLL_MS", 1_000)
        page = _FakeProgressPage([""])
        with pytest.raises(YouTubeUploadError, match="Không đọc được tiến độ"):
            yt._wait_for_bytes_uploaded(
                page, on_progress=None, should_cancel=None, video_id="abc"
            )


class TestDialogIsAttachedNotVisible:
    """Regression for the live hang: `36 x locator resolved to hidden
    <ytcp-uploads-dialog workflow-step="SELECT_FILES">`."""

    def test_open_dialog_waits_for_attached_never_visible(self, monkeypatch):
        import noveltrans.youtube_upload as yt

        page = _FakeStudioPage()
        page.url = "https://studio.youtube.com/channel/UCabc/videos/upload"
        page.goto = lambda *a, **k: None
        page.wait_for_load_state = lambda *a, **k: None

        yt._open_upload_dialog(page, timeout_ms=1_000)
        states = [state for sel, state in page.waits if sel == _DIALOG_SEL]
        assert "attached" in states
        assert "visible" not in states  # waiting on visibility is what hung the run

    def test_dialog_step_is_read_from_the_attribute(self):
        from noveltrans.youtube_upload import _dialog_step

        assert _dialog_step(_FakeStudioPage(step="DETAILS")) == "DETAILS"

    def test_file_accepted_uses_the_workflow_step(self):
        from noveltrans.youtube_upload import _file_accepted

        assert not _file_accepted(_FakeStudioPage(step="SELECT_FILES"), timeout_ms=2_000)
        assert _file_accepted(_FakeStudioPage(step="DETAILS"), timeout_ms=2_000)


class TestFinishConfirmation:
    """`_finish` used to wait for the dialog to become "hidden" — which it always is.

    That passed instantly and would have recorded every publish as successful without
    checking anything, including ones YouTube rejected for a missing required field.
    """

    def test_hidden_dialog_alone_is_not_treated_as_confirmation(self):
        from noveltrans.youtube_upload import _finish

        # attached (so not detached), no confirmation dialog → must NOT report success
        page = _FakeStudioPage(step="VISIBILITY", attached=True, confirm=False)
        page.visible = {_DONE_SEL}
        with pytest.raises(YouTubeUploadError, match="không xác nhận"):
            _finish(page, video_id="dQw4w9WgXcQ")

    def test_detached_dialog_confirms(self):
        from noveltrans.youtube_upload import _finish

        page = _FakeStudioPage(attached=False)
        page.visible = {_DONE_SEL}
        _finish(page, video_id="dQw4w9WgXcQ")  # does not raise

    def test_share_dialog_confirms(self):
        from noveltrans.youtube_upload import _finish

        page = _FakeStudioPage(attached=True, confirm=True)
        page.visible = {_DONE_SEL}
        _finish(page, video_id="dQw4w9WgXcQ")

    def test_error_names_the_step_it_stalled_on(self):
        """So a failure report says where it stopped instead of just "it didn't work"."""
        from noveltrans.youtube_upload import _finish

        page = _FakeStudioPage(step="CHECKS", attached=True, confirm=False)
        page.visible = {_DONE_SEL}
        with pytest.raises(YouTubeUploadError, match="CHECKS"):
            _finish(page, video_id="")


class TestClearProfile:
    """Changing channel means deleting the profile, so this is the one destructive path.

    YouTube's own switcher only moves between brand channels *within* one Google
    account, so it can't fix "signed in as the wrong account" — dropping the profile
    fixes both cases, but it removes a directory tree and has to be careful about it.
    """

    def _profile(self, tmp_path, monkeypatch, *, chromium_like=True):
        import noveltrans.youtube_upload as yt

        path = tmp_path / ".youtube-profile"
        path.mkdir()
        if chromium_like:
            (path / "Default").mkdir()
            (path / "Local State").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(yt, "profile_dir", lambda: path)
        return path

    def test_removes_an_existing_profile(self, tmp_path, monkeypatch):
        from noveltrans.youtube_upload import clear_profile

        path = self._profile(tmp_path, monkeypatch)
        assert clear_profile() is True
        assert not path.exists()

    def test_no_profile_is_not_an_error(self, tmp_path, monkeypatch):
        import noveltrans.youtube_upload as yt

        monkeypatch.setattr(yt, "profile_dir", lambda: tmp_path / "nothing-here")
        assert yt.clear_profile() is False

    def test_refuses_to_delete_something_that_is_not_a_browser_profile(
        self, tmp_path, monkeypatch
    ):
        """If the path isn't what we think, recursive delete is reckless — bail loudly."""
        from noveltrans.youtube_upload import clear_profile

        path = self._profile(tmp_path, monkeypatch, chromium_like=False)
        (path / "important.txt").write_text("do not delete me", encoding="utf-8")
        with pytest.raises(YouTubeUploadError, match="không giống profile"):
            clear_profile()
        assert (path / "important.txt").exists()

    def test_switch_login_clears_the_profile_first(self, tmp_path, monkeypatch):
        """Without this, a valid session loads straight through and the window closes
        before the user can pick a different channel — the whole bug being fixed."""
        import noveltrans.youtube_upload as yt

        self._profile(tmp_path, monkeypatch)
        cleared = []
        monkeypatch.setattr(yt, "clear_profile", lambda: cleared.append(True))
        # Stop right after the clear: we only care that it happened before the launch.
        monkeypatch.setattr(
            yt, "_require_playwright", lambda: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        with pytest.raises(RuntimeError):
            yt.open_login(switch=True)
        assert cleared == [True]

    def test_plain_login_does_not_clear_the_profile(self, tmp_path, monkeypatch):
        import noveltrans.youtube_upload as yt

        self._profile(tmp_path, monkeypatch)
        cleared = []
        monkeypatch.setattr(yt, "clear_profile", lambda: cleared.append(True))
        monkeypatch.setattr(
            yt, "_require_playwright", lambda: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        with pytest.raises(RuntimeError):
            yt.open_login()
        assert cleared == []


class _FakeChannelPage:
    """A page whose URL and account-name element stand in for a logged-in Studio."""

    def __init__(self, url: str, name: str = ""):
        self.url = url
        self._name = name

    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def inner_text(self, timeout=None):
                if page._name and "account-name" in selector:
                    return page._name
                raise RuntimeError("no such element")

        return _Loc()


class TestCurrentChannel:
    """Reported back after a login so a wrong channel is caught before a video is public."""

    def test_reads_id_and_name(self):
        from noveltrans.youtube_upload import _current_channel

        page = _FakeChannelPage(
            "https://studio.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos",
            name="Fox Novel",
        )
        assert _current_channel(page) == ("UCabcdefghijklmnopqrstuv", "Fox Novel")

    def test_id_alone_is_enough_when_the_name_element_is_gone(self):
        from noveltrans.youtube_upload import _current_channel

        page = _FakeChannelPage("https://studio.youtube.com/channel/UCabcdefghijklmnopqrstuv")
        assert _current_channel(page) == ("UCabcdefghijklmnopqrstuv", "")

    def test_unknown_url_yields_empties_rather_than_raising(self):
        from noveltrans.youtube_upload import _current_channel

        assert _current_channel(_FakeChannelPage("https://studio.youtube.com/")) == ("", "")


class _FakePlaywright:
    """Minimal stand-in for `sync_playwright()`, as in test_discord_unlock."""

    def __init__(self, *, working_channels=("chrome",)):
        self.working_channels = working_channels
        self.stopped = False
        self.launched: list = []
        self.chromium = self

    def start(self):
        return self

    def stop(self):
        self.stopped = True

    def launch_persistent_context(self, user_data_dir, *, channel=None, **kw):
        self.launched.append(channel)
        if channel not in self.working_channels:
            raise RuntimeError(f"no such channel: {channel}")
        return object()


class TestLaunchContext:
    def test_prefers_real_chrome(self):
        from noveltrans.youtube_upload import _launch_context

        fake = _FakePlaywright(working_channels=("chrome",))
        _launch_context(lambda: fake, headless=False)
        assert fake.launched == ["chrome"]

    def test_falls_back_to_bundled_chromium(self):
        from noveltrans.youtube_upload import _launch_context

        fake = _FakePlaywright(working_channels=(None,))
        _launch_context(lambda: fake, headless=False)
        assert fake.launched == ["chrome", None]

    def test_no_browser_raises_and_leaks_nothing(self):
        from noveltrans.youtube_upload import _launch_context

        fake = _FakePlaywright(working_channels=())
        with pytest.raises(YouTubeUploadError, match="Không mở được trình duyệt"):
            _launch_context(lambda: fake, headless=False)
        assert fake.stopped is True  # no orphaned Playwright process


class TestStateFileIsJsonWeCanReadBack:
    def test_written_file_is_valid_utf8_json_with_vietnamese_intact(self, part):
        write_upload_state(part, status=STATE_PUBLISHED, title="Ta Có Một Thân Bị Động Kỹ - Phần 1")
        data = json.loads(upload_state_path(part).read_text(encoding="utf-8"))
        assert data["title"] == "Ta Có Một Thân Bị Động Kỹ - Phần 1"

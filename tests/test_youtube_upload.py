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
    _STILL_CHECKING_PUBLISH_ANYWAY_SEL,
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


class TestDescriptionCap:
    """Feature 065 — `UploadRequest` is the choke point that must never let an over-budget
    description reach Studio, the same way `save_tags` is for the 500-char tag budget.

    Not redundant with the builders: the description usually comes straight off a `.txt`
    sidecar which may have been written before the cap existed, and re-rendering a part
    just to shorten its description isn't something to ask of the user.
    """

    def _request(self, part, description):
        return UploadRequest(video=part, title="Phần 1", description=description)

    def test_request_clamps_an_oversized_description(self, tmp_path):
        from noveltrans.tts.description import (
            YOUTUBE_DESCRIPTION_CHAR_LIMIT,
            description_length,
        )

        part = tmp_path / "p.mp4"
        part.write_bytes(b"x")
        request = self._request(part, "x" * 9000)
        assert description_length(request.description) <= YOUTUBE_DESCRIPTION_CHAR_LIMIT

    def test_request_leaves_a_normal_description_alone(self, tmp_path):
        part = tmp_path / "p.mp4"
        part.write_bytes(b"x")
        text = "Tên truyện: A\n\nMục lục chương:\n0:00 Chương 1\n\nTạo bởi: Fox Novel\n"
        assert self._request(part, text).description == text

    def test_clamped_description_ends_on_a_whole_line(self, tmp_path):
        part = tmp_path / "p.mp4"
        part.write_bytes(b"x")
        text = "\n".join(f"{i}:00 Chương {i}: {'ả' * 40}" for i in range(400)) + "\n"
        clamped = self._request(part, text).description
        assert clamped.endswith("\n")
        # every surviving line is a complete one from the original
        original = set(text.splitlines())
        assert all(line in original for line in clamped.splitlines())


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

    def __init__(
        self, *, step="SELECT_FILES", attached=True, visible=(), confirm=False,
        still_checking=False,
    ):
        self.step = step
        self.attached = attached
        self.visible = set(visible)
        self.confirm = confirm
        self.still_checking = still_checking  # "We're still checking your content" gate
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

            @property
            def last(self):
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

            def is_visible(self, timeout=None):
                if selector == _STILL_CHECKING_PUBLISH_ANYWAY_SEL:
                    return page.still_checking
                return selector in page.visible

            def click(self):
                page.clicked.append(selector)
                if selector == _STILL_CHECKING_PUBLISH_ANYWAY_SEL:
                    page.still_checking = False  # "Publish anyway" dismisses the gate

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

    def test_still_checking_content_dialog_is_dismissed_then_confirms(self):
        """YouTube's "We're still checking your content" gate (Publish anyway / Go back)
        must be clicked through, not left to stall the run until the timeout fires."""
        from noveltrans.youtube_upload import _finish

        page = _FakeStudioPage(attached=True, confirm=True, still_checking=True)
        page.visible = {_DONE_SEL}
        _finish(page, video_id="dQw4w9WgXcQ")  # does not raise
        assert _STILL_CHECKING_PUBLISH_ANYWAY_SEL in page.clicked


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


# ----------------------------------------------- 034: thumbnail update flow
#
# The same discipline as the upload tests above: no network, no Playwright process, and
# every fake raises Playwright's own TimeoutError so `_first_present` behaves as it does
# live. What is under test is the pair of gates that make this flow honest — Studio must
# prove it took the image, and prove it saved it — because the flow's ONLY output is the
# thumbnail, so a silently skipped step would report success for nothing.


@pytest.fixture
def cover(part: Path) -> Path:
    """The `<stem>.jpg` sidecar the cover editor writes beside every rendered part."""
    jpg = part.parent / (part.stem + ".jpg")
    jpg.write_bytes(b"\xff\xd8\xff" + b"jpeg-ish" * 64)
    return jpg


class TestUploadedVideoId:
    """Eligibility for a thumbnail push: does the record name a video we can navigate to?"""

    def test_no_record_has_no_id(self, part):
        from noveltrans.youtube_upload import uploaded_video_id

        assert uploaded_video_id(part) == ""

    @pytest.mark.parametrize("status", [STATE_DRAFT, STATE_COMMITTED, STATE_PUBLISHED])
    def test_every_state_that_names_a_video_is_eligible(self, part, status):
        """Deliberately status-blind, unlike `is_uploadable`. A scheduled or private video
        still has an editable thumbnail, and replacing one cannot duplicate anything —
        so the double-publish paranoia has nothing to protect here."""
        from noveltrans.youtube_upload import uploaded_video_id

        write_upload_state(part, status=status, video_id="dQw4w9WgXcQ")
        assert uploaded_video_id(part) == "dQw4w9WgXcQ"

    def test_hand_marked_part_is_not_eligible(self, part):
        """`mark_uploaded_by_hand` records no id and never had one. Pushing a cover at a
        video we cannot identify is the one thing this must never do."""
        from noveltrans.youtube_upload import mark_uploaded_by_hand, uploaded_video_id

        mark_uploaded_by_hand(part)
        assert uploaded_video_id(part) == ""


class TestThumbnailIsCurrent:
    """Advisory only — it must fail toward "stale" so the safe answer is always
    "offer to push it"."""

    def test_cover_regenerated_after_the_push_reads_stale(self, part, cover):
        import os

        from noveltrans.youtube_upload import _now_iso, thumbnail_is_current

        write_upload_state(part, thumbnail_updated_at=_now_iso())
        future = cover.stat().st_mtime + 600
        os.utime(cover, (future, future))
        assert thumbnail_is_current(part, cover) is False

    def test_push_after_the_cover_reads_current(self, part, cover):
        import os

        from noveltrans.youtube_upload import _now_iso, thumbnail_is_current

        past = cover.stat().st_mtime - 600
        os.utime(cover, (past, past))
        write_upload_state(part, thumbnail_updated_at=_now_iso())
        assert thumbnail_is_current(part, cover) is True

    def test_no_record_reads_stale(self, part, cover):
        from noveltrans.youtube_upload import thumbnail_is_current

        assert thumbnail_is_current(part, cover) is False

    def test_unparseable_timestamp_reads_stale(self, part, cover):
        from noveltrans.youtube_upload import thumbnail_is_current

        write_upload_state(part, thumbnail_updated_at="hôm qua")
        assert thumbnail_is_current(part, cover) is False


class TestThumbnailRequestValidation:
    def test_accepts_a_good_request(self, part, cover):
        from noveltrans.youtube_upload import ThumbnailRequest

        ThumbnailRequest(video=part, thumbnail=cover).validate()

    def test_rejects_a_missing_image(self, part, tmp_path):
        from noveltrans.youtube_upload import ThumbnailRequest

        with pytest.raises(YouTubeUploadError, match="Không tìm thấy ảnh bìa"):
            ThumbnailRequest(video=part, thumbnail=tmp_path / "nope.jpg").validate()

    def test_rejects_an_empty_image(self, part):
        from noveltrans.youtube_upload import ThumbnailRequest

        empty = part.parent / (part.stem + ".jpg")
        empty.write_bytes(b"")
        with pytest.raises(YouTubeUploadError, match="rỗng"):
            ThumbnailRequest(video=part, thumbnail=empty).validate()

    def test_rejects_an_image_over_youtubes_two_megabytes(self, part, cover):
        """Local and free to check, so a file YouTube would refuse never costs a browser."""
        from noveltrans.youtube_upload import _MAX_THUMBNAIL_BYTES, ThumbnailRequest

        cover.write_bytes(b"x" * (_MAX_THUMBNAIL_BYTES + 1))
        with pytest.raises(YouTubeUploadError, match="2 MB"):
            ThumbnailRequest(video=part, thumbnail=cover).validate()

    def test_rejects_an_unsupported_format(self, part):
        from noveltrans.youtube_upload import ThumbnailRequest

        webp = part.parent / (part.stem + ".webp")
        webp.write_bytes(b"RIFFwebp")
        with pytest.raises(YouTubeUploadError, match="định dạng"):
            ThumbnailRequest(video=part, thumbnail=webp).validate()

    def test_does_not_require_the_mp4_to_still_exist(self, part, cover):
        """The deliberate divergence from `UploadRequest`: the video is already on
        YouTube, so someone who deleted the local render to reclaim disk must still be
        able to push a new cover."""
        from noveltrans.youtube_upload import ThumbnailRequest

        part.unlink()
        ThumbnailRequest(video=part, thumbnail=cover).validate()


class _FakeEditPage:
    """Studio's video edit page.

    Models the one signal this whole flow rests on: `#save` is DISABLED on arrival,
    becomes enabled when a change lands, and goes back to disabled once the save
    commits. That swing is what tells "the image went in" from "the input silently
    no-oped", and "it saved" from "it didn't".
    """

    def __init__(
        self,
        *,
        video_id="dQw4w9WgXcQ",
        lands_on=None,
        inputs=(),
        editor=True,
        accepts=True,
        commits=True,
        toast="",
        save_present=True,
    ):
        self.video_id = video_id
        self.lands_on = lands_on
        self.url = ""
        self.inputs = set(inputs)
        self.editor = editor
        self.accepts = accepts  # does the file enable Save?
        self.commits = commits  # does clicking Save disable it again?
        self.toast = toast
        self.save_present = save_present
        self.save_enabled = False  # nothing to save yet
        self.sent: list = []
        self.tried: list = []
        self.clicked: list = []
        self.goto_urls: list = []
        self.waits: list = []

    def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        self.url = self.lands_on if self.lands_on is not None else url

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        from noveltrans.youtube_upload import _EDIT_PAGE_SEL, _SAVE_SEL, _TOAST_SEL

        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def wait_for(self, state=None, timeout=None):
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                page.tried.append(selector)
                page.waits.append((selector, state))
                if selector == _EDIT_PAGE_SEL:
                    ok = page.editor
                elif selector == _SAVE_SEL:
                    ok = page.save_present
                else:
                    ok = selector in page.inputs
                if not ok:
                    raise PlaywrightTimeoutError(f"{selector} not {state}")

            def set_input_files(self, path):
                if selector not in page.inputs:
                    raise RuntimeError(f"not a file input: {selector}")
                page.sent.append((selector, path))
                if page.accepts:
                    page.save_enabled = True

            def get_attribute(self, name, timeout=None):
                if selector == _SAVE_SEL and name == "disabled":
                    return None if page.save_enabled else "true"
                return None

            def inner_text(self, timeout=None):
                return page.toast if selector == _TOAST_SEL else ""

            def click(self):
                page.clicked.append(selector)
                if selector == _SAVE_SEL and page.commits:
                    page.save_enabled = False

            def is_visible(self, timeout=None):
                return False  # no text-matched fallback on this fake

        return _Loc()


class TestOpenEditPage:
    def test_navigates_to_the_video_edit_url(self):
        from noveltrans.youtube_upload import _open_edit_page

        page = _FakeEditPage()
        _open_edit_page(page, "dQw4w9WgXcQ")
        assert "/video/dQw4w9WgXcQ/edit" in page.goto_urls[0]

    def test_logged_out_raises_needs_login(self):
        from noveltrans.youtube_upload import _open_edit_page

        page = _FakeEditPage(lands_on="https://accounts.google.com/signin")
        with pytest.raises(YouTubeUploadError) as excinfo:
            _open_edit_page(page, "dQw4w9WgXcQ")
        assert excinfo.value.needs_login is True

    def test_being_bounced_away_from_the_video_names_it(self):
        """Deleted video, or the profile is on a different channel now — either way the
        user needs the id and the link, not a generic timeout."""
        from noveltrans.youtube_upload import _open_edit_page

        page = _FakeEditPage(lands_on="https://studio.youtube.com/channel/UC123/videos")
        with pytest.raises(YouTubeUploadError, match="dQw4w9WgXcQ"):
            _open_edit_page(page, "dQw4w9WgXcQ")

    def test_missing_editor_is_a_named_failure(self):
        from noveltrans.youtube_upload import _open_edit_page

        page = _FakeEditPage(editor=False)
        with pytest.raises(YouTubeUploadError, match="giao diện"):
            _open_edit_page(page, "dQw4w9WgXcQ")

    def test_waits_for_attached_never_visible(self):
        """The zero-size `ytcp-*` wrapper trait that hung two live runs of the upload
        flow applies to every Studio container, this page included."""
        from noveltrans.youtube_upload import _EDIT_PAGE_SEL, _open_edit_page

        page = _FakeEditPage()
        _open_edit_page(page, "dQw4w9WgXcQ")
        states = [state for sel, state in page.waits if sel == _EDIT_PAGE_SEL]
        assert states and "visible" not in states


class TestSendThumbnail:
    def test_uses_the_first_selector_that_works(self, cover):
        from noveltrans.youtube_upload import _EDIT_THUMBNAIL_INPUT_SELS, _send_thumbnail

        page = _FakeEditPage(inputs={_EDIT_THUMBNAIL_INPUT_SELS[0]})
        _send_thumbnail(page, cover, video_id="dQw4w9WgXcQ")
        assert page.sent == [(_EDIT_THUMBNAIL_INPUT_SELS[0], str(cover))]

    def test_falls_through_to_the_widest_selector(self, cover):
        from noveltrans.youtube_upload import _EDIT_THUMBNAIL_INPUT_SELS, _send_thumbnail

        widest = _EDIT_THUMBNAIL_INPUT_SELS[-1]
        page = _FakeEditPage(inputs={widest})
        _send_thumbnail(page, cover, video_id="dQw4w9WgXcQ")
        assert page.sent == [(widest, str(cover))]
        # the narrow ones were tried first, as the ordering intends
        assert page.tried[:2] == list(_EDIT_THUMBNAIL_INPUT_SELS[:2])

    def test_a_missing_input_is_a_hard_error_not_a_skip(self, cover):
        """The headline behaviour of this feature.

        `_set_details` swallows exactly this case and reports "kênh chưa được phép đặt
        ảnh bìa", because there the thumbnail is one field of an upload that must still
        finish. Here it IS the job: continuing would report success while the channel
        still shows the old cover.
        """
        from noveltrans.youtube_upload import _send_thumbnail

        page = _FakeEditPage(inputs=set())
        page.expect_file_chooser = None  # no fallback available on this fake
        with pytest.raises(YouTubeUploadError, match="YouTube Studio"):
            _send_thumbnail(page, cover, video_id="dQw4w9WgXcQ")
        assert page.sent == []

    def test_an_input_that_silently_no_ops_raises_here(self, cover):
        """Setting a wrong element is a no-op. Without the Save-enabled gate the run
        would sail on and report a successful save of nothing."""
        from noveltrans.youtube_upload import _EDIT_THUMBNAIL_INPUT_SELS, _send_thumbnail

        page = _FakeEditPage(inputs={_EDIT_THUMBNAIL_INPUT_SELS[0]}, accepts=False)
        page.expect_file_chooser = None
        with pytest.raises(YouTubeUploadError):
            _send_thumbnail(page, cover, video_id="dQw4w9WgXcQ")
        assert page.clicked == []  # never reached the save

    def test_a_studio_rejection_reaches_the_user_verbatim(self, cover):
        from noveltrans.youtube_upload import _EDIT_THUMBNAIL_INPUT_SELS, _send_thumbnail

        page = _FakeEditPage(
            inputs={_EDIT_THUMBNAIL_INPUT_SELS[0]}, accepts=False, toast="Ảnh quá lớn"
        )
        page.expect_file_chooser = None
        with pytest.raises(YouTubeUploadError, match="Ảnh quá lớn"):
            _send_thumbnail(page, cover, video_id="dQw4w9WgXcQ")


class TestSaveConfirmation:
    """Mirrors `TestFinishConfirmation`, and guards the same bug: never confirm on a
    state that was already true before the click."""

    def test_save_going_back_to_disabled_confirms(self):
        from noveltrans.youtube_upload import _SAVE_SEL, _save_edits

        page = _FakeEditPage()
        page.save_enabled = True  # as `_thumbnail_accepted` left it
        _save_edits(page, video_id="dQw4w9WgXcQ")
        assert page.clicked == [_SAVE_SEL]

    def test_a_saved_toast_confirms(self):
        """Some Studio builds leave Save enabled; the toast is the independent fallback."""
        from noveltrans.youtube_upload import _save_edits

        page = _FakeEditPage(commits=False, toast="Đã lưu thay đổi")
        page.save_enabled = True
        _save_edits(page, video_id="dQw4w9WgXcQ")

    def test_a_never_confirming_save_raises_with_the_link(self):
        from noveltrans.youtube_upload import _save_edits

        page = _FakeEditPage(commits=False)
        page.save_enabled = True
        with pytest.raises(YouTubeUploadError, match="không xác nhận"):
            _save_edits(page, video_id="dQw4w9WgXcQ")

    def test_an_error_toast_short_circuits_into_the_error(self):
        from noveltrans.youtube_upload import _save_edits

        page = _FakeEditPage(commits=False, toast="Không thể lưu ảnh bìa")
        page.save_enabled = True
        with pytest.raises(YouTubeUploadError, match="Không thể lưu ảnh bìa"):
            _save_edits(page, video_id="dQw4w9WgXcQ")

    def test_an_unclickable_save_says_the_cover_did_not_change(self):
        from noveltrans.youtube_upload import _save_edits

        page = _FakeEditPage(save_present=False)
        with pytest.raises(YouTubeUploadError, match="chưa được đổi"):
            _save_edits(page, video_id="dQw4w9WgXcQ")

    def test_disabled_save_alone_is_not_proof_the_image_landed(self):
        """`#save` is disabled on arrival, so "disabled" means nothing on its own. The
        gate that gives it meaning is `_thumbnail_accepted` seeing it ENABLED first —
        exactly the always-true check that made `_finish` report every publish a success."""
        from noveltrans.youtube_upload import _thumbnail_accepted

        page = _FakeEditPage()  # save_enabled False, never touched
        assert _thumbnail_accepted(page, timeout_ms=2_000) is False


class TestUpdateThumbnailOneGuards:
    """`page=None` throughout: any locator access would raise AttributeError instead of
    the error we assert on, which proves nothing was touched."""

    def test_a_part_with_no_video_on_youtube_raises_before_the_page(self, part, cover):
        from noveltrans.youtube_upload import ThumbnailRequest, update_thumbnail_one

        with pytest.raises(YouTubeUploadError, match="chưa có video"):
            update_thumbnail_one(None, ThumbnailRequest(video=part, thumbnail=cover))

    def test_a_hand_marked_part_raises(self, part, cover):
        from noveltrans.youtube_upload import (
            ThumbnailRequest,
            mark_uploaded_by_hand,
            update_thumbnail_one,
        )

        mark_uploaded_by_hand(part)
        with pytest.raises(YouTubeUploadError, match="chưa có video"):
            update_thumbnail_one(None, ThumbnailRequest(video=part, thumbnail=cover))

    def test_a_bad_image_raises_before_the_page(self, part, tmp_path):
        from noveltrans.youtube_upload import ThumbnailRequest, update_thumbnail_one

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        with pytest.raises(YouTubeUploadError):
            update_thumbnail_one(
                None, ThumbnailRequest(video=part, thumbnail=tmp_path / "nope.jpg")
            )


class TestUpdateThumbnailRecord:
    def _page(self):
        from noveltrans.youtube_upload import _EDIT_THUMBNAIL_INPUT_SELS

        return _FakeEditPage(inputs={_EDIT_THUMBNAIL_INPUT_SELS[0]})

    def test_records_when_the_cover_was_pushed_and_which_file(self, part, cover):
        from noveltrans.youtube_upload import ThumbnailRequest, update_thumbnail_one

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        result = update_thumbnail_one(
            self._page(), ThumbnailRequest(video=part, thumbnail=cover)
        )
        state = read_upload_state(part)
        assert result.video_id == "dQw4w9WgXcQ"
        assert state["thumbnail_file"] == cover.name
        assert state["thumbnail_updated_at"]

    def test_never_moves_the_publication_state_machine(self, part, cover):
        """A thumbnail push cannot change whether a video is published, so writing a
        status here would be a lie the rest of the app would then act on."""
        from noveltrans.youtube_upload import ThumbnailRequest, update_thumbnail_one

        write_upload_state(
            part,
            status=STATE_PUBLISHED,
            video_id="dQw4w9WgXcQ",
            published_at="2026-07-01T20:00:00+07:00",
        )
        update_thumbnail_one(self._page(), ThumbnailRequest(video=part, thumbnail=cover))
        state = read_upload_state(part)
        assert state["status"] == STATE_PUBLISHED
        assert state["video_id"] == "dQw4w9WgXcQ"
        assert state["published_at"] == "2026-07-01T20:00:00+07:00"

    def test_a_failed_save_writes_nothing(self, part, cover):
        from noveltrans.youtube_upload import (
            _EDIT_THUMBNAIL_INPUT_SELS,
            ThumbnailRequest,
            update_thumbnail_one,
        )

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        page = _FakeEditPage(inputs={_EDIT_THUMBNAIL_INPUT_SELS[0]}, commits=False)
        with pytest.raises(YouTubeUploadError):
            update_thumbnail_one(page, ThumbnailRequest(video=part, thumbnail=cover))
        assert "thumbnail_updated_at" not in read_upload_state(part)

    def test_cancelling_before_the_save_writes_nothing(self, part, cover):
        from noveltrans.youtube_upload import (
            ThumbnailRequest,
            UploadCancelled,
            update_thumbnail_one,
        )

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        calls = {"n": 0}

        def should_cancel():
            calls["n"] += 1
            return calls["n"] > 1  # true only after the image has gone in

        with pytest.raises(UploadCancelled):
            update_thumbnail_one(
                self._page(),
                ThumbnailRequest(video=part, thumbnail=cover),
                should_cancel=should_cancel,
            )
        assert "thumbnail_updated_at" not in read_upload_state(part)


class TestUpdateThumbnailBatch:
    """One browser for the whole run, and one failure must not cost the others."""

    def _patch(self, monkeypatch, behaviour):
        import noveltrans.youtube_upload as mod

        page = _FakeEditPage()
        monkeypatch.setattr(mod, "_require_playwright", lambda: object())
        launches = []

        def fake_launch(_sync, *, headless):
            launches.append(headless)
            return object(), _FakeContext(page)

        monkeypatch.setattr(mod, "_launch_context", fake_launch)
        monkeypatch.setattr(mod, "_close", lambda *a: None)
        seen = []

        def fake_one(_page, request, **kw):
            seen.append(request.label)
            return behaviour(request)

        monkeypatch.setattr(mod, "update_thumbnail_one", fake_one)
        return launches, seen

    def _requests(self, part, cover, n=3):
        from noveltrans.youtube_upload import ThumbnailRequest

        return [
            ThumbnailRequest(
                video=part, thumbnail=cover, video_id="dQw4w9WgXcQ", label=f"Phần {i + 1}"
            )
            for i in range(n)
        ]

    def test_one_browser_for_the_whole_run(self, monkeypatch, part, cover):
        from noveltrans.youtube_upload import ThumbnailResult, update_thumbnail_batch

        launches, seen = self._patch(
            monkeypatch, lambda r: ThumbnailResult("dQw4w9WgXcQ", "", "now")
        )
        update_thumbnail_batch(self._requests(part, cover))
        assert launches == [False]  # headed, exactly once
        assert seen == ["Phần 1", "Phần 2", "Phần 3"]

    def test_a_failing_part_does_not_abort_the_rest(self, monkeypatch, part, cover):
        from noveltrans.youtube_upload import ThumbnailResult, update_thumbnail_batch

        def behaviour(request):
            if request.label == "Phần 2":
                raise YouTubeUploadError("hỏng")
            return ThumbnailResult("dQw4w9WgXcQ", "", "now")

        _launches, seen = self._patch(monkeypatch, behaviour)
        done: list = []
        update_thumbnail_batch(
            self._requests(part, cover), on_part_done=lambda i, r, e: done.append((i, e))
        )
        assert seen == ["Phần 1", "Phần 2", "Phần 3"]
        assert done[1] == (1, "hỏng")

    def test_needs_login_aborts_the_whole_run(self, monkeypatch, part, cover):
        """Every remaining part would fail identically, so continuing is only noise."""
        from noveltrans.youtube_upload import update_thumbnail_batch

        def behaviour(request):
            raise YouTubeUploadError("chưa đăng nhập", needs_login=True)

        _launches, seen = self._patch(monkeypatch, behaviour)
        with pytest.raises(YouTubeUploadError):
            update_thumbnail_batch(self._requests(part, cover))
        assert seen == ["Phần 1"]

    def test_validates_every_request_before_opening_a_browser(self, monkeypatch, part, tmp_path):
        from noveltrans.youtube_upload import ThumbnailRequest, update_thumbnail_batch

        launches, _seen = self._patch(monkeypatch, lambda r: None)
        with pytest.raises(YouTubeUploadError):
            update_thumbnail_batch(
                [ThumbnailRequest(video=part, thumbnail=tmp_path / "nope.jpg")]
            )
        assert launches == []

    def test_a_dirty_editor_is_discarded_between_parts(self, monkeypatch, part, cover):
        """A part that failed after its image went in leaves Studio's "bỏ thay đổi?"
        guard up, which would block navigation for every part after it."""
        import noveltrans.youtube_upload as mod
        from noveltrans.youtube_upload import ThumbnailResult, update_thumbnail_batch

        self._patch(monkeypatch, lambda r: ThumbnailResult("dQw4w9WgXcQ", "", "now"))
        dismissed: list = []
        monkeypatch.setattr(mod, "_dismiss_unsaved_changes", lambda p: dismissed.append(1))
        update_thumbnail_batch(self._requests(part, cover))
        assert len(dismissed) == 2  # between each pair, not before the first


class _FakeContext:
    """Just enough browser context for `update_thumbnail_batch` to get at its page."""

    def __init__(self, page):
        self.pages = [page]
        page.set_default_timeout = lambda ms: None


# ------------------------------------------------ 039: playlists (read + sync)
#
# The clear phase REMOVES things from a playlist viewers may be watching, so the tests
# that matter most here are the ones proving it can't run past a failure into the add
# phase, and that what was in the playlist is captured before anything is destroyed.


class _FakePlaylistPage:
    """Studio's playlist page: a list of entries, each removable via a row menu.

    `stuck` models the failure this feature is designed around — a removal that silently
    no-ops, e.g. because the menu selector drifted.
    """

    def __init__(self, *, entries=(), stuck=False, playlists=()):
        self.entries = list(entries)
        self.stuck = stuck
        self.playlists = list(playlists)
        self.url = "https://studio.youtube.com/channel/UC123/playlists"
        self.clicked: list = []
        self.removed = 0

    def goto(self, url, wait_until=None):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def set_default_timeout(self, ms):
        pass

    @property
    def keyboard(self):
        page = self

        class _Kb:
            def press(self, key):
                page.clicked.append(f"key:{key}")

        return _Kb()

    def locator(self, selector):
        from noveltrans.youtube_upload import (
            _PLAYLIST_ENTRY_SEL,
            _PLAYLIST_REMOVE_TEXTS,
        )

        page = self
        is_entry = selector == _PLAYLIST_ENTRY_SEL
        is_remove = any(t in selector for t in _PLAYLIST_REMOVE_TEXTS)

        class _Loc:
            def __init__(self, index=0):
                self._index = index

            @property
            def first(self):
                # keeps this locator's row index, as `row.locator(x).first` does live
                return self

            def nth(self, i):
                return _Loc(i)

            def count(self):
                return len(page.entries) if is_entry else 0

            def wait_for(self, state=None, timeout=None):
                return None

            def inner_text(self, timeout=None):
                if is_entry and self._index < len(page.entries):
                    return page.entries[self._index]
                return ""

            def is_visible(self, timeout=None):
                return is_remove

            def click(self, timeout=None):
                page.clicked.append(selector)
                # Clicking "Xoá khỏi danh sách phát" is the one click that mutates the
                # playlist. `stuck` models the drifted-selector case: the click lands, the
                # entry stays — which is exactly what the runaway guard exists to catch.
                if is_remove and not page.stuck and page.entries:
                    page.entries.pop(0)
                    page.removed += 1

            def locator(self, sub):
                return _Loc(self._index)

        return _Loc()


class TestSetPlaylistReportsFailures:
    """`_set_playlist` used to fail silently on a drifted picker DOM — the video just
    ended up in no playlist with no explanation, which read as the run "freezing" on
    the playlist step. It must now report why via `on_progress` instead."""

    class _TriggerNeverFound:
        """Every lookup times out — simulates Studio's playlist-picker DOM having moved."""

        @property
        def first(self):
            return self

        @property
        def last(self):
            return self

        def wait_for(self, state=None, timeout=None):
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not found")

        def is_visible(self, timeout=None):
            return False

        def click(self):
            pass

    class _Page:
        def __init__(self, loc):
            self._loc = loc

        def locator(self, selector):
            return self._loc

        def wait_for_timeout(self, ms):
            pass

    def test_missing_trigger_reports_and_returns(self):
        from noveltrans.youtube_upload import _set_playlist

        messages: list[str] = []
        _set_playlist(
            self._Page(self._TriggerNeverFound()), "Truyện A", on_progress=messages.append
        )

        assert messages
        assert "bỏ qua" in messages[0]

    def test_missing_trigger_is_silent_without_on_progress(self):
        """No callback given (the default) must not raise — best-effort stays best-effort."""
        from noveltrans.youtube_upload import _set_playlist

        _set_playlist(self._Page(self._TriggerNeverFound()), "Truyện A")  # does not raise


class TestClearPlaylistSafety:
    def test_captures_what_was_in_it_before_removing_anything(self):
        """Clearing is destructive and per-row. If it fails halfway, this list is the only
        thing that turns "your playlist is broken" into something actionable."""
        from noveltrans.youtube_upload import _playlist_entries

        page = _FakePlaylistPage(entries=["Phần 1", "Phần 2", "Phần 3"])
        assert _playlist_entries(page) == ["Phần 1", "Phần 2", "Phần 3"]

    def test_a_removal_that_never_reduces_the_count_raises_instead_of_looping(self):
        """A drifted menu selector must stop the run, not hammer Studio until someone
        kills the browser."""
        from noveltrans.youtube_upload import _clear_playlist

        page = _FakePlaylistPage(entries=["Phần 1", "Phần 2"], stuck=True)
        with pytest.raises(YouTubeUploadError, match="Không gỡ được"):
            _clear_playlist(page)

    def test_the_error_says_how_many_are_left(self):
        from noveltrans.youtube_upload import _clear_playlist

        page = _FakePlaylistPage(entries=["Phần 1", "Phần 2"], stuck=True)
        with pytest.raises(YouTubeUploadError, match="còn 2"):
            _clear_playlist(page)

    def test_it_removes_every_entry_and_reports_the_count(self):
        """The success path of the loop: it re-reads the first row each time, because the
        DOM re-renders after every removal and held row handles go stale."""
        from noveltrans.youtube_upload import _clear_playlist

        page = _FakePlaylistPage(entries=["Phần 1", "Phần 2", "Phần 3"])
        assert _clear_playlist(page) == 3
        assert page.entries == []

    def test_progress_is_reported_per_removal(self):
        from noveltrans.youtube_upload import _clear_playlist

        page = _FakePlaylistPage(entries=["a", "b"])
        seen: list = []
        _clear_playlist(page, on_progress=seen.append)
        assert len(seen) == 2

    def test_cancelling_stops_the_clear_partway(self):
        from noveltrans.youtube_upload import UploadCancelled, _clear_playlist

        page = _FakePlaylistPage(entries=["a", "b", "c"])
        calls = {"n": 0}

        def should_cancel():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(UploadCancelled):
            _clear_playlist(page, should_cancel=should_cancel)
        assert page.entries, "stopped before emptying it"

    def test_an_empty_playlist_clears_to_zero_without_touching_anything(self):
        from noveltrans.youtube_upload import _clear_playlist

        page = _FakePlaylistPage(entries=[])
        assert _clear_playlist(page) == 0
        assert page.clicked == []

    def test_emptiness_is_read_from_the_dom_not_assumed(self):
        from noveltrans.youtube_upload import _playlist_is_empty

        assert _playlist_is_empty(_FakePlaylistPage(entries=[])) is True
        assert _playlist_is_empty(_FakePlaylistPage(entries=["Phần 1"])) is False


class TestPlaylistSyncRequest:
    def test_a_part_never_uploaded_is_rejected(self, part):
        from noveltrans.youtube_upload import PlaylistSyncRequest

        with pytest.raises(YouTubeUploadError, match="chưa có video"):
            PlaylistSyncRequest(video=part, label="Phần 1").validate()

    def test_a_part_with_a_video_id_resolves(self, part):
        from noveltrans.youtube_upload import PlaylistSyncRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        request = PlaylistSyncRequest(video=part, label="Phần 1")
        request.validate()
        assert request.resolve() == "dQw4w9WgXcQ"


class TestSyncPlaylistBatch:
    """The phase ordering is the whole safety story: clear → verify → add, never overlap."""

    def _patch(self, monkeypatch, *, cleared=2, empty_after=True):
        import noveltrans.youtube_upload as mod

        monkeypatch.setattr(mod, "_require_playwright", lambda: object())
        page = _FakePlaylistPage()
        monkeypatch.setattr(
            mod, "_launch_context", lambda _s, *, headless: (object(), _FakeContext(page))
        )
        monkeypatch.setattr(mod, "_close", lambda *a: None)
        monkeypatch.setattr(mod, "_open_playlist_page", lambda p, name: None)
        monkeypatch.setattr(mod, "_playlist_entries", lambda p: ["cũ 1", "cũ 2"])
        monkeypatch.setattr(mod, "_clear_playlist", lambda p, **kw: cleared)
        monkeypatch.setattr(mod, "_playlist_is_empty", lambda p: empty_after)
        added: list = []
        monkeypatch.setattr(
            mod, "_add_to_playlist", lambda p, vid, name, **kw: added.append(vid)
        )
        return added

    def _requests(self, part, n=3):
        from noveltrans.youtube_upload import PlaylistSyncRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        return [
            PlaylistSyncRequest(video=part, video_id=f"vid{i}xxxxxxx", label=f"Phần {i + 1}")
            for i in range(n)
        ]

    def test_videos_are_added_in_request_order(self, monkeypatch, part):
        from noveltrans.youtube_upload import sync_playlist_batch

        added = self._patch(monkeypatch)
        result = sync_playlist_batch("Truyện A", self._requests(part))
        assert added == ["vid0xxxxxxx", "vid1xxxxxxx", "vid2xxxxxxx"]
        assert result["added"] == ["Phần 1", "Phần 2", "Phần 3"]

    def test_a_clear_that_does_not_verify_empty_never_reaches_the_add_phase(
        self, monkeypatch, part
    ):
        """**The load-bearing test.** A half-cleared playlist that then gets a partial
        re-add is worse than either end state and impossible to reason about after."""
        from noveltrans.youtube_upload import sync_playlist_batch

        added = self._patch(monkeypatch, empty_after=False)
        with pytest.raises(YouTubeUploadError, match="chưa trống"):
            sync_playlist_batch("Truyện A", self._requests(part))
        assert added == []

    def test_the_failure_reports_what_was_in_the_playlist(self, monkeypatch, part):
        from noveltrans.youtube_upload import sync_playlist_batch

        self._patch(monkeypatch, cleared=1, empty_after=False)
        with pytest.raises(YouTubeUploadError, match="1/2"):
            sync_playlist_batch("Truyện A", self._requests(part))

    def test_the_previous_contents_come_back_in_the_result(self, monkeypatch, part):
        from noveltrans.youtube_upload import sync_playlist_batch

        self._patch(monkeypatch)
        result = sync_playlist_batch("Truyện A", self._requests(part))
        assert result["had"] == ["cũ 1", "cũ 2"]
        assert result["removed"] == 2

    def test_an_empty_playlist_name_is_refused_before_the_browser_opens(
        self, monkeypatch, part
    ):
        import noveltrans.youtube_upload as mod
        from noveltrans.youtube_upload import sync_playlist_batch

        launched = []
        monkeypatch.setattr(mod, "_require_playwright", lambda: launched.append(1))
        with pytest.raises(YouTubeUploadError, match="Chưa chọn danh sách phát"):
            sync_playlist_batch("  ", self._requests(part))
        assert launched == []

    def test_a_part_with_no_video_is_refused_before_the_browser_opens(
        self, monkeypatch, part
    ):
        import noveltrans.youtube_upload as mod
        from noveltrans.youtube_upload import PlaylistSyncRequest, sync_playlist_batch

        launched = []
        monkeypatch.setattr(mod, "_require_playwright", lambda: launched.append(1))
        with pytest.raises(YouTubeUploadError, match="chưa có video"):
            sync_playlist_batch("A", [PlaylistSyncRequest(video=part, label="Phần 1")])
        assert launched == []


class _FakeNavPage:
    """A browser whose pages either mount, show Studio's error page, or are gone.

    `behaviour` maps a URL substring to one of "ok" / "error" / "missing" / "closed",
    so a test can say "the first two URL shapes fail, the third works" — which is exactly
    the situation that broke the live run.
    """

    def __init__(self, behaviour, *, url=""):
        self.behaviour = dict(behaviour)
        self.url = url
        self.visited: list = []

    def _mode(self) -> str:
        for key, mode in self.behaviour.items():
            if key in (self.url or ""):
                return mode
        return "missing"

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url
        mode = self._mode()
        if mode == "closed":
            raise RuntimeError("Target page, context or browser has been closed")
        if mode == "missing":
            raise RuntimeError("net::ERR_ABORTED")

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            def is_visible(self, timeout=None):
                # the error-text probe is the only is_visible caller in this path
                if any(t in selector for t in ("Oops, something went wrong", "Rất tiếc")):
                    return page._mode() == "error"
                return False

        return _Loc()


class TestPlaylistsPageNavigation:
    """Regression: the first guessed URL (`/channel/<id>/playlists`) renders Studio's
    "Oops, something went wrong" page — a *successful* navigation with a normal DOM, so
    it burned the whole 45s wait and then blamed the selectors."""

    def test_it_falls_through_an_error_page_to_a_url_that_works(self):
        from noveltrans.youtube_upload import _goto_playlists_page

        page = _FakeNavPage({"/content/playlists": "error", "/videos/playlists": "ok"})
        landed = _goto_playlists_page(page, "UC123")
        assert "/videos/playlists" in landed
        assert len(page.visited) == 2  # the bad one was rejected, not waited out

    def test_the_studio_error_page_is_recognised_not_waited_out(self):
        from noveltrans.youtube_upload import _page_shows_studio_error

        assert _page_shows_studio_error(_FakeNavPage({"x": "error"}, url="x")) is True
        assert _page_shows_studio_error(_FakeNavPage({"x": "ok"}, url="x")) is False

    def test_every_candidate_failing_names_them_all(self):
        """The error has to be actionable: which URLs were tried, and the fact that typing
        a name by hand still works."""
        from noveltrans.youtube_upload import _goto_playlists_page

        page = _FakeNavPage({})  # nothing matches -> "missing" everywhere
        with pytest.raises(YouTubeUploadError) as excinfo:
            _goto_playlists_page(page, "UC123")
        message = str(excinfo.value)
        assert "studio.youtube.com" in message
        assert "youtube.com/feed/playlists" in message
        assert "bằng tay" in message

    def test_it_tries_youtube_com_when_every_studio_shape_fails(self):
        """youtube.com proper is a different, far more stable surface — the point of
        having it last is that it isn't subject to Studio's reshuffles."""
        from noveltrans.youtube_upload import _goto_playlists_page

        page = _FakeNavPage({"studio.youtube.com": "error", "feed/playlists": "ok"})
        assert "feed/playlists" in _goto_playlists_page(page, "UC123")

    def test_a_logged_out_bounce_raises_needs_login(self):
        from noveltrans.youtube_upload import _goto_playlists_page

        page = _FakeNavPage({"accounts.google.com": "missing"})
        page.goto = lambda url, wait_until=None: setattr(
            page, "url", "https://accounts.google.com/signin"
        )
        with pytest.raises(YouTubeUploadError) as excinfo:
            _goto_playlists_page(page, "UC123")
        assert excinfo.value.needs_login is True

    def test_closing_the_window_reads_as_one_sentence(self):
        """Regression: this surfaced live as a raw
        TargetClosedError('Locator.wait_for: Target page, context or browser has been
        closed'). Closing a window is a normal thing for a person to do."""
        from noveltrans.youtube_upload import _goto_playlists_page

        page = _FakeNavPage({"": "closed"})
        with pytest.raises(YouTubeUploadError, match="đã bị đóng"):
            _goto_playlists_page(page, "UC123")

    def test_browser_gone_recognises_playwright_and_message_forms(self):
        from noveltrans.youtube_upload import _browser_gone

        class TargetClosedError(Exception):
            pass

        assert _browser_gone(TargetClosedError("boom")) is True
        assert _browser_gone(RuntimeError("... has been closed")) is True
        assert _browser_gone(RuntimeError("something else")) is False


class TestPlaylistExtractionIsWideNet:
    """Regression from the second live run.

    `/content/playlists` — the CORRECT url, confirmed against the channel — loaded fine,
    but the code gated on a `ytcp-playlist-section` container that does not exist and
    walked away from a working page, then reported all four URLs as failures. The gate was
    the bug, not the URL.
    """

    class _Page:
        """A page that loaded fine and has rows under a tag we didn't predict."""

        def __init__(self, *, rows=(), row_tag="ytcp-unknown-row", links=(), tags=None):
            self.rows = list(rows)
            self.row_tag = row_tag
            # links are (href, text) — the href is the grouping key, as it is live: a row
            # points at the same playlist from its thumbnail, its title and its count.
            self.links = [
                x if isinstance(x, tuple) else (f"/playlist/PL{i:010d}/videos", x)
                for i, x in enumerate(links)
            ]
            self.tags = tags or ["ytcp-unknown-row×3", "ytcp-app×1"]
            self.url = "https://studio.youtube.com/channel/UC1/content/playlists"

        def goto(self, url, wait_until=None):
            self.url = url

        def wait_for_load_state(self, state=None, timeout=None):
            pass

        def wait_for_timeout(self, ms):
            pass

        def evaluate(self, script):
            return self.tags

        def locator(self, selector):
            page = self
            from noveltrans.youtube_upload import _PLAYLIST_LINK_SEL

            if selector == page.row_tag:
                items = page.rows
            elif selector == _PLAYLIST_LINK_SEL:
                items = page.links
            else:
                items = []

            class _Loc:
                def __init__(self, i=0):
                    self._i = i

                @property
                def first(self):
                    return self

                def nth(self, i):
                    return _Loc(i)

                def count(self):
                    return len(items)

                def inner_text(self, timeout=None):
                    if self._i >= len(items):
                        return ""
                    item = items[self._i]
                    return item[1] if isinstance(item, tuple) else item

                def get_attribute(self, name, timeout=None):
                    if self._i >= len(items):
                        return None
                    item = items[self._i]
                    return item[0] if isinstance(item, tuple) and name == "href" else None

                def is_visible(self, timeout=None):
                    return False

                def locator(self, sub):
                    return _Loc(self._i)

            return _Loc()

    def test_a_page_with_an_unknown_row_tag_still_yields_titles_via_links(self):
        """The anchor fallback is the point: an href is structural and survives the
        component renames that break tag- and id-based selectors."""
        from noveltrans.youtube_upload import _extract_playlist_titles

        page = self._Page(links=["Truyện A", "Truyện B"])
        assert _extract_playlist_titles(page) == ["Truyện A", "Truyện B"]

    def test_a_known_row_tag_is_preferred_over_the_link_fallback(self):
        from noveltrans.youtube_upload import _extract_playlist_titles

        page = self._Page(rows=["Từ hàng"], row_tag="ytcp-playlist-row", links=["Từ link"])
        assert _extract_playlist_titles(page) == ["Từ hàng"]

    def test_navigation_no_longer_requires_a_container_element(self):
        """The whole regression in one assertion: a page that loaded and isn't the error
        page is accepted, whatever components it happens to be built from."""
        from noveltrans.youtube_upload import _goto_playlists_page

        page = self._Page()
        landed = _goto_playlists_page(page, "UC1")
        assert "/content/playlists" in landed

    def test_duplicate_titles_collapse(self):
        from noveltrans.youtube_upload import _extract_playlist_titles

        page = self._Page(links=["Truyện A", "Truyện A", "Truyện B"])
        assert _extract_playlist_titles(page) == ["Truyện A", "Truyện B"]

    def test_an_unreadable_page_reports_the_components_it_found(self):
        """"Giao diện có thể đã thay đổi" is useless on its own. Naming the components is
        the difference between a bug report and a fix."""
        from noveltrans.youtube_upload import _dom_inventory

        page = self._Page(tags=["ytcp-mystery-row×7", "ytcp-app×1"])
        assert "ytcp-mystery-row×7" in _dom_inventory(page)

    def test_the_inventory_never_raises_on_a_hostile_page(self):
        from noveltrans.youtube_upload import _dom_inventory

        class _Broken:
            def evaluate(self, script):
                raise RuntimeError("nope")

        assert "không đọc được" in _dom_inventory(_Broken())


class TestPlaylistTitlesAreNamesNotCounts:
    """Regression from live run 3: the picker offered "No videos / 75 videos / 31 videos /
    10 episodes" — every row's video-count cell, read as if it were the playlist name."""

    def test_the_count_cell_never_wins_over_the_name(self):
        from noveltrans.youtube_upload import _best_title

        assert _best_title(["75 videos", "Truyện Ma", "Công khai"]) == "Truyện Ma"
        assert _best_title(["No videos", "Chào Mừng Đến Với Phòng Livestream"]) == (
            "Chào Mừng Đến Với Phòng Livestream"
        )

    def test_every_metadata_shape_seen_live_is_refused(self):
        from noveltrans.youtube_upload import _is_playlist_metadata

        for junk in (
            "No videos", "75 videos", "31 videos", "10 episodes",  # the reported dropdown
            "Không có video", "12 video", "5 tập",
            "Công khai", "Riêng tư", "Unlisted",
            "2026-07-29", "  ", "Xem tất cả",
        ):
            assert _is_playlist_metadata(junk), junk

    def test_a_real_title_is_never_mistaken_for_metadata(self):
        from noveltrans.youtube_upload import _is_playlist_metadata

        for title in (
            "Chào Mừng Đến Với Phòng Livestream Ác Mộng",
            "Truyện Ma",
            "10 Năm Cô Đơn",  # starts with a number but isn't a count
            "Tập Truyện Ngắn",
        ):
            assert not _is_playlist_metadata(title), title

    def test_links_are_grouped_by_playlist_so_the_name_wins(self):
        """The row's thumbnail, title and count all link to the same playlist. Grouping by
        the id in the href is what says which strings belong together — reading each
        anchor independently is exactly how the counts got in."""
        from noveltrans.youtube_upload import _titles_by_playlist_link

        page = TestPlaylistExtractionIsWideNet._Page(
            links=[
                ("/playlist/PLaaaaaaaaaa/videos", ""),          # thumbnail link, no text
                ("/playlist/PLaaaaaaaaaa/videos", "Truyện Ma"),  # the title
                ("/playlist/PLaaaaaaaaaa/videos", "75 videos"),  # the count
                ("/playlist/PLbbbbbbbbbb/videos", "10 episodes"),
                ("/playlist/PLbbbbbbbbbb/videos", "Truyện Kinh Dị"),
            ]
        )
        assert _titles_by_playlist_link(page) == ["Truyện Ma", "Truyện Kinh Dị"]

    def test_a_playlist_with_only_metadata_text_is_dropped_not_named_after_its_count(self):
        """Better to omit a playlist than to list "No videos" as one."""
        from noveltrans.youtube_upload import _titles_by_playlist_link

        page = TestPlaylistExtractionIsWideNet._Page(
            links=[("/playlist/PLaaaaaaaaaa/videos", "No videos")]
        )
        assert _titles_by_playlist_link(page) == []

    def test_row_extraction_also_skips_the_count_line(self):
        from noveltrans.youtube_upload import _extract_playlist_titles

        page = TestPlaylistExtractionIsWideNet._Page(
            rows=["75 videos\nTruyện Ma\nCông khai"], row_tag="ytcp-playlist-row"
        )
        assert _extract_playlist_titles(page) == ["Truyện Ma"]


# A real cue line, not a placeholder: `_cue_probe` searches the editor for the .srt's own
# first line, and a one-character cue is too short to search for.
_SRT_TEXT = "1\n00:00:00,000 --> 00:00:01,000\nChương một: người khách lạ\n"


class TestSubtitleRequestValidation:
    """Feature 044 — uploading the .srt as a YouTube caption track."""

    def test_a_part_never_uploaded_is_rejected(self, part, tmp_path):
        from noveltrans.youtube_upload import SubtitleRequest

        srt = tmp_path / "a.srt"
        srt.write_text(_SRT_TEXT, encoding="utf-8")
        with pytest.raises(YouTubeUploadError, match="chưa có video"):
            SubtitleRequest(video=part, subtitle=srt, label="Phần 1").validate()

    def test_a_missing_srt_is_rejected(self, part, tmp_path):
        from noveltrans.youtube_upload import SubtitleRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        with pytest.raises(YouTubeUploadError, match="phụ đề"):
            SubtitleRequest(video=part, subtitle=tmp_path / "nope.srt").validate()

    def test_an_empty_srt_is_rejected(self, part, tmp_path):
        from noveltrans.youtube_upload import SubtitleRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        srt = tmp_path / "a.srt"
        srt.write_text("", encoding="utf-8")
        with pytest.raises(YouTubeUploadError, match="rỗng"):
            SubtitleRequest(video=part, subtitle=srt).validate()

    def test_a_good_request_resolves_its_video_id(self, part, tmp_path):
        from noveltrans.youtube_upload import SubtitleRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        srt = tmp_path / "a.srt"
        srt.write_text(_SRT_TEXT, encoding="utf-8")
        request = SubtitleRequest(video=part, subtitle=srt)
        request.validate()
        assert request.resolve() == "dQw4w9WgXcQ"


class _NativeFileWindow(RuntimeError):
    """What a live run gets for clicking Continue outside `expect_file_chooser`.

    Nothing raises this in production — the browser simply stops responding, forever, with
    the OS file window on top of it. Raising here is how a test can see the hang at all.
    """


class _FakeSubtitlePage:
    """A subtitles page: a file input appears only after N clicks, if at all.

    Models the one thing the first live run proved matters: Continue opens an OS file
    window. `native_chooser=True` makes this page behave like Studio does — clicking
    Continue while an interception is armed yields a chooser, and clicking it unguarded
    raises `_NativeFileWindow` where the real browser would just hang.
    """

    def __init__(self, *, clicks_to_input=1, published=True, lands_on=None,
                 language_gate=False, gate_clears=True, native_chooser=False,
                 cue="", editor_polls=0, caption_fields=0):
        self.clicks_to_input = clicks_to_input
        # The caption editor: `cue` is the text that only appears once it has loaded, after
        # `editor_polls` polls. `caption_fields` is the structural fallback's answer.
        self.cue = cue
        self.editor_polls = editor_polls
        self.caption_fields = caption_fields
        self.published = published
        self.lands_on = lands_on
        # The gate seen live: a video with no language set shows "Set language" + Confirm
        # instead of the subtitles table.
        self.language_gate = language_gate
        self.gate_clears = gate_clears
        self.native_chooser = native_chooser
        self.arming = False  # inside an expect_file_chooser block
        self.chooser_opened = False
        self.url = ""
        self.clicks = 0
        self.sent: list = []
        self.clicked: list = []  # the selectors clicked, in order
        self.goto_urls: list = []

    def expect_file_chooser(self, timeout=None):
        page = self

        class _Expect:
            def __enter__(self):
                page.arming = True
                return self

            def __exit__(self, *exc):
                page.arming = False
                return False

            @property
            def value(self):
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                # No OS window on a page whose input already took the file — Studio moves
                # straight on, and the expectation times out. That is a success, not a bug.
                if not page.chooser_opened:
                    raise PlaywrightTimeoutError("no file chooser")
                return _Chooser(page)

        class _Chooser:
            def __init__(self, page):
                self.page = page

            def set_files(self, path):
                self.page.sent.append(path)

        return _Expect()

    def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        self.url = self.lands_on if self.lands_on is not None else url

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        if self.editor_polls > 0:
            self.editor_polls -= 1  # the editor finishes loading while we wait

    def evaluate(self, script):
        # `_editor_caption_fields` counts filled boxes; everything else wants labels.
        if "let n = 0" in script:
            return self.caption_fields
        return ["ytcp-mystery×3"]

    def locator(self, selector):
        page = self
        is_file = "input[type='file']" in selector
        # `body:has-text(...)` is the Studio-error probe — this page is NOT an error page,
        # so it must read False. Answering True there made every URL candidate look broken.
        # Keyed off the error TEXT, not the selector shape: the probe moved from
        # `body:has-text(...)` to `:text(...)` and a prefix check silently stopped working.
        is_error_probe = any(
            t in selector for t in ("Oops, something went wrong", "Rất tiếc")
        )
        # `:text(...)` reaches the same controls as `…:has-text(…)` — the tag-agnostic
        # click added after the live run uses the former.
        # `:text-is(...)` is the exact form `_choose_with_timing` prefers; it reaches the
        # same controls as the other two.
        texty = (
            "has-text" in selector
            or selector.startswith(":text(")
            or selector.startswith(":text-is(")
        )
        is_confirm = texty and any(t in selector for t in ("Confirm", "Xác nhận"))
        is_language = texty and any(
            t in selector for t in ("Tiếng Việt", "Vietnamese", "Set language")
        )
        is_state = "has-text" in selector and any(
            t in selector for t in ("Đã xuất bản", "Published")
        )
        is_continue = texty and any(t in selector for t in ("Continue", "Tiếp tục"))
        is_button = texty and not is_state and not is_error_probe

        class _Loc:
            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            def wait_for(self, state=None, timeout=None):
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                if is_file and page.clicks >= page.clicks_to_input:
                    return
                raise PlaywrightTimeoutError(selector)

            def is_visible(self, timeout=None):
                if is_error_probe:
                    return False
                if page.cue and page.cue in selector:
                    return page.editor_polls <= 0  # the editor is still loading
                if is_confirm:
                    return page.language_gate
                if is_language:
                    return page.language_gate  # only offered while the gate is up
                if is_state:
                    return page.published
                return is_button

            def inner_text(self, timeout=None):
                # Whatever the selector asked for is what this element reads.
                start = selector.find('("') + 2
                return selector[start : selector.find('")', start)]

            def locator(self, sub):
                # Scoping a row and then searching inside it: the row selector narrows
                # nothing on this page, so the inner selector is the whole question.
                return page.locator(sub)

            def click(self):
                page.clicks += 1
                page.clicked.append(selector)
                if is_confirm and page.gate_clears:
                    page.language_gate = False
                if is_continue and page.native_chooser:
                    if not page.arming:
                        raise _NativeFileWindow(selector)  # a live run would hang here
                    page.chooser_opened = True

            def set_input_files(self, path):
                page.sent.append(path)

        return _Loc()


class TestSubtitleUploadFlow:
    def _request(self, part, tmp_path):
        from noveltrans.youtube_upload import SubtitleRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        srt = tmp_path / "a.srt"
        srt.write_text(_SRT_TEXT, encoding="utf-8")
        return SubtitleRequest(video=part, subtitle=srt, label="Phần 1")

    def test_it_clicks_through_to_the_file_input_and_sends_the_srt(self, part, tmp_path):
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=2)
        assert upload_subtitle_one(page, self._request(part, tmp_path)) == "dQw4w9WgXcQ"
        assert page.sent == [str(tmp_path / "a.srt")]

    def test_continue_is_never_clicked_outside_the_chooser_interception(
        self, part, tmp_path
    ):
        """The first live hang, and the reason this flow exists in its current shape.

        Studio's Continue opens the OS file window. Playwright can only intercept that
        while `expect_file_chooser` is armed; unguarded, the native window sits on top of
        the browser and the run waits for it forever — no exception, no timeout, just a
        stopped app. The old code set an input directly and *then* clicked Continue, so a
        page whose real picker lives behind the button hung every time.
        """
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=2, native_chooser=True)
        assert upload_subtitle_one(page, self._request(part, tmp_path)) == "dQw4w9WgXcQ"
        assert page.sent  # and no _NativeFileWindow escaped

    def test_the_file_still_lands_when_only_the_os_window_can_take_it(
        self, part, tmp_path
    ):
        """No reachable input at all — the interception is then the only way in."""
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=99, native_chooser=True)
        assert upload_subtitle_one(page, self._request(part, tmp_path)) == "dQw4w9WgXcQ"
        assert page.sent == [str(tmp_path / "a.srt")]

    def test_never_reaching_a_file_input_reports_the_dom(self, part, tmp_path):
        """The 039 lesson: a failure has to name the components actually on the page, not
        say "giao diện có thể đã thay đổi" and leave the reader guessing."""
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=99)
        with pytest.raises(YouTubeUploadError, match="ytcp-mystery"):
            upload_subtitle_one(page, self._request(part, tmp_path))
        assert page.sent == []

    def test_the_draft_is_opened_for_editing_before_publish_is_pressed(
        self, part, tmp_path
    ):
        """MEASURED live: the upload only creates a draft — Studio says "Vietnamese
        subtitles uploaded" and the track stays unpublished. Publish lives in the caption
        editor, behind "Edit subtitles"; pressing it on the translations page pressed
        nothing at all."""
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=1)
        upload_subtitle_one(page, self._request(part, tmp_path))
        edit = next(i for i, s in enumerate(page.clicked) if "Edit subtitles" in s)
        # Vietnamese first in `_SUB_PUBLISH_TEXTS`, so that is the label tried first.
        publish = next(
            i for i, s in enumerate(page.clicked) if "Xuất bản" in s or "Publish" in s
        )
        assert edit < publish

    def test_publish_waits_for_the_editor_to_load_the_cues(self, part, tmp_path):
        """MEASURED live: Publish on a still-loading editor publishes an EMPTY track and
        Studio answers "no subtitles". The click is not a no-op, so being early here costs
        the track — it is the one step where slow beats eager."""
        from noveltrans.youtube_upload import _cue_probe, upload_subtitle_one

        request = self._request(part, tmp_path)
        page = _FakeSubtitlePage(
            clicks_to_input=1, cue=_cue_probe(request.subtitle), editor_polls=3
        )
        upload_subtitle_one(page, request)
        assert page.editor_polls == 0  # it really waited, rather than pressing on
        assert any("Xuất bản" in s or "Publish" in s for s in page.clicked)

    def test_an_editor_that_never_loads_is_not_published_at_all(self, part, tmp_path):
        """The safe end of the same rule: no cues on screen, no Publish click. The .srt is
        already in Studio as a draft, so refusing costs nothing and pressing costs the
        track."""
        from noveltrans.youtube_upload import _cue_probe, upload_subtitle_one

        request = self._request(part, tmp_path)
        page = _FakeSubtitlePage(
            clicks_to_input=1, cue=_cue_probe(request.subtitle), editor_polls=10**6
        )
        with pytest.raises(YouTubeUploadError, match="không tải xong"):
            upload_subtitle_one(page, request)
        assert not any("Xuất bản" in s or "Publish" in s for s in page.clicked)
        assert "subtitle_uploaded_at" not in read_upload_state(part)

    def test_a_dense_caption_table_is_enough_when_the_cue_cannot_be_searched(
        self, part, tmp_path
    ):
        """The structural fallback, for a .srt whose first line is too short to probe with:
        a loaded editor carries a text box and two timestamps per cue, the list page it was
        opened from carries none."""
        from noveltrans.youtube_upload import upload_subtitle_one

        request = self._request(part, tmp_path)
        request.subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nÀ\n", encoding="utf-8")
        page = _FakeSubtitlePage(clicks_to_input=1, cue="never-visible", caption_fields=9)
        upload_subtitle_one(page, request)
        assert any("Xuất bản" in s or "Publish" in s for s in page.clicked)

    def test_an_unconfirmed_publish_says_the_track_is_still_a_draft(self, part, tmp_path):
        """The failure a user can act on: the file IS in Studio, it just is not live. The
        message has to say that and name the two clicks that finish it by hand."""
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=1, published=False)
        with pytest.raises(YouTubeUploadError, match="bản nháp"):
            upload_subtitle_one(page, self._request(part, tmp_path))

    def test_an_unconfirmed_publish_is_a_failure_not_a_success(self, part, tmp_path):
        """Same posture as 034's save gate: report a track because Studio says it exists,
        never because a click was made."""
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=1, published=False)
        with pytest.raises(YouTubeUploadError, match="không xác nhận"):
            upload_subtitle_one(page, self._request(part, tmp_path))

    def test_nothing_is_recorded_when_the_publish_is_not_confirmed(self, part, tmp_path):
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=1, published=False)
        with pytest.raises(YouTubeUploadError):
            upload_subtitle_one(page, self._request(part, tmp_path))
        assert "subtitle_uploaded_at" not in read_upload_state(part)

    def test_success_records_only_the_subtitle_fields(self, part, tmp_path):
        """A caption upload cannot change publication state, so writing one would be a lie."""
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(clicks_to_input=1)
        upload_subtitle_one(page, self._request(part, tmp_path))
        state = read_upload_state(part)
        assert state["status"] == STATE_PUBLISHED
        assert state["video_id"] == "dQw4w9WgXcQ"
        assert state["subtitle_uploaded_at"] and state["subtitle_file"] == "a.srt"

    def test_a_logged_out_bounce_raises_needs_login(self, part, tmp_path):
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(lands_on="https://accounts.google.com/signin")
        with pytest.raises(YouTubeUploadError) as excinfo:
            upload_subtitle_one(page, self._request(part, tmp_path))
        assert excinfo.value.needs_login is True

    def test_it_tries_the_next_url_shape_when_studio_errors(self, part, tmp_path):
        """Built in from the start this time rather than after three failed live runs."""
        import noveltrans.youtube_upload as mod

        page = _FakeSubtitlePage(clicks_to_input=1)
        seen = {"n": 0}

        def fake_error(_p):
            seen["n"] += 1
            return seen["n"] == 1  # the first URL renders Studio's error page

        original = mod._page_shows_studio_error
        mod._page_shows_studio_error = fake_error
        try:
            mod.upload_subtitle_one(page, self._request(part, tmp_path))
        finally:
            mod._page_shows_studio_error = original
        assert len(page.goto_urls) == 2


class TestSubtitleLanguageGate:
    """Regression from the first live run.

    `/translations` loaded correctly, but the video had no language set, so YouTube showed
    a "Set language" dropdown and a Confirm button INSTEAD of the subtitles table. There is
    no file input until that is answered — so the click ladder found nothing and blamed the
    selectors for a page that was working exactly as designed.
    """

    def _request(self, part, tmp_path):
        from noveltrans.youtube_upload import SubtitleRequest

        write_upload_state(part, status=STATE_PUBLISHED, video_id="dQw4w9WgXcQ")
        srt = tmp_path / "a.srt"
        srt.write_text(_SRT_TEXT, encoding="utf-8")
        return SubtitleRequest(video=part, subtitle=srt, label="Phần 1")

    def test_the_gate_is_detected_by_its_confirm_button(self):
        from noveltrans.youtube_upload import _subtitle_language_gate

        assert _subtitle_language_gate(_FakeSubtitlePage(language_gate=True)) is True
        assert _subtitle_language_gate(_FakeSubtitlePage(language_gate=False)) is False

    def test_it_answers_the_gate_and_carries_on_to_upload(self, part, tmp_path):
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(language_gate=True, clicks_to_input=3)
        assert upload_subtitle_one(page, self._request(part, tmp_path)) == "dQw4w9WgXcQ"
        assert page.sent == [str(tmp_path / "a.srt")]
        assert page.language_gate is False  # it was actually answered, not skipped

    def test_a_gate_that_will_not_clear_says_what_to_do_by_hand(self, part, tmp_path):
        """Refuses with instructions rather than sending a file into a page that cannot
        accept one."""
        from noveltrans.youtube_upload import YouTubeUploadError, upload_subtitle_one

        page = _FakeSubtitlePage(language_gate=True, gate_clears=False)
        with pytest.raises(YouTubeUploadError, match="chưa đặt ngôn ngữ"):
            upload_subtitle_one(page, self._request(part, tmp_path))
        assert page.sent == []

    def test_a_video_without_the_gate_is_untouched_by_it(self, part, tmp_path):
        from noveltrans.youtube_upload import upload_subtitle_one

        page = _FakeSubtitlePage(language_gate=False, clicks_to_input=1)
        upload_subtitle_one(page, self._request(part, tmp_path))
        assert page.sent == [str(tmp_path / "a.srt")]


class TestFailureDiagnostics:
    """A click ladder fails because the label it wants isn't there. The diagnostic that
    resolves that is the list of labels that ARE — not the component tags, which answer a
    different question."""

    class _Page:
        def __init__(self, labels=("Confirm", "Set language"), tags=("ytcp-app×1",)):
            self.labels = list(labels)
            self.tags = list(tags)

        def evaluate(self, script):
            return self.labels if "getBoundingClientRect" in script else self.tags

    def test_it_lists_the_visible_clickable_labels(self):
        from noveltrans.youtube_upload import _page_actions

        assert "Confirm" in _page_actions(self._Page())
        assert "Set language" in _page_actions(self._Page())

    def test_it_never_raises_on_a_hostile_page(self):
        from noveltrans.youtube_upload import _page_actions

        class _Broken:
            def evaluate(self, script):
                raise RuntimeError("nope")

        assert "không đọc được" in _page_actions(_Broken())

    def test_an_empty_page_says_so_rather_than_returning_nothing(self):
        from noveltrans.youtube_upload import _page_actions

        assert "không có nút" in _page_actions(self._Page(labels=[]))

    def test_every_subtitle_failure_path_carries_the_labels(self):
        """No failure in this flow may say only "giao diện có thể đã thay đổi" — that was
        the 039 mistake and it cost three rounds. Checked across the whole flow, since the
        dialog handling moved out of `upload_subtitle_one` into `_send_subtitle_file`."""
        import inspect

        from noveltrans import youtube_upload as mod

        src = "".join(
            inspect.getsource(f)
            for f in (mod.upload_subtitle_one, mod._send_subtitle_file)
        )
        assert src.count("_page_actions(page)") >= 3
        assert src.count("_dom_inventory(page)") >= 3


class TestClickTextAnywhere:
    """Regression: the live failure was a DROPDOWN reading "Set language".

    `_click_by_text` only searches `ytcp-button` / `tp-yt-paper-button` / `button`, so a
    control that is none of those was invisible to it — and the id guesses missed too. The
    run sat on a page whose control was plainly on screen.
    """

    class _Page:
        """Only `:text(...)` selectors resolve here — button-scoped ones do not, exactly
        as a non-button control behaves live."""

        def __init__(self, *, texts=("Set language",)):
            self.texts = list(texts)
            self.clicked: list = []

        def locator(self, selector):
            page = self

            class _Loc:
                @property
                def last(self):
                    return self

                @property
                def first(self):
                    return self

                def _matches(self):
                    if not selector.startswith(":text("):
                        return False  # a button-scoped selector finds nothing here
                    return any(t in selector for t in page.texts)

                def is_visible(self, timeout=None):
                    return self._matches()

                def click(self):
                    page.clicked.append(selector)

            return _Loc()

    def test_it_clicks_a_control_that_is_not_a_button(self):
        from noveltrans.youtube_upload import _click_text_anywhere

        page = self._Page()
        assert _click_text_anywhere(page, ("Set language",)) is True
        assert page.clicked and ":text(" in page.clicked[0]

    def test_the_button_only_search_would_have_missed_it(self):
        """Pins why the extra helper exists — remove it and this control is unreachable."""
        from noveltrans.youtube_upload import _click_by_text

        page = self._Page()
        assert _click_by_text(page, ("Set language",)) is False
        assert page.clicked == []

    def test_it_reports_failure_when_the_text_is_absent(self):
        from noveltrans.youtube_upload import _click_text_anywhere

        page = self._Page(texts=())
        assert _click_text_anywhere(page, ("Set language",)) is False

    def test_it_uses_last_so_it_lands_on_the_innermost_element(self):
        """`:text()` matches the smallest element containing the text; `.last` keeps the
        control itself rather than a wrapper that merely contains it."""
        import inspect

        from noveltrans import youtube_upload as mod

        src = inspect.getsource(mod._click_text_anywhere)
        assert '.last' in src

    def test_every_gate_step_falls_back_to_it(self):
        """All three steps — open the dropdown, pick the language, press Confirm — must
        survive a non-button control, not just the one that failed live."""
        import inspect

        from noveltrans import youtube_upload as mod

        gate = inspect.getsource(mod._answer_subtitle_language_gate)
        pick = inspect.getsource(mod._pick_vietnamese)
        assert gate.count("_click_text_anywhere") == 2  # open + confirm
        assert pick.count("_click_text_anywhere") == 2  # item, and item-after-search


class TestPickVietnameseOnAnEnglishList:
    """Regression, measured live: the Languages page renders in ENGLISH even with `?hl=vi`.

        :text("Tiếng Việt")   matches=0
        :text("Vietnamese")   matches=1  visible

    The old code typed "Tiếng Việt" into the dropdown's search box before looking, which
    filtered the English list to nothing — so the item it then searched for genuinely did
    not exist, and the run parked on the page with Confirm greyed out.
    """

    class _Page:
        def __init__(self, *, offers=("Vietnamese",), has_search=False):
            self.offers = list(offers)
            self.has_search = has_search
            self.typed: list = []
            self.clicked: list = []

        @property
        def keyboard(self):
            page = self

            class _Kb:
                def press(self, key):
                    pass

                def type(self, text, delay=None):
                    page.typed.append(text)
                    # a search box filters the list to what actually matches
                    page.offers = [o for o in page.offers if text.lower() in o.lower()]

            return _Kb()

        def wait_for_timeout(self, ms):
            pass

        def locator(self, selector):
            page = self

            class _Loc:
                @property
                def first(self):
                    return self

                @property
                def last(self):
                    return self

                def is_visible(self, timeout=None):
                    return any(f'"{o}"' in selector for o in page.offers)

                def wait_for(self, state=None, timeout=None):
                    from playwright.sync_api import (
                        TimeoutError as PlaywrightTimeoutError,
                    )

                    if "search-input" in selector and page.has_search:
                        return
                    raise PlaywrightTimeoutError(selector)

                def click(self):
                    page.clicked.append(selector)

            return _Loc()

    def test_it_finds_the_english_name_when_the_vietnamese_one_is_absent(self):
        from noveltrans.youtube_upload import _pick_vietnamese

        page = self._Page(offers=("Vietnamese",))
        assert _pick_vietnamese(page) is True
        assert any("Vietnamese" in c for c in page.clicked)

    def test_it_does_not_type_into_the_search_box_before_looking(self):
        """The bug in one assertion: typing first is what emptied the list."""
        from noveltrans.youtube_upload import _pick_vietnamese

        page = self._Page(offers=("Vietnamese",), has_search=True)
        assert _pick_vietnamese(page) is True
        assert page.typed == []

    def test_it_still_uses_the_search_box_for_a_list_that_needs_one(self):
        from noveltrans.youtube_upload import _pick_vietnamese

        page = self._Page(offers=(), has_search=True)
        page.offers = []  # nothing on screen until searched
        assert _pick_vietnamese(page) is False
        assert page.typed  # it did try the search box

    def test_it_searches_for_the_same_name_it_then_looks_for(self):
        """Typing one name and looking for another is exactly what broke live."""
        import inspect

        from noveltrans import youtube_upload as mod

        src = inspect.getsource(mod._pick_vietnamese)
        # inside the search loop, both the type and the click use the loop variable
        assert "page.keyboard.type(text" in src
        assert "_click_text_anywhere(page, (text,)" in src

    def test_the_vietnamese_names_include_the_english_form(self):
        from noveltrans.youtube_upload import _VIETNAMESE_TEXTS

        assert "Vietnamese" in _VIETNAMESE_TEXTS


class TestSubtitleEditRowScoping:
    """The translations page lists one row per language, and `_click_text_anywhere` takes
    `.last` — so on a video that already carries an English track, the page-wide click for
    "Edit subtitles" opens the English row and publishes somebody else's captions."""

    class _Page:
        def __init__(self, *, has_vi_row=True):
            self.has_vi_row = has_vi_row
            self.clicked: list = []

        def locator(self, selector):
            page = self
            is_row = "[role='row']" in selector
            vi = any(t in selector for t in ("Tiếng Việt", "Vietnamese"))

            class _InRow:
                @property
                def first(self):
                    return self

                def is_visible(self, timeout=None):
                    return page.has_vi_row and vi

                def click(self):
                    page.clicked.append("vi-row")

            class _Loc:
                @property
                def first(self):
                    return self

                @property
                def last(self):
                    return self

                def locator(self, sub):
                    return _InRow()

                def is_visible(self, timeout=None):
                    # The page-wide text search always finds a control; it is the LAST
                    # row's, which is the whole problem.
                    return not is_row

                def click(self):
                    page.clicked.append("last-row")

            return _Loc()

    def test_the_vietnamese_row_wins_over_the_last_row_on_the_page(self):
        from noveltrans.youtube_upload import _click_subtitle_edit

        page = self._Page()
        assert _click_subtitle_edit(page) is True
        assert page.clicked == ["vi-row"]

    def test_a_page_with_no_such_row_still_gets_its_editor_opened(self):
        """Scoping is a preference, not a gate — one-row videos are the common case."""
        from noveltrans.youtube_upload import _click_subtitle_edit

        page = self._Page(has_vi_row=False)
        assert _click_subtitle_edit(page) is True
        assert page.clicked == ["last-row"]


def _body_of(name: str) -> str:
    """The source of `youtube_upload.<name>` with its docstring removed.

    Order-of-operations tests read the source, and a docstring that *describes* the order
    made them pass or fail on prose rather than code — the rewrite that fixed the hang
    tripped two of them by explaining itself.
    """
    import inspect

    from noveltrans import youtube_upload as mod

    src = inspect.getsource(getattr(mod, name))
    head, sep, rest = src.partition('"""')
    return head + rest.partition('"""')[2] if sep else src


class TestMeasuredUploadDialog:
    """Feature 044, measured — the labels four rounds of guessing never found.

    The post-gate page offers `Edit subtitles | Upload manual | Add language`. "Upload
    file", which every earlier attempt assumed, does not exist anywhere on it.
    """

    def test_upload_manual_is_the_label_that_exists(self):
        from noveltrans.youtube_upload import _SUB_UPLOAD_TEXTS

        assert "Upload manual" in _SUB_UPLOAD_TEXTS
        assert _SUB_UPLOAD_TEXTS[0] == "Upload manual"  # tried first

    def test_the_timing_branch_keeps_our_timings(self):
        """"Without timing" would make YouTube re-align by speech recognition and discard
        the exact boundaries feature 040 captured from the TTS run."""
        from noveltrans.youtube_upload import _SUB_WITH_TIMING_TEXTS

        assert _SUB_WITH_TIMING_TEXTS[0] == "With timing"
        assert not any("Without" in t for t in _SUB_WITH_TIMING_TEXTS)

    def test_continue_is_its_own_step(self):
        from noveltrans.youtube_upload import _SUB_CONTINUE_TEXTS

        assert "Continue" in _SUB_CONTINUE_TEXTS

    def test_the_dialog_is_driven_in_the_measured_order(self):
        """upload -> timing -> file input -> continue. Any other order fails live."""
        src = _body_of("_send_subtitle_file")
        assert src.index("_SUB_UPLOAD_TEXTS") < src.index("_choose_with_timing")
        assert src.index("_choose_with_timing") < src.index("set_input_files")

    def test_a_video_input_is_never_handed_the_srt(self):
        """Studio's shell carries file inputs for videos and thumbnails too. Feeding one of
        those an .srt is not a harmless no-op — it starts an upload of the wrong thing."""
        from noveltrans.youtube_upload import _input_takes_subtitles

        class _Input:
            def __init__(self, accept):
                self.accept = accept

            def get_attribute(self, name, timeout=None):
                return self.accept

        assert _input_takes_subtitles(_Input("video/*")) is False
        assert _input_takes_subtitles(_Input("image/jpeg,image/png")) is False
        assert _input_takes_subtitles(_Input(".srt,.sbv,.vtt")) is True
        assert _input_takes_subtitles(_Input(None)) is True  # no claim -> scoping decides

    def test_the_vietnamese_label_cannot_select_without_timing(self):
        """`:text()` is a case-insensitive SUBSTRING match and `_click_text_anywhere` takes
        `.last` — so on a Vietnamese dialog `Có mã thời gian` also matches "**Không** có mã
        thời gian", the radio that throws our timings away, and it is the later of the two.
        """
        from noveltrans.youtube_upload import _choose_with_timing

        class _Page:
            """Both radios are present; the exact form only reaches the right one."""

            def __init__(self):
                self.clicked: list = []

            def locator(self, selector):
                page = self
                # The exact form does not match the longer label; the substring form does.
                exact = selector.startswith(":text-is(")
                start = selector.find('("') + 2
                wanted = selector[start : selector.find('")', start)]
                reads = wanted if exact else f"Không {wanted.lower()}"

                class _Loc:
                    @property
                    def first(self):
                        return self

                    def is_visible(self, timeout=None):
                        return wanted in ("Có mã thời gian", "Có thời gian")

                    def inner_text(self, timeout=None):
                        return reads

                    def click(self):
                        page.clicked.append(reads)

                return _Loc()

        page = _Page()
        assert _choose_with_timing(page) is True
        assert page.clicked == ["Có mã thời gian"]

    def test_the_cue_probe_is_the_first_spoken_line(self, tmp_path):
        """Not the index, not the `-->` timings, and never a double quote — the snippet
        goes straight into a `:text("…")` selector."""
        from noveltrans.youtube_upload import _cue_probe

        srt = tmp_path / "a.srt"
        srt.write_text(
            '1\n00:00:00,000 --> 00:00:02,000\nĐêm ấy trời "mưa" rất to\n', encoding="utf-8"
        )
        assert _cue_probe(srt) == "Đêm ấy trời"

        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nÀ\n", encoding="utf-8")
        assert _cue_probe(srt) == ""  # too short to search for; the field count answers

        assert _cue_probe(tmp_path / "missing.srt") == ""

    def test_the_editor_is_waited_for_before_publish_is_pressed(self):
        """Structural half of the "no subtitles" fix: no reordering may put the Publish
        click above the wait."""
        src = _body_of("_publish_subtitle_track")
        assert src.index("_wait_for_subtitle_editor") < src.index("_SUB_PUBLISH_TEXTS")
        assert src.index("_click_subtitle_edit") < src.index("_wait_for_subtitle_editor")

    def test_the_file_input_is_set_directly_before_falling_back_to_a_chooser(self):
        """Setting a hidden input needs no OS dialog to intercept; the chooser path stays
        for a build with no reachable input."""
        src = _body_of("_send_subtitle_file")
        assert src.index("set_input_files") < src.index("expect_file_chooser")

    def test_every_continue_click_is_inside_the_chooser_interception(self):
        """The structural half of the hang regression. The behavioural test proves the
        current path is safe; this one stops a future edit from adding a second, unguarded
        Continue click somewhere else in the function — which is precisely the shape the
        bug had: set an input, then press Continue with nothing armed to catch the OS
        window."""
        src = _body_of("_send_subtitle_file")
        guard = src.index("expect_file_chooser")
        clicks, at = [], src.find("_click_text_anywhere(page, _SUB_CONTINUE_TEXTS")
        while at != -1:
            clicks.append(at)
            at = src.find("_click_text_anywhere(page, _SUB_CONTINUE_TEXTS", at + 1)
        assert clicks, "the Continue click disappeared — this test is now checking nothing"
        assert all(at > guard for at in clicks)


class TestPauseHook:
    """The per-part `on_checkpoint` hook the browser workers pass in (049).

    Only the API shape is pinned here: the batch functions drive a real Playwright
    session, so that the hook actually fires between parts is verified by inspection and
    by the manual run recorded in 049.01-HISTORY.md — not by this suite.
    """

    @pytest.mark.parametrize(
        "name",
        ["upload_batch", "upload_subtitle_batch", "update_thumbnail_batch", "sync_playlist_batch"],
    )
    def test_the_hook_is_optional_and_keyword_only(self, name):
        import inspect

        from noveltrans import youtube_upload

        param = inspect.signature(getattr(youtube_upload, name)).parameters["on_checkpoint"]
        assert param.default is None  # every existing caller keeps working untouched
        assert param.kind is inspect.Parameter.KEYWORD_ONLY

    @pytest.mark.parametrize(
        "name",
        ["upload_batch", "upload_subtitle_batch", "update_thumbnail_batch", "sync_playlist_batch"],
    )
    def test_the_hook_sits_at_the_part_boundary_not_mid_transfer(self, name):
        # Guards the one thing that would break a live upload: holding inside
        # `_wait_for_bytes_uploaded` would freeze a transfer mid-stream and trip
        # Playwright's timeout. It must only ever follow the per-part cancel check.
        import inspect
        import re

        from noveltrans import youtube_upload

        source = inspect.getsource(getattr(youtube_upload, name))
        calls = [m.start() for m in re.finditer(r"on_checkpoint\(\)", source)]
        assert len(calls) == 1
        preceding = source[: calls[0]].rstrip().splitlines()[-3:]
        assert any("_check_cancel(should_cancel)" in line for line in preceding)

"""Upload rendered part-videos to YouTube by driving YouTube Studio in a real browser.

The Video tab already writes everything an upload needs next to each part
(`<name>.mp4`, `.title.txt`, `.txt` description, `.tags.txt`, `.jpg` thumbnail) —
each part in its own subfolder precisely so one video can be uploaded without
hunting through the others. This module is the last mile: it fills Studio's upload
form with those files instead of the user doing it by hand.

Design choices (see changes/033-VIDEO-UPLOAD-AUTOMATION):
  * **Browser automation, not the Data API.** No OAuth app, no client secret, no
    10 000-unit daily quota (an API upload costs 1600 — six videos a day and you're
    done). The trade is fragility: Studio's DOM is A/B tested and localized, so every
    selector lives in the constants block below, grouped by step, ready to retune.
  * Playwright drives a *dedicated, persistent* profile — separate from the user's
    everyday browser — that they sign into once via `open_login()`. Google's login
    flow is far more hostile to automation than Discord's, so the sign-in is always
    manual in a visible window; we only ever *reuse* the resulting session.
  * `upload_batch` opens ONE browser for a whole run of parts. Studio's bot checks
    notice a login-upload-quit cycle repeated per video, and the launch itself costs
    seconds.

`update_thumbnail_batch` (feature 034) is the one flow that does *not* upload: it drives
Studio's standalone **video edit page** to replace the cover of a video that is already
on the channel, for when the cover is re-cut after publishing. It is also the one flow
where a missing thumbnail input is fatal rather than a shrug — see `_send_thumbnail`.

**Never double-publish.** A part's upload state is a sidecar next to its `.mp4`
(`<name>.upload.json`), written *before* the file is handed to Studio and updated as
soon as the draft's video id is known. Anything already `published` is skipped. See
`read_upload_state` for the state machine and the failure window that remains.

Note: automating Studio is against YouTube's ToS on paper, the same way
`discord_unlock` is against Discord's. Keep runs human-paced and low-frequency.

Playwright is an optional dependency (`pip install 'noveltrans[browser]'` then
`playwright install chromium`); it is imported lazily so the core app runs without it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from noveltrans.browser import BrowserUnavailableError
from noveltrans.browser import close as _close
from noveltrans.browser import launch_persistent_context, require_playwright
from noveltrans.storage.library import DEFAULT_LIBRARY_DIR

# -- Studio selectors ---------------------------------------------------------
#
# Kept here, grouped by the step that uses them, so they're easy to retune when
# Studio's DOM drifts — which it does, constantly. Two rules held throughout:
#   1. Prefer `#id` on Studio's own `ytcp-*` / `tp-yt-paper-*` custom elements.
#      Those ids are wired to Polymer bindings and survive redesigns and — the part
#      that matters here — are the SAME whether Studio renders in Vietnamese or
#      English. Playwright's CSS engine pierces their open shadow roots for free.
#   2. Where only text will do, match Vietnamese *and* English: we don't control
#      the account's Studio language and can't assume the app's own locale matches.

# `?hl=vi` pins Studio's UI language. That collapses the localisation problem to one
# known language *and* one known date format, which matters most for the schedule
# pickers. It's a request, not a guarantee — an account-level language setting can win —
# so `_page_is_vietnamese` still reads the truth off the DOM rather than trusting this.
_UPLOAD_URL = "https://www.youtube.com/upload?hl=vi"  # redirects into Studio's dialog
_STUDIO_HOME = "https://studio.youtube.com/?hl=vi"
# Google bounces a logged-out profile to accounts.google.com (sometimes via a
# youtube.com/signin hop).
_LOGGED_OUT_URL_RE = re.compile(r"accounts\.google\.com|youtube\.com/signin")
_LOGGED_IN_URL_RE = re.compile(r"studio\.youtube\.com/channel/")

# step 1 — file + details
_DIALOG_SEL = "ytcp-uploads-dialog"
# Studio stamps the dialog with the pane it is showing: SELECT_FILES → DETAILS →
# MONETIZATION/CHECKS/VISIBILITY. A language-independent enum straight from Studio's own
# state machine, and the only honest way to tell "the file was accepted" from "still on
# the drop pane" — the dialog element itself is a zero-size wrapper that Playwright
# always reports as hidden, so visibility says nothing.
_DIALOG_STEP_ATTR = "workflow-step"
_STEP_SELECT_FILES = "SELECT_FILES"
# Tried in order, widest-net last. `input[type=file]` is plain HTML and cannot be
# renamed or shadowed away, so it is the one that always works — the narrower forms are
# only there to avoid grabbing an unrelated input if Studio ever ships two.
# (An over-specific selector here is what made the first live run stall on the empty
# "Drag and drop video files" dialog: the element names had drifted.)
_FILE_INPUT_SELS = (
    "ytcp-uploads-dialog input[type='file']",
    "ytcp-uploads-file-picker input[type='file']",
    "input[type='file']",
)
# Fallback path: click Studio's own button and catch the OS file chooser Playwright
# intercepts. Slower and text-dependent, but it doesn't care where the input hides.
_SELECT_FILES_SEL = "ytcp-button#select-files-button, #select-files-button"
_SELECT_FILES_TEXTS = ("Chọn tệp", "Select files")
_TITLE_SEL = "#title-textarea #textbox"
_DESCRIPTION_SEL = "#description-textarea #textbox"
# The dialog holds several file inputs (video, thumbnail, subtitles); scope to the
# thumbnail editor or we'd hand the .jpg to the video uploader.
_THUMBNAIL_INPUT_SEL = "ytcp-thumbnails-compact-editor input[type='file'], #file-loader"

# step 1 — playlist
_PLAYLIST_TRIGGER_SEL = "ytcp-video-metadata-playlists ytcp-text-dropdown-trigger"
_PLAYLIST_SEARCH_SEL = "#search-input input, ytcp-playlist-dialog #search-input input"
_PLAYLIST_ITEM_SEL = "ytcp-checkbox-group ytcp-ve, #items ytcp-checkbox-group ytcp-ve"
_PLAYLIST_NEW_SEL = "ytcp-playlist-dialog #create-playlist-button, #create-playlist-button"
_PLAYLIST_NEW_TITLE_SEL = "#create-playlist-form #title-input textarea, #playlist-title-input textarea"
_PLAYLIST_NEW_CREATE_TEXTS = ("Tạo", "Create")
_PLAYLIST_DONE_TEXTS = ("Xong", "Done")

# step 1 — audience + the "Hiện thêm" panel (tags, language)
_MADE_FOR_KIDS_NO_SEL = "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']"
_SHOW_MORE_SEL = "#toggle-button, ytcp-button#toggle-button"
_TAGS_INPUT_SEL = "#tags-container #text-input, ytcp-form-input-container#tags-container input"
_LANGUAGE_TRIGGER_SEL = "#language-container ytcp-text-dropdown-trigger, #language-container"
_LANGUAGE_SEARCH_SEL = "#search-input input, tp-yt-paper-dialog #search-input input"
_LANGUAGE_ITEM_SEL = "tp-yt-paper-item, ytcp-text-menu tp-yt-paper-item"
# Studio lists the language under its *own* name in a Vietnamese UI and under the
# English name otherwise; accept either.
_VIETNAMESE_TEXTS = ("Tiếng Việt", "Vietnamese")

# step 2/3/4 — navigation
_NEXT_SEL = "#next-button"
_NEXT_TEXTS = ("Tiếp", "Tiếp theo", "Next")

# step 4 — visibility
_VISIBILITY_RADIOS = {
    "public": "tp-yt-paper-radio-button[name='PUBLIC']",
    "unlisted": "tp-yt-paper-radio-button[name='UNLISTED']",
    "private": "tp-yt-paper-radio-button[name='PRIVATE']",
}
_SCHEDULE_RADIO_SEL = "#schedule-radio-button, tp-yt-paper-radio-button[name='SCHEDULE']"
_DATE_TRIGGER_SEL = "#datepicker-trigger, ytcp-datetime-picker #datepicker-trigger"
_DATE_INPUT_SEL = "ytcp-date-picker tp-yt-paper-input input, #datepicker-trigger input"
_TIME_INPUT_SEL = "#time-of-day-container input, ytcp-time-of-day-picker input"
_DONE_SEL = "#done-button"
_PUBLISH_TEXTS = ("Xuất bản", "Lên lịch", "Lưu", "Publish", "Schedule", "Save")
# What Studio puts up after a successful publish — either the share sheet or the
# "still processing" notice, depending on how far encoding got.
_CONFIRM_DIALOG_SEL = (
    "ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog, "
    "ytcp-uploads-video-processed-dialog"
)

# the draft's public link, available minutes before processing finishes
_VIDEO_URL_SEL = "#share-url, ytcp-video-info a[href*='youtu.be'], a.video-url-fadeable"
_VIDEO_ID_RE = re.compile(r"(?:youtu\.be/|watch\?v=|/video/)([A-Za-z0-9_-]{11})")

# Studio's own progress line ("Đang tải lên 45%…" / "Uploading 45%…" / "Đã xử lý xong").
_PROGRESS_SEL = ".progress-label, ytcp-video-upload-progress .progress-label"
# Bytes still moving. Anything about *processing* or *checks* happens after the transfer,
# so those mean the file is safely on YouTube and we may publish and move on.
_UPLOADING_RE = re.compile(r"đang tải lên|uploading", re.IGNORECASE)
_TRANSFER_DONE_RE = re.compile(
    r"tải lên xong|đã tải lên|tải lên hoàn tất|đang xử lý|đã xử lý|kiểm tra|"
    r"upload complete|uploaded|processing|checks",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(\d+)\s*%")

# -- video edit page (feature 034: replace the thumbnail of a published video) --
#
# A different Studio surface from the upload dialog: the standalone edit page, chosen
# because it has an explicit save affordance. `#save` is disabled while there is nothing
# to save, which makes it a language-independent TWO-WAY signal — it going *enabled*
# proves Studio took the image, it going *disabled* proves the save landed. The
# thumbnail control inside the upload dialog has no save of its own; it only commits
# when the whole dialog does, which no longer exists once the video is published.
#
# Like the upload selectors above, NONE of these have been verified against a live
# channel. They are written so that drift stops the run and names the step, never so
# that it quietly reports success: this flow's only output is the thumbnail, so a
# skipped step would be a lie.
_EDIT_URL = "https://studio.youtube.com/video/{video_id}/edit?hl=vi"
# The metadata editor mounting is how we know the id resolved to a real video on this
# channel — a deleted or foreign id bounces to the video list instead.
_EDIT_PAGE_SEL = "ytcp-video-metadata-editor, #metadata-container, ytcp-uploads-details"
# Same component the upload dialog renders (see `_THUMBNAIL_INPUT_SEL`), widest net
# last — the first live run of 033 stalled because only narrow `ytcp-*` forms were tried.
_EDIT_THUMBNAIL_INPUT_SELS = (
    "ytcp-thumbnails-compact-editor input[type='file']",
    "ytcp-video-thumbnail-editor input[type='file']",
    "#file-loader",
    "ytcp-video-metadata-editor input[type='file']",
)
_EDIT_THUMBNAIL_BUTTON_SEL = (
    "#still-picker-upload-button, ytcp-thumbnails-compact-editor #upload-button"
)
_EDIT_THUMBNAIL_TEXTS = ("Tải file lên", "Tải tệp lên", "Upload file", "Upload thumbnail")
_SAVE_SEL = "#save, ytcp-button#save, ytcp-button#save-button"
_SAVE_TEXTS = ("Lưu", "Save")
# Studio's post-save toast. Secondary confirmation only: the primary one is `#save`
# going back to disabled, which needs no text matching at all.
_TOAST_SEL = "ytcp-toast, tp-yt-paper-toast, ytcp-snackbar, #notification-text"
_SAVED_RE = re.compile(r"đã lưu|lưu thay đổi|saved|changes saved", re.IGNORECASE)
_SAVE_ERROR_RE = re.compile(
    r"không thể|thất bại|lỗi|quá lớn|không hợp lệ|error|failed|too large|invalid",
    re.IGNORECASE,
)
# Navigating away with an unsaved change puts up Studio's own confirmation. In a batch
# that lands between parts, so it must be cleared or one failed part blocks every
# remaining one behind a modal nobody sees.
_DISCARD_DIALOG_SEL = "ytcp-confirmation-dialog, tp-yt-paper-dialog#dialog"
_DISCARD_TEXTS = ("Loại bỏ", "Bỏ thay đổi", "Discard", "Discard changes")

# -- timings ------------------------------------------------------------------
# Studio is slow and network-bound; these are deliberately generous. A part-video is
# often > 1 GB, so the *upload* wait is measured in tens of minutes, not seconds.
_DIALOG_WAIT_MS = 60_000  # upload dialog to appear after navigating
_STEP_WAIT_MS = 30_000  # any single control to become interactive
_UPLOAD_WAIT_MS = 4 * 3600_000  # file bytes to finish going up (4h ceiling)
_UPLOAD_POLL_MS = 2_000
_TYPE_DELAY_MS = 8  # for short fields; long text goes in via insert_text (see _fill_box)
_SETTLE_MS = 600  # let Polymer re-render between steps
# How far ahead of "now" the first scheduled publish must sit. A part is often several
# GB; by the time it has transferred, a 5-minute lead is in the past and YouTube refuses.
_MIN_SCHEDULE_LEAD = timedelta(minutes=15)
# Between parts in a batch: cheap, and the main thing separating "automation" from
# "hammering" in the eyes of whatever watches for it.
_BETWEEN_PARTS_MS = 20_000
# Grace period before tearing the browser down at the end of a run.
_SETTLE_BEFORE_CLOSE_MS = 5_000

# thumbnail update (034). Much cheaper than an upload — a page load, not a transfer.
_EDIT_PAGE_WAIT_MS = 45_000  # the edit page to mount
_THUMB_ACCEPT_MS = 20_000  # Studio to register the new image (save becomes enabled)
_SAVE_CONFIRM_MS = 60_000  # the save to land (a 2 MB image + Studio round-trip)
_BETWEEN_THUMBNAILS_MS = 8_000  # human pacing, shorter than `_BETWEEN_PARTS_MS`
_MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024  # YouTube's hard limit
_THUMBNAIL_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


class YouTubeUploadError(Exception):
    """An upload could not be completed.

    `needs_login` marks the recoverable case where the dedicated profile has no valid
    Google session — the fix is the one-time login, not a retry. `video_id` is set when
    the failure happened *after* Studio accepted the file, so the caller can tell the
    user exactly which draft to go look at instead of leaving them guessing.
    """

    def __init__(self, message: str, *, needs_login: bool = False, video_id: str = ""):
        super().__init__(message)
        self.needs_login = needs_login
        self.video_id = video_id


class UploadCancelled(Exception):
    """The user cancelled the run. Carries the draft id if one was already created."""

    def __init__(self, message: str = "Đã huỷ tải lên.", *, video_id: str = ""):
        super().__init__(message)
        self.video_id = video_id


def profile_dir() -> Path:
    """Dedicated browser profile holding the channel's Google login.

    Lives inside the library data dir (hidden) so it travels with the user's data and
    stays out of their normal browser profiles — and, unlike the Discord profile, this
    one holds a session for an account the user actually cares about, which is the
    other reason it must never be the everyday profile.
    """
    return DEFAULT_LIBRARY_DIR / ".youtube-profile"


# -- what to upload -----------------------------------------------------------

VISIBILITIES = ("public", "unlisted", "private", "schedule")


@dataclass
class UploadRequest:
    """One part's worth of upload inputs, all read from the sidecars beside the .mp4."""

    video: Path
    title: str
    description: str = ""
    tags: str = ""  # comma-joined, as written to `<name>.tags.txt`
    thumbnail: Path | None = None
    playlist: str = ""  # "" → don't touch the playlist picker
    visibility: str = "private"
    publish_at: datetime | None = None  # required when visibility == "schedule"
    label: str = ""  # human name for progress lines, e.g. "Phần 3"

    def validate(self) -> None:
        """Reject a request that can't succeed, before any browser is launched."""
        if not self.video.is_file():
            raise YouTubeUploadError(f"Không tìm thấy file video: {self.video}")
        if not self.title.strip():
            raise YouTubeUploadError(f"{self.label or self.video.stem}: thiếu tiêu đề.")
        if self.visibility not in VISIBILITIES:
            raise YouTubeUploadError(f"Chế độ hiển thị không hợp lệ: {self.visibility}")
        if self.visibility == "schedule" and self.publish_at is None:
            raise YouTubeUploadError("Chọn hẹn giờ đăng nhưng chưa có thời điểm đăng.")
        if self.thumbnail is not None and not Path(self.thumbnail).is_file():
            # Not fatal on its own, but silently uploading without the cover the user
            # spent time on is worse than saying so.
            raise YouTubeUploadError(f"Không tìm thấy ảnh bìa: {self.thumbnail}")


@dataclass
class UploadResult:
    video_id: str
    url: str
    visibility: str
    publish_at: datetime | None = None
    skipped: bool = False  # already published; nothing was sent this run


@dataclass
class ThumbnailRequest:
    """One part's thumbnail replacement: an already-uploaded video + the new image.

    `video` is only the identity key for the `<name>.upload.json` record — unlike
    `UploadRequest`, the .mp4 itself need not still exist. The video lives on YouTube
    now, and someone who deleted the local render to reclaim disk should still be able
    to push a new cover.
    """

    video: Path
    thumbnail: Path
    video_id: str = ""  # "" → resolved from the record in `update_thumbnail_one`
    label: str = ""  # human name for progress lines, e.g. "Phần 3"

    def validate(self) -> None:
        """Reject a request that can't succeed, before any browser is launched.

        Every check here is local and free, so a file YouTube would refuse costs zero
        seconds of Chrome — the same discipline as `UploadRequest.validate()`.
        """
        thumbnail = Path(self.thumbnail)
        name = self.label or Path(self.video).stem
        if not thumbnail.is_file():
            raise YouTubeUploadError(f"Không tìm thấy ảnh bìa: {thumbnail}")
        size = thumbnail.stat().st_size
        if not size:
            raise YouTubeUploadError(f"{name}: ảnh bìa rỗng — hãy tạo lại ảnh bìa.")
        if size > _MAX_THUMBNAIL_BYTES:
            raise YouTubeUploadError(
                f"{name}: ảnh bìa nặng {size / 1024 / 1024:.1f} MB, YouTube chỉ nhận "
                "tối đa 2 MB."
            )
        if thumbnail.suffix.lower() not in _THUMBNAIL_SUFFIXES:
            raise YouTubeUploadError(
                f"{name}: YouTube không nhận định dạng “{thumbnail.suffix}”. "
                "Dùng .jpg, .png, .gif hoặc .bmp."
            )


@dataclass
class ThumbnailResult:
    video_id: str
    url: str
    updated_at: str


# -- schedule arithmetic (pure, tested) ---------------------------------------


def schedule_times(start: datetime, count: int, spacing_days: int = 1) -> list[datetime]:
    """Publish times for `count` parts: part 1 at `start`, each next `spacing_days` later.

    The headline use case is a serialized audio novel released one part a day. Uses
    `timedelta(days=…)` deliberately: the user picks a wall-clock hour ("20:00 every
    day") and expects that hour to stick, which naive date arithmetic gives them.
    A spacing of 0 puts every part at the same instant — allowed, since YouTube does.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    if spacing_days < 0:
        raise ValueError("spacing_days must be >= 0")
    return [start + timedelta(days=spacing_days * i) for i in range(count)]


def validate_schedule_start(start: datetime, *, now: datetime | None = None) -> None:
    """Reject a first publish time YouTube would refuse. Raises, or returns silently.

    YouTube rejects a schedule in the past outright, and a batch of multi-GB parts takes
    real time to upload — "in five minutes" is reliably in the past by the time part one
    finishes transferring. The margin is deliberately generous for that reason.
    """
    now = now or datetime.now()
    if start <= now:
        raise YouTubeUploadError(
            f"Thời điểm đăng ({start:%d/%m/%Y %H:%M}) đã ở quá khứ. YouTube không nhận "
            "lịch đăng trong quá khứ — chọn thời điểm sau."
        )
    if start - now < _MIN_SCHEDULE_LEAD:
        raise YouTubeUploadError(
            "Thời điểm đăng quá gần hiện tại. Video nặng vài GB nên mất thời gian tải "
            f"lên; chọn ít nhất {int(_MIN_SCHEDULE_LEAD.total_seconds() // 60)} phút nữa."
        )


def _format_date(when: datetime, *, vietnamese: bool) -> str:
    """Studio's date box as the picker's own locale writes it.

    Vietnamese Studio renders "27 thg 7, 2026"; English renders "Jul 27, 2026". The
    box parses what it renders, so we have to match the UI language rather than pick
    one format — hence `_page_is_vietnamese` deciding this per session.
    """
    if vietnamese:
        return f"{when.day} thg {when.month}, {when.year}"
    return f"{when:%b} {when.day}, {when.year}"


def _format_time(when: datetime, *, vietnamese: bool) -> str:
    """Studio's time box: 24-hour in Vietnamese, 12-hour AM/PM in English."""
    if vietnamese:
        return f"{when:%H:%M}"
    return f"{when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"


# -- upload state sidecar (pure, tested) --------------------------------------
#
# The state machine. Write points are chosen so the local record is always AT LEAST as
# committed as reality on YouTube: we may claim a video exists when it doesn't, never
# the reverse. The reverse is what publishes an episode twice on a public channel.
#
#   (no file)  → nothing has been attempted. The ONLY auto-uploadable state.
#   started    → written BEFORE the .mp4 is handed to Studio. Finding this later means a
#                previous attempt died before we learned the draft id; a draft MAY exist.
#   draft      → Studio accepted the file and told us the video id. Not published yet.
#   committed  → written BEFORE the publish click. Finding this later means the click
#                may or may not have landed — from here we genuinely cannot tell, so it
#                is never downgraded to a retryable state.
#   published  → the publish/schedule click was confirmed. Always skipped from then on.
#   unknown    → the state file exists but is unreadable. Treated exactly like `started`:
#                "I can't tell" must never be mistaken for "never uploaded".
#
# Remaining window: a crash between YouTube committing the publish and us writing
# `published` leaves the part as `committed`. Unavoidable without a second source of
# truth, and it fails *safe* — the user is told to check the channel, and nothing is
# re-uploaded behind their back. Deleting the sidecar is the deliberate escape hatch.

_STATE_EXT = ".upload.json"

STATE_STARTED = "started"
STATE_DRAFT = "draft"
STATE_COMMITTED = "committed"
STATE_PUBLISHED = "published"
STATE_UNKNOWN = "unknown"

# Every state that means "a human has to look at this". Note `committed` is here and
# `published` is not: published is a settled outcome, committed is an open question.
_UNRESOLVED = {STATE_STARTED, STATE_DRAFT, STATE_COMMITTED, STATE_UNKNOWN}


def upload_state_path(video: Path) -> Path:
    """`<name>.upload.json`, beside the .mp4 like every other sidecar."""
    video = Path(video)
    return video.parent / (video.stem + _STATE_EXT)


def read_upload_state(video: Path) -> dict:
    """The part's recorded upload state, or `{}` if it was never attempted.

    A corrupt or half-written file reads as `unknown` — **never** as `{}`. Those two
    are the most dangerous pair in this module to confuse: `{}` means "safe to upload",
    and a truncated JSON file is precisely the situation where that is least true.
    """
    path = upload_state_path(video)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return {"status": STATE_UNKNOWN, "error": f"không đọc được trạng thái: {exc}"}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {"status": STATE_UNKNOWN, "error": f"file trạng thái hỏng: {exc}"}
    if not isinstance(data, dict):
        return {"status": STATE_UNKNOWN, "error": "file trạng thái sai định dạng"}
    return data


def write_upload_state(video: Path, **fields) -> dict:
    """Merge `fields` into the part's state file and return the merged dict.

    Merges rather than overwrites so a later `published` write keeps the `video_id` an
    earlier `draft` write recorded.

    Written atomically (temp file → `os.replace`) because this runs immediately before
    an irreversible click: a torn write here produces a truncated file, and the whole
    point of `read_upload_state` treating that as `unknown` is that we never have to
    find out what a half-written record would have meant.
    """
    state = read_upload_state(video)
    state.update(fields)
    path = upload_state_path(video)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX and on Windows for same-directory replaces
    return state


def is_published(video: Path) -> bool:
    """True if this part was already published — the check that prevents duplicates."""
    return read_upload_state(video).get("status") == STATE_PUBLISHED


def needs_attention(video: Path) -> bool:
    """True for a part whose last attempt didn't reach a settled outcome.

    These are the ones a human has to look at; we will not touch them again.
    """
    return read_upload_state(video).get("status") in _UNRESOLVED


def clear_upload_state(video: Path) -> bool:
    """Forget a part's upload record so it can be queued again. True if one existed.

    The deliberate escape hatch out of `started` / `draft` / `committed` / `unknown` —
    the states the app refuses to touch on its own because it cannot tell whether a
    video exists on the channel. Only a human can answer that, so only a human may
    call this.

    Callers must warn first, and the warning depends on `has_remote_draft()`: a record
    with no video id means nothing ever reached YouTube and clearing is free, while one
    with an id means there IS something on the channel and re-uploading would duplicate
    it.
    """
    path = upload_state_path(video)
    if not path.is_file():
        return False
    path.unlink()
    return True


def mark_uploaded_by_hand(video: Path) -> dict:
    """Record that the user uploaded this part themselves, so batches skip it.

    `marked_by_hand` distinguishes it from a record this app wrote: there is no video id
    and never was, so nothing downstream should treat it as a link to a real video.
    """
    return write_upload_state(
        video, status=STATE_PUBLISHED, published_at=_now_iso(), marked_by_hand=True
    )


def has_remote_draft(video: Path) -> bool:
    """True if the record names a video that exists on YouTube.

    The difference between "clearing this is harmless" and "clearing this can create a
    duplicate" — an interrupted run that died before the file was ever sent has no id.
    """
    state = read_upload_state(video)
    return bool(state.get("video_id")) and state.get("status") != STATE_PUBLISHED


def uploaded_video_id(video: Path) -> str:
    """The YouTube video id recorded for this part, or "" if there isn't one.

    Eligibility for a thumbnail update, and deliberately status-blind where
    `is_uploadable` is paranoid: `draft`, `committed` and `published` all name a video
    that exists on the channel and whose thumbnail is editable. Replacing a thumbnail
    cannot duplicate anything, so the rule that protects against double-publishing has
    nothing to protect here.

    A `marked_by_hand` record has no id and never had one, so it is excluded for free —
    which is the point: we must never push a cover at a video we can't identify.
    """
    return str(read_upload_state(video).get("video_id") or "")


def thumbnail_is_current(video: Path, thumbnail: Path) -> bool:
    """True if the recorded thumbnail push is at least as new as the image on disk.

    Advisory only — it feeds the confirmation dialog's "these N are already up to date"
    line, and must never be used to skip work silently. Anything unreadable or
    unparseable reads as *stale*, so the safe answer is always "offer to push it".
    """
    updated = read_upload_state(video).get("thumbnail_updated_at")
    if not updated:
        return False
    try:
        pushed_at = datetime.fromisoformat(str(updated))
        made_at = datetime.fromtimestamp(Path(thumbnail).stat().st_mtime)
    except (ValueError, OSError):
        return False
    if pushed_at.tzinfo is not None:  # `_now_iso` is tz-aware; st_mtime is naive local
        pushed_at = pushed_at.astimezone().replace(tzinfo=None)
    return pushed_at >= made_at


def is_uploadable(video: Path) -> bool:
    """True only when starting an upload cannot possibly duplicate an existing video.

    That is: no record at all. Erring toward "skip" costs the user a manual upload;
    erring the other way publishes the same episode twice on a public channel.
    """
    return not read_upload_state(video)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# -- browser plumbing ---------------------------------------------------------


def _require_playwright():
    """Import Playwright's sync API, or raise a message that says how to install it."""
    try:
        return require_playwright()
    except BrowserUnavailableError as exc:
        raise YouTubeUploadError(
            "Chưa cài Playwright cho tính năng tự tải lên YouTube. Chạy:\n"
            "  pip install 'noveltrans[browser]'\n"
            "  playwright install chromium"
        ) from exc


def _launch_context(sync_playwright, *, headless: bool):
    """Open the dedicated profile in the user's real Chrome (bundled Chromium if not).

    Google's sign-in and Studio's bot checks routinely refuse Playwright's bundled
    Chromium — a different build with a headless-ish fingerprint — so we drive the
    *installed* Chrome. Same dedicated profile either way.
    """
    try:
        return launch_persistent_context(sync_playwright, profile_dir(), headless=headless)
    except BrowserUnavailableError as exc:
        raise YouTubeUploadError(
            "Không mở được trình duyệt để tải lên YouTube. Cài Google Chrome, hoặc "
            "chạy:  playwright install chromium"
        ) from exc


def _page_is_vietnamese(page) -> bool:
    """Whether Studio is rendering in Vietnamese, for the date/time boxes.

    Reads `<html lang>`, which Studio sets from the *account's* language — not the OS
    and not our app's locale, so it has to be sniffed per session rather than assumed.
    """
    try:
        return (page.locator("html").get_attribute("lang") or "").lower().startswith("vi")
    except Exception:
        return False


def _first_present(page, selector: str, *, timeout_ms: int = _STEP_WAIT_MS, state: str = "visible"):
    """Wait for the first element matching a (possibly comma-joined) selector.

    Every selector constant above lists fallbacks for exactly this: when Studio's A/B
    variant renames one hook, the other still matches and the run survives.

    `state="attached"` is for Studio's `ytcp-*` wrapper elements. Several of them are
    zero-size containers whose content is projected into an overlay, so Playwright
    computes them as *hidden* even while the user is looking straight at the dialog —
    waiting for "visible" on one of those hangs forever. Real controls (contenteditable
    boxes, radios, buttons) are genuinely visible and keep the default.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    locator = page.locator(selector).first
    try:
        locator.wait_for(state=state, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return None
    return locator


def _dialog_step(page) -> str:
    """Studio's own `workflow-step` on the upload dialog, e.g. "SELECT_FILES".

    The single most useful signal in the whole flow: it says which pane the dialog is
    showing, in a language-independent enum, without inferring anything from what
    happens to be visible. Used to tell "the file was accepted" from "still sitting on
    the drop pane", and quoted in errors so a failure report says exactly where it stopped.
    """
    try:
        return (
            page.locator(_DIALOG_SEL).first.get_attribute(_DIALOG_STEP_ATTR, timeout=3_000) or ""
        )
    except Exception:
        return ""


def _click_by_text(page, texts, *, timeout_ms: int = _STEP_WAIT_MS) -> bool:
    """Click the first Studio button whose label matches any of `texts`. False if none.

    Only for controls with no stable id. `has-text` is a substring match, so "Tiếp"
    also catches "Tiếp theo" — intended.
    """
    for text in texts:
        selector = (
            f'ytcp-button:has-text("{text}"), tp-yt-paper-button:has-text("{text}"), '
            f'button:has-text("{text}")'
        )
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=timeout_ms // len(texts) or 1_000):
                locator.click()
                return True
        except Exception:
            continue
    return False


def _click_any(page, selector: str, texts=(), *, timeout_ms: int = _STEP_WAIT_MS) -> bool:
    """Click by id first, fall back to label text. The house pattern for every button."""
    locator = _first_present(page, selector, timeout_ms=timeout_ms)
    if locator is not None:
        try:
            locator.click()
            return True
        except Exception:
            pass
    return _click_by_text(page, texts, timeout_ms=timeout_ms) if texts else False


def _fill_box(page, selector: str, text: str, *, timeout_ms: int = _STEP_WAIT_MS) -> None:
    """Replace the contents of one of Studio's contenteditable boxes (title/description).

    Three things this has to get right:

    * They're `contenteditable` divs behind a Polymer wrapper, not `<input>`s, so
      `fill()` silently no-ops on some builds. Click and drive the keyboard instead.
    * Studio pre-fills the title with the *filename*. The select-all + delete is
      mandatory, not defensive: without it every title becomes "slug-0001-0010Tên
      truyện - Phần 1".
    * `insert_text` rather than `type()`. A part description is the whole chapter
      timestamp table — several KB — and per-keystroke typing would take the better
      part of a minute and drop characters along the way. `insert_text` still fires the
      input events the Polymer binding listens for, so the value is committed.
    """
    box = _first_present(page, selector, timeout_ms=timeout_ms)
    if box is None:
        raise YouTubeUploadError(
            "Không tìm thấy ô nhập trong YouTube Studio (giao diện có thể đã thay đổi)."
        )
    box.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    if text:
        page.keyboard.insert_text(text)


def _check_cancel(should_cancel, *, video_id: str = "") -> None:
    if should_cancel is not None and should_cancel():
        raise UploadCancelled(video_id=video_id)


def _report(on_progress, message: str) -> None:
    if on_progress is not None:
        on_progress(message)


# -- one-time login -----------------------------------------------------------


def clear_profile() -> bool:
    """Delete the dedicated profile, signing the app out of YouTube. True if one existed.

    The reliable way to change channel. YouTube's own switcher only moves between brand
    channels *within* one Google account, so it can't fix "signed in as the wrong
    account" — and its DOM is one more thing to keep working. Dropping the profile
    resets both cases at the cost of one 30-second sign-in.

    Only ever removes `profile_dir()` itself, and only when it looks like a browser
    profile: this deletes a directory tree, so it verifies what it is aiming at first.
    """
    import shutil

    path = profile_dir()
    if not path.is_dir():
        return False
    # A persistent Chromium profile always has these. If neither is present we are not
    # looking at what we think we are, and deleting recursively would be reckless.
    if not any((path / name).exists() for name in ("Default", "Local State")):
        raise YouTubeUploadError(
            f"Thư mục {path} không giống profile trình duyệt — không xoá để tránh mất "
            "dữ liệu. Hãy kiểm tra và xoá thủ công."
        )
    shutil.rmtree(path)
    return True


def _current_channel(page) -> tuple[str, str]:
    """(channel id, channel name) for the session on `page`; either may be "".

    Shown back to the user after a login so they can confirm they landed on the channel
    they meant — the whole failure mode this exists for is signing into the wrong one
    and not finding out until a video is public on it.
    """
    match = re.search(r"/channel/(UC[\w-]+)", page.url or "")
    channel_id = match.group(1) if match else ""
    for selector in (
        "ytcp-account-section #account-name",
        "#account-name",
        "ytcp-header #entity-name",
        "yt-formatted-string#account-name",
    ):
        try:
            name = (page.locator(selector).first.inner_text(timeout=3_000) or "").strip()
        except Exception:
            continue
        if name:
            return channel_id, name
    return channel_id, ""


def open_login(timeout_ms: int = 300_000, *, switch: bool = False) -> tuple[str, str]:
    """Open a visible Studio window so the channel's account can log in once.

    Blocks (run me in a worker thread) until Studio finishes loading a channel — either
    immediately (session still valid) or after the user signs in — then closes. The
    session is saved in the persistent profile for later uploads. Returns
    `(channel_id, channel_name)` so the caller can show *which* channel got connected.

    `switch=True` drops the existing profile first, so the sign-in starts from scratch
    on Google's account chooser. Without it, an already-valid session matches
    immediately and the window closes before the user can change anything — which is
    exactly the trap someone who signed into the wrong channel falls into.

    Headless is never an option here: Google's sign-in throws captchas and new-device
    checks that a human has to clear, and a headless fingerprint invites more of them.
    """
    if switch:
        clear_profile()
    sync_playwright = _require_playwright()
    playwright, context = _launch_context(sync_playwright, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(_STUDIO_HOME, wait_until="domcontentloaded")
        page.wait_for_url(_LOGGED_IN_URL_RE, timeout=timeout_ms)
        return _current_channel(page)
    except Exception as exc:  # window closed early, timeout, etc.
        raise YouTubeUploadError(f"Đăng nhập YouTube chưa hoàn tất: {exc}") from exc
    finally:
        _close(context, playwright)


# -- the Studio upload flow, step by step -------------------------------------


def _open_upload_dialog(page, *, timeout_ms: int):
    """Navigate to the upload page and wait for the metadata dialog to mount."""
    page.goto(_UPLOAD_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass  # Studio keeps long-poll connections open; networkidle often never fires

    if _LOGGED_OUT_URL_RE.search(page.url):
        raise YouTubeUploadError(
            "Profile YouTube chưa đăng nhập. Vào Settings → “Đăng nhập YouTube” để "
            "đăng nhập kênh một lần.",
            needs_login=True,
        )
    # `attached`, never `visible`: the dialog is a zero-size wrapper around an overlay,
    # so Playwright reports it hidden even while it fills the screen. Waiting for
    # visibility here is what made the first live runs hang until the user killed Chrome.
    dialog = _first_present(page, _DIALOG_SEL, timeout_ms=_DIALOG_WAIT_MS, state="attached")
    if dialog is None:
        raise YouTubeUploadError(
            "Không mở được hộp thoại tải lên của YouTube Studio. Thử đăng nhập lại, "
            "hoặc tải lên thủ công."
        )
    return dialog


def _send_file(page, video: Path) -> None:
    """Hand the .mp4 to Studio, then confirm Studio actually took it.

    Two strategies, because this is the single step with no alternative — nothing else
    in the flow matters if the file never goes in:

    1. Set the `<input type=file>` directly. Preferred: no text matching, no OS dialog,
       works while the element is hidden (hence `state="attached"`, not `"visible"`).
    2. Click Studio's own "Select files" button inside `expect_file_chooser`, which
       intercepts the chooser before the OS shows it. Text-dependent and slower, but it
       doesn't care where the input lives.

    Then verify: the dialog must move off the drag-and-drop pane. Without that check a
    silent no-op sails on and fails ten steps later with a confusing message about the
    title box — which is exactly how the first live run presented.
    """
    for selector in _FILE_INPUT_SELS:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="attached", timeout=5_000)
            locator.set_input_files(str(video))
        except Exception:
            continue
        if _file_accepted(page):
            return

    try:
        with page.expect_file_chooser(timeout=_STEP_WAIT_MS) as chooser_info:
            if not _click_any(
                page, _SELECT_FILES_SEL, _SELECT_FILES_TEXTS, timeout_ms=10_000
            ):
                raise YouTubeUploadError(
                    "Không tìm thấy nút chọn file trong hộp thoại tải lên của YouTube "
                    "Studio (giao diện có thể đã thay đổi)."
                )
        chooser_info.value.set_files(str(video))
    except YouTubeUploadError:
        raise
    except Exception as exc:
        raise YouTubeUploadError(
            "Không đưa được file video vào YouTube Studio — giao diện Studio có thể đã "
            "thay đổi. Hãy tải lên thủ công lần này và báo lỗi để cập nhật selector."
        ) from exc

    if not _file_accepted(page):
        raise YouTubeUploadError(
            "Đã chọn file nhưng YouTube Studio không bắt đầu tải lên (vẫn ở bước "
            f"“{_dialog_step(page) or 'không rõ'}”). Hãy thử tải lên thủ công lần này."
        )


def _file_accepted(page, *, timeout_ms: int = 15_000) -> bool:
    """True once Studio has left the drag-and-drop pane for the metadata form.

    Primary signal is Studio's own `workflow-step` leaving `SELECT_FILES`. The title box
    and progress label are the fallback: both only exist after a file is taken, so
    either appearing is also proof the upload started.
    """
    deadline = timeout_ms
    while deadline > 0:
        step = _dialog_step(page)
        if step and step != _STEP_SELECT_FILES:
            return True
        if _first_present(page, _TITLE_SEL, timeout_ms=1_000) is not None:
            return True
        if _first_present(page, _PROGRESS_SEL, timeout_ms=1_000) is not None:
            return True
        deadline -= 2_000
    return False


def _grab_video_id(page, *, timeout_ms: int = _STEP_WAIT_MS) -> str:
    """The draft's video id, which Studio shows within seconds of the upload starting.

    Grabbed as early as possible and recorded before anything else can fail: knowing
    the id is what turns "something might be half-uploaded on your channel" into "go
    look at this exact video".
    """
    locator = _first_present(page, _VIDEO_URL_SEL, timeout_ms=timeout_ms)
    if locator is None:
        return ""
    for getter in (lambda: locator.get_attribute("href"), locator.inner_text):
        try:
            match = _VIDEO_ID_RE.search(getter() or "")
        except Exception:
            continue
        if match:
            return match.group(1)
    return ""


def transfer_state(text: str) -> tuple[bool, int | None]:
    """Read Studio's progress line: `(transfer finished?, percent or None)`. Pure.

    Studio writes things like "Uploading 17%…", "Đang tải lên 17%…", "Upload complete.
    Now processing…", "Processing up to HD", "Checks complete. No issues found." The
    transfer is finished once the line stops saying *uploading* — anything about
    processing or checks happens after the bytes are in.

    **This is the gate that decides when it is safe to move to the next part**, so it is
    pure and tested rather than inferred from the DOM. Getting it wrong queues every
    upload at once and then kills them all by closing the browser, which is exactly what
    happened when the gate was "the publish button is enabled": Studio does *not* disable
    that button during a transfer, so it was always true and nothing ever waited.
    """
    text = (text or "").strip()
    if not text:
        return False, None
    match = _PERCENT_RE.search(text)
    percent = int(match.group(1)) if match else None
    if _UPLOADING_RE.search(text):
        # Still transferring — unless it is sitting at a full 100%.
        return (percent is not None and percent >= 100), percent
    if _TRANSFER_DONE_RE.search(text):
        return True, percent
    # A line that mentions neither is unrecognised; treat it as still going rather than
    # assume completion. Erring this way costs time; erring the other way loses uploads.
    return False, percent


def _wait_for_bytes_uploaded(page, *, on_progress, should_cancel, video_id: str) -> None:
    """Block until Studio has all of this part's bytes, reporting progress meanwhile.

    We wait for the *bytes*, never for *processing*. YouTube processes a multi-hour video
    for hours, publishing is allowed while that runs, and the video becomes available as
    it finishes. Waiting for processing would turn a 12-part batch into a multi-day job.

    But the transfer itself we must wait for: the browser closes at the end of the run,
    and anything still in flight dies with it.
    """
    waited = 0
    last = ""
    seen_label = False
    while waited < _UPLOAD_WAIT_MS:
        _check_cancel(should_cancel, video_id=video_id)
        try:
            text = (page.locator(_PROGRESS_SEL).first.inner_text() or "").strip()
        except Exception:
            text = ""
        if text:
            seen_label = True
            if text != last:
                _report(on_progress, text)
                last = text
            finished, _percent = transfer_state(text)
            if finished:
                return
        page.wait_for_timeout(_UPLOAD_POLL_MS)
        waited += _UPLOAD_POLL_MS

    if not seen_label:
        # Never found the progress line at all. Publishing now would be a guess, and a
        # wrong guess abandons the transfer — so stop and say so.
        raise YouTubeUploadError(
            "Không đọc được tiến độ tải lên của YouTube Studio (giao diện có thể đã "
            f"thay đổi; bước hiện tại: “{_dialog_step(page) or 'không rõ'}”). Dừng lại "
            "để không bỏ dở video đang tải.",
            video_id=video_id,
        )
    raise YouTubeUploadError(
        f"Quá thời gian chờ YouTube nhận file (dừng ở “{last}”). Kiểm tra mạng rồi thử lại.",
        video_id=video_id,
    )


def _set_playlist(page, name: str, *, created_playlists: set | None = None) -> None:
    """Tick `name` in the playlist picker, creating the playlist if it doesn't exist.

    Best-effort by design: a playlist is organisational, and failing the whole upload
    over it would be a bad trade. Anything unexpected leaves the picker closed and the
    video in no playlist.

    `created_playlists` carries the names this *session* already created. If one of them
    isn't found on a later part, that's a flaky search, not a missing playlist — and
    creating it again would leave two identically-named playlists on the channel. We
    skip instead, which costs one part its playlist entry and nothing else.
    """
    if not _click_any(page, _PLAYLIST_TRIGGER_SEL):
        return
    page.wait_for_timeout(_SETTLE_MS)

    search = _first_present(page, _PLAYLIST_SEARCH_SEL, timeout_ms=_STEP_WAIT_MS)
    if search is not None:
        search.click()
        page.keyboard.type(name, delay=_TYPE_DELAY_MS)
        page.wait_for_timeout(_SETTLE_MS)

    # An exact-title match wins; a substring match would tick "Phần 1" for "Phần 10".
    existing = page.locator(f'{_PLAYLIST_ITEM_SEL}:has-text("{name}")').first
    matched = False
    try:
        if existing.is_visible(timeout=3_000):
            for line in existing.inner_text().splitlines():
                if line.strip() == name:
                    existing.click()
                    matched = True
                    break
    except Exception:
        matched = False

    already_created = created_playlists is not None and name in created_playlists
    if not matched and not already_created:
        if _click_any(page, _PLAYLIST_NEW_SEL):
            page.wait_for_timeout(_SETTLE_MS)
            title_box = _first_present(page, _PLAYLIST_NEW_TITLE_SEL, timeout_ms=_STEP_WAIT_MS)
            if title_box is not None:
                title_box.click()
                page.keyboard.type(name, delay=_TYPE_DELAY_MS)
                _click_by_text(page, _PLAYLIST_NEW_CREATE_TEXTS)
                page.wait_for_timeout(_SETTLE_MS)
                if created_playlists is not None:
                    created_playlists.add(name)

    _click_by_text(page, _PLAYLIST_DONE_TEXTS)
    page.wait_for_timeout(_SETTLE_MS)


def _set_language(page) -> None:
    """Set the video language to Vietnamese. Best-effort, like the playlist."""
    if not _click_any(page, _LANGUAGE_TRIGGER_SEL):
        return
    page.wait_for_timeout(_SETTLE_MS)
    search = _first_present(page, _LANGUAGE_SEARCH_SEL, timeout_ms=5_000)
    if search is not None:
        search.click()
        page.keyboard.type(_VIETNAMESE_TEXTS[0], delay=_TYPE_DELAY_MS)
        page.wait_for_timeout(_SETTLE_MS)
    for text in _VIETNAMESE_TEXTS:
        item = page.locator(f'{_LANGUAGE_ITEM_SEL}:has-text("{text}")').first
        try:
            if item.is_visible(timeout=3_000):
                item.click()
                page.wait_for_timeout(_SETTLE_MS)
                return
        except Exception:
            continue
    page.keyboard.press("Escape")  # leave no dropdown covering the Next button


def _set_details(page, request: UploadRequest, *, on_progress, created_playlists=None) -> None:
    """Fill step 1: title, description, thumbnail, playlist, audience, tags, language."""
    _report(on_progress, "Điền tiêu đề và mô tả…")
    _fill_box(page, _TITLE_SEL, request.title)
    if request.description:
        _fill_box(page, _DESCRIPTION_SEL, request.description)

    if request.thumbnail is not None:
        _report(on_progress, "Tải ảnh bìa…")
        thumb = page.locator(_THUMBNAIL_INPUT_SEL).first
        try:
            thumb.wait_for(state="attached", timeout=_STEP_WAIT_MS)
            thumb.set_input_files(str(request.thumbnail))
            page.wait_for_timeout(_SETTLE_MS)
        except Exception:
            # A channel without thumbnail privileges (unverified) has no such input.
            _report(on_progress, "Bỏ qua ảnh bìa (kênh chưa được phép đặt ảnh bìa).")

    if request.playlist:
        _report(on_progress, "Thêm vào danh sách phát…")
        _set_playlist(page, request.playlist, created_playlists=created_playlists)

    # Mandatory: Studio refuses to publish until the made-for-kids question is answered.
    _report(on_progress, "Chọn “không dành cho trẻ em”…")
    kids = _first_present(page, _MADE_FOR_KIDS_NO_SEL)
    if kids is None:
        raise YouTubeUploadError(
            "Không tìm thấy mục “Video này có dành cho trẻ em không?”. YouTube bắt buộc "
            "trả lời mục này — hãy tải lên thủ công lần này."
        )
    kids.click()

    # Tags and language live behind "Hiện thêm"/"Show more".
    _click_any(page, _SHOW_MORE_SEL, ("Hiện thêm", "Show more"))
    page.wait_for_timeout(_SETTLE_MS)

    if request.tags:
        _report(on_progress, "Điền tags…")
        tags_box = _first_present(page, _TAGS_INPUT_SEL, timeout_ms=10_000)
        if tags_box is not None:
            tags_box.click()
            # Studio turns each comma into a chip, so the comma-joined string from
            # `<name>.tags.txt` can go in as-is.
            page.keyboard.type(request.tags, delay=_TYPE_DELAY_MS)

    _report(on_progress, "Đặt ngôn ngữ video…")
    _set_language(page)


def _advance_to_visibility(page) -> None:
    """Click through the Monetisation / Ad-suitability / Checks steps to step 4.

    Studio shows a different number of these depending on the channel (monetised or
    not), so we click Next until it stops being offered rather than a fixed 3 times.
    """
    for _ in range(5):
        if not _click_any(page, _NEXT_SEL, _NEXT_TEXTS, timeout_ms=10_000):
            return
        page.wait_for_timeout(_SETTLE_MS)


def _set_visibility(page, request: UploadRequest, *, on_progress) -> None:
    """Pick public/unlisted/private, or fill the schedule date+time."""
    if request.visibility != "schedule":
        _report(on_progress, "Đặt chế độ hiển thị…")
        radio = _first_present(page, _VISIBILITY_RADIOS[request.visibility])
        if radio is None:
            raise YouTubeUploadError(
                "Không tìm thấy mục chế độ hiển thị trong YouTube Studio."
            )
        radio.click()
        page.wait_for_timeout(_SETTLE_MS)
        return

    when = request.publish_at
    _report(on_progress, f"Hẹn giờ đăng: {when:%d/%m/%Y %H:%M}…")
    if not _click_any(page, _SCHEDULE_RADIO_SEL, ("Lên lịch", "Schedule")):
        raise YouTubeUploadError("Không tìm thấy mục hẹn giờ đăng trong YouTube Studio.")
    page.wait_for_timeout(_SETTLE_MS)

    vietnamese = _page_is_vietnamese(page)
    _click_any(page, _DATE_TRIGGER_SEL, timeout_ms=10_000)
    page.wait_for_timeout(_SETTLE_MS)

    date_box = _first_present(page, _DATE_INPUT_SEL, timeout_ms=10_000)
    if date_box is None:
        raise YouTubeUploadError(
            "Không tìm thấy ô chọn ngày đăng. Hãy hẹn giờ thủ công cho phần này."
        )
    date_box.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(_format_date(when, vietnamese=vietnamese), delay=_TYPE_DELAY_MS)
    page.keyboard.press("Enter")
    page.wait_for_timeout(_SETTLE_MS)

    time_box = _first_present(page, _TIME_INPUT_SEL, timeout_ms=10_000)
    if time_box is not None:
        time_box.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type(_format_time(when, vietnamese=vietnamese), delay=_TYPE_DELAY_MS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(_SETTLE_MS)


def _finish(page, *, video_id: str) -> None:
    """Commit: click Xuất bản / Lên lịch / Lưu, and make sure it actually took."""
    if not _click_any(page, _DONE_SEL, _PUBLISH_TEXTS):
        raise YouTubeUploadError(
            "Không bấm được nút xuất bản. Video đang là bản nháp trên kênh — hãy kiểm "
            "tra và xuất bản thủ công.",
            video_id=video_id,
        )
    # Confirmation is the upload dialog going away, or Studio's post-publish share /
    # still-processing dialog appearing. NOT the dialog becoming "hidden": it is a
    # zero-size wrapper that reads as hidden from the moment it mounts, so that check
    # passed instantly and would have reported every publish a success without looking.
    waited = 0
    while waited < _STEP_WAIT_MS:
        if _first_present(page, _DIALOG_SEL, timeout_ms=1_000, state="detached") is not None:
            return
        if _first_present(page, _CONFIRM_DIALOG_SEL, timeout_ms=1_000, state="attached") is not None:
            return
        page.wait_for_timeout(1_000)
        waited += 3_000
    raise YouTubeUploadError(
        "Đã bấm xuất bản nhưng YouTube Studio không xác nhận (hộp thoại vẫn mở ở bước "
        f"“{_dialog_step(page) or 'không rõ'}”) — có thể còn mục bắt buộc chưa điền. "
        "Kiểm tra bản nháp trên kênh.",
        video_id=video_id,
    )


# -- the Studio edit page: replacing a published video's thumbnail ------------


def _open_edit_page(page, video_id: str, *, timeout_ms: int = _EDIT_PAGE_WAIT_MS):
    """Navigate to a video's edit page and confirm the metadata editor mounted.

    Three distinct failures get three distinct messages, because their fixes differ:
    not logged in (the one-time sign-in), Studio bouncing us away from the video (it
    was deleted, or the profile is on a different channel now), and the editor never
    mounting (DOM drift).
    """
    page.goto(_EDIT_URL.format(video_id=video_id), wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass  # Studio keeps long-poll connections open; networkidle often never fires

    if _LOGGED_OUT_URL_RE.search(page.url or ""):
        raise YouTubeUploadError(
            "Profile YouTube chưa đăng nhập. Vào Settings → “Đăng nhập YouTube” để "
            "đăng nhập kênh một lần.",
            needs_login=True,
            video_id=video_id,
        )
    if video_id not in (page.url or ""):
        raise YouTubeUploadError(
            f"Không mở được trang chỉnh sửa video {video_id} "
            f"(https://youtu.be/{video_id}). Video có thể đã bị xoá, thuộc kênh khác, "
            "hoặc phiên đăng nhập đã đổi kênh.",
            video_id=video_id,
        )
    # `attached`, never `visible` — the same zero-size `ytcp-*` wrapper trait that hung
    # the first live runs of the upload flow applies to every Studio container.
    editor = _first_present(page, _EDIT_PAGE_SEL, timeout_ms=timeout_ms, state="attached")
    if editor is None:
        raise YouTubeUploadError(
            "Trang chỉnh sửa video của YouTube Studio không mở ra (giao diện có thể đã "
            "thay đổi). Hãy đổi ảnh bìa thủ công lần này và báo lỗi để cập nhật selector.",
            video_id=video_id,
        )
    return editor


def _toast_text(page) -> str:
    """Whatever Studio's toast says right now, or "". Never raises."""
    try:
        return (page.locator(_TOAST_SEL).first.inner_text(timeout=1_000) or "").strip()
    except Exception:
        return ""


def _save_button_disabled(page) -> bool:
    """True when Studio's Save button is disabled — i.e. there is nothing left to save."""
    locator = _first_present(page, _SAVE_SEL, timeout_ms=2_000, state="attached")
    if locator is None:
        return False
    try:
        return locator.get_attribute("disabled") is not None
    except Exception:
        return False


def _thumbnail_accepted(page, *, timeout_ms: int = _THUMB_ACCEPT_MS) -> bool:
    """True once Save has become enabled — Studio's own "there is an unsaved change" bit.

    The counterpart to `_file_accepted`, and it exists for the same reason:
    `set_input_files` against a selector that matched the wrong element is a silent
    no-op, and without this check the run would sail on and report a successful save
    of nothing.
    """
    waited = 0
    while waited < timeout_ms:
        text = _toast_text(page)
        if text and _SAVE_ERROR_RE.search(text):
            return False  # Studio rejected the image; the caller quotes the toast
        if _first_present(page, _SAVE_SEL, timeout_ms=1_000, state="attached") is not None:
            if not _save_button_disabled(page):
                return True
        page.wait_for_timeout(1_000)
        waited += 2_000
    return False


def _send_thumbnail(page, image: Path, *, video_id: str) -> None:
    """Hand the image to the edit page's thumbnail editor, then prove Studio took it.

    Two strategies, the same ladder as `_send_file` and for the same reason — this is
    the step with no alternative. Set the input directly first (works while it's
    hidden); click Studio's own upload button inside `expect_file_chooser` second.

    **Unlike `_set_details`, a missing input is fatal here.** There the thumbnail is one
    field of an upload that must still finish, so a channel without thumbnail privileges
    should still get its video up. Here it *is* the job: continuing would report
    "đã cập nhật ảnh bìa" while the channel still shows the old cover.
    """
    for selector in _EDIT_THUMBNAIL_INPUT_SELS:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="attached", timeout=5_000)
            locator.set_input_files(str(image))
        except Exception:
            continue
        if _thumbnail_accepted(page):
            return

    try:
        with page.expect_file_chooser(timeout=_STEP_WAIT_MS) as chooser_info:
            if not _click_any(
                page, _EDIT_THUMBNAIL_BUTTON_SEL, _EDIT_THUMBNAIL_TEXTS, timeout_ms=10_000
            ):
                raise YouTubeUploadError(
                    "Không tìm thấy chỗ tải ảnh bìa trên trang chỉnh sửa video. Kênh có "
                    "thể chưa được phép đặt ảnh bìa tuỳ chỉnh (cần xác minh số điện "
                    "thoại), hoặc giao diện Studio đã thay đổi.",
                    video_id=video_id,
                )
        chooser_info.value.set_files(str(image))
    except YouTubeUploadError:
        raise
    except Exception as exc:
        # Quote Studio's own message when it has one: a rejection ("Ảnh quá lớn") names
        # the real problem, while the generic text sends the user hunting for a selector
        # bug that isn't there.
        said = _toast_text(page)
        raise YouTubeUploadError(
            "Không đưa được ảnh bìa vào YouTube Studio"
            + (f" — Studio báo: “{said}”." if said else " — giao diện có thể đã thay đổi.")
            + " Hãy đổi ảnh bìa thủ công lần này và báo lỗi để cập nhật selector.",
            video_id=video_id,
        ) from exc

    if not _thumbnail_accepted(page):
        said = _toast_text(page)
        raise YouTubeUploadError(
            "Đã chọn ảnh bìa nhưng YouTube Studio không ghi nhận thay đổi (nút “Lưu” "
            "vẫn tắt)."
            + (f" Studio báo: “{said}”." if said else "")
            + " Ảnh có thể sai định dạng, hoặc kênh chưa được phép đặt ảnh bìa tuỳ chỉnh.",
            video_id=video_id,
        )


def _save_edits(page, *, video_id: str) -> None:
    """Click Lưu / Save and require Studio to confirm it. Raises otherwise.

    Two accepted proofs, in order of trustworthiness: Save going back to *disabled*
    (Polymer's own "no unsaved changes" state), or a toast matching `_SAVED_RE`.

    Mirrors `_finish` — and its bug. Never confirm on a state that was already true
    before the click: `#save` is disabled on arrival, so "disabled" means nothing on its
    own. It only counts because `_thumbnail_accepted` already saw it enabled.
    """
    if not _click_any(page, _SAVE_SEL, _SAVE_TEXTS):
        raise YouTubeUploadError(
            "Không bấm được nút “Lưu” trên trang chỉnh sửa video (giao diện có thể đã "
            "thay đổi). Ảnh bìa chưa được đổi.",
            video_id=video_id,
        )
    waited = 0
    while waited < _SAVE_CONFIRM_MS:
        text = _toast_text(page)
        if text and _SAVE_ERROR_RE.search(text):
            raise YouTubeUploadError(
                f"YouTube Studio báo lỗi khi lưu ảnh bìa: “{text}”.", video_id=video_id
            )
        if text and _SAVED_RE.search(text):
            return
        if _save_button_disabled(page):
            return
        page.wait_for_timeout(1_000)
        waited += 1_000
    raise YouTubeUploadError(
        "Đã bấm “Lưu” nhưng YouTube Studio không xác nhận đã lưu ảnh bìa. "
        f"Kiểm tra video trên kênh: https://youtu.be/{video_id}",
        video_id=video_id,
    )


def _dismiss_unsaved_changes(page) -> None:
    """Clear Studio's "bỏ thay đổi?" guard so the next part can navigate. Never raises.

    Only appears when a part failed *after* its image went in. Without this, one failed
    part in a batch blocks every remaining part behind a modal nobody can see.
    """
    try:
        if _first_present(page, _DISCARD_DIALOG_SEL, timeout_ms=2_000, state="attached") is None:
            return
        _click_by_text(page, _DISCARD_TEXTS, timeout_ms=5_000)
        page.wait_for_timeout(_SETTLE_MS)
    except Exception:
        pass


# -- public entry points ------------------------------------------------------


def upload_one(
    page,
    request: UploadRequest,
    *,
    on_progress=None,
    should_cancel=None,
    created_playlists: set | None = None,
) -> UploadResult:
    """Run the whole Studio flow for one part on an already-open page.

    Split out from `upload_batch` so a batch can reuse a single browser, and so the
    flow is drivable from a test with a fake page. Records upload state as it goes;
    see the state-machine notes above.
    """
    request.validate()
    video = Path(request.video)

    if is_published(video):
        state = read_upload_state(video)
        return UploadResult(
            video_id=state.get("video_id", ""),
            url=state.get("url", ""),
            visibility=state.get("visibility", request.visibility),
            skipped=True,
        )
    if needs_attention(video):
        state = read_upload_state(video)
        raise YouTubeUploadError(
            f"{request.label or video.stem}: lần tải trước bị gián đoạn"
            + (f" (video {state['video_id']})" if state.get("video_id") else "")
            + ". Kiểm tra kênh rồi xoá file .upload.json nếu muốn tải lại — ứng dụng "
            "không tự tải lại để tránh đăng trùng.",
            video_id=state.get("video_id", ""),
        )

    _check_cancel(should_cancel)
    # Written BEFORE the bytes go out: if we die during the upload, the next run must
    # find evidence that something was attempted rather than cheerfully re-uploading.
    write_upload_state(video, status=STATE_STARTED, started_at=_now_iso(), title=request.title)

    _report(on_progress, "Mở hộp thoại tải lên…")
    _open_upload_dialog(page, timeout_ms=_STEP_WAIT_MS)

    _report(on_progress, "Gửi file video…")
    _send_file(page, video)

    # Studio reveals youtu.be/<id> while the bytes are still going up. Recording it here
    # is what turns "something might be half-uploaded on your channel" into "go look at
    # this exact video", so it happens before anything else can fail.
    video_id = _grab_video_id(page)
    if video_id:
        write_upload_state(
            video, status=STATE_DRAFT, video_id=video_id, url=f"https://youtu.be/{video_id}"
        )

    _set_details(page, request, on_progress=on_progress, created_playlists=created_playlists)
    _check_cancel(should_cancel, video_id=video_id)

    # Visibility lives on the last step, and so does the publish button we use as the
    # "transfer finished" signal — so advance first, then wait.
    _advance_to_visibility(page)
    _set_visibility(page, request, on_progress=on_progress)
    _check_cancel(should_cancel, video_id=video_id)

    _report(on_progress, "Chờ YouTube nhận đủ file…")
    _wait_for_bytes_uploaded(
        page, on_progress=on_progress, should_cancel=should_cancel, video_id=video_id
    )

    if not video_id:  # last chance before the dialog closes and the link goes away
        video_id = _grab_video_id(page, timeout_ms=5_000)

    # Past this fence cancellation is ignored and the state never goes backwards:
    # aborting mid-publish would produce exactly the "we don't know if it's live" state
    # this whole design exists to avoid.
    write_upload_state(
        video,
        status=STATE_COMMITTED,
        video_id=video_id,
        visibility=request.visibility,
        publish_at=request.publish_at.isoformat() if request.publish_at else "",
    )

    _report(on_progress, "Xuất bản…")
    _finish(page, video_id=video_id)

    url = f"https://youtu.be/{video_id}" if video_id else ""
    write_upload_state(
        video,
        status=STATE_PUBLISHED,
        video_id=video_id,
        url=url,
        visibility=request.visibility,
        published_at=_now_iso(),
        publish_at=request.publish_at.isoformat() if request.publish_at else "",
    )
    return UploadResult(
        video_id=video_id,
        url=url,
        visibility=request.visibility,
        publish_at=request.publish_at,
    )


def upload_batch(
    requests,
    *,
    headless: bool = False,
    on_progress=None,
    on_part_done=None,
    should_cancel=None,
) -> list:
    """Upload every request in order through ONE browser session. Returns the results.

    Runs headed by default, for the same reason `discord_unlock.run_unlock` does:
    Google challenges this profile hard, and a headless session invites a mid-run
    re-challenge that would read as a failure. The window is visible and the UI tells
    the user to leave it alone.

    A part that fails does NOT abort the run — the rest still upload, and the failure
    is reported per part via `on_part_done(index, result, error)`. One bad sidecar
    shouldn't cost the user the other nine uploads.
    """
    requests = list(requests)
    for request in requests:  # fail fast, before the browser costs anything
        request.validate()
    starts = [r.publish_at for r in requests if r.publish_at is not None]
    if starts:
        validate_schedule_start(min(starts))

    sync_playwright = _require_playwright()
    playwright, context = _launch_context(sync_playwright, headless=headless)
    results: list = []
    created_playlists: set = set()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(_STEP_WAIT_MS)
        for index, request in enumerate(requests):
            label = request.label or Path(request.video).stem
            _check_cancel(should_cancel)
            if index:
                # Human pacing between parts. Cheap, and the main thing separating
                # "automation" from "hammering".
                page.wait_for_timeout(_BETWEEN_PARTS_MS)
            try:
                result = upload_one(
                    page,
                    request,
                    on_progress=lambda msg, lbl=label: _report(on_progress, f"{lbl}: {msg}"),
                    should_cancel=should_cancel,
                    created_playlists=created_playlists,
                )
            except UploadCancelled:
                raise
            except YouTubeUploadError as exc:
                results.append(None)
                if on_part_done is not None:
                    on_part_done(index, None, str(exc))
                if exc.needs_login:
                    raise  # every remaining part would fail the same way
                continue
            results.append(result)
            if on_part_done is not None:
                on_part_done(index, result, "")
        # Closing the context kills anything the tab is still doing. Every part waited
        # for its own transfer before publishing, so nothing should be in flight — but
        # YouTube commits the publish asynchronously, and a couple of seconds here costs
        # nothing next to re-uploading a multi-GB part.
        try:
            page.wait_for_timeout(_SETTLE_BEFORE_CLOSE_MS)
        except Exception:
            pass
    finally:
        _close(context, playwright)
    return results


def update_thumbnail_one(
    page,
    request: ThumbnailRequest,
    *,
    on_progress=None,
    should_cancel=None,
) -> ThumbnailResult:
    """Replace one already-uploaded video's thumbnail, on an already-open page.

    Split from the batch for the same two reasons `upload_one` is: a batch reuses one
    browser, and the flow has to be drivable from a test with a fake page.

    Nothing is written to the record until Studio confirms the save, so a failure leaves
    the sidecar exactly as it was. There is no half-state to reason about here — which
    is what makes this action so much cheaper than an upload, and why it needs none of
    the write-before-the-irreversible-click machinery around publishing.
    """
    request.validate()
    video = Path(request.video)
    video_id = request.video_id or uploaded_video_id(video)
    if not video_id:
        raise YouTubeUploadError(
            f"{request.label or video.stem}: chưa có video trên YouTube cho phần này "
            "(hoặc phần này chỉ được đánh dấu thủ công), nên không biết đổi ảnh bìa của "
            "video nào."
        )

    _check_cancel(should_cancel)
    _report(on_progress, "Mở trang chỉnh sửa video…")
    _open_edit_page(page, video_id)

    _report(on_progress, "Gửi ảnh bìa mới…")
    _send_thumbnail(page, Path(request.thumbnail), video_id=video_id)

    # Last cancellation point. Past the save click there is nothing to abort, and
    # stopping between "YouTube took it" and "we recorded it" costs one redundant
    # re-push — cheap, unlike the publish fence in `upload_one`.
    _check_cancel(should_cancel, video_id=video_id)
    _report(on_progress, "Lưu thay đổi…")
    _save_edits(page, video_id=video_id)

    updated_at = _now_iso()
    # Merge-only, and deliberately narrow: `status`, `video_id` and `published_at` are
    # untouched. A thumbnail push cannot move the publication state machine, so writing
    # one here would be a lie the rest of the app would then act on.
    write_upload_state(
        video,
        thumbnail_updated_at=updated_at,
        thumbnail_file=Path(request.thumbnail).name,
    )
    return ThumbnailResult(
        video_id=video_id, url=f"https://youtu.be/{video_id}", updated_at=updated_at
    )


def update_thumbnail_batch(
    requests,
    *,
    headless: bool = False,
    on_progress=None,
    on_part_done=None,
    should_cancel=None,
) -> list:
    """Update every request's thumbnail through ONE browser session. Returns the results.

    Same contract as `upload_batch`: headed by default (Google challenges this profile
    hard, and a headless session invites a mid-run re-challenge), `on_part_done(index,
    result, error)` per part, and a failing part does NOT abort the run — except
    `needs_login`, where every remaining part would fail identically.

    Between parts it clears any leftover "bỏ thay đổi?" guard and paces itself. The
    pacing is shorter than an upload's because the work per part is a page load rather
    than a multi-GB transfer, but it is still there: this is a second automated Studio
    surface on the same profile.
    """
    requests = list(requests)
    for request in requests:  # fail fast, before the browser costs anything
        request.validate()

    sync_playwright = _require_playwright()
    playwright, context = _launch_context(sync_playwright, headless=headless)
    results: list = []
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(_STEP_WAIT_MS)
        for index, request in enumerate(requests):
            label = request.label or Path(request.video).stem
            _check_cancel(should_cancel)
            if index:
                _dismiss_unsaved_changes(page)
                page.wait_for_timeout(_BETWEEN_THUMBNAILS_MS)
            try:
                result = update_thumbnail_one(
                    page,
                    request,
                    on_progress=lambda msg, lbl=label: _report(on_progress, f"{lbl}: {msg}"),
                    should_cancel=should_cancel,
                )
            except UploadCancelled:
                raise
            except YouTubeUploadError as exc:
                results.append(None)
                if on_part_done is not None:
                    on_part_done(index, None, str(exc))
                if exc.needs_login:
                    raise  # every remaining part would fail the same way
                continue
            results.append(result)
            if on_part_done is not None:
                on_part_done(index, result, "")
    finally:
        _close(context, playwright)
    return results

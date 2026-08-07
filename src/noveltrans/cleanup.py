"""Delete local files whose job is done, once something else provably holds them.

A finished novel is mostly dead weight: per-chapter MP3s that only ever existed to be
rendered into a video, and rendered part-videos that are already on YouTube and mirrored
to OneDrive. Feature 051's own test novel is 21 GB, and about 20 of those are files the
user has no further use for.

**This module deletes the user's data, so every rule here is a refusal by default.**
Nothing is offered for deletion unless the thing that replaces it is *present and
checkable right now* — not "was uploaded once", not "should be there". The planning half
is pure and heavily tested; the deleting half is four lines and does only what the plan
says.

Two rules, and what each one waits for:

  * **Chapter audio** (`exports/audio/0041-….mp3`) — safe once a rendered part-video
    covers that chapter, because the audio is inside the video. The `.cues.json` beside
    it additionally waits for the part's `.srt`, since that is what the cues become.
  * **Part videos** (`exports/video/<part>/<part>.mp4`) — safe only when **both** copies
    exist: published on YouTube (its `.upload.json` says so) *and* **seen on OneDrive, by
    looking**, at the size the local file is now. One backup is not two.

    The OneDrive manifest is NOT accepted as proof, and that is not caution for its own
    sake: measured against the real library, it claimed 28 part-videos were `done` while
    the matching folders on OneDrive were **empty**. A manifest records what a run
    believed. Deleting 10 GB on the strength of a belief is how people lose things.

What is never offered, whatever the state:

  * `.upload.json` — the publication record. Deleting it makes the app believe the part
    was never uploaded, and the next run publishes the episode to the channel a second
    time. It is a few hundred bytes; there is no case for touching it.
  * `chapters.db`, `meta.json`, and every `.srt` / `.jpg` / `.title.txt` / `.tags.txt` /
    `.txt` — small, and each is needed to re-render or re-upload a part.
  * Anything at all when its replacement cannot be verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from noveltrans.onedrive_upload import STATUS_DONE, read_manifest
from noveltrans.storage.project import AUDIO_DIR, EXPORTS_DIR, VIDEO_DIR
from noveltrans.youtube_upload import is_published

KIND_AUDIO = "audio"
KIND_VIDEO = "video"

# `exports/audio/0041-chuong-41-….mp3` — the leading number is the chapter index.
_CHAPTER_INDEX_RE = re.compile(r"^(\d+)-")
# `exports/video/<slug>-0041-0060/` — the part's chapter range, trailing.
_PART_RANGE_RE = re.compile(r"-(\d+)-(\d+)$")

# Audio the renderer consumed. `.mp3`/`.wav` are the bulk; `.cues.json` is tiny but it is
# purely an intermediate on the way to the part's `.srt`.
_AUDIO_SUFFIXES = (".mp3", ".wav")
_CUES_SUFFIX = ".cues.json"

# The manifest records exact bytes, so comparing against IT is an equality test.
_SIZE_MUST_MATCH = True

# Comparing against ONEDRIVE is not, and assuming it was is a bug this nearly shipped:
# OneDrive shows a rounded size in the grid ("400,0 MB"), so a 419,928,664-byte video
# reads back as 419,430,400 and exact equality rejected every single file.
#
# 1% is the compromise. It is an order of magnitude wider than the display's rounding
# (one decimal place ⇒ ~0.0125% at MB scale) and far tighter than any real difference: a
# re-rendered part or a different video differs by whole percent. Deliberately much
# stricter than the 12% the *upload* path uses, because this one authorises a delete.
_DELETE_SIZE_TOLERANCE = 0.01


def sizes_match_for_delete(remote: int | None, local: int) -> bool:
    """Whether OneDrive's rounded size can be the local file. Pure, tested.

    `None` is never a match here — the opposite of the upload path, where an unreadable
    cell is assumed fine. Nothing gets deleted on a size we could not read.
    """
    if remote is None or local <= 0:
        return False
    return abs(remote - local) <= local * _DELETE_SIZE_TOLERANCE


@dataclass(frozen=True)
class Removable:
    """One file that may be deleted, and the reason it is safe to."""

    path: Path
    relpath: str
    size: int
    kind: str  # KIND_AUDIO | KIND_VIDEO
    reason: str  # shown to the user, in Vietnamese


def chapter_index(filename: str) -> int | None:
    """The chapter number an audio filename starts with, or None. Pure."""
    match = _CHAPTER_INDEX_RE.match(filename or "")
    return int(match.group(1)) if match else None


def part_range(folder_name: str) -> tuple[int, int] | None:
    """The `(first, last)` chapters a part folder covers, or None. Pure.

    Returns None rather than guessing when the name does not carry a range — a folder we
    cannot read the range of covers nothing, so nothing is deleted on its account.
    """
    match = _PART_RANGE_RE.search(folder_name or "")
    if not match:
        return None
    first, last = int(match.group(1)), int(match.group(2))
    return (first, last) if first <= last else (last, first)


def rendered_parts(project_path: Path) -> list[Path]:
    """Part folders that actually contain a rendered `.mp4`.

    A folder without one is a part that was set up and never rendered, and the chapters it
    names are emphatically not safe to delete audio for.
    """
    video_dir = Path(project_path) / EXPORTS_DIR / VIDEO_DIR
    if not video_dir.is_dir():
        return []
    return sorted(
        folder
        for folder in video_dir.iterdir()
        if folder.is_dir() and any(folder.glob("*.mp4"))
    )


def covered_chapters(project_path: Path) -> set[int]:
    """Every chapter number that a **rendered** part-video already contains."""
    covered: set[int] = set()
    for folder in rendered_parts(project_path):
        span = part_range(folder.name)
        if span:
            covered.update(range(span[0], span[1] + 1))
    return covered


def _parts_with_subtitles(project_path: Path) -> set[int]:
    """Chapters covered by a part that has BOTH a `.mp4` and a `.srt`."""
    covered: set[int] = set()
    for folder in rendered_parts(project_path):
        span = part_range(folder.name)
        if span and any(folder.glob("*.srt")):
            covered.update(range(span[0], span[1] + 1))
    return covered


def plan_audio_cleanup(project_path: Path) -> list[Removable]:
    """Chapter audio already baked into a rendered part-video.

    A chapter with no rendered part covering it is never offered, so a half-rendered
    novel loses nothing.
    """
    project_path = Path(project_path)
    audio_dir = project_path / EXPORTS_DIR / AUDIO_DIR
    if not audio_dir.is_dir():
        return []
    covered = covered_chapters(project_path)
    with_subs = _parts_with_subtitles(project_path)

    out: list[Removable] = []
    for path in sorted(audio_dir.iterdir()):
        if not path.is_file():
            continue
        index = chapter_index(path.name)
        if index is None:
            continue
        name = path.name.lower()
        if name.endswith(_CUES_SUFFIX):
            # Cues become the part's .srt; wait for that to exist before dropping them.
            if index not in with_subs:
                continue
            reason = "đã có phụ đề .srt của phần video"
        elif name.endswith(_AUDIO_SUFFIXES):
            if index not in covered:
                continue
            reason = "đã nằm trong video của phần"
        else:
            continue  # anything else in here is not ours to delete
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append(
            Removable(
                path=path,
                relpath=path.relative_to(project_path).as_posix(),
                size=size,
                kind=KIND_AUDIO,
                reason=reason,
            )
        )
    return out


def video_cleanup_candidates(project_path: Path) -> list[Removable]:
    """Part videos published on YouTube. **Candidates only — not safe to delete yet.**

    The second condition, "and it is on OneDrive", is deliberately NOT answered here.

    The manifest was the obvious source and it is not trustworthy enough to delete on.
    MEASURED against the real library: it claimed 28 part-videos were `done` while the
    matching folders on OneDrive were **empty**. A manifest records what a run believed;
    only OneDrive knows what OneDrive has, and the gap between the two is exactly the
    10 GB a user would have lost.

    So `verify_on_onedrive` goes and looks, and nothing here is offered for deletion until
    it has. That costs a browser session, which is the correct price for deleting a
    backup's only sibling.
    """
    project_path = Path(project_path)
    out: list[Removable] = []
    for folder in rendered_parts(project_path):
        for video in sorted(folder.glob("*.mp4")):
            if not is_published(video):
                continue
            try:
                size = video.stat().st_size
            except OSError:
                continue
            out.append(
                Removable(
                    path=video,
                    relpath=video.relative_to(project_path).as_posix(),
                    size=size,
                    kind=KIND_VIDEO,
                    reason="đã đăng YouTube — còn chờ kiểm tra bản trên OneDrive",
                )
            )
    return out


def manifest_claims_backed_up(project_path: Path, relpath: str, size: int) -> bool:
    """Whether the local manifest *believes* this file is mirrored at this size.

    Advisory only, and never sufficient on its own — see `video_cleanup_candidates` for
    what it got wrong. Useful for showing the user which parts are worth checking, and for
    skipping a live check that would obviously fail.
    """
    manifest = read_manifest(Path(project_path))
    if manifest.note:
        return False
    record = manifest.files.get(relpath)
    if not isinstance(record, dict) or record.get("status") != STATUS_DONE:
        return False
    return not _SIZE_MUST_MATCH or record.get("size") == size


def verify_on_onedrive(
    project_path: Path,
    candidates: list[Removable],
    *,
    remote_root: str = "",
    headless: bool = False,
    on_progress=None,
) -> tuple[list[Removable], list[Removable]]:
    """Look on OneDrive. Returns `(confirmed, unconfirmed)`.

    A file is confirmed only when the folder opens and the remote copy is **the size the
    local one is now**. Anything else — folder missing, file missing, size different,
    listing unreadable — lands in `unconfirmed` and is never offered for deletion.

    Opens a browser, so run me on a worker thread.
    """
    from noveltrans.onedrive_upload import (
        _close,
        _launch_context,
        _open_path,
        _open_root,
        _remote_sizes,
        _require_playwright,
        read_manifest,
    )

    project_path = Path(project_path)
    root = remote_root or read_manifest(project_path).remote_root
    if not root:
        return [], list(candidates)
    root_segments = [s for s in root.split("/") if s]

    confirmed: list[Removable] = []
    unconfirmed: list[Removable] = []
    playwright, context = _launch_context(_require_playwright(), headless=headless)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        _open_root(page)
        # Grouped by folder so each part is opened once, however many files it holds.
        by_folder: dict[str, list[Removable]] = {}
        for item in candidates:
            by_folder.setdefault(str(Path(item.relpath).parent), []).append(item)

        for index, (folder, items) in enumerate(sorted(by_folder.items())):
            if on_progress is not None:
                on_progress(index, len(by_folder), folder)
            segments = root_segments + [s for s in folder.split("/") if s and s != "."]
            _open_root(page)
            if not _open_path(page, segments):
                unconfirmed.extend(items)
                continue
            sizes = _remote_sizes(page)
            for item in items:
                name = Path(item.relpath).name
                remote = next(
                    (v for k, v in sizes.items() if k.strip() == name), None
                )
                if sizes_match_for_delete(remote, item.size):
                    confirmed.append(
                        Removable(
                            path=item.path,
                            relpath=item.relpath,
                            size=item.size,
                            kind=item.kind,
                            reason="đã đăng YouTube và đã kiểm tra có trên OneDrive",
                        )
                    )
                else:
                    unconfirmed.append(item)
    finally:
        _close(context, playwright)
    return confirmed, unconfirmed


def plan_cleanup(project_path: Path) -> list[Removable]:
    """What can be deleted **without** contacting OneDrive: the audio, and only the audio.

    Video deletion needs `video_cleanup_candidates` followed by `verify_on_onedrive`,
    because the local record of what is backed up has been measured wrong.
    """
    return plan_audio_cleanup(project_path)


def total_size(items: list[Removable]) -> int:
    return sum(item.size for item in items)


def remove_files(items: list[Removable]) -> tuple[int, int, list[str]]:
    """Delete exactly what the plan lists. Returns `(deleted, bytes freed, errors)`.

    Deliberately dumb: it re-derives nothing and re-checks nothing, so the only thing that
    can ever be deleted is what a `plan_*` function decided and the user then confirmed.
    """
    deleted = 0
    freed = 0
    errors: list[str] = []
    for item in items:
        try:
            item.path.unlink()
        except FileNotFoundError:
            continue  # already gone; nothing to report
        except OSError as exc:
            errors.append(f"{item.relpath}: {exc}")
            continue
        deleted += 1
        freed += item.size
    return deleted, freed, errors

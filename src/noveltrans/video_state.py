"""Manual override for a part's "Trạng thái" (đã tạo) tick — a sidecar beside the .mp4.

The "đã tạo" status is normally derived purely from disk: the .mp4 exists or it doesn't.
This module lets the user override that for a single part — e.g. to mark a part as done
when it's rendered/managed outside this app, or to flag a part that exists on disk as not
actually finished (needs a re-render).

Deliberately separate from `youtube_upload.py`'s `.upload.json`: "created" and "uploaded"
are independent facts about a part, and folding them into one file would mean clearing an
upload record (`clear_upload_state`) also silently erases an unrelated manual "created" tick.

The override is only ever persisted when it *disagrees* with disk — see
`set_created_override`. That keeps the common case (status matches disk, nobody has ever
touched the tick) free of sidecar files, and lets a real render or a deleted file make a
stale override self-heal on the next read.
"""

from __future__ import annotations

import json
from pathlib import Path

_STATE_EXT = ".created.json"


def state_path(video: Path) -> Path:
    """`<name>.created.json`, beside the .mp4 like every other sidecar."""
    video = Path(video)
    return video.parent / (video.stem + _STATE_EXT)


def read_state(video: Path) -> dict:
    """The part's recorded override, or `{}` if none was ever set (or it's unreadable).

    Unlike the upload sidecar, there's no "unresolved" state to protect here — a missing
    or corrupt file just means "no manual override", which is the same as never having
    touched the tick at all.
    """
    path = state_path(video)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(video: Path, **fields) -> dict:
    """Merge `fields` into the part's override file and return the merged dict."""
    state = read_state(video)
    state.update(fields)
    path = state_path(video)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX and on Windows for same-directory replaces
    return state


def created_override(video: Path) -> bool | None:
    """The manual override, or `None` if the status is purely automatic."""
    state = read_state(video)
    if "created" not in state:
        return None
    return bool(state["created"])


def set_created_override(video: Path, wanted: bool, *, file_exists: bool) -> None:
    """Record the user's tick, or clear it if it now agrees with disk.

    Only a *disagreement* with `file_exists` is worth remembering — once `wanted` matches
    reality again (a real render finished, or the file got deleted), the override has
    nothing left to say and is removed so it can't fight a future state change.
    """
    path = state_path(video)
    if wanted == file_exists:
        if path.is_file():
            path.unlink()
        return
    write_state(video, created=wanted)


def effective_created(video: Path) -> bool:
    """The status to show/act on: the manual override if set, else plain file existence."""
    override = created_override(video)
    if override is not None:
        return override
    return Path(video).is_file()

""""This part has a chapter inserted after it was rendered" — a sidecar beside the .mp4.

Written by `chapter_insert.apply_insert` (feature 104) on the one part whose span grew to
take a newly inserted chapter. Read by the video tab, which flags the part and offers a
re-render, and by everything that would otherwise treat the folder's span as an accurate
description of its video: the cleanup planner (that audio is still needed) and the
description resync (the .txt was written for the old chapter list).

`<stem>.inserted.json`, so it travels with every other sidecar when a part folder is
renamed:

    {"version": 1, "chapters": [45], "inserted_at": "…", "prompted": ""}

`prompted` records how far the video tab has nagged about it: "" (never), "waiting" (told
the user the new chapter needs audio first), "ready" (offered the re-render). A render that
included the inserted chapters removes them; an empty list removes the file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from noveltrans.tts.video import iter_rendered_part_dirs, video_part_name

_MARKER_EXT = ".inserted.json"
PROMPT_STATES = ("", "waiting", "ready")


def marker_path(video: Path) -> Path:
    video = Path(video)
    return video.parent / (video.stem + _MARKER_EXT)


def read_marker(video: Path) -> dict:
    """The marker, or `{}` when there is none (or it is unreadable — same thing here)."""
    try:
        data = json.loads(marker_path(video).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("chapters"), list):
        return {}
    try:
        data["chapters"] = sorted({int(n) for n in data["chapters"]})
    except (TypeError, ValueError):
        return {}
    if data.get("prompted") not in PROMPT_STATES:
        data["prompted"] = ""
    return data


def write_marker(video: Path, data: dict) -> None:
    """Write `data`, or remove the marker when it lists no chapters. Atomic."""
    path = marker_path(video)
    if not data.get("chapters"):
        path.unlink(missing_ok=True)
        return
    body = {
        "version": 1,
        "chapters": sorted({int(n) for n in data["chapters"]}),
        "inserted_at": data.get("inserted_at") or _now(),
        "prompted": data.get("prompted") if data.get("prompted") in PROMPT_STATES else "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def has_marker(video: Path) -> bool:
    return bool(read_marker(video).get("chapters"))


def clear_rendered(video: Path, rendered: set[int]) -> list[int]:
    """Drop the chapter numbers a fresh render included; returns what is still missing."""
    data = read_marker(video)
    if not data:
        return []
    left = [n for n in data["chapters"] if n not in rendered]
    if left != data["chapters"]:
        # What is left is a different situation from the one the user was asked about.
        write_marker(video, {**data, "chapters": left, "prompted": ""})
    return left


def set_prompted(video: Path, state: str) -> None:
    data = read_marker(video)
    if data and state in PROMPT_STATES:
        write_marker(video, {**data, "prompted": state})


def folder_has_marker(part_dir: Path) -> bool:
    """True when the part folder `part_dir` (named by its stem) carries a marker."""
    part_dir = Path(part_dir)
    return has_marker(part_dir / f"{part_dir.name}.mp4")


def inserted_parts(video_dir: Path, slug: str) -> list[tuple[Path, int | None, int | None, dict]]:
    """`(video, first, last, marker)` for every marked part of the CHAPTER edition.

    `first`/`last` are None for the whole-novel video. The source edition is never marked:
    its numbers are release ordinals, which a chapter insert does not move.
    """
    video_dir = Path(video_dir)
    found: list[tuple[Path, int | None, int | None, dict]] = []
    for part_dir, first, last in iter_rendered_part_dirs(video_dir, slug):
        video = part_dir / f"{part_dir.name}.mp4"
        marker = read_marker(video)
        if marker:
            found.append((video, first, last, marker))
    whole = video_dir / slug / video_part_name(slug, 0, 0, whole_novel=True)
    for video in (whole, video_dir / whole.name):
        marker = read_marker(video)
        if marker:
            found.append((video, None, None, marker))
            break
    return found


def marked_part_covering(video_dir: Path, slug: str, first: int, last: int) -> Path | None:
    """The .mp4 path of a marked part folder whose span contains `first..last`, or None.

    The window planner trims a window to the chapters that have audio, so while an
    inserted chapter at the edge of a part has none, the window is one chapter narrower
    than the folder. This is how that window still finds its folder and upload record.
    """
    for part_dir, a, b in iter_rendered_part_dirs(Path(video_dir), slug):
        if a <= first and last <= b and folder_has_marker(part_dir):
            return part_dir / f"{part_dir.name}.mp4"
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

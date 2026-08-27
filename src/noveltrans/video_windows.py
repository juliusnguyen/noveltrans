"""Manually forced part boundaries — from splitting an over-long part (YouTube's 12h cap)
or merging two adjacent short ones back together.

Persisted per NOVEL, not per-part: a boundary is a property of the whole plan, not of any
one rendered file. Stored as `{first_num: last_num}` in `video_manual_windows.json` at the
project root, next to `meta.json`/`chapters.db` — every future "Tạo video" run keeps
honoring it, the same way an already-"đã tạo" part's span gets locked (see
`noveltrans.tts.video.plan_locked_video_windows`, which callers feed this map into merged
with the disk-discovered "committed" map, manual entries taking precedence).

Merging is exactly the inverse of splitting: two adjacent windows collapse into the span
that a single (never-split) window would have covered, and a later merge of two split
halves is how a split gets undone — there's no separate "clear override" action.

CHAPTER numbers only. A novel can also have the site's own audio edition, whose parts are
keyed by release ordinal (see `noveltrans.tts.merge.plan_source_windows`), and this flat map
has no room for a second number space — an entry meant for one edition would silently
reshape the other's plan. So the source edition deliberately does not use this file, and the
split/merge menu entries are hidden for it. Supporting both would mean a versioned schema
(`{"chapters": {...}, "nguon": {...}}`) with a back-compat read of the flat form.
"""

from __future__ import annotations

import json
from pathlib import Path

_FILE_NAME = "video_manual_windows.json"


def manual_windows_path(project_path: Path) -> Path:
    """`video_manual_windows.json`, at the project root."""
    return Path(project_path) / _FILE_NAME


def read_manual_windows(project_path: Path) -> dict[int, int]:
    """`{first_num: last_num}` of every manually forced boundary, or `{}` if none set."""
    path = manual_windows_path(project_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[int, int] = {}
    for key, value in data.items():
        try:
            result[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def write_manual_windows(project_path: Path, windows: dict[int, int]) -> None:
    """Overwrite the manual-boundary file with `windows` (atomic temp-file + replace)."""
    path = manual_windows_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = {str(first): last for first, last in sorted(windows.items())}
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    tmp.replace(path)


def split_window(
    project_path: Path, first_num: int, last_num: int, tail_chapters: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Force `first_num`-`last_num` to split, its last `tail_chapters` becoming a new part.

    Returns the two new `(first, last)` spans. Raises `ValueError` if `tail_chapters`
    isn't strictly between 0 and the window's chapter count (a split must leave both
    halves non-empty).
    """
    total = last_num - first_num + 1
    if not (0 < tail_chapters < total):
        raise ValueError(
            f"Số chương cắt ra phải từ 1 đến {total - 1} (phần có {total} chương)."
        )
    cut = last_num - tail_chapters
    first_half = (first_num, cut)
    second_half = (cut + 1, last_num)

    windows = read_manual_windows(project_path)
    windows.pop(first_num, None)  # the old single-span entry, if any, no longer applies
    windows[first_half[0]] = first_half[1]
    windows[second_half[0]] = second_half[1]
    write_manual_windows(project_path, windows)
    return first_half, second_half


def merge_windows(
    project_path: Path, first_a: int, last_a: int, first_b: int, last_b: int
) -> tuple[int, int]:
    """Force two ADJACENT parts (`last_a + 1 == first_b`) to merge into one part.

    Returns the merged `(first, last)` span. Raises `ValueError` if the two windows
    aren't actually adjacent — merging across a gap would leave chapters out of order.
    """
    if last_a + 1 != first_b:
        raise ValueError("Chỉ có thể gộp 2 phần liền kề nhau (không có khoảng trống).")

    windows = read_manual_windows(project_path)
    windows.pop(first_a, None)
    windows.pop(first_b, None)
    windows[first_a] = last_b
    write_manual_windows(project_path, windows)
    return first_a, last_b

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

"Adjacent" cannot be `last_a + 1 == first_b`, because a window's ends are trimmed to the
chapters actually available in it (`tts.video.plan_locked_video_windows`), while the grid
the planner walks is numbered over every chapter. A DISABLED chapter (feature 098) sits in
between the two: chương 234 disabled makes the part after 223-233 display as 235-241, and
the two parts are contiguous in everything that gets rendered. So two parts are adjacent
when every chapter strictly between them is disabled — see `merge_adjacency_error`, which
owns both that rule and the message shown when it refuses, so the context menu's gating and
this module's own guard cannot drift apart.

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


def _describe_gap(blocking: list[int]) -> str:
    """"chương 234" / "chương 234, 236" / "chương 234, 236, 238 và 4 chương khác".

    Capped so a wide gap can't turn the tooltip into a wall of numbers.
    """
    shown = ", ".join(str(n) for n in blocking[:3])
    rest = len(blocking) - 3
    return f"chương {shown} và {rest} chương khác" if rest > 0 else f"chương {shown}"


def merge_adjacency_error(
    last_a: int, first_b: int, disabled_numbers: set[int] | None = None
) -> str | None:
    """Why these two parts can't be merged, or `None` if they can.

    The single owner of the adjacency rule AND of the text shown when it refuses, so
    `merge_windows` (the API guard) and the context menu's enable/tooltip state can't
    disagree about either — see the module docstring for why the rule isn't
    `last_a + 1 == first_b`.

    Adjacent means: `first_b` is after `last_a`, and every chapter strictly between them is
    disabled (which includes the ordinary case of no chapters between them at all). A gap
    holding any chapter that still renders is a real gap — merging across it would pull
    that chapter out of order — and so is a number with no chapter at all, or one not yet
    voiced: those can start rendering later and would then be swallowed into a part that
    may already be uploaded.

    The ordering test is not redundant: without it an overlapping pair (91-96 and 95-100)
    gives an empty `range` and would pass vacuously.
    """
    if first_b <= last_a:
        return (
            "Chỉ có thể gộp 2 phần liền kề nhau — phần sau phải bắt đầu ngay sau phần trước."
        )
    disabled = disabled_numbers or set()
    blocking = [n for n in range(last_a + 1, first_b) if n not in disabled]
    if blocking:
        return (
            f"Chỉ gộp được 2 phần liền kề: giữa 2 phần này còn {_describe_gap(blocking)} "
            "chưa bị bỏ qua.\n\nMuốn gộp thì hãy bỏ qua (các) chương đó trước, hoặc gộp "
            "lần lượt từng cặp phần liền kề."
        )
    return None


def merge_windows(
    project_path: Path,
    first_a: int,
    last_a: int,
    first_b: int,
    last_b: int,
    *,
    disabled_numbers: set[int] | None = None,
) -> tuple[int, int]:
    """Force two ADJACENT parts to merge into one part — see `merge_adjacency_error`.

    Returns the merged `(first, last)` span, which swallows any disabled chapter that sat
    between the two. That is deliberate: such a chapter is excluded from the render anyway,
    and if it is ever re-enabled it belongs to this merged part rather than reopening a
    boundary the user removed by hand.

    `disabled_numbers` is the set of disabled 1-based chapter numbers. Omitted, nothing
    counts as disabled, so the rule collapses to the pre-098 `last_a + 1 == first_b` — the
    fail-safe direction: a merge can only be refused, never wrongly allowed.

    Raises `ValueError`, with a message meant for a dialog, if the two aren't adjacent.
    """
    reason = merge_adjacency_error(last_a, first_b, disabled_numbers)
    if reason is not None:
        raise ValueError(reason)

    windows = read_manual_windows(project_path)
    windows.pop(first_a, None)
    windows.pop(first_b, None)
    windows[first_a] = last_b
    write_manual_windows(project_path, windows)
    return first_a, last_b

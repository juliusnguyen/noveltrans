"""Manual corrections to the "Phần N" numbering — when the published series is off by N.

The part number is normally pure arithmetic off the chapter range
(`noveltrans.tts.merge.part_number`, or a window's position in the whole-novel sequence once
`plan_locked_video_windows` starts freezing committed spans). That arithmetic is right about
the novel and can still be wrong about the CHANNEL: if an earlier part went up as two videos,
or the series started its numbering somewhere else, every part this app plans from then on
carries a number one or more off from what viewers already see.

Fixing that is a renumber, not a relabel: correcting "Phần 75" to "Phần 74" has to drag 76 to
75 and so on, because the parts after it are just as wrong. So what's stored is a DELTA at an
anchor chapter, and a window's number is its computed number plus every delta anchored at or
before it. Two independent corrections therefore compose by addition, with no "which pin
wins" rule, and a later manual split of an EARLIER part still legitimately pushes everything
after it up by one — the delta rides along instead of swallowing the new part.

Stored per NOVEL in `video_part_offsets.json` at the project root, next to
`video_manual_windows.json` — deliberately NOT inside it. That file's values are `last_num`,
a chapter number, and it is fed straight into `plan_locked_video_windows`'s `committed`
argument; a delta that leaked into that map would silently reshape a chapter span. A renumber
must move numbers and never spans, and separate files make the mistake impossible. For the
same reason nothing here touches `merge.part_number` or `plan_locked_video_windows`: they
keep computing the base numbering, and the offset is applied strictly on top of their output.

Numbers only — no file on disk is named after a part (`tts.video.video_part_name` uses the
chapter range), so unlike a split or a merge, a renumber renames and deletes nothing.

Unlike `video_windows.py`, this schema is versioned from the start and keyed by edition, so
the novel's own chapters and the site's audio edition ("nguồn", numbered by release ordinal
via `merge.plan_source_windows`) each get their own number space:

    {"version": 1, "chapters": {"1501": -1}, "nguon": {}}
"""

from __future__ import annotations

import json
from pathlib import Path

_FILE_NAME = "video_part_offsets.json"

_VERSION = 1

# The two number spaces, matching the two editions the video tab can plan.
_CHAPTER_KEY = "chapters"
_SOURCE_KEY = "nguon"


def part_offsets_path(project_path: Path) -> Path:
    """`video_part_offsets.json`, at the project root."""
    return Path(project_path) / _FILE_NAME


def _edition_key(source_audio: bool) -> str:
    return _SOURCE_KEY if source_audio else _CHAPTER_KEY


def _read_document(project_path: Path) -> dict:
    """The whole sidecar as a dict, or `{}` if it is missing or unreadable."""
    path = part_offsets_path(project_path)
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
    return data


def read_part_offsets(
    project_path: Path, *, source_audio: bool = False
) -> dict[int, int]:
    """One edition's `{anchor_first_num: delta}`, or `{}` if none set.

    Every failure mode — missing file, unparseable JSON, wrong shape, junk keys or values —
    reads as "no corrections", so a damaged sidecar degrades to the plain arithmetic
    numbering rather than breaking the video tab.
    """
    section = _read_document(project_path).get(_edition_key(source_audio))
    if not isinstance(section, dict):
        return {}
    result: dict[int, int] = {}
    for key, value in section.items():
        try:
            anchor, delta = int(key), int(value)
        except (TypeError, ValueError):
            continue
        if delta:  # a zero delta is the absence of a correction, not a correction
            result[anchor] = delta
    return result


def write_part_offsets(
    project_path: Path, offsets: dict[int, int], *, source_audio: bool = False
) -> None:
    """Replace one edition's offsets, leaving the other edition's untouched.

    Read-modify-write of the whole document rather than a plain overwrite: writing the
    chapter edition must never drop the source edition's corrections (atomic temp-file +
    replace, as `video_windows.write_manual_windows` does).
    """
    document = _read_document(project_path)
    body = {
        str(anchor): int(delta)
        for anchor, delta in sorted(offsets.items())
        if int(delta)
    }
    document["version"] = _VERSION
    document[_edition_key(source_audio)] = body
    # Keep the other edition present even when it has never been written, so the file is
    # self-describing to anyone who opens it.
    document.setdefault(_edition_key(not source_audio), {})

    path = part_offsets_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, indent=2), encoding="utf-8")
    tmp.replace(path)


def effective_part_number(
    first_num: int, base_part_num: int, offsets: dict[int, int]
) -> int:
    """`base_part_num` corrected by every delta anchored at or before `first_num`."""
    if not offsets:
        return base_part_num
    shift = sum(delta for anchor, delta in offsets.items() if anchor <= first_num)
    return base_part_num + shift


def apply_part_offsets(
    base_numbers: dict[int, int], offsets: dict[int, int]
) -> dict[int, int]:
    """Correct a whole `{first_num: base_part_num}` map at once.

    The single choke point both number engines (`tab_video._part_number` and
    `workers.VideoWorker.run`) go through, so the tab and the render worker cannot drift
    apart on what a part is called.
    """
    if not offsets:
        return dict(base_numbers)
    return {
        first_num: effective_part_number(first_num, base, offsets)
        for first_num, base in base_numbers.items()
    }


def plan_renumber(
    part_numbers: dict[int, int],
    offsets: dict[int, int],
    anchor_first_num: int,
    new_part_num: int,
) -> dict[int, int]:
    """The offsets map that renames `anchor_first_num`'s part to `new_part_num`.

    Pure — nothing is written. `part_numbers` is `{first_num: EFFECTIVE part number}` for
    the parts currently planned (what the user is looking at), not the base numbering.

    The anchor and every part after it shift by `new_part_num` minus the anchor's current
    number; parts before it keep the numbers they were published under, which is the whole
    point of the feature. Because the tail shifts by a constant and was already strictly
    increasing, only two things can go wrong, and both are refused:

    * a number below 1 — `build_upload_title` would cheerfully emit "Phần 0";
    * a collision with the part immediately before the anchor — two videos of one novel
      sharing a title is an operational hazard, not a cosmetic one, since
      `youtube_upload` matches playlist entries and Studio rows by exact title.
    """
    if anchor_first_num not in part_numbers:
        raise ValueError("Không tìm thấy phần cần đổi số.")
    new_part_num = int(new_part_num)
    if new_part_num < 1:
        raise ValueError("Số phần phải từ 1 trở lên.")

    earlier = [first for first in part_numbers if first < anchor_first_num]
    if earlier:
        prev_first = max(earlier)
        prev = part_numbers[prev_first]
        if new_part_num <= prev:
            # A dead end here would be unhelpful: wanting this part lower usually means the
            # numbering went wrong EARLIER, so name the part to renumber instead.
            raise ValueError(
                f"Số phần phải lớn hơn {prev} — Phần {prev} ngay trước đó đã dùng số này.\n\n"
                f"Nếu cả Phần {prev} cũng sai số, hãy đổi số từ Phần {prev} trở đi "
                f"(các phần sau sẽ tự dời theo)."
            )

    change = new_part_num - part_numbers[anchor_first_num]
    result = dict(offsets)
    updated = result.get(anchor_first_num, 0) + change
    if updated:
        result[anchor_first_num] = updated
    else:
        # Back to the automatic numbering at this anchor — drop it rather than store a 0,
        # so the menu can tell "has a correction here" from "had one once".
        result.pop(anchor_first_num, None)
    return result


def renumber_part(
    project_path: Path,
    part_numbers: dict[int, int],
    anchor_first_num: int,
    new_part_num: int,
    *,
    source_audio: bool = False,
) -> dict[int, int]:
    """Renumber and persist. Returns the new `{first_num: part number}` for every part.

    Raises `ValueError` (with a message meant for a dialog) without writing anything if the
    renumber is not allowed — see `plan_renumber`.
    """
    offsets = read_part_offsets(project_path, source_audio=source_audio)
    updated = plan_renumber(part_numbers, offsets, anchor_first_num, new_part_num)
    write_part_offsets(project_path, updated, source_audio=source_audio)

    change = new_part_num - part_numbers[anchor_first_num]
    return {
        first: num + (change if first >= anchor_first_num else 0)
        for first, num in part_numbers.items()
    }


def clear_part_offsets(project_path: Path, *, source_audio: bool = False) -> None:
    """Drop every correction for this edition — back to the automatic numbering."""
    write_part_offsets(project_path, {}, source_audio=source_audio)


def clear_part_offset_at(
    project_path: Path, anchor_first_num: int, *, source_audio: bool = False
) -> dict[int, int]:
    """Drop the correction anchored at `anchor_first_num`, keeping the others.

    The fine-grained undo: parts after this anchor lose only this breakpoint's shift, and
    any correction anchored further back still applies to them.
    """
    offsets = read_part_offsets(project_path, source_audio=source_audio)
    offsets.pop(int(anchor_first_num), None)
    write_part_offsets(project_path, offsets, source_audio=source_audio)
    return offsets

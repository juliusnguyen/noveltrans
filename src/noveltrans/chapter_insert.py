"""Inserting a chapter anywhere, and carrying every number-keyed file along (feature 104).

`NovelProject.insert_chapter` shifts the idx — and so the displayed number — of the chapters
after the insert point, up to the first gap. Everything stored in a chapter's row moves with
it. Everything on disk named by chapter number does not, so this module moves it:

    exports/audio/0012-<title>-<voice>.wav (+ .cues.json)   per-chapter audio
    exports/audio/<slug>-0011-0020.mp3                      merged audio
    exports/video/<slug>-0011-0020/<slug>-0011-0020.*       a part and all its sidecars
    exports/video/<slug>-0011-0020.*                        the pre-per-folder layout
    video_manual_windows.json, video_part_offsets.json      the video tab's corrections

The rule: **every part keeps exactly its chapters, except the one that takes the new
chapter.** That part's span grows by one and it is marked (`video_inserts`) so the video tab
can offer a re-render; every later part is renamed to its new numbers with its video
untouched. The source edition (`-nguon-`) is numbered by release, not chapter, and is left
alone.

**Plan, then apply** — the same split as `rename.py`. `plan_insert` touches nothing, so the
GUI can show what will happen. `apply_insert` moves files, writes the JSON, and makes the DB
change last; if any step fails it puts every file back and re-raises, so an insert either
happens completely or not at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from noveltrans.rename import Move
from noveltrans.storage.project import NovelProject
from noveltrans.tts.video import iter_rendered_part_dirs, video_part_name
from noveltrans.video_inserts import marker_path, read_marker
from noveltrans.video_part_numbers import (
    part_offsets_path,
    read_part_offsets,
    write_part_offsets,
)
from noveltrans.video_windows import (
    manual_windows_path,
    read_manual_windows,
    write_manual_windows,
)

_CHAPTER_AUDIO_RE = re.compile(r"^(\d+)-")


@dataclass(frozen=True)
class InsertShift:
    """How chapter numbers map across one insert.

    `new_num` (N) is the inserted chapter's number; old numbers N..`last_shifted` move up by
    one (`last_shifted` < N when the insert fills a hole). `owner` is the old number of the
    chapter whose part takes the new one, or None when it starts a part of its own.
    """

    new_num: int
    last_shifted: int
    owner: int | None

    def num(self, n: int) -> int:
        return n + 1 if self.new_num <= n <= self.last_shifted else n

    def first(self, n: int) -> int:
        # last_shifted + 1 is always an empty slot before the insert; a span starting there
        # must move too, or it would start on the chapter that just moved into that slot.
        return n + 1 if self.new_num <= n <= self.last_shifted + 1 else n

    def contains(self, a: int, b: int) -> bool:
        return self.owner is not None and a <= self.owner <= b

    def span(self, a: int, b: int) -> tuple[int, int]:
        if self.contains(a, b):
            return min(a, self.new_num), max(self.num(b), self.new_num)
        return self.first(a), self.num(b)


@dataclass
class AffectedPart:
    old: tuple[int, int] | None  # None = the whole-novel video
    new: tuple[int, int] | None
    uploaded: bool


@dataclass
class InsertPlan:
    at: int
    title: str
    shift: InsertShift
    moves: list[Move] = field(default_factory=list)
    delete_merged: list[Path] = field(default_factory=list)
    audio_relinks: dict[int, str] = field(default_factory=dict)  # NEW idx -> rel path
    manual_windows: dict[int, int] | None = None  # None = leave the file alone
    part_offsets: dict[int, int] | None = None
    markers: dict[Path, dict] = field(default_factory=dict)  # final .mp4 path -> body
    affected: list[AffectedPart] = field(default_factory=list)
    renamed_parts: int = 0
    collisions: list[Path] = field(default_factory=list)

    @property
    def renumbers(self) -> bool:
        return self.shift.last_shifted >= self.shift.new_num

    @property
    def audio_renames(self) -> int:
        return sum(1 for m in self.moves if m.kind == "audio")


def _owner(project: NovelProject, at: int, q: int, ref_idx: int, below: bool,
           spans: list[tuple[int, int]]) -> int | None:
    n = at + 1
    if below and not any(c.index > ref_idx for c in project.chapters()):
        return None  # appended after the novel's last chapter: it starts the next part
    if q == at and any(a <= n <= b for a, b in spans):
        return n  # filling a hole that a part already covers
    return ref_idx + 1


def plan_insert(
    project: NovelProject, at: int, title: str, ref_idx: int, *, below: bool
) -> InsertPlan:
    """Everything an insert at idx `at` would change, without changing any of it."""
    at_, q = project.insertion_span(at)
    slug = project.meta.slug_name()
    video_dir, audio_dir = project.video_dir, project.audio_dir
    parts = list(iter_rendered_part_dirs(video_dir, slug))
    flat = _flat_parts(video_dir, slug)
    windows = read_manual_windows(project.path)
    spans = [(a, b) for _, a, b in parts] + [(a, b) for _, a, b in flat] + list(windows.items())
    shift = InsertShift(at + 1, q, _owner(project, at, q, ref_idx, below, spans))
    plan = InsertPlan(at=at, title=title, shift=shift)
    sources: set[Path] = set()

    def add(src: Path, dst: Path, kind: str) -> None:
        plan.moves.append(Move(src, dst, kind))
        sources.add(src)

    # Per-chapter audio, highest first so no rename lands on a name not yet vacated.
    merged_re = re.compile(rf"^{re.escape(slug)}-(\d{{4}})-(\d{{4}})\.")
    if audio_dir.is_dir():
        chapter_files, merged = [], []
        for entry in sorted(audio_dir.iterdir()):
            if not entry.is_file():
                continue
            m = merged_re.match(entry.name)
            if m:
                merged.append((entry, int(m.group(1)), int(m.group(2))))
                continue
            m = _CHAPTER_AUDIO_RE.match(entry.name)
            if m and shift.num(int(m.group(1))) != int(m.group(1)):
                chapter_files.append((int(m.group(1)), entry))
        for n, entry in sorted(chapter_files, key=lambda t: -t[0]):
            rest = entry.name[len(entry.name.split("-", 1)[0]):]
            add(entry, entry.with_name(f"{n + 1:04d}{rest}"), "audio")
        for entry, a, b in sorted(merged, key=lambda t: -t[1]):
            if a < shift.new_num <= b:
                plan.delete_merged.append(entry)  # lost its continuity; the tab rebuilds it
                continue
            na, nb = shift.first(a), shift.num(b)
            if (na, nb) != (a, b):
                suffix = entry.name[merged_re.match(entry.name).end() - 1:]
                add(entry, entry.with_name(f"{slug}-{na:04d}-{nb:04d}{suffix}"), "audio")

    renamed = {m.src: m.dst for m in plan.moves}
    for chapter in project.chapters():
        if not chapter.audio_path:
            continue
        new_path = renamed.get(project.path / chapter.audio_path)
        if new_path is not None:
            new_idx = chapter.index + 1 if at <= chapter.index < q else chapter.index
            plan.audio_relinks[new_idx] = new_path.relative_to(project.path).as_posix()

    # Video parts, highest first; inside a folder, files before the folder itself.
    for part_dir, a, b in sorted(parts, key=lambda t: -t[1]):
        new = shift.span(a, b)
        new_stem = video_part_name(slug, *new)[: -len(".mp4")]
        final_video = video_dir / new_stem / f"{new_stem}.mp4"
        _plan_marker(plan, part_dir / f"{part_dir.name}.mp4", final_video, shift, (a, b))
        if new == (a, b):
            continue
        plan.renamed_parts += 0 if shift.contains(a, b) else 1
        for child in sorted(part_dir.iterdir()):
            if child.is_file() and child.name.startswith(part_dir.name + "."):
                add(child, part_dir / (new_stem + child.name[len(part_dir.name):]), "video-file")
        add(part_dir, video_dir / new_stem, "video-dir")
    for stem, a, b in sorted(flat, key=lambda t: -t[1]):
        new = shift.span(a, b)
        new_stem = video_part_name(slug, *new)[: -len(".mp4")]
        _plan_marker(plan, video_dir / f"{stem}.mp4", video_dir / f"{new_stem}.mp4", shift, (a, b))
        if new == (a, b):
            continue
        plan.renamed_parts += 0 if shift.contains(a, b) else 1
        for child in sorted(video_dir.iterdir()):
            if child.is_file() and child.name.startswith(stem + "."):
                add(child, video_dir / (new_stem + child.name[len(stem):]), "video-file")

    # The whole-novel video covers every chapter: always the containing part, never renamed.
    whole_name = video_part_name(slug, 0, 0, whole_novel=True)
    for whole in (video_dir / slug / whole_name, video_dir / whole_name):
        if whole.is_file():
            body = read_marker(whole) or {"chapters": []}
            chapters = [shift.num(n) for n in body["chapters"]] + [shift.new_num]
            plan.markers[whole] = {**body, "chapters": chapters, "prompted": ""}
            plan.affected.append(AffectedPart(None, None, _uploaded(whole)))
            break

    if windows:
        mapped = dict(shift.span(a, b) for a, b in windows.items())
        if mapped != windows:
            plan.manual_windows = mapped
    offsets = read_part_offsets(project.path)
    if offsets:
        firsts = {a: shift.span(a, b)[0] for a, b in spans}
        mapped = {firsts.get(anchor, shift.first(anchor)): d for anchor, d in offsets.items()}
        if mapped != offsets:
            plan.part_offsets = mapped

    for move in plan.moves:
        if move.dst.exists() and move.dst not in sources:
            plan.collisions.append(move.dst)
    return plan


def _plan_marker(plan: InsertPlan, video: Path, final_video: Path,
                 shift: InsertShift, old: tuple[int, int]) -> None:
    """Carry an existing marker's numbers across the shift, and mark the containing part."""
    body = read_marker(video)
    contains = shift.contains(*old)
    if not body and not contains:
        return
    chapters = [shift.num(n) for n in body.get("chapters", [])]
    if contains:
        chapters.append(shift.new_num)
        plan.affected.append(AffectedPart(old, shift.span(*old), _uploaded(video)))
    plan.markers[final_video] = {
        **body, "chapters": chapters, "prompted": "" if contains else body.get("prompted", ""),
    }


def _uploaded(video: Path) -> bool:
    return (video.parent / f"{video.stem}.upload.json").is_file()


def _flat_parts(video_dir: Path, slug: str) -> list[tuple[str, int, int]]:
    """`(stem, first, last)` of parts rendered flat in `video_dir`, before per-part folders."""
    if not video_dir.is_dir():
        return []
    stem_re = re.compile(rf"^({re.escape(slug)}-(\d{{4}})-(\d{{4}}))\.mp4$")
    found = []
    for entry in sorted(video_dir.iterdir()):
        m = stem_re.match(entry.name)
        if m and entry.is_file():
            found.append((m.group(1), int(m.group(2)), int(m.group(3))))
    return found


def apply_insert(project: NovelProject, plan: InsertPlan) -> list[str]:
    """Carry out `plan`; returns warnings about leftovers that did not block the insert.

    Moves, then the JSON files and markers, then the one DB transaction. Any failure up to
    and including the DB commit undoes every file change before re-raising, so the novel
    is never left half-renumbered. Deleting stale merged audio comes after the commit and
    can only produce a warning.
    """
    if plan.collisions:
        raise ValueError(f"trùng với file đã có: {plan.collisions[0].name}")
    # Files before their folder: each Move.src is recorded against today's folder.
    ordered = [m for m in plan.moves if m.kind != "video-dir"]
    ordered += [m for m in plan.moves if m.kind == "video-dir"]
    done: list[Move] = []
    backups: dict[Path, bytes | None] = {}

    def backup(path: Path) -> None:
        if path not in backups:
            backups[path] = path.read_bytes() if path.is_file() else None

    try:
        for move in ordered:
            move.src.rename(move.dst)
            done.append(move)
        if plan.manual_windows is not None:
            backup(manual_windows_path(project.path))
            write_manual_windows(project.path, plan.manual_windows)
        if plan.part_offsets is not None:
            backup(part_offsets_path(project.path))
            write_part_offsets(project.path, plan.part_offsets)
        from noveltrans.video_inserts import write_marker

        for video, body in plan.markers.items():
            backup(marker_path(video))
            write_marker(video, body)
        project.insert_chapter(plan.at, plan.title, audio_relinks=plan.audio_relinks)
    except BaseException:
        for path, raw in backups.items():
            try:
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(raw)
            except OSError:
                pass
        for move in reversed(done):
            try:
                move.dst.rename(move.src)
            except OSError:
                pass
        raise
    warnings = []
    for path in plan.delete_merged:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            warnings.append(f"Không xoá được file audio gộp cũ {path.name}: {exc}")
    return warnings

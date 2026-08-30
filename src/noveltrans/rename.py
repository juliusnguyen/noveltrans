"""Moving one novel's generated files when its filename stem changes.

Pure: no Qt, no sqlite, no `NovelProject`. It is handed two slugs and a project folder and
it walks the filesystem, in the same shape as `video_windows.py` and `name_glossary.py` —
which is what lets the risky half of a rename be tested exhaustively before any dialog
exists.

**Plan, then apply.** `plan_rename` touches nothing; it returns the complete list of moves,
their total size, any destination that already exists, and how many of the parts have an
upload record. The GUI shows that to the user and only then calls `apply_rename`. A plan
with collisions is refused whole rather than applied partly — two novels whose titles
slugify alike is not hypothetical, because `slugify` strips every non-ASCII character and
truncates at 40.

What is in scope is exactly what carries the stem:

    exports/video/<stem>/<stem>.{mp4,srt,jpg,title.txt,txt,tags.txt,upload.json,created.json}
    exports/video/<stem>.mp4                     (the pre-per-folder layout)
    exports/audio/<stem>.{mp3,m4b}

and nothing else. Per-chapter audio (`0001-<chapter>-<voice>.wav`), downloaded source audio
(`nguon-0001-….mp3`), `chapters.db`, `meta.json`, `names.json` and
`video_manual_windows.json` are all keyed to the chapter or to the novel's identity rather
than to its name, so a rename must leave them alone. See §2.5 of the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# `<stem>`, `<stem>-0001-0010`, and the source edition's `<stem>-nguon-0001-0010`. The
# `-nguon` marker is spelled in tts.video.edition_slug; it is duplicated here rather than
# imported because importing tts pulls in the whole render stack for one string.
_SUFFIX_RE = re.compile(r"^(?:-nguon)?(?:-\d{4}-\d{4})?$")

_AUDIO_EXTS = {".mp3", ".m4b"}

# The shape `tts.video.build_upload_title` produces: "{name} - Phần {N}".
_PART_TITLE_RE = re.compile(r"^(?P<name>.*?)\s+-\s+(?P<part>Phần\s+\d+)$")


@dataclass(frozen=True)
class Move:
    src: Path
    dst: Path
    kind: str  # "video-dir" | "video-file" | "audio"


@dataclass
class RenamePlan:
    old_slug: str
    new_slug: str
    moves: list[Move] = field(default_factory=list)
    total_bytes: int = 0
    collisions: list[Path] = field(default_factory=list)
    published: int = 0  # parts carrying an upload record

    @property
    def is_empty(self) -> bool:
        return not self.moves

    @property
    def is_safe(self) -> bool:
        return not self.collisions


def _renamed(name: str, old_slug: str, new_slug: str) -> str:
    """Swap the leading slug, leaving the part numbers and every extension alone."""
    return new_slug + name[len(old_slug) :]


def _matches(stem: str, slug: str) -> bool:
    """Is `stem` this novel's — the slug itself, or the slug plus an edition/part suffix?

    Anchored on both ends so a *different* novel whose slug merely starts the same way is
    never swept up: `trong-sinh` must not match `trong-sinh-lam-nong`, whose remainder
    (`-lam-nong`) is not an edition marker or a part range.
    """
    return stem.startswith(slug) and bool(_SUFFIX_RE.match(stem[len(slug) :]))


def plan_rename(project_path, old_slug: str, new_slug: str) -> RenamePlan:
    """Everything that would move, without moving any of it."""
    project_path = Path(project_path)
    plan = RenamePlan(old_slug=old_slug, new_slug=new_slug)
    if not old_slug or not new_slug or old_slug == new_slug:
        return plan

    video_dir = project_path / "exports" / "video"
    audio_dir = project_path / "exports" / "audio"

    if video_dir.is_dir():
        for entry in sorted(video_dir.iterdir()):
            if entry.is_dir():
                if not _matches(entry.name, old_slug):
                    continue
                new_dir_name = _renamed(entry.name, old_slug, new_slug)
                _add(plan, entry, video_dir / new_dir_name, "video-dir")
                # The files keep living in this folder; their new parent is the renamed
                # one, so their moves are expressed against the folder they are in TODAY
                # and applied before it moves. See apply_rename.
                for child in sorted(entry.iterdir()):
                    if not child.is_file() or not child.name.startswith(entry.name):
                        continue
                    _add(
                        plan,
                        child,
                        entry / _renamed(child.name, entry.name, new_dir_name),
                        "video-file",
                    )
                    if child.name.endswith(".upload.json"):
                        plan.published += 1
            elif entry.is_file() and _matches(entry.stem.split(".")[0], old_slug):
                # The pre-per-folder layout, still recognised by _part_output_path.
                _add(plan, entry, video_dir / _renamed(entry.name, old_slug, new_slug), "video-file")

    if audio_dir.is_dir():
        for entry in sorted(audio_dir.iterdir()):
            # Only the merged output. Per-chapter wavs and downloaded source audio are
            # chapter-keyed and carry no slug.
            if entry.is_file() and entry.suffix in _AUDIO_EXTS and _matches(entry.stem, old_slug):
                _add(plan, entry, audio_dir / _renamed(entry.name, old_slug, new_slug), "audio")

    return plan


def _add(plan: RenamePlan, src: Path, dst: Path, kind: str) -> None:
    if dst.exists():
        plan.collisions.append(dst)
        return
    plan.moves.append(Move(src=src, dst=dst, kind=kind))
    if src.is_file():
        plan.total_bytes += src.stat().st_size


def apply_rename(plan: RenamePlan) -> list[Move]:
    """Carry out the plan; returns what actually moved.

    **Files first, then their folder.** The opposite order would invalidate every
    `Move.src` the moment the folder moved — the plan records file paths against the
    folder they are in today. Renaming a file inside a not-yet-renamed folder is a fine
    intermediate state; a `Move.src` that no longer exists is not.

    Refuses a plan with collisions outright. Otherwise it moves as much as it can and
    reports back: a permission error or an antivirus lock partway through leaves the novel
    half-renamed, and the caller needs to know which half. `plan_rename` is idempotent
    over that state — re-running it finds only what is left.
    """
    if not plan.is_safe:
        raise ValueError(f"tên trùng với file đã có: {plan.collisions[0]}")
    done: list[Move] = []
    for move in sorted(plan.moves, key=lambda m: m.kind == "video-dir"):
        if not move.src.exists() or move.dst.exists():
            continue  # already done by an earlier, interrupted run
        move.src.rename(move.dst)
        done.append(move)
    return done


def resync_title_sidecars(video_dir, slug: str, new_name: str, known_names) -> int:
    """Rewrite `.title.txt` for every rendered part still carrying one of the old names.

    The third of the three write-once sidecars, and the one that was missed:
    `_resync_tags_sidecars` and `_resync_description_sidecars` both exist in the video tab,
    `.title.txt` had no equivalent, and `_upload_request` prefers the sidecar over
    recomputing. So renaming a novel left an already-rendered, not-yet-uploaded part
    scheduled to **publish to YouTube under the old name** — a bug that predates this
    feature and would have outlived it.

    Here rather than in the video tab because a rename can start from either tab and both
    must fix the sidecars; nothing about the job needs a widget.

    Parts come from DISK, not from a window selection: what was rendered need not match the
    mode/batch a user happens to have on screen. Both editions are included, unlike the
    description resync — a title is `{name} - Phần {N}` and the `N` is read back out of the
    sidecar itself, so nothing here depends on reconstructing which chapters a part covers
    (which is exactly what makes the description resync chapter-only).

    Only a name this novel has **actually gone by** is replaced (`known_names`: the previous
    display name, the override, the translated title, the original). Same regenerate-and-diff
    discipline the description resync uses, and it is what leaves a genuinely hand-written
    title ("Bản đặc biệt - Phần 3") alone.
    """
    video_dir = Path(video_dir)
    new_name = (new_name or "").strip()
    known = {n.strip() for n in known_names if (n or "").strip()}
    if not video_dir.is_dir() or not new_name or not known:
        return 0

    updated = 0
    for part_dir in sorted(video_dir.iterdir()):
        if not part_dir.is_dir() or not _matches(part_dir.name, slug):
            continue
        sidecar = part_dir / f"{part_dir.name}.title.txt"
        if not sidecar.is_file() or not (part_dir / f"{part_dir.name}.mp4").is_file():
            continue
        try:
            current = sidecar.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        match = _PART_TITLE_RE.match(current)
        if match and match.group("name").strip() in known:
            fresh = f"{new_name} - {match.group('part')}"
        elif current in known:
            fresh = new_name  # a whole-novel part: the title is just the name
        else:
            continue  # hand-written, or a name this novel never had
        if fresh == current:
            continue
        sidecar.write_text(fresh + "\n", encoding="utf-8")
        updated += 1
    return updated

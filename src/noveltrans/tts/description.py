"""Fit a YouTube video description inside YouTube's 5000-character budget.

The per-part description (`tts.video.build_video_description`) is a header block, a chapter
index with clickable timestamps, and a credit line. Only the index grows with the batch
size, so a part that gathers a hundred-odd chapters pushes the whole thing past the cap and
YouTube Studio truncates or rejects it. These helpers keep the mandatory lines and trim the
index instead, reporting how many chapters they had to drop.

Unlike `tts.tags` — whose docstring explains at length why it charges a deliberately
conservative worst-case cost, YouTube's tag counter being undocumented and observed to
disagree with a naive sum — the description limit is documented (`snippet.description`, max
5000) and Studio counts it plainly, with no separator or quoting surcharge. There is nothing
to guess at, so NO safety margin is added here: burning 5% of the budget "just in case"
would cost several chapters per part for no known reason.

The one place a naive `len()` genuinely diverges from Studio's counter is astral characters.
Studio counts JavaScript `String.length`, i.e. UTF-16 code units, where an emoji costs 2 and
Python's `len()` says 1; Vietnamese diacritics are BMP and cost 1 in both. So
`description_length` measures UTF-16 code units — tags.py's philosophy (model the counter,
don't guess) applied to a counter that happens to be knowable.

Trimming drops the TAIL of the index, never the head, and stops at the first line that does
not fit rather than skipping it and carrying on the way `parse_tags` does. Two reasons:
YouTube only turns a description into clickable chapters when the first timestamp is `0:00`
and they ascend, and unlike an unordered tag set a hole in the middle of a chapter index
reads as a bug.

`build_short_description` is the other half of the story — the `Shorten by AI` button's
target shape, which drops the novel title / author / credit entirely and keeps only the
index, so far more chapters fit.
"""

from __future__ import annotations

import re

# YouTube's documented cap for `snippet.description`. See the module docstring for why this
# is applied literally, with no headroom, unlike tags.py's 500-char tag budget.
YOUTUBE_DESCRIPTION_CHAR_LIMIT = 5000

# How short we ask the model to make each chapter title. Not enforced locally — see
# `build_shorten_prompt`.
SHORT_TITLE_MAX_CHARS = 35

# The dropped-chapters marker. Its leading "… " doubles as the sentinel `was_truncated`
# looks for, so a description read back off disk can be recognised as trimmed without
# re-deriving it.
TRUNCATION_PREFIX = "… còn "
_TRUNCATION_TEMPLATE = (
    TRUNCATION_PREFIX + "{dropped} chương nữa "
    "(mục lục rút gọn cho vừa giới hạn {limit} ký tự mô tả của YouTube)."
)

# "Chương 12:" / "chương 007 " / "Ch. 3 - " / bare "Chương 12" — the Vietnamese chapter
# prefix, plus the CJK form that survives in an untranslated title. The trailing
# alternation is what keeps this off ordinary titles: the number must be followed by a
# separator, whitespace, or the end of the string, so "Chỉ 5 phút" and "Chương trình" never
# match (neither has digits in the right place to begin with).
_CHAPTER_PREFIX = re.compile(
    r"^\s*(?:chương|chuong|ch\.?)\s*0*(\d+)\s*(?:[:.)\-–—]\s*|\s+|$)",
    re.IGNORECASE,
)
_CJK_CHAPTER_PREFIX = re.compile(r"^\s*第\s*0*(\d+)\s*[章回]\s*[:：.\-–—]?\s*")

_LEADING_NUMBERING = re.compile(r"^\s*(?:[-*•]\s*)?(?:\d+\s*[.)\]]|\d+\s*[-–—])\s*")

# `M:SS` / `H:MM:SS` followed by a title — one line of the chapter index.
_TIMESTAMP_LINE = re.compile(r"^\d+:\d{2}(?::\d{2})?\s")
_DROPPED_COUNT = re.compile(re.escape(TRUNCATION_PREFIX) + r"(\d+)")


def description_length(text: str) -> int:
    """Length as YouTube Studio counts it: UTF-16 code units, not Python characters.

    Identical to `len()` for anything in the BMP (all Vietnamese text); an emoji or a rare
    CJK extension character costs 2, matching JavaScript's `String.length`.
    """
    return len((text or "").encode("utf-16-le")) // 2


def _limit(max_chars: int | None) -> int:
    """Resolve the cap at CALL time, not as a bound default.

    A default bound at `def` time can't be monkeypatched, which would force every GUI test
    that wants to see truncation to build a 150-chapter fixture. Resolving here lets a test
    shrink `YOUTUBE_DESCRIPTION_CHAR_LIMIT` and drive the real code path with the small
    fixtures that already exist.
    """
    return YOUTUBE_DESCRIPTION_CHAR_LIMIT if max_chars is None else int(max_chars)


def truncation_line(dropped: int, limit: int) -> str:
    """The `… còn N chương nữa (…)` line that replaces the chapters that didn't fit.

    Deliberately carries no leading timestamp, so YouTube's chapter parser ignores it.
    """
    return _TRUNCATION_TEMPLATE.format(dropped=dropped, limit=limit)


def was_truncated(text: str) -> bool:
    """True when `text` carries the dropped-chapters marker."""
    return any(line.startswith(TRUNCATION_PREFIX) for line in (text or "").splitlines())


def indexed_chapter_count(text: str) -> int:
    """How many chapters a description's index accounts for: listed + reported dropped.

    Lets a caller tell "a chapter was renamed" (the count holds) from "a chapter was
    deleted" (it doesn't) without re-deriving the description — and counting the marker's
    own N is what keeps a *trimmed* index from looking like a shrunken one.
    """
    listed = sum(1 for line in (text or "").splitlines() if _TIMESTAMP_LINE.match(line))
    match = _DROPPED_COUNT.search(text or "")
    return listed + (int(match.group(1)) if match else 0)


def clamp_description(text: str, *, max_chars: int | None = None) -> str:
    """Hard-cut `text` to the cap, preferring a line boundary.

    The last-resort guarantee, and the choke point for descriptions this app did not just
    build — `UploadRequest` runs every description through it because a `.txt` sidecar may
    have been written by a version of this app that predates the cap, and re-rendering a
    part just to shorten its description is not something we can ask of the user.

    Cutting is done in UTF-16 space so a surrogate pair is never split down the middle, and
    backs off to the last newline inside the budget so the result ends on a whole timestamp
    line rather than mid-word.
    """
    text = text or ""
    limit = _limit(max_chars)
    if description_length(text) <= limit:
        return text
    cut = text.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")
    newline = cut.rfind("\n")
    return cut[: newline + 1] if newline > 0 else cut


def fit_description(
    before: list[str],
    chapter_lines: list[str],
    after: list[str],
    *,
    max_chars: int | None = None,
) -> tuple[str, int]:
    """`(text, chapters dropped)` — the mandatory lines kept, the index trimmed to fit.

    `before` and `after` are the lines that must survive (header block, credit line);
    `chapter_lines` is the trimmable index. Guarantees
    `description_length(result) <= max_chars` — if even `before + after` busts the budget
    the whole text is clamped, because a description YouTube truncates at a place of its
    own choosing is strictly worse than one we shortened deliberately.

    The marker's own length is reserved at its WORST case (rendered with every chapter
    dropped) before the greedy loop, which breaks the circularity of "how long is the
    marker" depending on "how many lines fit". `len(str(n))` is monotonic, so the
    reservation can only ever be too generous — never too small — at a cost of 1-3
    characters.
    """
    limit = _limit(max_chars)
    total = len(chapter_lines)

    def join(lines: list[str]) -> str:
        return "\n".join(lines) + "\n"

    whole = join([*before, *chapter_lines, *after])
    if description_length(whole) <= limit:
        return whole, 0

    # Each chapter line costs its own length plus the "\n" that joins it, so the fixed cost
    # is exactly the text without any of them.
    fixed = description_length(join([*before, *after]))
    reserved = description_length(truncation_line(total, limit)) + 1
    budget = limit - fixed - reserved

    kept: list[str] = []
    used = 0
    for line in chapter_lines:
        cost = description_length(line) + 1
        if used + cost > budget:
            break  # a hole in the middle of a chapter index reads as a bug — stop here
        kept.append(line)
        used += cost

    dropped = total - len(kept)
    tail = [truncation_line(dropped, limit)] if dropped else []
    text = join([*before, *kept, *tail, *after])
    return clamp_description(text, max_chars=limit), dropped


# -- the AI-shortened form (the "Shorten by AI" button) -----------------------

def split_chapter_number(title: str) -> tuple[int | None, str]:
    """`"Chương 12: Nhặt được chậu rách"` → `(12, "Nhặt được chậu rách")`.

    Returns `(None, title)` untouched when there is no recognisable prefix. Several
    scrapers only synthesise a "Chương N" title as a fallback, so a bare descriptive title
    is normal — and inventing a number for one would renumber the index, which is a far
    worse bug than a long description: every timestamp would then point at the wrong
    chapter in the reader's mind.
    """
    title = (title or "").strip()
    for pattern in (_CHAPTER_PREFIX, _CJK_CHAPTER_PREFIX):
        match = pattern.match(title)
        if match:
            number = next((g for g in match.groups() if g), None)
            rest = title[match.end():].strip()
            if number is not None:
                return int(number), rest
    return None, title


def short_chapter_label(number: int | None) -> str:
    """`12` → `"C.12"`; `None` → `""` (no label rather than a made-up number)."""
    return f"C.{number}" if number is not None else ""


def build_shorten_prompt(titles: list[str]) -> str:
    """A Vietnamese instruction asking the LLM to shorten each chapter title.

    Numbered in, numbered out, one line each and nothing else — the same "CHỈ trả về danh
    sách" discipline as `build_tags_prompt`. Only the DESCRIPTIVE half of a title is ever
    sent here; the chapter number is stripped beforehand and re-attached locally, so the
    model has no opportunity to renumber the index.
    """
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
    return (
        "Bạn là biên tập viên truyện tiếng Việt. Hãy RÚT GỌN từng tên chương dưới đây "
        "cho ngắn hơn nhưng GIỮ NGUYÊN Ý NGHĨA.\n\n"
        f"{listing}\n\n"
        "Yêu cầu:\n"
        f"- Mỗi tên chương rút gọn tối đa khoảng {SHORT_TITLE_MAX_CHARS} ký tự.\n"
        "- Giữ tiếng Việt có dấu, giữ tên riêng.\n"
        "- Không thêm dấu chấm ở cuối.\n"
        "- Tên nào đã đủ ngắn thì chép lại nguyên văn.\n"
        f"- Trả về ĐÚNG {len(titles)} dòng, đánh số 1..{len(titles)} theo đúng thứ tự trên.\n\n"
        "CHỈ trả về danh sách đã đánh số, không giải thích, không thêm chữ nào khác."
    )


def parse_shortened_titles(raw: str, originals: list[str]) -> tuple[list[str], bool]:
    """Numbered reply → one title per original, plus `ok` (False = fell back).

    A mismatched line count is never salvaged by pairing up whatever happens to line up: a
    list shifted by one attaches every title to the wrong timestamp, which is worse than
    not shortening at all. On any mismatch the originals come back unchanged.
    """
    raw_lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    # A model that prefixes its list with "Đây là danh sách:" would otherwise blow the
    # count and lose the whole chunk — when the numbered lines alone match, take those.
    numbered = [line for line in raw_lines if _LEADING_NUMBERING.match(line)]
    source = numbered if len(numbered) == len(originals) else raw_lines
    if len(source) != len(originals):
        return list(originals), False

    cleaned = []
    for line in source:
        title = _LEADING_NUMBERING.sub("", line).strip().strip('"').strip("'").strip()
        cleaned.append(re.sub(r"\s+", " ", title) or "")
    if any(not title for title in cleaned):
        return list(originals), False
    return cleaned, True


def build_short_description(
    entries: list[tuple[str, str, str]],
    *,
    total_chapters: int,
    extras_before: list[str] | tuple[str, ...] = (),
    extras_after: list[str] | tuple[str, ...] = (),
    max_chars: int | None = None,
) -> tuple[str, int, bool]:
    """`(text, chapters dropped, extras kept)` — the stripped, index-first description.

    `entries` are `(timestamp, label, short_title)` triples. The baseline form drops the
    `Tên truyện:` / `Tác giả:` lines and the trailing `Tạo bởi:` credit, keeping the
    `Mục lục chương:` block — which takes the mandatory skeleton from ~130 characters to
    ~32 and a chapter line from ~63 to ~50, so far more chapters fit.

    `extras_before` / `extras_after` are those dropped lines offered back: they are added
    only when they are FREE, meaning including them costs not one chapter off the index.
    That is the whole point of shortening, so the trade is never made silently — if the
    header would push even a single chapter out, the header is what goes. `extras_kept`
    says which way it went, so the caller can tell the user.

    Still routed through `fit_description`, so the 5000-char guarantee and the `… còn N
    chương nữa` marker apply here too: a shortened index for a huge part degrades exactly
    the way a full one does.
    """
    lines = [
        " ".join(part for part in entry if part).strip()
        for entry in entries
    ]
    before = [f"Số chương: {total_chapters}", "", "Mục lục chương:"]
    bare, dropped = fit_description(before, lines, [], max_chars=max_chars)
    if not extras_before and not extras_after:
        return bare, dropped, False

    with_extras, dropped_with = fit_description(
        [*extras_before, *before], lines, list(extras_after), max_chars=max_chars
    )
    if dropped_with == dropped:
        return with_extras, dropped_with, True
    return bare, dropped, False


def looks_generated(text: str) -> bool:
    """True when `text` has the shape `build_video_description` produces.

    Shape, not sentinel: sidecars already on users' disks predate this feature and could
    never carry a marker we add now, so staleness has to be judged from the text itself —
    a `Tên truyện: ` first line, a `Mục lục chương:` line, a `Tạo bởi: ` last non-empty
    line. This is what lets the video tab rewrite a stale description automatically while
    leaving an AI-shortened one alone: the short form fails this on two independent counts
    (no title line, no credit line), and its shortened titles are NOT recoverable from the
    database, so overwriting one would be real data loss rather than a regeneration.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    return (
        lines[0].startswith("Tên truyện: ")
        and any(line.startswith("Mục lục chương:") for line in lines)
        and lines[-1].startswith("Tạo bởi: ")
    )

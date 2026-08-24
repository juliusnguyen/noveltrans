"""Adapter for Tiểu Thuyết Mạng (tieuthuyetmang.com) — Vietnamese novel site.

Landing page: https://tieuthuyetmang.com/truyen/<slug>
Chapter page: https://tieuthuyetmang.com/truyen/<slug>/doc/<chapterNumber>
Audio page:   https://tieuthuyetmang.com/truyen/<slug>/nghe/<chapterNumber>

A Next.js App Router site with **no server-rendered chapter markup**. Everything this
adapter needs is in the React Server Component "flight" stream the page embeds as
`self.__next_f.push([1,"…"])` script tags — including the **whole table of contents**,
which is why `fetch_metadata` and `fetch_chapter_list` share a single request (see
`_flight`). medoctruyen walks up to 500 TOC pages for the same result; this one makes one.

**Most chapters are paid.** On the novel this adapter was built against, 119 of 122
chapters were `isLocked`. The adapter reads only what the user's own logged-in account can
already read — their session cookie goes on via `HttpClient.set_cookies`, exactly as for
medoctruyen — and raises `AuthRequiredError` for anything still locked. Nothing here
attempts to work around the paywall.

Content is already Vietnamese, so novels carry `source_lang="vi"` and the Translate tab's
identity pass copies it through. Unlike webtruyendich this adapter does not *produce* a
translation, so it is deliberately NOT flagged `content_is_translated`.

**Do not parallelise downloads**, and prefer a request delay of 2s or more: this is a small
paid site and a 122-chapter novel is only ~4 minutes of requests at that rate.

Everything above the adapter class is pure and takes markup or a decoded stream, never a
URL to fetch — so the whole parsing surface is tested against saved fixtures with no
network, the same split webtruyendich uses.

KNOWN GAP: `parse_chapter` is the one part not verified against a live reader page. The
reader URL shape is confirmed (the site's own JS builds it three separate ways, all
`"/truyen/".concat(slug, "/doc/").concat(chapterNumber)`), but where a chapter's *text*
lives — flight stream field, server-rendered markup, or a separate API call — was not
observed. `scripts/diagnose_tieuthuyetmang.py` answers it; the extraction below is
structural rather than field-named so that it stands a good chance either way, and it
refuses loudly instead of returning a partial body.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from bs4 import BeautifulSoup

from noveltrans.errors import AudioUnavailableError, AuthRequiredError, ScrapeError
from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.scrapers import register
from noveltrans.scrapers.base import SiteAdapter

_ORIGIN = "https://tieuthuyetmang.com"
_SLUG_RE = re.compile(r"tieuthuyetmang\.com/truyen/([a-z0-9-]+)")
_CH_NUM_RE = re.compile(r"/(?:doc|nghe)/(\d+)")
# The flight stream arrives one `push` at a time; only chunk rows (`[1, "…"]`) carry data.
_PUSH_RE = re.compile(r"self\.__next_f\.push\(\[1\s*,\s*")
# How far left of a matched key to look for the start of its enclosing object. The whole
# stream is ~42 KB, so this is a guard against a pathological scan, not a real limit.
_MAX_OBJECT_SPAN = 8_192
# A chapter body has to clear this to be believed. Set low on purpose: it exists to reject
# a stray label or a URL that happens to be the longest string in an object, not to judge
# how long a chapter "should" be.
_MIN_BODY_CHARS = 200

# What counts as a media URL. `?`/`#` are allowed after the extension so a CDN that
# starts signing its URLs keeps matching; the site serves plain .mp3 today.
_MEDIA_RE = re.compile(r"\.(?:mp3|m4a|aac|ogg|wav|m3u8)(?:[?#]|$)", re.I)


def slug(url: str) -> str:
    match = _SLUG_RE.search(url)
    if not match:
        raise ScrapeError("Could not extract novel slug from URL", url)
    return match.group(1)


def landing_url(url: str) -> str:
    """Normalise any URL of a novel (landing, /doc/N, /nghe/N) to its landing page."""
    return f"{_ORIGIN}/truyen/{slug(url)}"


def chapter_url(novel_slug: str, number: int) -> str:
    """The reader URL for one chapter.

    MEASURED, not guessed — and the seven shapes guessed before this was measured all
    404'd. The site's own route chunk builds it in three separate places, every one of them
    `"/truyen/".concat(slug, "/doc/").concat(chapterNumber)`: the TOC list, the "Đọc tiếp"
    continue button, and the "Đọc truyện" empty state. Note it keys off `chapterNumber`,
    NOT the chapter's ULID `id`.
    """
    return f"{_ORIGIN}/truyen/{novel_slug}/doc/{number}"


def flight_payload(markup: str) -> str:
    """Reconstruct the React Server Component stream from a page's `__next_f` pushes.

    Each push carries a **JSON string literal**, so it is decoded with `raw_decode` rather
    than matched with a regex: a Vietnamese chapter title containing an escaped quote would
    truncate `push\\(\\[1,"(.*?)"\\]\\)` at the wrong place and silently drop the rest of the
    stream — the single most likely silent bug in this file.

    Returns "" when there are no pushes; callers decide whether that is fatal.
    """
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    for match in _PUSH_RE.finditer(markup):
        start = match.end()
        if start >= len(markup) or markup[start] != '"':
            continue
        try:
            text, _ = decoder.raw_decode(markup, start)
        except ValueError:
            continue
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _object_starts(stream: str, at: int) -> Iterator[int]:
    """Candidate `{` positions enclosing `at`, nearest first."""
    floor = max(0, at - _MAX_OBJECT_SPAN)
    pos = stream.rfind("{", floor, at + 1)
    while pos != -1:
        yield pos
        pos = stream.rfind("{", floor, pos)


def iter_objects(
    stream: str, needle: str, required: tuple[str, ...]
) -> Iterator[tuple[int, dict]]:
    """Every JSON object in the stream that contains `needle` and all of `required`.

    The reconstructed stream is **not** valid JSON — it is line-prefixed flight rows
    (`2:I[…]`, `a:["$","div",…]`) with `"$"` sentinels threaded through, so it can never be
    parsed whole. Instead each hit for `needle` is walked leftwards to the nearest `{` and
    `raw_decode` decides: an object that parses and carries the required keys is real, and
    a nested one that parses but lacks them is skipped for the next candidate out.

    Yields `(start_offset, object)` — the offset is what lets `find_story` fall back to
    "the story nearest the chapter list" when slug anchoring finds nothing.
    """
    decoder = json.JSONDecoder()
    seen: set[int] = set()
    at = stream.find(needle)
    while at != -1:
        for start in _object_starts(stream, at):
            if start in seen:
                break
            try:
                obj, _end = decoder.raw_decode(stream, start)
            except ValueError:
                continue
            if isinstance(obj, dict) and all(key in obj for key in required):
                seen.add(start)
                yield start, obj
                break
        at = stream.find(needle, at + 1)


def find_story(stream: str, novel_slug: str, url: str = "") -> dict:
    """The page's OWN story object — never a sidebar recommendation.

    The trap this function exists for: the landing page's stream also carries "recommended
    story" objects, and those carry MORE fields than the page's own — `slug`, `excerpt`,
    `author`, `chapters_count`. The story being displayed has only
    `storySlug` / `title` / `coverUrl` / `status` / `categories`; its author and
    description are server-rendered into the HTML instead (see `parse_metadata`).

    Measured, by getting it wrong live: requiring `("title", "chapters_count")` — the
    sidebar's shape — filtered the real story out and left only recommendations, so the
    novel came back under a completely different title. Hence: anchor on the slug, require
    nothing but a title, and **never fall back to an object belonging to another novel**.
    A wrong novel that looks right is far worse than a failure that says so.
    """
    for key in ("storySlug", "slug"):
        for _at, obj in iter_objects(stream, f'"{key}":"{novel_slug}"', ("title",)):
            if obj.get(key) == novel_slug:
                return obj
    raise ScrapeError(
        "Không tìm thấy dữ liệu truyện trong trang — trang có thể đã đổi giao diện", url
    )


def _html_author(soup) -> str:
    """The author name, which the flight data does not carry at all.

    It is server-rendered as a link to the author's own page, so the `/tac-gia/` href is
    the anchor — the surrounding "Tác giả:" label is prose and can be reworded.
    """
    link = soup.select_one('a[href^="/tac-gia/"]')
    return link.get_text(" ", strip=True) if link is not None else ""


def _html_description(soup) -> str:
    """The blurb under "Giới thiệu", also absent from the flight data.

    Tailwind's `prose` wrapper is the one stable handle the block has; failing that, the
    longest text block on the page is taken, since a synopsis is far longer than any label
    or nav item around it.

    Returned **verbatim**, including the "Giới thiệu truyện :" line it opens with. That
    line looked like a UI label worth stripping, but it is inside the block and was
    written by whoever posted the novel — it is part of the text, and the user asked for
    the block as the site shows it. Blank lines are collapsed; nothing else is touched.
    """
    block = soup.select_one("div.prose, .prose")
    if block is None:
        candidates = [
            element.get_text("\n", strip=True)
            for element in soup.find_all(["div", "section", "p"])
        ]
        text = max(candidates, key=len, default="")
    else:
        text = block.get_text("\n", strip=True)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_metadata(markup: str, url: str, site: str) -> NovelMeta:
    """Title/cover from the flight data, author and description from the HTML.

    Split on purpose, because that is how the page is built: the story component gets
    `title`/`coverUrl`/`status`/`categories`, while the author link and the "Giới thiệu"
    blurb are plain server-rendered markup. Reading only one of the two sources leaves
    half the fields permanently empty.
    """
    soup = BeautifulSoup(markup, "lxml")
    try:
        story = find_story(flight_payload(markup), slug(url), url)
    except ScrapeError:
        # Never guess another novel's object. The page's own <h1> is the safe fallback:
        # it is this novel's title by construction, and the rest of the fields come from
        # the same page's markup.
        story = {}

    title = str(story.get("title") or "").strip()
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading is not None else ""
    if not title:
        raise ScrapeError("Novel title not found — page layout may have changed", url)

    author = _html_author(soup)
    if not author:
        author_obj = story.get("author")
        if isinstance(author_obj, dict):
            author = str(author_obj.get("name") or "").strip()
        elif isinstance(author_obj, str):
            author = author_obj.strip()

    cover = str(story.get("coverUrl") or "").strip()
    if not cover:
        image = soup.select_one('img[src*="img.tieuthuyetmang.com"]')
        cover = (image.get("src") or "").strip() if image is not None else ""

    return NovelMeta(
        # Echoed unchanged: the library keys projects off the URL the user gave, and
        # rewriting it to the landing page orphans a project opened from a chapter link.
        url=url,
        site=site,
        title=title,
        author=author,
        description=_html_description(soup) or str(story.get("excerpt") or "").strip(),
        cover_url=cover,
        source_lang="vi",
    )


def _ordered(objects: list) -> list[dict]:
    """Chapter objects deduplicated by id and sorted into reading order.

    The lock-flag requirement is what separates a real TOC entry from a sidebar story's
    `latestChapter` stub, which carries a `chapterNumber` and a title but none of the
    per-chapter flags.
    """
    by_key: dict[object, dict] = {}
    for obj in objects:
        if not isinstance(obj, dict) or "chapterNumber" not in obj:
            continue
        if "isLocked" not in obj and "isFree" not in obj:
            continue
        try:
            number = int(obj["chapterNumber"])
        except (TypeError, ValueError):
            continue
        obj["chapterNumber"] = number
        by_key.setdefault(obj.get("id") or number, obj)
    return sorted(by_key.values(), key=lambda entry: entry["chapterNumber"])


def chapter_entries(stream: str) -> list[dict]:
    """Every chapter object anywhere in the stream, deduplicated and in reading order."""
    return _ordered([obj for _at, obj in iter_objects(stream, '"chapterNumber"', ("chapterNumber",))])


def story_chapters(stream: str, novel_slug: str) -> list[dict]:
    """This novel's chapters, taken from the component that names the novel.

    The list is rendered by a component whose props are
    `{"storyId": …, "storySlug": …, "chapters": [ … ]}`, so anchoring on the slug gives a
    list that cannot include anything belonging to another story. `chapter_entries` stays
    as the fallback for a build that reshapes those props — it is a page-wide census, so
    it is right only as long as no other novel's chapters are ever rendered here.
    """
    for _at, obj in iter_objects(
        stream, f'"storySlug":"{novel_slug}"', ("storySlug", "chapters")
    ):
        chapters = obj.get("chapters")
        if obj.get("storySlug") == novel_slug and isinstance(chapters, list):
            return _ordered(chapters)
    return chapter_entries(stream)


def parse_chapter_list(markup: str, url: str) -> list[ChapterRef]:
    novel_slug = slug(url)
    entries = story_chapters(flight_payload(markup), novel_slug)
    if not entries:
        raise ScrapeError("Chapter list not found — page layout may have changed", url)

    refs = []
    for index, entry in enumerate(entries):
        number = entry["chapterNumber"]
        # Title as the site gives it. No 🔒/VIP decoration: the title is persisted,
        # exported and read aloud by TTS, and lock state belongs to the account, not
        # to the chapter.
        title = str(entry.get("title") or "").strip() or f"Chương {number}"
        refs.append(
            # `index` is a dense 0-based reading order — the project DB keys chapters by
            # it, so it must never be the site's own `chapterNumber`.
            ChapterRef(index=index, title=title, url=chapter_url(novel_slug, number))
        )
    return refs


def chapter_number(url: str) -> int:
    match = _CH_NUM_RE.search(url)
    if not match:
        raise ScrapeError("Could not extract chapter number from URL", url)
    return int(match.group(1))


def _longest_prose(entry: dict) -> str:
    """The longest string value on a chapter object that could be its body.

    Deliberately does not name a field. Which key holds the text was not observable
    without a live reader page, and a wrong guess at `content` vs `body` vs `text` fails
    silently — whereas "the longest string that is not a URL and clears
    `_MIN_BODY_CHARS`" is right whatever the key turns out to be called.
    """
    best = ""
    for value in entry.values():
        if not isinstance(value, str) or value.startswith("http"):
            continue
        if len(value) > len(best):
            best = value
    return best if len(best) >= _MIN_BODY_CHARS else ""


def _paragraphs(text: str) -> list[str]:
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text("\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _longest_flight_string(stream: str) -> str:
    """The longest string value anywhere in the stream, not just on the chapter object.

    The first version only looked at the chapter object's own fields, which is why a page
    that plainly contained the text still came back empty: the body is a sibling row in
    the flight stream, not a property of the object that describes the chapter.
    """
    decoder = json.JSONDecoder()
    best = ""
    for match in re.finditer(r'"', stream):
        try:
            value, _ = decoder.raw_decode(stream, match.start())
        except ValueError:
            continue
        if isinstance(value, str) and not value.startswith("http") and len(value) > len(best):
            best = value
    return best if len(best) >= _MIN_BODY_CHARS else ""


def _text_block(soup) -> str:
    """The chapter body from server-rendered markup.

    MEASURED, twice over: the reader page returns the text in its HTML (the browser's
    Preview of a plain `GET` shows it), and this site renders long prose in a Tailwind
    `prose` / `whitespace-pre-wrap` wrapper — that is how the landing page's synopsis is
    built, so the same convention is tried first here.

    The general fallback is "the longest block of text on the page", which needs no class
    name at all: on a reader page the chapter dwarfs every label, nav item and footer
    around it. Picking the DEEPEST element that still holds essentially all of that text
    keeps it tight — otherwise the winner is whichever `<div>` wraps the whole document.
    """
    for selector in ("article", "div.prose, .prose", "[class*=whitespace-pre-wrap]"):
        block = soup.select_one(selector)
        if block is not None:
            text = block.get_text("\n", strip=True)
            if len(text) >= _MIN_BODY_CHARS:
                return text

    candidates: list[tuple[str, int]] = []
    for element in soup.find_all(["article", "section", "div", "p"]):
        if element.find_parent(["nav", "header", "footer", "aside"]) is not None:
            continue
        text = element.get_text("\n", strip=True)
        if len(text) >= _MIN_BODY_CHARS:
            candidates.append((text, len(list(element.parents))))
    if not candidates:
        return ""
    longest = max(len(text) for text, _depth in candidates)
    # Every ancestor of the real block holds the same text plus some chrome, so the
    # deepest element carrying essentially all of it is the block itself.
    tightest = max(
        (pair for pair in candidates if len(pair[0]) >= 0.95 * longest),
        key=lambda pair: pair[1],
    )
    return tightest[0]


def parse_chapter(markup: str, url: str, *, number: int, title: str = "") -> str:
    """One chapter's text, or a refusal that says which of the two reasons applies.

    Order matters: the lock gate runs **before** any extraction, so a truncated teaser can
    never be returned as though it were the chapter. The gate reads the site's own flags on
    the chapter object for this number — the same `isLocked` the TOC carries — rather than
    judging the body by length, which would call a genuinely short chapter locked.
    """
    stream = flight_payload(markup)
    entry: dict = {}
    for candidate in chapter_entries(stream):
        if candidate["chapterNumber"] == number:
            entry = candidate
            break

    if entry.get("isPreview") or '"isPreview":true' in markup:
        raise AuthRequiredError(
            "Chương này cần đăng nhập. Cookie phiên đăng nhập bị thiếu hoặc đã hết hạn — "
            "vào Cài đặt và dán lại cookie tieuthuyetmang.com",
            url,
        )
    if entry.get("isLocked"):
        raise AuthRequiredError(
            "Chương bị khoá (trả phí) — tài khoản của bạn chưa mở khoá chương này trên "
            "tieuthuyetmang.com",
            url,
        )

    # Server-rendered markup first — MEASURED: the reader page returns the chapter in the
    # HTML of a plain GET. Then the chapter object's own fields, then any long string in
    # the flight stream, because the body can be a sibling row rather than a property of
    # the object describing the chapter.
    lines = _paragraphs(_text_block(BeautifulSoup(markup, "lxml")))
    if not lines and entry:
        lines = _paragraphs(_longest_prose(entry))
    if not lines:
        lines = _paragraphs(_longest_flight_string(stream))
    # The reader prints the chapter title above the body; the DB already has it.
    if lines and title and lines[0].strip() == title.strip():
        lines = lines[1:]

    if not lines:
        # Two very different failures, and the fix for each is different — so they say
        # different things. No chapter object at all usually means the page was not
        # served as a logged-in reader page; an object with no text means the text is
        # fetched separately and the extractor needs to learn where from.
        reason = (
            "Trang chương không chứa dữ liệu chương nào — có thể chưa đăng nhập "
            "(vào Cài đặt dán cookie tieuthuyetmang.com), hoặc trang đã đổi giao diện."
            if not entry
            else "Trang có dữ liệu chương nhưng không kèm nội dung — nội dung nhiều khả "
            "năng được tải riêng bằng JavaScript."
        )
        raise ScrapeError(
            f"Không đọc được nội dung chương. {reason}\n\n"
            "Chạy lệnh này rồi gửi kết quả để cập nhật bộ đọc:\n"
            f"  python scripts/diagnose_tieuthuyetmang.py --chapter {url}",
            url,
        )
    return "\n\n".join(lines)


# --- audio -----------------------------------------------------------------
# Everything below was MEASURED against the live site with an entitled session
# (see 059.01-HISTORY.md). The three states the page can be in:
#
#   entitled, audio ready      player props present, `audioUrl` holds an .mp3
#   entitled, not yet uploaded player props present, `audioUrl` empty
#   signed out / not VIP       player props ABSENT, replaced by an upsell block
#
# That is why `audio_gate_reason` keys off the presence of the prop and not off the
# upsell copy: the copy is Vietnamese presentation text and will not survive a redesign.


def audio_page_url(novel_slug: str, number: int) -> str:
    """The listen URL for one chapter.

    MEASURED, like `chapter_url`, and keyed off `chapterNumber` the same way. This is
    where the media URL lives; the `/doc/` reader page never carries one, with or
    without a session cookie.
    """
    return f"{_ORIGIN}/truyen/{novel_slug}/nghe/{number}"


def audio_entries(stream: str, novel_slug: str) -> list[dict]:
    """This novel's chapters that claim to have audio, in reading order.

    Audio is published per *volume*, not per chapter: on the reference novel 21 of 122
    chapters carry `hasAudio`, each covering five chapters ("[ YTB TẬP 1 ] Chương 1-5"),
    anchored at a sparse set of `chapterNumber`s. Note the chapter ranges in those titles
    run far past the chapter count, so the TITLE cannot be used to map audio to chapters —
    only `chapterNumber` can.
    """
    return [entry for entry in story_chapters(stream, novel_slug) if entry.get("hasAudio")]


def audio_locked_count(entries: list[dict]) -> int:
    """How many audio entries are flagged `audioLocked`.

    STATUS LINE ONLY — never a filter. The flag describes what the site would show an
    anonymous visitor and has been observed false for every entry on an entitled
    session, so filtering on it would hide audio the user can actually fetch.
    """
    return sum(1 for entry in entries if entry.get("audioLocked"))


def find_audio_media(markup: str) -> str:
    """The media URL on a listen page, or "" when the page carries none.

    Tier 1 is a real `<audio>`/`<source>` element; tier 2 is the flight stream, which is
    where the live site actually puts it (as the `audioUrl` prop of the player component).
    The stream is walked with `raw_decode` over every quote rather than a regex, for the
    reason the module docstring gives — a regex over the raw text picks up the stream's
    own backslash escaping and yields a URL with a trailing `\\`.

    Returns "" rather than raising; `fetch_audio_url` decides what the emptiness means.
    """
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup.find_all(["audio", "source"]):
        src = (tag.get("src") or "").strip()
        if src and _MEDIA_RE.search(src):
            return src

    decoder = json.JSONDecoder()
    stream = flight_payload(markup)
    for match in re.finditer(r'"', stream):
        try:
            value, _ = decoder.raw_decode(stream, match.start())
        except ValueError:
            continue
        if isinstance(value, str) and _MEDIA_RE.search(value) and "/" in value:
            return value.strip()
    return ""


def audio_gate_reason(markup: str) -> str:
    """Why a listen page has no media: "vip" (not entitled), or "" (entitled).

    Structural on purpose. An entitled session gets the player component server-rendered
    with its props — `audioUrl` among them — whether or not the file has been uploaded
    yet. A signed-out or non-VIP session gets no player at all. So the prop's presence,
    not the URL's, is the entitlement signal.
    """
    return "" if '"audioUrl"' in flight_payload(markup) else "vip"

@register
class TieuthuyetmangAdapter(SiteAdapter):
    name = "tieuthuyetmang"
    display_name = "Tiểu Thuyết Mạng (tieuthuyetmang.com)"
    # Matches the landing page and every /doc/N and /nghe/N under it. The `/truyen/`
    # prefix is required so category and author pages don't resolve here.
    url_patterns = [r"tieuthuyetmang\.com/truyen/[a-z0-9-]+"]

    def __init__(self, client):
        super().__init__(client)
        # A scan calls fetch_metadata then fetch_chapter_list back to back, and both read
        # the same landing page. One entry is all that needs caching, and the adapter is
        # per-batch, so nothing has to invalidate it.
        self._cached: tuple[str, str] | None = None

    def _page(self, url: str) -> str:
        """The landing page's markup, fetched at most once per adapter.

        The MARKUP is cached, not the flight stream: the author and the description are
        server-rendered and never reach the stream, so metadata needs both halves of the
        page. Caching only the stream is what would force a second request.
        """
        if self._cached is not None and self._cached[0] == url:
            return self._cached[1]
        markup = self.client.get_html(url)
        if not flight_payload(markup):
            raise ScrapeError(
                "Không đọc được dữ liệu trang (không tìm thấy dữ liệu Next.js)", url
            )
        self._cached = (url, markup)
        return markup

    def fetch_metadata(self, url: str) -> NovelMeta:
        return parse_metadata(self._page(landing_url(url)), url, self.name)

    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        markup = self._page(landing_url(url))
        refs = parse_chapter_list(markup, url)
        locked = sum(
            1
            for entry in story_chapters(flight_payload(markup), slug(url))
            if entry.get("isLocked")
        )
        if locked:
            # A status line, never a filter: see `parse_chapter_list`. The user is told
            # what they are missing instead of being shown a TOC that silently omits it.
            self._status(
                f"🔒 {locked}/{len(refs)} chương đang khoá — cần cookie của tài khoản "
                "đã mở khoá"
            )
        return refs

    def fetch_chapter(self, ref: ChapterRef) -> str:
        return parse_chapter(
            self.client.get_html(ref.url),
            ref.url,
            number=chapter_number(ref.url),
            title=ref.title,
        )

    def fetch_audio_manifest(self, url: str) -> list[dict]:
        """The novel's audio entries, from the landing page already in the cache.

        Costs no extra request during a scan: `_page` has the markup, and the manifest
        is a filter over the same TOC `fetch_chapter_list` reads.
        """
        markup = self._page(landing_url(url))
        entries = audio_entries(flight_payload(markup), slug(url))
        if entries:
            locked = audio_locked_count(entries)
            note = f" ({locked} cần VIP theo trang chủ)" if locked else ""
            self._status(f"🎧 {len(entries)} mục có audio{note}")
        return entries

    def fetch_audio_url(self, ref: ChapterRef) -> str:
        """The media URL for one audio entry.

        Plain HTTP with the account's cookie is enough — the URL is server-rendered into
        the listen page's flight stream, so no browser automation is involved. The media
        host itself is NOT gated: the returned URL fetches anonymously, which is why the
        download step needs no session at all.
        """
        markup = self.client.get_html(audio_page_url(slug(ref.url), chapter_number(ref.url)))
        media = find_audio_media(markup)
        if media:
            return media
        if audio_gate_reason(markup) == "vip":
            raise AuthRequiredError(
                "Chương này có audio nhưng tài khoản chưa mở khoá — cần gói VIP, "
                "hoặc cookie trong Cài đặt đã hết hạn",
                ref.url,
            )
        # Entitled, but the site has not published this volume's file yet. Observed on
        # 8 of the reference novel's 21 entries: the player renders, `audioUrl` is empty.
        raise AudioUnavailableError(
            "Trang nghe chưa có file audio cho mục này (site chưa đăng)", ref.url
        )

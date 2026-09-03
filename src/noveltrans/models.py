"""Core dataclasses shared by scrapers, storage, translators and exporters."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from noveltrans.slug import slugify

# A novel the user wrote themselves — no source website, nothing to scrape.
LOCAL_SITE = "local"
LOCAL_URL_PREFIX = "local://"


def new_local_url() -> str:
    """A collision-proof synthetic URL for a hand-written novel.

    A local novel has no real URL, but the URL is *identity* in two places that would
    both break on an empty string:

    * `NovelProject.create` derives the project folder from `sha1(meta.url)[:8]`, so a
      blank URL gives every local novel the SAME hash — two novels whose titles slugify
      alike would share one folder, and the second one's chapters would be merged into
      the first one's `chapters.db` without a word of warning.
    * `Library.find_by_url("")` would match the first blank-URL project it walked past.

    A uuid4 is unique by construction, so neither can happen. `local://` also guarantees
    no `SiteAdapter.matches` will ever claim it — `adapter_for_url` returns None, which
    is exactly what the download path already treats as "nothing to fetch".
    """
    return f"{LOCAL_URL_PREFIX}{uuid.uuid4().hex}"


@dataclass
class NovelMeta:
    """Metadata for a novel, as scraped from its landing page."""

    url: str
    site: str  # adapter name, e.g. "ixdzs"
    title: str
    author: str = ""
    description: str = ""
    cover_url: str = ""
    source_lang: str = "zh"
    # filled in by the first translation run
    translated_title: str = ""
    translated_description: str = ""
    translated_author: str = ""
    translated_lang: str = ""
    # YouTube tag list (comma-joined), generated on demand for the video export
    tags: str = ""
    # AI image-generation prompt for the thumbnail base image, generated on demand
    thumbnail_prompt: str = ""
    # User override for how the title appears on video output — the point is dropping a
    # source tag like "[ĐM/EDIT] " without editing the scraped metadata. See display_name().
    display_title: str = ""
    # The pinned filename stem for everything this novel generates. Empty means "still
    # derived from the titles", which is what every project made before feature 074 has —
    # see slug_name(), which is the ONLY thing that should ever read this.
    slug: str = ""
    # Per-novel video export choices, remembered so switching between novels in the GUI
    # doesn't leave one novel's background image or playlist selected on another's video
    # tab — see VideoTab._on_project_selected. "" means "nothing chosen for this novel yet".
    video_image_path: str = ""
    upload_playlist: str = ""
    # Every other video-export setting for this novel, keyed by AppConfig property name —
    # background colour, cover image, credit, tagline, fonts, cover layout, plus the
    # workflow choices (quality, mode, batch size, ...). See `noveltrans.video_settings`
    # for which keys are inherited from the user's last-used value and which are not.
    # Empty means "this novel has never saved any"; it adopts a snapshot on first open.
    # `video_image_path` above predates this and stays its own field so existing
    # meta.json files keep working — video_settings mirrors it, and the mirror wins.
    video_settings: dict = field(default_factory=dict)
    # Last-chosen YouTube visibility ("private"/"unlisted"/"public"/"schedule"). "" means
    # "never chosen for this novel" — VideoTab falls back to "private" (the safe default)
    # rather than to whatever some OTHER novel happened to have selected last.
    upload_visibility: str = ""

    def slug_name(self) -> str:
        """The filename stem every generated file of this novel is named after.

        `exports/video/<stem>/<stem>.mp4` and its whole family of sidecars — `.srt`,
        `.jpg`, `.title.txt`, `.tags.txt`, `.upload.json`, `.created.json` — plus the
        merged audio in `exports/audio/`. **Not** `display_name()`; see its docstring.

        An empty `slug` reproduces the pre-074 rule byte for byte, so upgrading moves not
        one file and no project needs migrating. Only an explicit rename (or the first
        render, which pins whatever it was about to use) ever writes the field.

        Pinning is the point. While the stem was *derived*, anything that touched
        `translated_title` moved it — a re-translation into a different target language
        would silently orphan every rendered part and every upload record, with no error
        and no way back short of renaming files by hand. Once pinned, nothing moves the
        stem except a deliberate rename that moves the files with it.
        """
        return self.slug or slugify(self.translated_title or self.title)

    def display_name(self) -> str:
        """The novel title as it should appear on a video, thumbnail and description.

        Falls back through the user's override → the translated title → the original, so
        an empty override means "whatever we'd have shown anyway".

        **Not for filenames.** `slug_name()` owns those, and the two are deliberately
        allowed to disagree: renaming a novel must not move `video_dir/<stem>/<stem>.mp4`
        or the `<stem>.upload.json` beside it unless the user explicitly asked for the
        files to move too. Deriving the stem from this would strand every rendered part
        (they would read as "chưa tạo") and every upload record from feature 034.

        Each candidate is stripped *before* the fallback, not after: a box containing
        only spaces is "I didn't set one", and stripping last would let it win the chain
        and blank the title on every video and cover.
        """
        for candidate in (self.display_title, self.translated_title, self.title):
            text = (candidate or "").strip()
            if text:
                return text
        return ""

    def novel_label(self, *, with_source: bool = True) -> str:
        """How this novel is named wherever it is listed: translation, original, source.

        The single naming rule for the novel tab bar, its tooltip, the picker and the
        "Thông tin truyện" header, so those four cannot drift apart.

        **Translation first**, which reverses what `bilingual_title` did before feature
        068. That order was chosen so the original would anchor the row ("the Chinese is
        how the user recognises the novel on the source site"), but in a narrow tab column
        it buried the half the user actually reads — the first ~20 characters are all a tab
        shows, and they were being spent on text the user does not think in. The original
        is still there, one field along, for exactly the recognition the old order served.

        `display_title` wins the first slot when set: it is the user's own name for this
        novel (feature 025's override, see `display_name`), so it beats a machine
        translation that may still carry a "[ĐM/EDIT] " tag the override exists to drop.

        Every part is optional and each is dropped whole — an untranslated novel is not a
        title behind an empty separator, and a local novel carries no trailing site. Pass
        `with_source=False` where the site is already obvious from the surrounding UI.
        """
        translated = (self.display_title or "").strip() or (self.translated_title or "").strip()
        original = (self.title or "").strip()
        # Not twice. An override set to the original, or a source already in the target
        # language (translated_title == title), would otherwise read "X — X — site.com".
        parts = [translated, original] if translated != original else [original]
        if with_source:
            parts.append(self.source_host())
        return " — ".join(part for part in parts if part)

    def source_host(self) -> str:
        """The site this novel came from, as a bare hostname — "" when there isn't one.

        Taken from the URL rather than `site`, because `site` is the *adapter* name
        ("twkan", "69shuba") while what reads naturally in a list is the domain the user
        actually pasted from ("twkan.com"). `www.` is dropped so one site cannot appear
        under two spellings.

        Empty for a local novel: its URL is a synthetic `local://<uuid>`, whose netloc is
        a UUID, and printing that in the picker would be worse than printing nothing.
        """
        if self.is_local:
            return ""
        return urlparse(self.url).netloc.removeprefix("www.") or self.site

    @property
    def is_local(self) -> bool:
        """True for a novel the user wrote themselves — no site, nothing to scrape.

        Checked on BOTH fields on purpose: `site` carries the behaviour flag and the URL
        carries identity, and a hand-edited `meta.json` that drops one of them must not
        turn a local novel back into something the download path will try to fetch.
        """
        return self.site == LOCAL_SITE or self.url.startswith(LOCAL_URL_PREFIX)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NovelMeta":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ChapterRef:
    """One entry of a novel's table of contents."""

    index: int  # 0-based order
    title: str
    url: str


@dataclass
class SourceAudio:
    """One audio release published by the source site — NOT a chapter.

    These are a different edition of the work, not a property of a chapter: the reference
    novel ships 21 releases ("[ YTB TẬP 1 ] Chương 1-5") against 122 chapters, and their
    titles name chapter ranges that no single row corresponds to. Storing them on chapter
    rows made a five-chapter volume look like chapter 1's narration.

    The `audio_*`/`index`/`translated_title` members below are not decoration: they are the
    narrow protocol `plan_merge_windows`, `chapter_marker_title` and the video renderer
    already read off a Chapter. Satisfying it lets a release flow through the whole
    merge/render pipeline unchanged, with `ord` standing in for the chapter number so
    "phần 1..N" counts releases in reading order.
    """

    number: int  # the site's own chapterNumber — what /nghe/<n> keys off
    title: str = ""
    ord: int = 0  # 1-based position in the manifest's reading order
    path: str = ""  # project-relative; "" until downloaded
    seconds: float = 0.0
    error: str = ""
    updated_at: str = ""

    @property
    def has_audio(self) -> bool:
        return bool(self.path)

    # -- the Chapter-shaped protocol the merge/video pipeline reads --------------
    @property
    def index(self) -> int:
        return self.ord - 1

    @property
    def audio_path(self) -> str:
        return self.path

    @property
    def audio_seconds(self) -> float:
        return self.seconds

    @property
    def audio_source(self) -> str:
        return AUDIO_SOURCE_DOWNLOADED

    @property
    def translated_title(self) -> str:
        return self.title


# Chapter lifecycle: pending -> downloaded -> translated (or error at any step)
STATUS_PENDING = "pending"
STATUS_DOWNLOADED = "downloaded"
STATUS_TRANSLATED = "translated"
STATUS_ERROR = "error"

# Which text an audio file was voiced from, or that it was not voiced at all.
# Deliberately a separate namespace from the STATUS_* constants above:
# AUDIO_SOURCE_DOWNLOADED and STATUS_DOWNLOADED share the literal "downloaded" but mean
# unrelated things — narration fetched from the source site vs. chapter *text* fetched.
# Never compare a status against an audio source.
AUDIO_SOURCE_TRANSLATED = "translated"
AUDIO_SOURCE_ORIGINAL = "original"
AUDIO_SOURCE_DOWNLOADED = "downloaded"


@dataclass
class Chapter:
    """A chapter row as stored in a project's chapters.db."""

    index: int
    title: str
    url: str
    content: str = ""  # original Chinese text ("" = not downloaded)
    translated: str = ""  # "" = not translated
    translated_title: str = ""
    target_lang: str = ""  # language of `translated`
    translator: str = ""  # engine that produced `translated`, e.g. "CLI (agy)"
    translate_seconds: float = 0.0  # wall-clock time of the last translation
    # The translation as it stood before the style rewrite replaced it. Non-empty means
    # "this chapter has been rewritten", so one field is the undo copy, the resume
    # predicate and the done-flag at once. `restore_translation` puts the text back and
    # blanks these, which is what makes the chapter eligible for a fresh rewrite.
    translated_raw: str = ""
    translated_title_raw: str = ""
    status: str = STATUS_PENDING
    error: str = ""
    updated_at: str = ""
    # audio pipeline (parallel to download/translate status)
    audio_path: str = ""  # path relative to the project folder ("" = not generated)
    audio_voice: str = ""
    audio_source: str = AUDIO_SOURCE_TRANSLATED  # see AUDIO_SOURCE_* above
    audio_seconds: float = 0.0  # duration of the generated audio
    audio_error: str = ""
    # Fingerprint of the (title, text) this chapter's audio was actually voiced from, so a
    # later edit to that text can be noticed. EMPTY MEANS "unknown", never "stale": every
    # row in every existing library has one, and treating those as stale would tell the
    # user their whole novel needs re-voicing. See `audio_is_stale`.
    audio_text_hash: str = ""
    # True once the user renamed this chapter by hand; a re-scan then leaves the title
    # alone instead of overwriting it with the site's again.
    title_custom: bool = False
    title_source: str = ""  # the site's own title, kept so a rename can be undone

    @property
    def is_downloaded(self) -> bool:
        return bool(self.content)

    @property
    def is_translated(self) -> bool:
        return bool(self.translated)

    @property
    def is_rewritten(self) -> bool:
        return bool(self.translated_raw)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_path)

    def audio_source_text(self, use_translation: bool | None = None) -> tuple[str, str]:
        """The (title, text) pair this chapter's audio is — or would be — voiced from.

        `AudioWorker` reads BOTH: `synthesize_chapter(title, text, ...)` speaks the title
        aloud, and `slugify(title)` is in the audio filename. So a title edit invalidates
        a recording exactly as a body edit does.

        `use_translation=None` means "whatever this row's audio was made from", read off
        `audio_source` — that is what lets a chapter judge itself without being told which
        radio button the tab happens to be showing.
        """
        if use_translation is None:
            use_translation = self.audio_source != AUDIO_SOURCE_ORIGINAL
        if use_translation:
            return self.translated_title or self.title, self.translated
        return self.title, self.content

    def audio_fingerprint(self, use_translation: bool | None = None) -> str:
        """Hash of what would be voiced now. Compare against `audio_text_hash`.

        A hash rather than a "needs audio" flag set by each edit site: the text reaches the
        DB through edit_translation, save_translation, save_rewrite, restore_translation,
        apply_replacements, edit_content and edit_title, and the next feature will add an
        eighth. A flag needs every one of them to remember; this needs none of them to know
        it exists. It is also right for edit-then-undo, which a flag gets wrong.
        """
        title, text = self.audio_source_text(use_translation)
        digest = hashlib.sha1()  # noqa: S324 — change detection, not security
        digest.update(title.encode("utf-8"))
        digest.update(b"\x00")  # separator: ("ab", "c") must not hash like ("a", "bc")
        digest.update(text.encode("utf-8"))
        return digest.hexdigest()

    @property
    def audio_is_stale(self) -> bool:
        """True when the audio on disk no longer matches the text it was made from.

        Narration downloaded from the source site is excluded: it is a different edition,
        not a render of this text, so editing the translation says nothing about it.
        """
        if not self.has_audio or self.audio_source == AUDIO_SOURCE_DOWNLOADED:
            return False
        if not self.audio_text_hash:
            return False  # generated before fingerprints existed — assume good
        return self.audio_text_hash != self.audio_fingerprint()

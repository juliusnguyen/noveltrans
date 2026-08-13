"""Core dataclasses shared by scrapers, storage, translators and exporters."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

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

    def display_name(self) -> str:
        """The novel title as it should appear on a video, thumbnail and description.

        Falls back through the user's override → the translated title → the original, so
        an empty override means "whatever we'd have shown anyway".

        **Not for filenames.** The video slug stays keyed to the translated/original title
        (`slugify(meta.translated_title or meta.title)`): it decides
        `video_dir/<stem>/<stem>.mp4` and every sidecar beside it, including
        `<stem>.upload.json`. Deriving it from this would move all of them the moment
        someone edits the display title — rendered parts would read as "chưa tạo", and the
        upload records from feature 034 would point at files that no longer exist.

        Each candidate is stripped *before* the fallback, not after: a box containing
        only spaces is "I didn't set one", and stripping last would let it win the chain
        and blank the title on every video and cover.
        """
        for candidate in (self.display_title, self.translated_title, self.title):
            text = (candidate or "").strip()
            if text:
                return text
        return ""

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


# Chapter lifecycle: pending -> downloaded -> translated (or error at any step)
STATUS_PENDING = "pending"
STATUS_DOWNLOADED = "downloaded"
STATUS_TRANSLATED = "translated"
STATUS_ERROR = "error"


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
    status: str = STATUS_PENDING
    error: str = ""
    updated_at: str = ""
    # audio pipeline (parallel to download/translate status)
    audio_path: str = ""  # path relative to the project folder ("" = not generated)
    audio_voice: str = ""
    audio_source: str = "translated"  # which text the audio was voiced from
    audio_seconds: float = 0.0  # duration of the generated audio
    audio_error: str = ""
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
    def has_audio(self) -> bool:
        return bool(self.audio_path)

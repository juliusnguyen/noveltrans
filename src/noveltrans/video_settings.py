"""Which video-export settings belong to a novel, and which belong to the user.

Every setting on the Video tab used to live in one global QSettings bag, so the tab
showed whatever was last touched for *any* novel. That is not a cosmetic annoyance:
`ảnh nền` picked for one novel rendered into the next one's video, silently, because
the box was already filled in when the tab opened.

The fix is not "make everything per-novel" — that would make you re-pick your usual
quality and AI engine for every new novel. The settings split in two:

* **Identity** — what this novel *looks like*: its background image and colour, cover
  image, credit line, tagline, fonts, and the cover's text layout. These are never
  inherited from another novel. An unset one reads as the app default (a blank image,
  the pastel gradient), because inheriting here is precisely the reported bug.

* **Workflow** — how *you* like to work: render mode, quality, batch size, burn-in,
  and which LLM drives the AI helpers. A novel that has never set one inherits your
  last-used value, so your habits carry across new novels. Once a novel sets one, its
  own value wins.

Both kinds are stored per novel in `meta.json` under `video_settings`, keyed by the
`AppConfig` property name (so `video_thumbnail_title_pos` is one entry, not an x/y
pair). `effective()` resolves the two layers into the values the tab and the renderer
actually use; nothing outside this module needs to know which kind a key is.
"""

from __future__ import annotations

from typing import Any

# Per-novel, never inherited: an unset key falls back to the app default, NOT to
# whatever another novel chose. See the module docstring.
IDENTITY_KEYS: tuple[str, ...] = (
    "video_image_path",
    "video_bg_color",
    "video_thumbnail_image",
    "video_credit",
    "video_tagline",
    "video_font",
    "video_thumbnail_font",
    "video_thumbnail_title_pos",
    "video_thumbnail_part_pos",
    "video_thumbnail_title_scale",
    "video_thumbnail_part_scale",
    "video_thumbnail_tagline_scale",
    "video_thumbnail_title_align",
)

# Per-novel override, seeded from the user's last-used value when the novel has none.
WORKFLOW_KEYS: tuple[str, ...] = (
    "video_mode",
    "video_quality",
    "video_batch_size",
    "video_burn_subtitles",
    "video_ai_engine",
    "video_ai_model",
)

VIDEO_SETTING_KEYS: tuple[str, ...] = IDENTITY_KEYS + WORKFLOW_KEYS

# Keys whose value is an (x, y) pair. JSON has no tuples, so these come back from
# meta.json as lists and are normalised on read — the renderer unpacks them positionally
# and a list would work by luck, but equality checks against the defaults would not.
_PAIR_KEYS = frozenset({"video_thumbnail_title_pos", "video_thumbnail_part_pos"})


def identity_defaults() -> dict[str, Any]:
    """The app's own defaults for the identity keys — what a brand-new novel starts with.

    Imported lazily, like AppConfig does: `tts.thumbnail` and `tts.video` pull in the
    rendering stack, and this module is imported by storage code that must stay light.
    """
    from noveltrans.tts.thumbnail import (
        DEFAULT_PART_POS,
        DEFAULT_TEXT_SCALE,
        DEFAULT_TITLE_ALIGN,
        DEFAULT_TITLE_POS,
    )
    from noveltrans.tts.video import DEFAULT_VIDEO_FONT

    return {
        "video_image_path": "",
        "video_bg_color": "",  # "" = the default pastel gradient
        "video_thumbnail_image": "",  # "" = reuse the video background
        "video_credit": "Fox Novel",
        "video_tagline": "",
        "video_font": DEFAULT_VIDEO_FONT,
        "video_thumbnail_font": "nunito",
        "video_thumbnail_title_pos": DEFAULT_TITLE_POS,
        "video_thumbnail_part_pos": DEFAULT_PART_POS,
        "video_thumbnail_title_scale": DEFAULT_TEXT_SCALE,
        "video_thumbnail_part_scale": DEFAULT_TEXT_SCALE,
        "video_thumbnail_tagline_scale": DEFAULT_TEXT_SCALE,
        "video_thumbnail_title_align": DEFAULT_TITLE_ALIGN,
    }


def effective(stored: dict[str, Any] | None, config) -> dict[str, Any]:
    """Resolve one novel's saved settings against the app defaults and the user's habits.

    `stored` is the novel's `meta.video_settings` (empty for a novel that has never had
    any saved). Returns a value for every key in VIDEO_SETTING_KEYS, so callers can index
    it without `.get` defaults scattered around.
    """
    stored = stored or {}
    defaults = identity_defaults()
    resolved: dict[str, Any] = {}
    for key in IDENTITY_KEYS:
        resolved[key] = _normalise(key, stored[key]) if key in stored else defaults[key]
    for key in WORKFLOW_KEYS:
        # getattr, not a dict: AppConfig validates and clamps on read (an unknown font
        # or a stale engine name reads as the default), and that must not be bypassed.
        resolved[key] = stored[key] if key in stored else getattr(config, key)
    return resolved


def snapshot(config) -> dict[str, Any]:
    """Every video setting as it currently stands globally.

    Used once per novel, on first open after this feature landed, so novels that were
    already set up and rendered keep rendering identically instead of resetting to
    defaults — they adopt today's global values as their own, then diverge from there.
    """
    return {key: _normalise(key, getattr(config, key)) for key in VIDEO_SETTING_KEYS}


def _normalise(key: str, value: Any) -> Any:
    """Coerce a stored value back to the type the renderer expects (see _PAIR_KEYS)."""
    if key in _PAIR_KEYS and isinstance(value, (list, tuple)) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    return value

"""Application-level exceptions."""


class NovelTransError(Exception):
    """Base class for all NovelTrans errors."""


class ScrapeError(NovelTransError):
    """A page could not be fetched or parsed."""

    def __init__(self, message: str, url: str = ""):
        self.url = url
        super().__init__(f"{message} ({url})" if url else message)


class ObfuscatedContentError(ScrapeError):
    """Chapter content was fetched but could not be decoded (site obfuscation)."""


class AuthRequiredError(ScrapeError):
    """Full chapter body needs an authenticated session (missing/expired cookie),
    or the chapter is paywalled for this account."""


class RateLimitedError(ScrapeError):
    """The site is throttling reads ("reading too fast"); retry after a wait."""


class DailyLimitError(ScrapeError):
    """A hard per-day read quota was hit; waiting won't help within the batch.

    The user must lift it themselves (the site's own unlock flow) or wait for the
    daily reset, so the download should stop and surface the instructions.

    `code` and `discord_url` are the site's own single-use unlock code and Discord
    invite parsed off the limit page (when present), so the app can drive — or
    hand the user — the `/mochuong <code>` unlock instead of only showing text.
    """

    def __init__(
        self, message: str, url: str = "", *, code: str = "", discord_url: str = ""
    ):
        super().__init__(message, url)
        self.code = code
        self.discord_url = discord_url


class UnsupportedSiteError(NovelTransError):
    """No SiteAdapter matches the given URL."""


class TranslateError(NovelTransError):
    """Translation of a chunk/chapter failed after retries."""


class ExportError(NovelTransError):
    """Export to an output format failed."""


class TtsError(NovelTransError):
    """Text-to-speech synthesis failed or the engine is unavailable."""

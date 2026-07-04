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


class UnsupportedSiteError(NovelTransError):
    """No SiteAdapter matches the given URL."""


class TranslateError(NovelTransError):
    """Translation of a chunk/chapter failed after retries."""


class ExportError(NovelTransError):
    """Export to an output format failed."""

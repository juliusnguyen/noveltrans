"""SiteAdapter ABC and the shared rate-limited HttpClient."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import httpx
import requests

from noveltrans.errors import ScrapeError
from noveltrans.models import ChapterRef, NovelMeta

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class HttpClient:
    """requests.Session wrapper: polite delay, retries with backoff, encoding fix."""

    def __init__(
        self,
        delay_seconds: float = 1.5,
        max_retries: int = 3,
        timeout: float = 20.0,
        cookies: str = "",
    ):
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request_at = 0.0
        if cookies:
            self.set_cookies(cookies)

    def set_cookies(self, cookie_string: str) -> None:
        """Attach a raw browser Cookie header for sites that gate content on login.

        Sent with every request from this client, so scope one client to one site.
        """
        cookie_string = cookie_string.strip()
        if cookie_string:
            self._session.headers["Cookie"] = cookie_string

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self._session.request(method, url, timeout=self.timeout, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2.0**attempt)  # 1s, 2s backoff between tries
        raise ScrapeError(f"Request failed after {self.max_retries} tries: {last_error}", url)

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def get_json_post(self, url: str, **kwargs) -> dict:
        """POST (form data) and return the parsed JSON body."""
        response = self._request("POST", url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ScrapeError(f"Expected JSON response: {exc}", url) from exc

    def get_html(self, url: str, **kwargs) -> str:
        """GET a page and return decoded text, fixing mis-declared CN encodings."""
        response = self.get(url, **kwargs)
        # Many CN sites serve gbk/gb2312 without (or with a wrong) charset header.
        # requests falls back to ISO-8859-1; trust the content sniffer instead.
        if response.encoding in (None, "ISO-8859-1"):
            response.encoding = response.apparent_encoding
        return response.text


def _as_requests_response(hr: httpx.Response) -> requests.Response:
    """Repackage an httpx response so callers of HttpClient see the same type as before."""
    response = requests.Response()
    response.status_code = hr.status_code
    response.reason = hr.reason_phrase
    response.url = str(hr.url)
    response.headers = requests.structures.CaseInsensitiveDict(hr.headers)
    response._content = hr.content
    response.encoding = hr.encoding
    return response


class Http2Client(HttpClient):
    """HttpClient that speaks HTTP/2 (via httpx) for sites that refuse HTTP/1.1 clients.

    ixdzs8.com answers 403 to every HTTP/1.1 client, requests included, and 200 to HTTP/2
    with the same headers. Everything else (throttling, retries, the session headers and
    cookies) behaves as in HttpClient. `wrap` keeps reading the source client's headers at
    request time, so cookies set on it later still apply.
    """

    def __init__(
        self,
        delay_seconds: float = 1.5,
        max_retries: int = 3,
        timeout: float = 20.0,
        cookies: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        source: HttpClient | None = None,
    ):
        super().__init__(delay_seconds, max_retries, timeout, cookies)
        self._source = source
        self._http = httpx.Client(
            http2=True, timeout=timeout, transport=transport, follow_redirects=True
        )

    @classmethod
    def wrap(cls, source: HttpClient) -> Http2Client:
        return cls(
            delay_seconds=source.delay_seconds,
            max_retries=source.max_retries,
            timeout=source.timeout,
            source=source,
        )

    def _headers(self) -> dict[str, str]:
        session = (self._source or self)._session
        return dict(session.headers)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = self._headers()
        headers.update(kwargs.pop("headers", None) or {})
        params = kwargs.pop("params", None)
        data = kwargs.pop("data", None)
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self._http.request(
                    method, url, headers=headers, params=params, data=data
                )
                response.raise_for_status()
                return _as_requests_response(response)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2.0**attempt)  # 1s, 2s backoff between tries
        raise ScrapeError(f"Request failed after {self.max_retries} tries: {last_error}", url)


class SiteAdapter(ABC):
    """One novel site. Implementations live in noveltrans/scrapers/<site>.py."""

    name: str = ""  # short id, e.g. "ixdzs"
    display_name: str = ""  # human label shown in the GUI
    url_patterns: list[str] = []  # regexes matched against novel URLs

    # Most sites serve original-language text that NovelTrans translates later. A
    # few (e.g. webtruyendich) serve an already-finished translation — for those,
    # fetch_chapter returns the TRANSLATION, and the download worker lands it as
    # `translated` directly instead of `content`, skipping our own translators.
    content_is_translated: bool = False
    translated_lang: str = ""  # language of that translation, e.g. "vi"
    translator_label: str = ""  # recorded in Chapter.translator

    def __init__(self, client: HttpClient):
        self.client = client
        # Workers assign this so an adapter can explain a long or surprising step to
        # the user (69shuba opens a browser window). Left None outside the GUI.
        self.on_status: Callable[[str], None] | None = None

    def _status(self, message: str) -> None:
        """Tell the user what's happening, if anyone's listening."""
        if self.on_status is not None:
            self.on_status(message)

    @classmethod
    def matches(cls, url: str) -> bool:
        return any(re.search(p, url) for p in cls.url_patterns)

    def close(self) -> None:
        """Release anything held for the batch. Default: nothing.

        Most adapters fetch over the shared HttpClient and hold nothing of their own.
        One (69shuba) drives a browser for the life of a scan/download, and a leaked
        Chromium is a window the user has to force-quit — so workers call this in a
        `finally`. Must be idempotent and must never raise.
        """

    @abstractmethod
    def fetch_metadata(self, url: str) -> NovelMeta:
        """Scrape title/author/description from the novel's landing page."""

    @abstractmethod
    def fetch_chapter_list(self, url: str) -> list[ChapterRef]:
        """Scrape the full table of contents, in reading order."""

    @abstractmethod
    def fetch_chapter(self, ref: ChapterRef) -> str:
        """Return plain-text chapter content (paragraphs separated by \\n\\n).

        Raise ObfuscatedContentError if the site returns content that
        cannot be decoded into readable text.
        """

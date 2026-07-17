"""Tests for the 69shuba browser session.

No test here launches a browser: `BrowserSession` takes `sync_playwright` as an
argument precisely so it can be faked, and `looks_like_challenge` is pure. The real
Cloudflare interstitial and a real chapter page are used as fixtures, so the challenge
detector is checked against markup that actually exists rather than a hand-written mock.
"""

from __future__ import annotations

import pytest

from noveltrans.cf_browser import (
    BrowserSession,
    BrowserSessionError,
    looks_like_challenge,
    profile_dir,
)

from conftest import load_fixture


class TestLooksLikeChallenge:
    def test_true_for_the_real_interstitial(self):
        assert looks_like_challenge(load_fixture("69shuba", "challenge.html"))

    @pytest.mark.parametrize("name", ["novel.html", "toc.html", "chapter.html"])
    def test_false_for_real_content(self, name):
        # Guards against a marker that's coincidentally present in ordinary pages —
        # a false positive here would abort a working download.
        assert not looks_like_challenge(load_fixture("69shuba", name))

    def test_false_for_empty(self):
        assert not looks_like_challenge("")


def test_profile_dir_is_separate_from_the_discord_profile():
    from noveltrans.discord_unlock import profile_dir as discord_profile_dir

    assert profile_dir().name == ".cf-profile"
    assert profile_dir() != discord_profile_dir()


class _FakePage:
    """Stands in for a Playwright page: records navigations, serves canned markup."""

    def __init__(self, markup: str = "<html>ok</html>", fail: Exception | None = None):
        self.markup = markup
        self.fail = fail
        self.goto_urls: list[str] = []
        self.routes: list[str] = []
        self.timeout_ms: int | None = None

    def set_default_timeout(self, ms):
        self.timeout_ms = ms

    def route(self, pattern, _handler):
        self.routes.append(pattern)

    def goto(self, url, **_kw):
        if self.fail:
            raise self.fail
        self.goto_urls.append(url)

    def content(self):
        return self.markup


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class _FakePlaywright:
    """Fake sync_playwright: hands back one context, records teardown."""

    def __init__(self, page):
        self._page = page
        self.context = _FakeContext(page)
        self.launches = 0
        self.stopped = False
        self.chromium = self

    def __call__(self):
        return self

    def start(self):
        return self

    def launch_persistent_context(self, _path, **_kw):
        self.launches += 1
        return self.context

    def stop(self):
        self.stopped = True


def make_session(tmp_path, page=None, **kw) -> tuple[BrowserSession, _FakePlaywright]:
    page = page or _FakePage()
    fake = _FakePlaywright(page)
    session = BrowserSession(
        tmp_path / "profile", delay_seconds=0, sync_playwright=fake, **kw
    )
    return session, fake


class TestLaunchIsLazy:
    def test_constructing_does_not_launch(self, tmp_path):
        # Adapters get built for cancelled scans and in tests; that must never start
        # Chrome.
        _session, fake = make_session(tmp_path)
        assert fake.launches == 0

    def test_first_get_html_launches_once_and_reuses(self, tmp_path):
        session, fake = make_session(tmp_path)
        session.get_html("https://www.69shuba.com/book/59024/")
        session.get_html("https://www.69shuba.com/txt/59024/38369377")
        assert fake.launches == 1  # one context for the whole batch, not per page
        assert len(session._page.goto_urls) == 2

    def test_route_registered_once_for_the_session(self, tmp_path):
        session, _fake = make_session(tmp_path)
        session.get_html("https://www.69shuba.com/book/59024/")
        session.get_html("https://www.69shuba.com/book/59024/")
        assert session._page.routes == ["**/*"]  # not re-paid per navigation


class TestThrottle:
    def test_waits_between_navigations(self, tmp_path, monkeypatch):
        # The HttpClient throttle is bypassed on this path, so this delay is the only
        # thing keeping a 199-chapter batch from hammering the site.
        slept: list[float] = []
        monkeypatch.setattr("noveltrans.cf_browser.time.sleep", slept.append)
        session, _fake = make_session(tmp_path)
        session.delay_seconds = 1.5
        session.get_html("https://www.69shuba.com/book/59024/")
        session.get_html("https://www.69shuba.com/book/59024/")
        assert len(slept) == 1 and 0 < slept[0] <= 1.5


class TestChallengeAndFailure:
    def test_raises_on_a_challenge_instead_of_returning_it(self, tmp_path):
        # Handing interstitial markup to the parser would surface as a misleading
        # "page layout may have changed".
        page = _FakePage(markup=load_fixture("69shuba", "challenge.html"))
        session, _fake = make_session(tmp_path, page=page)
        with pytest.raises(BrowserSessionError, match="challenge"):
            session.get_html("https://www.69shuba.com/book/59024/")

    def test_navigation_failure_closes_the_session(self, tmp_path):
        # User closed the window / browser crashed: don't let 198 more chapters retry
        # a dead browser.
        page = _FakePage(fail=RuntimeError("Target page closed"))
        session, fake = make_session(tmp_path, page=page)
        with pytest.raises(BrowserSessionError):
            session.get_html("https://www.69shuba.com/book/59024/")
        assert fake.context.closed and fake.stopped
        assert session._page is None

    def test_returns_the_rendered_markup_on_success(self, tmp_path):
        markup = load_fixture("69shuba", "chapter.html")
        session, _fake = make_session(tmp_path, page=_FakePage(markup=markup))
        returned = session.get_html("https://www.69shuba.com/txt/59024/38369377")
        assert returned == markup
        assert "txtnav" in returned  # the container the adapter will parse


class TestClose:
    def test_close_is_safe_before_launch(self, tmp_path):
        session, _fake = make_session(tmp_path)
        session.close()  # must not raise

    def test_close_twice_is_safe(self, tmp_path):
        session, fake = make_session(tmp_path)
        session.get_html("https://www.69shuba.com/book/59024/")
        session.close()
        session.close()
        assert fake.context.closed and fake.stopped

    def test_context_manager_closes(self, tmp_path):
        session, fake = make_session(tmp_path)
        with session:
            session.get_html("https://www.69shuba.com/book/59024/")
        assert fake.context.closed and fake.stopped

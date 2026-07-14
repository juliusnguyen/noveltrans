"""Tests for the browser-free parts of the Discord auto-unlock module.

The Playwright automation itself needs a live Discord session and can't run in CI,
so we cover the pure logic: channel-URL validation and the input guards that reject
bad calls before any browser is launched.
"""

from __future__ import annotations

import pytest

from noveltrans.discord_unlock import (
    _launch_context,
    _PICKER_OPTION_SEL,
    DiscordUnlockError,
    _command_offered,
    _wait_until_sent,
    profile_dir,
    run_unlock,
    valid_channel_url,
)


class TestValidChannelUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://discord.com/channels/123456789/987654321",
            "https://discord.com/channels/123456789/987654321/",
            "https://discord.com/channels/@me/987654321",
        ],
    )
    def test_accepts_real_channel_links(self, url):
        assert valid_channel_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "https://discord.gg/QDP8zjCa38",  # an *invite*, not a channel link
            "https://discord.com/channels/123456789",  # missing channel id
            "discord.com/channels/1/2",  # missing scheme
            "https://example.com/channels/1/2",
        ],
    )
    def test_rejects_non_channel_links(self, url):
        assert not valid_channel_url(url)


class TestRunUnlockGuards:
    def test_rejects_bad_channel_url_before_launching(self):
        with pytest.raises(DiscordUnlockError):
            run_unlock("https://discord.gg/whatever", "URFME4PA")

    def test_rejects_empty_code_before_launching(self):
        with pytest.raises(DiscordUnlockError):
            run_unlock("https://discord.com/channels/1/2", "")


def test_profile_dir_is_dedicated_and_separate_from_library():
    path = profile_dir()
    assert path.name == ".discord-profile"


class _FakeLocator:
    """Stands in for a Playwright locator: options list or a composer with a script
    of successive inner_text() values (the last value repeats forever)."""

    def __init__(self, texts: list[str]):
        self.texts = texts

    @property
    def last(self):
        return self

    def all_inner_texts(self) -> list[str]:
        return self.texts

    def inner_text(self) -> str:
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]


class _FakePage:
    def __init__(self, *, options: list[str] = (), composer: list[str] = ("",)):
        self._options = _FakeLocator(list(options))
        self._composer = _FakeLocator(list(composer))
        self.waits = 0

    def locator(self, selector: str) -> _FakeLocator:
        return self._options if selector == _PICKER_OPTION_SEL else self._composer

    def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1


class _FakePlaywright:
    """Fake sync_playwright: records launches, fails the channels it doesn't have."""

    def __init__(self, available: tuple):
        self.available = available
        self.channels_tried: list = []
        self.stopped = False
        self.chromium = self

    def __call__(self):
        return self

    def start(self):
        return self

    def launch_persistent_context(self, _path, *, channel=None, **_kw):
        self.channels_tried.append(channel)
        if channel not in self.available:
            raise RuntimeError(f"no browser for channel {channel!r}")
        return f"context:{channel}"

    def stop(self):
        self.stopped = True


class TestLaunchContext:
    def test_prefers_real_chrome(self, tmp_path, monkeypatch):
        monkeypatch.setattr("noveltrans.discord_unlock.profile_dir", lambda: tmp_path / "p")
        fake = _FakePlaywright(available=("chrome", None))
        _pw, context = _launch_context(fake, headless=True)
        assert context == "context:chrome"
        assert fake.channels_tried == ["chrome"]  # never reached the bundled build

    def test_falls_back_to_bundled_chromium_without_chrome(self, tmp_path, monkeypatch):
        monkeypatch.setattr("noveltrans.discord_unlock.profile_dir", lambda: tmp_path / "p")
        fake = _FakePlaywright(available=(None,))
        _pw, context = _launch_context(fake, headless=True)
        assert context == "context:None"
        assert fake.channels_tried == ["chrome", None]

    def test_raises_and_stops_playwright_when_no_browser_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr("noveltrans.discord_unlock.profile_dir", lambda: tmp_path / "p")
        fake = _FakePlaywright(available=())
        with pytest.raises(DiscordUnlockError):
            _launch_context(fake, headless=True)
        assert fake.stopped  # no leaked Playwright process


class TestCommandOffered:
    def test_true_when_picker_lists_the_command(self):
        page = _FakePage(options=["/mochuong  Mở khoá 50 chương tiếp"])
        assert _command_offered(page)

    def test_false_when_picker_lists_only_other_commands(self):
        # Enter here would post "/mochuong" to the channel as a plain message.
        page = _FakePage(options=["/help  Trợ giúp", "/ping  Kiểm tra bot"])
        assert not _command_offered(page)

    def test_false_when_picker_is_empty(self):
        assert not _command_offered(_FakePage(options=[]))


class TestWaitUntilSent:
    def test_true_once_the_composer_clears(self):
        page = _FakePage(composer=["mochuong URFME4PA", "mochuong URFME4PA", ""])
        assert _wait_until_sent(page, "URFME4PA")
        assert page.waits == 2  # polled until Discord emptied the composer

    def test_false_when_the_command_never_leaves_the_composer(self):
        page = _FakePage(composer=["mochuong URFME4PA"])
        assert not _wait_until_sent(page, "URFME4PA")

    def test_false_when_only_the_code_lingers(self):
        # Command pill sent but the argument never went out — not an unlock.
        page = _FakePage(composer=["urfme4pa"])
        assert not _wait_until_sent(page, "URFME4PA")

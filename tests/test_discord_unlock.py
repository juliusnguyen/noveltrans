"""Tests for the browser-free parts of the Discord auto-unlock module.

The Playwright automation itself needs a live Discord session and can't run in CI,
so we cover the pure logic: channel-URL validation and the input guards that reject
bad calls before any browser is launched.
"""

from __future__ import annotations

import pytest

from noveltrans.discord_unlock import (
    DiscordUnlockError,
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

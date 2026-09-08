"""Feature 085 — the pure half of Telegram reporting: what it says, and when.

No network and no Qt. The throttle is tested by moving a clock forward by hand, which is
why it lives in `JobTracker` rather than in a Qt timer.

The load-bearing test is `test_a_command_from_any_other_chat_is_refused`: the bot is
reachable by anyone who finds it, so that check is the whole security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from noveltrans.notify import (
    DEFAULT_ANNOUNCE_AFTER,
    JobTracker,
    TelegramClient,
    TelegramCredentials,
    Update,
    default_machine_name,
    eta_seconds,
    format_duration,
    format_finished,
    format_help,
    format_job,
    format_status,
    parse_command,
)


@dataclass
class FakeJob:
    """The fields `JobRegistry.Job` exposes, without importing Qt."""

    id: int = 1
    kind: str = "Dịch"
    novel: str = "Vạn Cổ Thần Đế"
    done: int = 0
    total: int = 0
    message: str = ""
    paused: bool = False

    @property
    def label(self) -> str:
        return f"{self.kind} — {self.novel}" if self.novel else self.kind


class TestDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0 giây"), (45, "45 giây"), (90, "2 phút"), (1080, "18 phút"), (7560, "2,1 giờ")],
    )
    def test_it_reads_as_prose_not_as_a_table_cell(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_it_uses_the_vietnamese_decimal_comma(self):
        assert "," in format_duration(7560) and "." not in format_duration(7560)

    def test_a_negative_duration_does_not_produce_nonsense(self):
        assert format_duration(-5) == "0 giây"


class TestEta:
    def test_it_extrapolates_the_average_rate(self):
        # 100 of 400 chapters in 10 minutes → 30 minutes left.
        assert eta_seconds(100, 400, 600) == pytest.approx(1800)

    @pytest.mark.parametrize(
        ("done", "total", "elapsed"),
        [(0, 400, 600), (100, 0, 600), (400, 400, 600), (100, 400, 0)],
    )
    def test_it_says_nothing_rather_than_guessing(self, done, total, elapsed):
        """Before the first chapter lands there is no rate, and a made-up number on a
        phone is worse than no number."""
        assert eta_seconds(done, total, elapsed) is None


class TestFormatting:
    def test_a_running_job_shows_counts_percent_and_time_left(self):
        text = format_job(FakeJob(done=340, total=1200), elapsed=2400)
        assert "Dịch — Vạn Cổ Thần Đế" in text
        assert "340/1200 (28%)" in text
        assert "còn ~" in text

    def test_a_paused_job_says_so_and_offers_no_estimate(self):
        # An ETA for a job that is not moving would count down to a lie.
        text = format_job(FakeJob(done=340, total=1200, paused=True), elapsed=2400)
        assert "tạm dừng" in text and "còn ~" not in text

    def test_a_job_with_no_total_falls_back_to_its_message(self):
        text = format_job(FakeJob(total=0, message="Đang dò tên nhân vật…"))
        assert "Đang dò tên nhân vật…" in text

    def test_the_finish_notice_carries_the_totals_and_the_time(self):
        text = format_finished(FakeJob(done=1200, total=1200), 12240)
        assert text.startswith("✅")
        assert "1200/1200" in text and "3,4 giờ" in text

    def test_status_lists_every_job_in_one_message(self):
        text = format_status(
            [FakeJob(id=1, done=340, total=1200), FakeJob(id=2, kind="Tạo video", done=3, total=10)],
            {1: 2400},
        )
        assert "2 tác vụ" in text
        assert "Dịch" in text and "Tạo video" in text

    def test_status_says_plainly_when_nothing_is_running(self):
        assert "Không có tác vụ nào" in format_status([])

    def test_help_lists_exactly_the_commands_that_exist(self):
        text = format_help()
        for command in ("/status", "/pause", "/resume"):
            assert command in text
        assert "/stop" not in text  # never offered: a mistap must not discard hours of work


class TestMachineLabel:
    """Two machines can share one bot for SENDING, so a message has to say which is which."""

    def test_the_label_leads_every_kind_of_message(self):
        job = FakeJob(done=340, total=1200)
        assert format_job(job, 2400, "Mac mini").startswith("🖥 Mac mini · ")
        assert format_finished(job, 2400, "Mac mini").startswith("🖥 Mac mini · ")
        assert format_status([job], {1: 2400}, "Mac mini").startswith("🖥 Mac mini · ")
        assert format_status([], machine="Mac mini").startswith("🖥 Mac mini · ")

    def test_one_machine_adds_no_clutter(self):
        # The single-machine case is the common one and must look exactly as before.
        assert format_job(FakeJob(done=1, total=2)).startswith("⏳")

    def test_the_default_name_is_never_a_number(self):
        """`platform.node()` is the IP address on a DHCP macOS box (measured:
        192.168.2.8), and splitting it on "." gives "192" — a label naming nothing."""
        name = default_machine_name()
        assert name and not name.replace("-", "").isdigit()


class TestCommandParsing:
    @pytest.mark.parametrize(
        "text", ["/status", "status", "/status@NovelTransBot", "  /STATUS  ", "/status ngay"]
    )
    def test_it_accepts_the_shapes_telegram_actually_sends(self, text):
        assert parse_command(text) == "status"

    @pytest.mark.parametrize("text", ["", "   ", "xin chào", "/stop", "/delete", "chào bot"])
    def test_ordinary_chatter_does_nothing(self, text):
        """The bot shares a chat with whatever else is said in it; guessing at intent
        would make a stray message pause someone's run."""
        assert parse_command(text) == ""


class TestAuthorisation:
    def test_a_command_from_the_configured_chat_is_allowed(self):
        credentials = TelegramCredentials(token="t", chat_id="12345")
        assert Update(1, "12345", "/status").authorised(credentials)

    def test_a_command_from_any_other_chat_is_refused(self):
        """THE security boundary. A bot is reachable by anyone who finds it, and without
        this a stranger could pause an overnight run."""
        credentials = TelegramCredentials(token="t", chat_id="12345")
        assert not Update(1, "99999", "/pause").authorised(credentials)

    def test_an_unconfigured_chat_id_authorises_nobody(self):
        # Empty must mean "no one", never "everyone".
        assert not Update(1, "12345", "/status").authorised(TelegramCredentials(token="t"))


class TestTracker:
    def test_a_short_job_is_never_announced(self):
        """A twenty-second export must not buzz a phone — and this is why no per-job-kind
        allow-list is needed."""
        tracker = JobTracker(started_at=1000.0)
        assert not tracker.should_announce(1020.0, DEFAULT_ANNOUNCE_AFTER)

    def test_a_long_job_is_announced_once(self):
        tracker = JobTracker(started_at=1000.0)
        assert tracker.should_announce(1070.0, DEFAULT_ANNOUNCE_AFTER)
        tracker.announced = True
        assert not tracker.should_announce(1200.0, DEFAULT_ANNOUNCE_AFTER)

    def test_an_unannounced_job_is_never_edited(self):
        tracker = JobTracker(started_at=1000.0)
        assert not tracker.should_update(2000.0, "bất kỳ", 120)

    def test_it_waits_out_the_interval(self):
        tracker = JobTracker(started_at=1000.0, message_id=7, last_sent_at=1000.0,
                             last_line="cũ", announced=True)
        assert not tracker.should_update(1060.0, "mới", 120)
        assert tracker.should_update(1130.0, "mới", 120)

    def test_an_unchanged_line_is_not_rewritten(self):
        """A stalled job would otherwise keep rewriting an identical message forever."""
        tracker = JobTracker(started_at=1000.0, message_id=7, last_sent_at=1000.0,
                             last_line="340/1200", announced=True)
        assert not tracker.should_update(9999.0, "340/1200", 120)


class _FakeTransport:
    """Stands in for the Bot API: records calls, returns scripted replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload):
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        return self.replies.pop(0) if self.replies else {}

    @property
    def methods(self) -> list[str]:
        return [method for method, _payload in self.calls]


class TestClient:
    def _client(self, transport, **kwargs):
        return TelegramClient(
            TelegramCredentials(token="tok", chat_id="42"), transport=transport, **kwargs
        )

    def test_send_returns_the_message_id_so_it_can_be_edited(self):
        transport = _FakeTransport({"message_id": 99})
        assert self._client(transport).send("xin chào") == 99
        assert transport.methods == ["sendMessage"]
        assert transport.calls[0][1]["chat_id"] == "42"

    def test_edit_rewrites_the_same_message(self):
        transport = _FakeTransport({"message_id": 99})
        assert self._client(transport).edit(99, "mới") is True
        assert transport.calls[0][1]["message_id"] == 99

    def test_nothing_is_sent_when_it_is_not_configured(self):
        transport = _FakeTransport({"message_id": 1})
        client = TelegramClient(TelegramCredentials(token="tok"), transport=transport)
        assert client.send("xin chào") is None
        assert transport.calls == []  # not even attempted

    def test_a_network_failure_costs_a_status_line_and_nothing_else(self):
        """Rule 1: a notifier must never be able to break a job."""

        def broken(_url, _payload):
            raise OSError("mạng hỏng")

        client = self._client(broken)
        assert client.send("xin chào") is None
        assert client.edit(1, "x") is False
        assert client.poll() == []

    def test_poll_reads_messages_and_ignores_anything_else(self):
        transport = _FakeTransport(
            [
                {"update_id": 5, "message": {"chat": {"id": 42}, "text": "/status"}},
                {"update_id": 6, "edited_message": {"chat": {"id": 42}}},  # no `message`
                {"update_id": 7, "message": {"text": "không có chat"}},
            ]
        )
        updates = self._client(transport).poll()
        assert [(u.update_id, u.chat_id, u.text) for u in updates] == [(5, "42", "/status")]

    def test_a_conflict_is_flagged_rather_than_swallowed(self):
        """`getUpdates` allows ONE consumer. Two machines on one token knock each other
        off forever, so this is the one error that must not be retried blindly."""
        import urllib.error

        def conflicted(_url, _payload):
            raise urllib.error.HTTPError("u", 409, "Conflict", {}, None)

        client = self._client(conflicted)
        assert client.poll() == []
        assert client.conflict is True

    def test_an_ordinary_failure_is_not_a_conflict(self):
        def flaky(_url, _payload):
            raise OSError("mạng hỏng")

        client = self._client(flaky)
        assert client.poll() == []
        assert client.conflict is False  # a dropped connection must keep retrying

    def test_poll_passes_the_offset_so_a_message_is_read_once(self):
        transport = _FakeTransport([])
        self._client(transport).poll(offset=12)
        assert transport.calls[0][1]["offset"] == 12

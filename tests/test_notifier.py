"""Feature 085 — the Qt half: what actually reaches Telegram as jobs come and go.

The notifier is driven through the real `JobRegistry` with a fake Telegram client, and the
sends are made synchronous so a test never races a thread pool.

Two tests carry the feature's rules: `test_a_short_job_is_never_mentioned` (no buzzing a
phone over a 20-second export) and `test_a_command_from_another_chat_changes_nothing` (the
bot is reachable by anyone who finds it).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

from noveltrans.gui.jobs import JobRegistry
from noveltrans.gui.notifier import JobNotifier
from noveltrans.notify import TelegramCredentials, Update

CREDENTIALS = TelegramCredentials(token="tok", chat_id="42")


class _FakeClient:
    """Records what would have been sent; hands back predictable message ids."""

    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.next_id = 100

    def send(self, text):
        self.sent.append(text)
        self.next_id += 1
        return self.next_id

    def edit(self, message_id, text):
        self.edits.append((message_id, text))
        return True

    def poll(self, offset=0, timeout=30):
        return []


class _FakeWorker(QObject):
    """A worker the registry will accept: it needs `finished`, and `progress` to be useful."""

    finished = Signal()
    progress = Signal(int, int, str)

    def __init__(self):
        super().__init__()
        self.paused = False

    def isFinished(self):
        return False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


@pytest.fixture
def rig(qapp, monkeypatch):
    """A registry + notifier wired to a fake client, with sends run inline.

    `_submit` is replaced rather than the executor: it keeps the notifier's real code path
    and only removes the thread hop, so ordering in the test is the ordering in the app.
    """
    registry = JobRegistry()
    client = _FakeClient()
    notifier = JobNotifier(registry, CREDENTIALS, client=client, announce_after=60,
                           update_seconds=120)
    monkeypatch.setattr(notifier, "_submit", lambda work: work())
    clock = {"now": 1000.0}
    monkeypatch.setattr("noveltrans.gui.notifier.time.monotonic", lambda: clock["now"])
    return notifier, registry, client, clock


def _start(registry, kind="Dịch", novel="Truyện"):
    worker = _FakeWorker()
    job = registry.register(worker, kind=kind, novel=novel)
    return worker, job


class TestAnnouncing:
    def test_a_short_job_is_never_mentioned(self, rig):
        """A twenty-second export must not buzz a phone."""
        notifier, registry, client, clock = rig
        worker, _job = _start(registry)
        clock["now"] += 20
        worker.progress.emit(5, 5, "")
        worker.finished.emit()
        assert client.sent == [] and client.edits == []

    def test_a_long_job_announces_itself_once(self, rig):
        notifier, registry, client, clock = rig
        worker, _job = _start(registry)
        clock["now"] += 90
        worker.progress.emit(10, 1200, "")
        worker.progress.emit(11, 1200, "")  # a second tick must not re-announce
        assert len(client.sent) == 1
        assert "Dịch — Truyện" in client.sent[0]

    def test_progress_edits_the_same_message_instead_of_sending_more(self, rig):
        notifier, registry, client, clock = rig
        worker, _job = _start(registry)
        clock["now"] += 90
        worker.progress.emit(10, 1200, "")
        message_id = client.next_id
        clock["now"] += 200
        worker.progress.emit(400, 1200, "")
        assert len(client.sent) == 1  # still one message…
        assert client.edits and client.edits[-1][0] == message_id  # …rewritten in place
        assert "400/1200" in client.edits[-1][1]

    def test_a_thousand_ticks_do_not_become_a_thousand_messages(self, rig):
        notifier, registry, client, clock = rig
        worker, _job = _start(registry)
        clock["now"] += 90
        for done in range(1, 1000):
            worker.progress.emit(done, 1000, "")
        assert len(client.sent) == 1
        assert len(client.edits) == 0  # the clock never advanced past the interval

    def test_finishing_sends_a_new_message_because_an_edit_does_not_push(self, rig):
        notifier, registry, client, clock = rig
        worker, _job = _start(registry)
        clock["now"] += 90
        worker.progress.emit(10, 1200, "")
        clock["now"] += 600
        worker.progress.emit(1200, 1200, "")
        worker.finished.emit()
        assert len(client.sent) == 2
        assert client.sent[-1].startswith("✅")
        assert "Dịch — Truyện" in client.sent[-1]

    def test_a_job_that_was_never_announced_says_nothing_when_it_ends(self, rig):
        notifier, registry, client, clock = rig
        worker, _job = _start(registry)
        clock["now"] += 10
        worker.finished.emit()
        assert client.sent == []

    def test_the_tick_announces_a_worker_that_reports_progress_rarely(self, rig):
        """A video render emits one signal per part; without the timer it would never
        reach its announce threshold at all."""
        notifier, registry, client, clock = rig
        _worker, _job = _start(registry, kind="Tạo video")
        clock["now"] += 300
        notifier._tick()
        assert len(client.sent) == 1 and "Tạo video" in client.sent[0]

    def test_nothing_is_sent_when_the_bot_is_not_configured(self, qapp, monkeypatch):
        registry = JobRegistry()
        client = _FakeClient()
        notifier = JobNotifier(registry, TelegramCredentials(), client=client)
        monkeypatch.setattr(notifier, "_submit", lambda work: work())
        worker, _job = _start(registry)
        worker.progress.emit(10, 1200, "")
        notifier._tick()
        assert client.sent == []


class TestCommands:
    def test_status_reports_every_running_job(self, rig):
        notifier, registry, client, clock = rig
        _start(registry, kind="Dịch")
        _start(registry, kind="Tạo video")
        notifier._on_command("status")
        assert len(client.sent) == 1
        assert "Dịch" in client.sent[0] and "Tạo video" in client.sent[0]

    def test_status_works_before_a_job_has_been_announced(self, rig):
        """Asking is not the same as being told: /status answers about everything running,
        including a job too young to have announced itself."""
        notifier, registry, client, clock = rig
        _start(registry)
        notifier._on_command("status")
        assert "Dịch — Truyện" in client.sent[0]

    def test_pause_and_resume_go_through_the_same_registry_as_the_popup(self, rig):
        notifier, registry, client, clock = rig
        worker, job = _start(registry)
        notifier._on_command("pause")
        assert worker.paused is True and job.paused is True
        notifier._on_command("resume")
        assert worker.paused is False and job.paused is False
        assert "Đã tạm dừng" in client.sent[0] and "Đã chạy tiếp" in client.sent[1]

    def test_pausing_nothing_says_so_rather_than_going_quiet(self, rig):
        notifier, registry, client, clock = rig
        notifier._on_command("pause")
        assert client.sent and "Không có tác vụ nào" in client.sent[0]

    def test_help_lists_the_commands(self, rig):
        notifier, registry, client, clock = rig
        notifier._on_command("help")
        assert "/status" in client.sent[0]


class TestPollLoop:
    def _updates(self, notifier, *updates):
        """Run one pass of the poll loop's body over `updates`, capturing what it emits."""
        seen: list[str] = []
        notifier.command_received.connect(seen.append)
        for update in updates:
            if not update.authorised(notifier.credentials):
                continue
            from noveltrans.notify import parse_command

            command = parse_command(update.text)
            if command:
                notifier.command_received.emit(command)
        return seen

    def test_a_command_from_the_configured_chat_is_acted_on(self, rig):
        notifier, _registry, _client, _clock = rig
        assert self._updates(notifier, Update(1, "42", "/status")) == ["status"]

    def test_a_command_from_another_chat_changes_nothing(self, rig):
        """The security boundary, at the layer that would act on it."""
        notifier, _registry, _client, _clock = rig
        assert self._updates(notifier, Update(1, "99999", "/pause")) == []

    def test_chatter_in_the_right_chat_is_still_ignored(self, rig):
        notifier, _registry, _client, _clock = rig
        assert self._updates(notifier, Update(1, "42", "chào bạn")) == []


class TestTwoMachinesOneBot:
    """Sending is unrestricted; long polling is not. One bot, two machines is supported —
    both report, only one listens."""

    def test_every_message_says_which_machine_sent_it(self, qapp, monkeypatch):
        registry = JobRegistry()
        client = _FakeClient()
        notifier = JobNotifier(registry, CREDENTIALS, client=client, announce_after=0,
                               machine="Mac mini")
        monkeypatch.setattr(notifier, "_submit", lambda work: work())
        worker, _job = _start(registry)
        worker.progress.emit(10, 1200, "")
        assert client.sent[0].startswith("🖥 Mac mini · ")

    def test_the_second_machine_reports_but_never_polls(self, qapp, monkeypatch):
        registry = JobRegistry()
        client = _FakeClient()
        notifier = JobNotifier(registry, CREDENTIALS, client=client, announce_after=0,
                               accept_commands=False, machine="PC")
        monkeypatch.setattr(notifier, "_submit", lambda work: work())
        notifier.start()
        assert notifier._poll_thread is None  # no second poller to fight the first
        worker, _job = _start(registry)
        worker.progress.emit(10, 1200, "")
        assert client.sent and "PC" in client.sent[0]  # …but it still reports
        notifier.stop()

    def test_a_conflict_stands_down_instead_of_fighting(self, qapp, monkeypatch):
        """Retrying would knock the OTHER machine off on every poll, so both would answer
        intermittently. Say so once and stop."""
        registry = JobRegistry()

        class _Conflicted(_FakeClient):
            conflict = True

            def poll(self, offset=0, timeout=30):
                return []

        client = _Conflicted()
        notifier = JobNotifier(registry, CREDENTIALS, client=client, machine="PC")
        monkeypatch.setattr(notifier, "_submit", lambda work: work())
        notifier._poll_loop()  # returns rather than looping
        assert len(client.sent) == 1
        assert "Máy khác đang nhận lệnh" in client.sent[0]


class TestLifecycle:
    def test_start_does_nothing_without_credentials(self, qapp):
        notifier = JobNotifier(JobRegistry(), TelegramCredentials(), client=_FakeClient())
        notifier.start()
        assert notifier._poll_thread is None  # no thread left running for nothing
        notifier.stop()

    def test_stop_is_safe_before_start(self, qapp):
        JobNotifier(JobRegistry(), CREDENTIALS, client=_FakeClient()).stop()

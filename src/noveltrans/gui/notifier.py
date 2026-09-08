"""Wire `JobRegistry` to a Telegram bot: push progress out, take commands back.

All the judgement — what a message says, when one is worth sending, which chat may command
the app — lives in `noveltrans.notify` and is pure. This file is the Qt half: it listens to
the registry, keeps the per-job state, and makes sure **no network call ever runs on the GUI
thread**. A 20-second timeout on a bad connection would otherwise freeze the whole app.

Two background threads, chosen for different reasons:

* **sending** goes through a single-worker `ThreadPoolExecutor`, so messages keep their
  order and a slow send cannot overlap the next one;
* **polling** is a plain daemon `threading.Thread`, not a `QThread`, because a long poll
  sits inside `urlopen` for 30 seconds and cannot be interrupted. A daemon thread lets the
  app quit instantly instead of waiting it out or printing Qt's "destroyed while running".

Commands arrive on that poll thread and are re-emitted as a Qt signal, which Qt queues onto
the GUI thread — so pausing a job from a phone runs on exactly the same thread, and through
exactly the same `JobRegistry.pause`, as clicking the button in the menu-bar popup.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal

from noveltrans.notify import (
    DEFAULT_ANNOUNCE_AFTER,
    DEFAULT_UPDATE_SECONDS,
    JobTracker,
    TelegramClient,
    TelegramCredentials,
    format_finished,
    format_help,
    format_job,
    format_status,
    machine_prefix,
    parse_command,
)

# How often the tracked jobs are re-examined. A job whose worker reports progress rarely
# (a video render emits one signal per part) would otherwise never reach its announce
# threshold, because that check only ever ran on an incoming signal.
_TICK_MS = 15_000


class JobNotifier(QObject):
    """Reports every long job to Telegram and answers `/status`, `/pause`, `/resume`."""

    command_received = Signal(str)  # emitted from the poll thread, handled on the GUI thread

    def __init__(
        self,
        registry,
        credentials: TelegramCredentials,
        *,
        update_seconds: float = DEFAULT_UPDATE_SECONDS,
        announce_after: float = DEFAULT_ANNOUNCE_AFTER,
        machine: str = "",
        accept_commands: bool = True,
        client: TelegramClient | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.registry = registry
        self.credentials = credentials
        self.update_seconds = update_seconds
        self.announce_after = announce_after
        # Labels every message, so two machines can report into one chat with one token.
        self.machine = machine
        # Only ONE machine may poll a given bot — see `_poll_loop`. Sending is unrestricted,
        # so a machine with this off still reports everything it is doing.
        self.accept_commands = accept_commands
        self.client = client or TelegramClient(credentials)
        self._trackers: dict[int, JobTracker] = {}
        # The registry drops a Job before `job_removed` reaches anyone, and the signal only
        # carries an id — so the last state of each job is kept here to build the finish
        # message from. Cleared in `_on_removed`, so it cannot outlive the job.
        self._last_seen: dict[int, object] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

        registry.job_added.connect(self._on_added)
        registry.job_changed.connect(self._on_changed)
        registry.job_removed.connect(self._on_removed)
        self.command_received.connect(self._on_command)

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Begin reporting and listening. Safe to call when nothing is configured."""
        if not self.credentials.configured:
            return
        self._timer.start()
        if self.accept_commands and self._poll_thread is None:
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="telegram-poll", daemon=True
            )
            self._poll_thread.start()

    def stop(self) -> None:
        """Stop reporting. The poll thread is a daemon, so quitting never waits on it."""
        self._timer.stop()
        self._poll_stop.set()
        self._executor.shutdown(wait=False)

    # --------------------------------------------------------------- sending

    def _send(self, text: str) -> None:
        self._submit(lambda: self.client.send(text))

    def _submit(self, work) -> None:
        """Run `work` on the sender thread, swallowing a shutdown race."""
        try:
            self._executor.submit(work)
        except RuntimeError:
            pass  # the app is quitting; a status line is not worth a traceback

    # ---------------------------------------------------- registry callbacks

    def _on_added(self, job) -> None:
        self._trackers[job.id] = JobTracker(started_at=time.monotonic())
        self._last_seen[job.id] = job

    def _on_changed(self, job) -> None:
        tracker = self._trackers.get(job.id)
        if tracker is None or not self.credentials.configured:
            return
        self._last_seen[job.id] = job
        now = time.monotonic()
        if tracker.should_announce(now, self.announce_after):
            self._announce(job, tracker, now)
            return
        line = format_job(job, tracker.elapsed(now), self.machine)
        if tracker.should_update(now, line, self.update_seconds):
            tracker.last_line, tracker.last_sent_at = line, now
            message_id = tracker.message_id
            self._submit(lambda: self.client.edit(message_id, line))

    def _on_removed(self, job_id: int) -> None:
        tracker = self._trackers.pop(job_id, None)
        job = self._last_seen.pop(job_id, None)
        if tracker is None or job is None or not tracker.announced:
            return  # never announced → too short to be worth telling anyone it ended
        # A NEW message, not an edit: an edit does not push, and finishing is the event
        # actually worth a buzz.
        self._send(format_finished(job, tracker.elapsed(time.monotonic()), self.machine))

    def _announce(self, job, tracker: JobTracker, now: float) -> None:
        """First message for a job that has proved it will run long enough to matter."""
        tracker.announced = True
        tracker.last_line = format_job(job, tracker.elapsed(now), self.machine)
        tracker.last_sent_at = now
        text = tracker.last_line

        def send_and_remember():
            # Written on the sender thread, read on the GUI thread. No lock: the write is a
            # single attribute assignment, and the only cost of losing the race is that one
            # refresh is skipped because `message_id` is still None.
            tracker.message_id = self.client.send(text)

        self._submit(send_and_remember)

    def _tick(self) -> None:
        """Re-examine every job, so a quiet worker still gets announced and refreshed."""
        for job in self.registry.jobs():
            self._on_changed(job)

    # -------------------------------------------------------------- commands

    def _poll_loop(self) -> None:
        offset = 0
        while not self._poll_stop.is_set():
            updates = self.client.poll(offset)
            if self.client.conflict:
                # Another machine is already long-polling this bot. Retrying would not just
                # fail here — each poll knocks the other machine off, so both would answer
                # intermittently. Stand down, say so once, and keep reporting progress
                # (sending has no such restriction).
                self._send(
                    f"{machine_prefix(self.machine)}⚠️ Máy khác đang nhận lệnh cho bot này, nên máy "
                    "này chỉ báo tiến độ. Muốn nhận lệnh ở đây thì tắt “Nhận lệnh” ở máy kia."
                )
                return
            for update in updates:
                offset = max(offset, update.update_id + 1)
                # THE security check: a bot is reachable by anyone who finds it.
                if not update.authorised(self.credentials):
                    continue
                command = parse_command(update.text)
                if command:
                    self.command_received.emit(command)
            if not updates:
                # A failed poll returns [] immediately; without this the loop would spin.
                self._poll_stop.wait(3)

    def _on_command(self, command: str) -> None:
        """Handle a phone command — on the GUI thread, through the same registry API the
        menu-bar popup uses, so there is one code path for pausing a job."""
        jobs = self.registry.jobs()
        if command == "status":
            now = time.monotonic()
            elapsed = {
                job.id: self._trackers[job.id].elapsed(now)
                for job in jobs
                if job.id in self._trackers
            }
            self._send(format_status(jobs, elapsed, self.machine))
        elif command in ("pause", "resume"):
            paused = command == "pause"
            affected = [job for job in jobs if job.pausable]
            for job in affected:
                self.registry.pause(job.id) if paused else self.registry.resume(job.id)
            if not affected:
                self._send(format_status([], machine=self.machine))
            else:
                verb = "⏸ Đã tạm dừng" if paused else "▶️ Đã chạy tiếp"
                self._send(f"{machine_prefix(self.machine)}{verb} {len(affected)} tác vụ.")
        elif command == "help":
            self._send(format_help())

"""Report long-running jobs to a Telegram bot, and answer questions from it.

The app's batches run for hours — translating a novel, a QC scan with the AI judge, a video
render. Feature 049 made them survive closing the window; this makes them visible from
somewhere other than the machine they run on.

**Why Telegram and not a webhook.** A desktop app cannot receive a webhook: that needs a
public HTTPS address, and this one lives behind a router. Telegram's other mode fits
exactly — `getUpdates` **long polling** is an OUTBOUND connection the app holds open for
~30 s, so commands typed on a phone arrive with no port forwarding, no public IP and no
server. Two requests a minute when idle.

**One message per job, edited in place.** A 1200-chapter run emits thousands of progress
ticks; sending them would be unusable. Instead each job gets ONE message that is edited as
it goes (`editMessageText`), so the phone shows a live line rather than a stream. The
finish notice is a NEW message on purpose — an edit does not push, and finishing is the
event actually worth a buzz.

**Two hard rules, both defensive:**

1. **A notifier must never be able to break a job.** Every call here swallows every
   exception and returns a falsy value. A dropped Wi-Fi connection must cost a status
   line, never a translation run.
2. **Only the configured chat may command the app.** A bot is reachable by anyone who
   finds it, so `Update.authorised` is checked before a command is ever acted on —
   an allow-list of exactly one. Without it a stranger could pause someone's overnight
   render. Commands are deliberately limited to read + pause/resume, all reversible;
   cancelling hours of work still requires the keyboard.

Everything except `TelegramClient` is pure, so the formatting, the throttle and the command
parsing unit-test without a network.
"""

from __future__ import annotations

import json
import platform
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

TELEGRAM_API = "https://api.telegram.org"
POLL_TIMEOUT = 30  # seconds Telegram holds a long poll open before answering empty
DEFAULT_UPDATE_SECONDS = 120  # how often a running job's message is refreshed
DEFAULT_ANNOUNCE_AFTER = 60  # a job must last this long before it is worth a notification

# The commands the bot answers. Deliberately read + reversible only: `/stop` would let a
# mistap on a phone throw away hours of quota already spent.
COMMANDS = ("status", "pause", "resume", "help")

_HELP = (
    "Lệnh có thể dùng:\n"
    "/status — đang chạy gì, tới đâu rồi\n"
    "/pause — tạm dừng mọi tác vụ (dừng ở ranh giới chương, không mất gì)\n"
    "/resume — chạy tiếp\n"
    "/help — bảng này"
)


def default_machine_name() -> str:
    """A short, human name for this computer, for labelling its messages.

    `platform.node()` is not the answer on its own: on a DHCP network macOS reports the
    IP address as the host name (measured: `192.168.2.8`), and splitting that on "." to
    drop a `.local` suffix yields "192" — a label that identifies nothing. So the first
    segment is used only when it is actually a name, and the OS name is the last resort.
    """
    node = platform.node().strip()
    head = node.split(".")[0]
    if head and not head.replace("-", "").isdigit():
        return head
    return {"Darwin": "Mac", "Windows": "PC"}.get(platform.system(), platform.system() or "máy")


# -- credentials --------------------------------------------------------------


@dataclass(frozen=True)
class TelegramCredentials:
    """Bot token + the one chat allowed to talk to it."""

    token: str = ""
    chat_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token.strip() and self.chat_id.strip())


# -- incoming commands --------------------------------------------------------


@dataclass(frozen=True)
class Update:
    """One message Telegram handed back, reduced to what this app cares about."""

    update_id: int
    chat_id: str
    text: str

    def authorised(self, credentials: TelegramCredentials) -> bool:
        """True only for the configured chat. THE security check — see the module docstring."""
        return bool(credentials.chat_id) and self.chat_id == str(credentials.chat_id)


def parse_command(text: str) -> str:
    """The command in `text`, or `""`.

    Accepts `/status`, `status`, and the `/status@MyBot` form Telegram produces in groups;
    anything else is ignored rather than guessed at, so ordinary chatter in the same chat
    does nothing.
    """
    word = (text or "").strip().split()[:1]
    if not word:
        return ""
    command = word[0].lstrip("/").split("@")[0].lower()
    return command if command in COMMANDS else ""


# -- formatting ---------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """A duration a human reads at a glance: `45 giây`, `18 phút`, `2,1 giờ`.

    Not `widgets.format_duration` (`3m05s`): that one is a table cell where compactness
    wins, this one is prose on a phone. Vietnamese decimal comma, like the rest of the UI.
    """
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} giây"
    if seconds < 3600:
        return f"{round(seconds / 60)} phút"
    return f"{seconds / 3600:.1f}".replace(".", ",") + " giờ"


def eta_seconds(done: int, total: int, elapsed: float) -> float | None:
    """Seconds left at the average rate so far, or None when it cannot be known yet.

    The average since the job started, not a rolling window: a chapter can take 10 s or 3
    minutes depending on the engine's mood, and a rolling estimate jumps around so much on
    this workload that it reads as broken. Stable and slightly wrong beats jittery.
    """
    if done <= 0 or total <= 0 or done >= total or elapsed <= 0:
        return None
    return (elapsed / done) * (total - done)


def _progress_line(job, elapsed: float) -> str:
    """`340/1200 (28%) · còn ~2,1 giờ` — the part that changes as a job runs."""
    if not job.total:
        return job.message or "đang chạy…"
    percent = round(job.done / job.total * 100)
    line = f"{job.done}/{job.total} ({percent}%)"
    if job.paused:
        return f"{line} · ⏸ đã tạm dừng"
    remaining = eta_seconds(job.done, job.total, elapsed)
    if remaining is not None:
        line += f" · còn ~{format_duration(remaining)}"
    return line


def machine_prefix(machine: str) -> str:
    """`🖥 Mac mini · ` — which machine is talking.

    Sending is NOT exclusive on the Bot API, so two machines can report into one chat with
    one token. Without a label those two streams are unreadable, which is the only reason
    this exists. Empty (the single-machine case) adds nothing at all.
    """
    return f"🖥 {machine} · " if machine else ""


def format_job(job, elapsed: float = 0.0, machine: str = "") -> str:
    """The whole message for one running job — the text that gets edited in place."""
    return f"{machine_prefix(machine)}⏳ {job.label}\n{_progress_line(job, elapsed)}"


def format_finished(job, elapsed: float, machine: str = "") -> str:
    """The finish notice. Sent as a NEW message so it actually pushes to the phone."""
    return (
        f"{machine_prefix(machine)}✅ Xong: {job.label}\n"
        f"{job.done}/{job.total or job.done} · {format_duration(elapsed)}"
    )


def format_status(
    jobs, elapsed_by_id: dict[int, float] | None = None, machine: str = ""
) -> str:
    """The reply to `/status`: every running job, or a plain "nothing is running".

    One message for all of them rather than one per job — a reply is something the user
    asked for and is waiting on, so it should arrive as a single answer.
    """
    jobs = list(jobs)
    if not jobs:
        return f"{machine_prefix(machine)}😴 Không có tác vụ nào đang chạy."
    elapsed_by_id = elapsed_by_id or {}
    lines = [f"{machine_prefix(machine)}📋 {len(jobs)} tác vụ đang chạy:"]
    for job in jobs:
        lines.append(f"\n• {job.label}\n  {_progress_line(job, elapsed_by_id.get(job.id, 0.0))}")
    return "".join(lines)


def format_help() -> str:
    return _HELP


# -- when to send -------------------------------------------------------------


@dataclass
class JobTracker:
    """Per-job state: when it started, its message id, and when it last spoke.

    The throttle lives here rather than in the Qt layer so it can be tested by moving a
    clock forward, with no timers and no event loop.
    """

    started_at: float
    message_id: int | None = None
    last_sent_at: float = 0.0
    last_line: str = ""
    announced: bool = False

    def elapsed(self, now: float) -> float:
        return max(0.0, now - self.started_at)

    def should_announce(self, now: float, announce_after: float) -> bool:
        """True once a job has run long enough to be worth a notification.

        This is what keeps a 20-second export from buzzing a phone, without needing a
        per-job-kind allow-list that would need updating for every new worker.
        """
        return not self.announced and self.elapsed(now) >= announce_after

    def should_update(self, now: float, line: str, interval: float) -> bool:
        """True when the live message is stale enough — and actually different — to redraw.

        Both conditions matter: time alone would keep rewriting an identical line for a
        stalled job, and change alone would fire on every one of a thousand progress ticks.
        """
        if not self.announced or self.message_id is None:
            return False
        if line == self.last_line:
            return False
        return (now - self.last_sent_at) >= interval


# -- the network edge ---------------------------------------------------------


@dataclass
class TelegramClient:
    """The only part that touches the network. Every method fails soft, on purpose.

    `urllib` rather than `requests`: this runs on a background thread while a batch is
    working, and the standard library is one less thing that can be missing in a frozen
    build. `transport` is injectable so tests never open a socket.
    """

    credentials: TelegramCredentials
    api_url: str = TELEGRAM_API
    timeout: float = 20.0
    transport: object = field(default=None, repr=False)  # callable(url, payload) -> dict
    # Set when Telegram says another process is already long-polling this bot (HTTP 409).
    # The ONE failure that must not be retried: `getUpdates` allows a single consumer, so
    # two machines on one token knock each other off forever. Retrying is not just futile,
    # it also breaks commands on the OTHER machine. See `JobNotifier._poll_loop`.
    conflict: bool = False

    def _call(self, method: str, payload: dict, *, timeout: float | None = None) -> dict:
        """POST to the Bot API and return its `result`, or `{}` on any failure at all."""
        if not self.credentials.token:
            return {}
        url = f"{self.api_url}/bot{self.credentials.token}/{method}"
        try:
            if self.transport is not None:
                return self.transport(url, payload) or {}
            data = urllib.parse.urlencode(payload).encode("utf-8")
            request = urllib.request.Request(url, data=data)  # noqa: S310 — fixed https API
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body.get("result") or {} if body.get("ok") else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                self.conflict = True
            return {}
        except Exception:
            # Rule 1: a notifier must never be able to break a job. A dead network, a
            # revoked token, a Telegram outage — all of it costs a status line and nothing
            # else, and the next tick will try again.
            return {}

    def send(self, text: str) -> int | None:
        """Send a new message; returns its id so it can be edited later."""
        if not self.credentials.configured:
            return None
        result = self._call(
            "sendMessage",
            {"chat_id": self.credentials.chat_id, "text": text, "disable_web_page_preview": "true"},
        )
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return int(message_id) if message_id else None

    def edit(self, message_id: int, text: str) -> bool:
        """Rewrite an existing message in place — how a job's line stays live."""
        if not self.credentials.configured or not message_id:
            return False
        result = self._call(
            "editMessageText",
            {
                "chat_id": self.credentials.chat_id,
                "message_id": message_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
        )
        return bool(result)

    def poll(self, offset: int = 0, timeout: int = POLL_TIMEOUT) -> list[Update]:
        """Long-poll for commands. Returns [] on any failure, so a caller can just loop.

        The HTTP timeout is deliberately longer than the poll's: Telegram holds the request
        open for `timeout` seconds and answers empty, so cutting it off sooner would make
        every poll look like a network error.
        """
        payload = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if offset:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout + 15)
        if not isinstance(result, list):
            return []
        updates = []
        for item in result:
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            if not item.get("update_id") or not chat.get("id"):
                continue
            updates.append(
                Update(
                    update_id=int(item["update_id"]),
                    chat_id=str(chat["id"]),
                    text=str(message.get("text") or ""),
                )
            )
        return updates

    def describe_self(self) -> str:
        """The bot's @name, for the settings dialog's test button. "" if it cannot be read."""
        result = self._call("getMe", {})
        username = result.get("username") if isinstance(result, dict) else ""
        return f"@{username}" if username else ""

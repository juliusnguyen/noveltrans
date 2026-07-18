"""Make CLI tools (ffmpeg/ffprobe) findable when the app is launched from Finder.

A macOS GUI app opened from Finder/Launchpad/Dock inherits only a minimal PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`) — it does **not** include Homebrew, MacPorts, or
`~/.local/bin`, which is where ffmpeg usually lives. A terminal run works because the shell
sets a fuller PATH, so "works in the terminal but the Tạo video button is greyed out in the
.app" is exactly this gap: `shutil.which("ffmpeg")` returns None and every ffmpeg/ffprobe
subprocess would fail with FileNotFoundError.

Prepending the standard tool directories to `os.environ["PATH"]` once at startup fixes it
for the whole process — `shutil.which` and every `subprocess` call inherit the augmented
PATH, so the app finds ffmpeg the same way a terminal would.
"""

from __future__ import annotations

import os
from pathlib import Path

# Common places CLI tools land on macOS/Linux, highest priority first. `~/.local/bin`
# (pip --user / pipx / manual installs) is added per-user in augment_tool_path.
_TOOL_DIRS = (
    "/opt/homebrew/bin",  # Apple Silicon Homebrew
    "/usr/local/bin",     # Intel Homebrew / common installs
    "/opt/local/bin",     # MacPorts
)


def augment_tool_path(environ: dict[str, str] | None = None, home: Path | None = None) -> str:
    """Prepend the standard tool dirs (those that exist) to PATH; return the new PATH.

    Idempotent — a directory already on PATH is never duplicated, so calling this twice (or
    running from a terminal that already has these dirs) is a no-op. Mutates `environ` in
    place (defaults to `os.environ`).
    """
    env = os.environ if environ is None else environ
    home = home or Path.home()
    candidates = [*_TOOL_DIRS, str(home / ".local" / "bin")]

    current = env.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    have = set(parts)
    prefix = [d for d in candidates if d not in have and os.path.isdir(d)]

    new_path = os.pathsep.join([*prefix, *parts]) if prefix else current
    env["PATH"] = new_path
    return new_path

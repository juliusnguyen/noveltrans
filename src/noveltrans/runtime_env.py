"""Make the process environment sane regardless of how the app was launched.

Two unrelated gaps, both only visible once you're launched a way a terminal never would:

* **PATH.** A macOS GUI app opened from Finder/Launchpad/Dock inherits only a minimal
  PATH (`/usr/bin:/bin:/usr/sbin:/sbin`) — it does **not** include Homebrew, MacPorts, or
  `~/.local/bin`, which is where ffmpeg usually lives. A terminal run works because the
  shell sets a fuller PATH, so "works in the terminal but the Tạo video button is greyed
  out in the .app" is exactly this gap: `shutil.which("ffmpeg")` returns None and every
  ffmpeg/ffprobe subprocess would fail with FileNotFoundError. Windows has the same class
  of problem — launching `NovelTrans.exe` from Explorer/Start Menu doesn't pick up
  Chocolatey, Scoop, or winget's install locations unless they happen to already be on
  the system/user PATH — plus a Windows build ships its own bundled `ffmpeg.exe`, which
  is on nobody's PATH at all until `augment_tool_path()` runs.

* **Missing stdio.** A Windows build frozen with `console=False` (see
  `NovelTrans-windows.spec`) has no console attached at all, so `sys.stdout`/`sys.stderr`/
  `sys.stdin` are `None` — not redirected, genuinely absent. Any dependency that prints
  unconditionally (a `tqdm`/huggingface_hub download progress bar, ONNX Runtime logging,
  ...) then crashes the instant it runs, with "'NoneType' object has no attribute
  'write'". `ensure_std_streams()` gives them a devnull sink instead.

Both are fixed once at startup — `augment_tool_path()` mutates `os.environ["PATH"]` so
`shutil.which` and every `subprocess` call inherit it; `ensure_std_streams()` mutates
`sys.stdout`/`sys.stderr`/`sys.stdin` so anything downstream that assumes they exist
stops crashing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Common places CLI tools land on macOS/Linux, highest priority first. `~/.local/bin`
# (pip --user / pipx / manual installs) is added per-user in augment_tool_path.
_TOOL_DIRS = (
    "/opt/homebrew/bin",  # Apple Silicon Homebrew
    "/usr/local/bin",     # Intel Homebrew / common installs
    "/opt/local/bin",     # MacPorts
)

# Chocolatey installs its shims to a fixed, non-per-user path; Scoop and winget are
# per-user and derived from `home` in `_candidate_dirs` below.
_WINDOWS_TOOL_DIRS = (r"C:\ProgramData\chocolatey\bin",)


def _candidate_dirs(home: Path) -> list[str]:
    """Standard tool dirs for the current platform, highest priority first."""
    if sys.platform == "win32":
        return [
            *_WINDOWS_TOOL_DIRS,
            str(home / "scoop" / "shims"),
            str(home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"),
        ]
    return [*_TOOL_DIRS, str(home / ".local" / "bin")]


def augment_tool_path(
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    bundle_dir: Path | None = None,
) -> str:
    """Prepend the standard tool dirs (those that exist) to PATH; return the new PATH.

    Idempotent — a directory already on PATH is never duplicated, so calling this twice (or
    running from a terminal that already has these dirs) is a no-op. Mutates `environ` in
    place (defaults to `os.environ`).

    `bundle_dir` is where a Windows build's bundled `ffmpeg.exe`/`ffprobe.exe` live (see
    `packaging/NovelTrans-windows.spec`) — plus its `ffmpeg` subfolder, for anyone who
    drops their own build there instead. Defaults to `sys._MEIPASS`, which PyInstaller's
    bootloader sets at runtime to the directory actually holding bundled files — **not**
    `sys.executable`'s directory: PyInstaller 6+'s onedir layout puts everything except
    the .exe itself in an `_internal` subfolder, so deriving this from the executable's
    path would silently miss the bundled binaries. Nothing when running from source (no
    bootloader, no `_MEIPASS`). Added after the standard tool dirs, so a real install
    (winget/scoop/Homebrew/...) still wins over the bundled copy.
    """
    env = os.environ if environ is None else environ
    home = home or Path.home()
    if bundle_dir is None:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            bundle_dir = Path(meipass)

    candidates = _candidate_dirs(home)
    if bundle_dir is not None:
        candidates = [*candidates, str(bundle_dir), str(bundle_dir / "ffmpeg")]

    current = env.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    have = set(parts)
    prefix = [d for d in candidates if d not in have and os.path.isdir(d)]

    new_path = os.pathsep.join([*prefix, *parts]) if prefix else current
    env["PATH"] = new_path
    return new_path


def ensure_std_streams() -> None:
    """Replace `None` stdio streams with devnull sinks — see the module docstring.

    Idempotent: a stream that already exists (the normal case everywhere except a
    console-less Windows build) is left untouched.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 — lives for the process, no leak
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r")  # noqa: SIM115

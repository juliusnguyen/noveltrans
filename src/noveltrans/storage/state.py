"""AppState — cross-session working state, stored in ~/.noveltrans/state.json.

Remembers which novel you were working on (and the ones before it) so the app
can reopen right where you left off, without pasting the URL again.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".noveltrans"
STATE_FILE = "state.json"
MAX_RECENT = 20


class AppState:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR):
        self.path = Path(state_dir) / STATE_FILE
        self.last_project: str = ""
        self.recent: list[str] = []  # project paths, most recent first
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.last_project = str(data.get("last_project", ""))
        recent = data.get("recent", [])
        if isinstance(recent, list):
            self.recent = [str(p) for p in recent][:MAX_RECENT]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"last_project": self.last_project, "recent": self.recent},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def touch_project(self, project_path: str) -> None:
        """Record `project_path` as the most recently used project."""
        if not project_path:
            return
        self.last_project = project_path
        self.recent = [project_path] + [p for p in self.recent if p != project_path]
        self.recent = self.recent[:MAX_RECENT]
        self.save()

    def valid_last_project(self) -> str:
        """The last project path if its folder still exists, else ""."""
        if self.last_project and (Path(self.last_project) / "meta.json").is_file():
            return self.last_project
        return ""

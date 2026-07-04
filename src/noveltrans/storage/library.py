"""Library — the folder that holds all NovelProject folders."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage.project import META_FILE, NovelProject

DEFAULT_LIBRARY_DIR = Path.home() / "NovelTrans"


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create_project(self, meta: NovelMeta, refs: list[ChapterRef]) -> NovelProject:
        return NovelProject.create(self.root, meta, refs)

    def list_projects(self) -> list[Path]:
        """Project folders sorted by title, without opening their databases."""
        return sorted(
            (p for p in self.root.iterdir() if p.is_dir() and NovelProject.is_project_dir(p)),
            key=lambda p: p.name,
        )

    def project_meta(self, path: Path) -> NovelMeta:
        """Read a project's metadata without opening its database."""
        data = json.loads((Path(path) / META_FILE).read_text(encoding="utf-8"))
        return NovelMeta.from_dict(data)

    def find_by_url(self, url: str) -> Path | None:
        for path in self.list_projects():
            if self.project_meta(path).url == url:
                return path
        return None

    def delete_project(self, path: Path) -> None:
        path = Path(path)
        if not NovelProject.is_project_dir(path):
            raise ValueError(f"Not a NovelTrans project folder: {path}")
        if path.parent != self.root:
            raise ValueError(f"Refusing to delete a folder outside the library: {path}")
        shutil.rmtree(path)

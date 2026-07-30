from pathlib import Path

import pytest

from noveltrans.models import ChapterRef, NovelMeta

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_app_config(tmp_path_factory, monkeypatch):
    """Give every test its own AppConfig storage, never the developer's real one.

    Several tests build a real `AppConfig()` — which is `QSettings("noveltrans",
    "noveltrans")` — and assign to it. `test_tab_scrape._tab_with_project` sets
    `config.library_dir` to a pytest temp path, so running the suite silently repointed
    the real app at a directory that ceases to exist when pytest cleans up. Found only
    because feature 045 added a history list and its contents were visibly pytest paths.

    `QSettings.setDefaultFormat(IniFormat)` was tried first and does NOT work here —
    measured: the format stays NativeFormat and the write still lands in the real plist.
    Patching the one constructor the app uses is what actually holds.

    Per-test rather than per-session, so settings written by one test cannot leak into
    the next.
    """
    from PySide6.QtCore import QSettings

    from noveltrans.config import AppConfig

    directory = tmp_path_factory.mktemp("appconfig")
    counter = {"n": 0}

    def isolated_init(self):
        counter["n"] += 1
        self._s = QSettings(
            str(directory / f"settings{counter['n']}.ini"), QSettings.Format.IniFormat
        )

    monkeypatch.setattr(AppConfig, "__init__", isolated_init)


@pytest.fixture
def sample_meta() -> NovelMeta:
    return NovelMeta(
        url="https://example.com/novel/123",
        site="example",
        title="测试小说 Test Novel",
        author="某作者",
        description="一本测试小说。",
    )


@pytest.fixture
def sample_refs() -> list[ChapterRef]:
    return [
        ChapterRef(index=i, title=f"第{i + 1}章", url=f"https://example.com/novel/123/{i + 1}")
        for i in range(5)
    ]


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    return tmp_path / "library"


@pytest.fixture(scope="session")
def qapp():
    """Offscreen QApplication for model/widget tests."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def load_fixture(site: str, name: str) -> str:
    return (FIXTURES_DIR / site / name).read_text(encoding="utf-8")

from pathlib import Path

import pytest

from noveltrans.models import ChapterRef, NovelMeta

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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

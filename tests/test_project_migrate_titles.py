"""Feature 076 — the one-time repair of titles already damaged on disk.

Prevention only helps new translations; the reporting library had 14 titles already
written. This pass runs inside `NovelProject._migrate`, needs no engine and no network,
and must never touch a chapter body — measured, no body in that library was damaged.
"""

from __future__ import annotations

import pytest

from noveltrans.models import ChapterRef, NovelMeta
from noveltrans.storage.project import NovelProject

REFUSAL = (
    "Xin lỗi, bạn chưa cung cấp nội dung chương 127 để dịch. Vui lòng dán văn bản tiếng "
    "Trung cần dịch, tôi sẽ dịch sang tiếng Việt theo phong cách văn học chuyên nghiệp."
)
LEAK = "Chương 196: Ôn Thục Nghi bùng nổ\n" + "Trước cửa thư viện Khung Thương, gió thổi. " * 5
BODY = "Nội dung chương, đã dịch đầy đủ và hoàn toàn bình thường."


@pytest.fixture
def meta() -> NovelMeta:
    return NovelMeta(url="https://example.com/n/1", site="example", title="测试小说")


def _project(library_dir, meta, titles: list[str]) -> NovelProject:
    refs = [
        ChapterRef(index=i, title=t, url=f"https://example.com/n/1/{i}")
        for i, t in enumerate(titles)
    ]
    return NovelProject.create(library_dir, meta, refs)


def _seed(project: NovelProject, rows: dict[int, str]) -> None:
    """Write damaged translations, then rewind the repair marker so open() re-runs it.

    Damage is applied through `save_translation`, the same door the worker used, so the
    rows are byte-identical to the ones actually found in the library.
    """
    for idx, bad_title in rows.items():
        project.save_content(idx, "正文。")
        project.save_translation(idx, bad_title, BODY, "vi", "CLI (claude, sonnet)")
    project._db.execute("PRAGMA user_version = 0")
    project._db.commit()


def _reopen(project: NovelProject) -> NovelProject:
    path = project.path
    project.close()
    return NovelProject.open(path)


class TestRepair:
    def test_a_refusal_on_a_bare_title_is_rebuilt_locally(self, library_dir, meta):
        project = _project(library_dir, meta, ["第126章", "第127章"])
        _seed(project, {1: REFUSAL})
        assert _reopen(project).chapter(1).translated_title == "Chương 127"

    def test_a_title_plus_body_leak_keeps_only_its_first_line(self, library_dir, meta):
        project = _project(library_dir, meta, ["第196章 暴走的溫淑儀（求月票）"])
        _seed(project, {0: LEAK})
        assert _reopen(project).chapter(0).translated_title == "Chương 196: Ôn Thục Nghi bùng nổ"

    def test_the_chapter_body_is_never_touched(self, library_dir, meta):
        project = _project(library_dir, meta, ["第127章"])
        _seed(project, {0: REFUSAL})
        assert _reopen(project).chapter(0).translated == BODY

    def test_a_clean_title_is_left_alone(self, library_dir, meta):
        project = _project(library_dir, meta, ["第1章 重生"])
        _seed(project, {0: "Chương 1: Trùng sinh"})
        assert _reopen(project).chapter(0).translated_title == "Chương 1: Trùng sinh"

    def test_a_correct_translation_containing_an_apology_survives(self, library_dir, meta):
        # 對不起 means "sorry". A repair keyed on apologies would destroy this row, and two
        # like it exist in the reporting library.
        project = _project(library_dir, meta, ["第268章 對不起通訊器先生"])
        _seed(project, {0: "Chương 268: Xin lỗi nhé máy liên lạc tiên sinh"})
        assert _reopen(project).chapter(0).translated_title == (
            "Chương 268: Xin lỗi nhé máy liên lạc tiên sinh"
        )

    def test_an_unrepairable_row_is_left_visible_rather_than_guessed_at(
        self, library_dir, meta
    ):
        # Damaged, but the source carries no number to rebuild from and the first line is
        # itself the refusal. Leaving it lets the user see and fix it.
        project = _project(library_dir, meta, ["第268章 對不起通訊器先生"])
        _seed(project, {0: REFUSAL})
        assert _reopen(project).chapter(0).translated_title == REFUSAL

    def test_it_repairs_every_damaged_row_in_one_pass(self, library_dir, meta):
        project = _project(library_dir, meta, ["第90章", "第91章", "第1章 重生", "第96章"])
        _seed(project, {0: REFUSAL, 1: REFUSAL, 2: "Chương 1: Trùng sinh", 3: REFUSAL})
        reopened = _reopen(project)
        assert [reopened.chapter(i).translated_title for i in range(4)] == [
            "Chương 90",
            "Chương 91",
            "Chương 1: Trùng sinh",
            "Chương 96",
        ]

    def test_it_honours_the_row_s_target_language(self, library_dir, meta):
        project = _project(library_dir, meta, ["第127章"])
        project.save_content(0, "正文。")
        project.save_translation(0, REFUSAL, BODY, "en", "CLI (claude, sonnet)")
        project._db.execute("PRAGMA user_version = 0")
        project._db.commit()
        assert _reopen(project).chapter(0).translated_title == "Chapter 127"


class TestItRunsOnce:
    def test_the_marker_is_set_after_the_pass(self, library_dir, meta):
        project = _project(library_dir, meta, ["第127章"])
        _seed(project, {0: REFUSAL})
        reopened = _reopen(project)
        assert reopened._db.execute("PRAGMA user_version").fetchone()[0] == 1

    def test_a_later_open_does_not_scan_again(self, library_dir, meta):
        # Damage introduced AFTER the pass stays put: this is a one-time repair of a
        # historical defect, not a permanent sanitiser that would keep rewriting rows the
        # user may have edited by hand.
        project = _project(library_dir, meta, ["第127章"])
        _seed(project, {0: REFUSAL})
        repaired = _reopen(project)
        assert repaired.chapter(0).translated_title == "Chương 127"

        repaired.save_translation(0, REFUSAL, BODY, "vi", "CLI (claude, sonnet)")
        assert _reopen(repaired).chapter(0).translated_title == REFUSAL

    def test_a_fresh_project_is_marked_without_work(self, library_dir, meta):
        project = _project(library_dir, meta, ["第1章 重生"])
        assert project._db.execute("PRAGMA user_version").fetchone()[0] == 1

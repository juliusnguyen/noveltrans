"""The shared `gui.rename_novel` flow (offscreen Qt).

Every dialog is monkeypatched. That is not a shortcut — a QMessageBox.exec() in a test
blocks the whole suite forever, which is exactly what happened while this feature was
being built when the Video tab's focus-out handler was routed through the asking branch.
The dialog's *decision* is what these tests are about; its pixels are not.
"""

from __future__ import annotations

import pytest

from noveltrans.gui import rename_novel as rn
from noveltrans.models import NovelMeta
from noveltrans.storage import NovelProject


@pytest.fixture
def project(library_dir, sample_meta, sample_refs):
    p = NovelProject.create(library_dir, sample_meta, sample_refs)
    p.save_meta_translation("Cứu Chuyện", "mô tả", "vi")
    yield p
    p.close()


def _rendered_part(project, stem: str) -> None:
    part = project.video_dir / stem
    part.mkdir(parents=True, exist_ok=True)
    (part / f"{stem}.mp4").write_bytes(b"mp4")
    (part / f"{stem}.upload.json").write_text('{"state": "published"}', encoding="utf-8")


def _choose(monkeypatch, move: bool):
    """Answer the consequences dialog without showing it."""
    monkeypatch.setattr(rn, "_ask", lambda parent, plan, busy: ("chosen", move))


def _cancel(monkeypatch):
    monkeypatch.setattr(rn, "_ask", lambda parent, plan, busy: (None, False))


class TestWithNothingRendered:
    def test_it_adopts_the_new_stem_without_asking(self, qapp, project, monkeypatch):
        # Nothing on disk carries the old stem, so there is no question to ask. If _ask
        # were reached this would raise.
        monkeypatch.setattr(rn, "_ask", lambda *a: pytest.fail("should not ask"))
        assert rn.rename_novel(None, project, "Trọng Sinh") is True
        assert project.meta.display_name() == "Trọng Sinh"
        assert project.meta.slug == "trong-sinh"

    def test_renaming_to_the_same_name_writes_nothing(self, qapp, project):
        project.rename_novel("Trọng Sinh", pin_slug="trong-sinh")
        assert rn.rename_novel(None, project, "Trọng Sinh") is False

    def test_a_name_that_slugs_the_same_keeps_the_stem(self, qapp, project, monkeypatch):
        monkeypatch.setattr(rn, "_ask", lambda *a: pytest.fail("should not ask"))
        rn.rename_novel(None, project, "cứu  chuyện")
        assert project.meta.slug == "cuu-chuyen"


class TestKeepingTheFiles:
    """The default branch, confirmed with the user."""

    def test_the_name_changes_and_the_stem_does_not(self, qapp, project, monkeypatch):
        _rendered_part(project, "cuu-chuyen-0001-0005")
        _choose(monkeypatch, move=False)
        assert rn.rename_novel(None, project, "Trọng Sinh") is True
        assert project.meta.display_name() == "Trọng Sinh"
        assert project.meta.slug == "cuu-chuyen"

    def test_the_rendered_part_and_its_upload_record_stay_put(
        self, qapp, project, monkeypatch
    ):
        """**The invariant.** A moved `.upload.json` re-publishes a live video."""
        _rendered_part(project, "cuu-chuyen-0001-0005")
        _choose(monkeypatch, move=False)
        rn.rename_novel(None, project, "Trọng Sinh")
        part = project.video_dir / "cuu-chuyen-0001-0005"
        assert (part / "cuu-chuyen-0001-0005.mp4").is_file()
        assert (part / "cuu-chuyen-0001-0005.upload.json").is_file()

    def test_the_pin_is_what_keeps_the_kept_files_findable(
        self, qapp, project, monkeypatch
    ):
        """Without the pin, a later re-translation would move the stem out from under the
        files this branch deliberately left alone."""
        _rendered_part(project, "cuu-chuyen-0001-0005")
        _choose(monkeypatch, move=False)
        rn.rename_novel(None, project, "Trọng Sinh")
        project.save_meta_translation("Something Else Entirely", "d", "en")
        assert project.meta.slug_name() == "cuu-chuyen"


class TestMovingTheFiles:
    def test_the_files_follow_and_the_stem_is_repointed(self, qapp, project, monkeypatch):
        _rendered_part(project, "cuu-chuyen-0001-0005")
        _choose(monkeypatch, move=True)
        monkeypatch.setattr(rn.QMessageBox, "information", lambda *a, **k: None)
        rn.rename_novel(None, project, "Trọng Sinh")
        assert project.meta.slug == "trong-sinh"
        moved = project.video_dir / "trong-sinh-0001-0005"
        assert (moved / "trong-sinh-0001-0005.mp4").is_file()
        assert (moved / "trong-sinh-0001-0005.upload.json").is_file()
        assert not (project.video_dir / "cuu-chuyen-0001-0005").exists()


class TestRefusals:
    def test_cancelling_writes_nothing(self, qapp, project, monkeypatch):
        _rendered_part(project, "cuu-chuyen-0001-0005")
        _cancel(monkeypatch)
        assert rn.rename_novel(None, project, "Trọng Sinh") is False
        assert project.meta.display_title == ""

    def test_a_collision_renames_the_display_only_and_warns(
        self, qapp, project, monkeypatch
    ):
        _rendered_part(project, "cuu-chuyen-0001-0005")
        _rendered_part(project, "trong-sinh-0001-0005")  # the destination already exists
        warned = []
        monkeypatch.setattr(
            rn.QMessageBox, "warning", lambda *a, **k: warned.append(a[2])
        )
        monkeypatch.setattr(rn, "_ask", lambda *a: pytest.fail("should not ask"))
        assert rn.rename_novel(None, project, "Trọng Sinh") is True
        assert warned and "trong-sinh-0001-0005" in warned[0]
        assert project.meta.slug == "cuu-chuyen"  # nothing moved
        assert project.meta.display_name() == "Trọng Sinh"

    def test_offer_migration_false_never_asks(self, qapp, project, monkeypatch):
        # The Video tab's "Tên hiển thị" box: it commits on focus-out, so it must never
        # put a modal question in the user's way.
        _rendered_part(project, "cuu-chuyen-0001-0005")
        monkeypatch.setattr(rn, "_ask", lambda *a: pytest.fail("should not ask"))
        assert rn.rename_novel(None, project, "Trọng Sinh", offer_migration=False) is True
        assert project.meta.slug == "cuu-chuyen"


class TestThePrompt:
    """What the user is told, asserted without building a modal box."""

    def _plan(self, project):
        from noveltrans.rename import plan_rename

        _rendered_part(project, "cuu-chuyen-0001-0005")
        return plan_rename(project.path, "cuu-chuyen", "trong-sinh")

    def test_a_busy_workspace_is_offered_no_move_button(self, qapp, project):
        text, move_label = rn.rename_prompt(self._plan(project), busy=True)
        assert move_label is None
        assert "Đang có việc chạy" in text

    def test_an_idle_workspace_is_offered_the_move_button(self, qapp, project):
        text, move_label = rn.rename_prompt(self._plan(project), busy=False)
        assert move_label and "file" in move_label
        assert "trong-sinh" in text

    def test_it_warns_that_published_titles_stay_old_on_youtube(self, qapp, project):
        text, _ = rn.rename_prompt(self._plan(project), busy=False)
        assert "ĐÃ ĐĂNG trên YouTube vẫn giữ" in text

    def test_it_says_the_covers_still_carry_the_old_name(self, qapp, project):
        text, _ = rn.rename_prompt(self._plan(project), busy=False)
        assert "Tạo lại tất cả ảnh bìa" in text

    def test_it_leads_with_what_keeping_the_files_still_gets_you(self, qapp, project):
        # The default branch has to read as sufficient, not as a compromise.
        text, _ = rn.rename_prompt(self._plan(project), busy=False)
        assert "Đổi tên hiển thị là đủ" in text


def test_the_scraped_titles_are_never_touched(qapp, library_dir, sample_refs):
    meta = NovelMeta(url="https://x.test/1", site="x", title="原标题")
    project = NovelProject.create(library_dir, meta, sample_refs)
    project.save_meta_translation("Tên đã dịch", "mô tả", "vi")
    rn.rename_novel(None, project, "Tên mới")
    assert project.meta.title == "原标题"
    assert project.meta.translated_title == "Tên đã dịch"
    project.close()

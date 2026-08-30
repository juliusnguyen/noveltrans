from noveltrans.gui.widgets import format_duration
from noveltrans.models import Chapter


class TestAudioChapterTableModel:
    def _model(self, qapp):
        from noveltrans.gui.widgets import AudioChapterTableModel

        model = AudioChapterTableModel()
        model.set_chapters(
            [
                Chapter(index=0, title="第1章", url="u"),  # not translated
                Chapter(index=1, title="第2章", url="u", content="x", translated="dịch",
                        translated_title="Chương 2"),  # translated, no audio
                Chapter(index=2, title="第3章", url="u", content="x", translated="dịch",
                        translated_title="Chương 3",
                        audio_path="exports/audio/0003-chuong-3.wav",
                        audio_voice="Ngọc Lan", audio_seconds=754.0),  # has audio
                Chapter(index=3, title="第4章", url="u", content="x", translated="dịch",
                        audio_error="hỏng"),  # audio error
            ]
        )
        return model

    def test_statuses_and_columns(self, qapp):
        from PySide6.QtCore import Qt

        m = self._model(qapp)
        col = m.STATUS_COLUMN
        statuses = [m.data(m.index(r, col)) for r in range(4)]
        assert statuses == ["Chưa dịch", "Chưa tạo", "Đã tạo", "Lỗi"]
        assert m.data(m.index(2, m.TITLE_COLUMN)) == "Chương 3"
        assert m.data(m.index(0, m.TITLE_COLUMN)) == "第1章"  # falls back to original
        assert m.data(m.index(2, m.DURATION_COLUMN)) == "12m34s"
        assert m.data(m.index(2, m.VOICE_COLUMN)) == "Ngọc Lan"
        assert m.data(m.index(3, m.ERROR_COLUMN)) == "hỏng"
        # regenerate button only for translated chapters
        assert m.data(m.index(0, m.REGENERATE_COLUMN), Qt.ItemDataRole.UserRole) is False
        assert m.data(m.index(1, m.REGENERATE_COLUMN), Qt.ItemDataRole.UserRole) is True

    def test_original_source_status_and_title(self, qapp):
        from PySide6.QtCore import Qt

        m = self._model(qapp)
        m.set_source(use_translation=False)  # Bản gốc
        col = m.STATUS_COLUMN
        # row 0 has no content → "Chưa tải" (not "Chưa dịch"); rows 1-2 have content
        assert m.data(m.index(0, col)) == "Chưa tải"
        assert m.data(m.index(1, col)) == "Chưa tạo"
        assert m.data(m.index(2, col)) == "Đã tạo"
        # title column shows the original title, not the translated one
        assert m.data(m.index(2, m.TITLE_COLUMN)) == "第3章"
        # regenerate button follows content availability now
        assert m.data(m.index(0, m.REGENERATE_COLUMN), Qt.ItemDataRole.UserRole) is False
        assert m.data(m.index(1, m.REGENERATE_COLUMN), Qt.ItemDataRole.UserRole) is True


class TestChapterTableModelEditing:
    def _model(self, qapp):
        from noveltrans.gui.widgets import ChapterTableModel

        model = ChapterTableModel()
        model.set_chapters(
            [
                Chapter(index=0, title="第1章", url="u", content="x"),  # not translated
                Chapter(index=1, title="第2章", url="u", content="x", translated="dịch",
                        translated_title="Chương 2"),
            ]
        )
        return model

    def test_only_translated_titles_are_editable(self, qapp):
        from PySide6.QtCore import Qt

        m = self._model(qapp)
        col = m.TRANSLATED_TITLE_COLUMN
        assert not (m.flags(m.index(0, col)) & Qt.ItemFlag.ItemIsEditable)
        assert m.flags(m.index(1, col)) & Qt.ItemFlag.ItemIsEditable
        # other columns stay read-only
        assert not (m.flags(m.index(1, m.TITLE_COLUMN)) & Qt.ItemFlag.ItemIsEditable)

    def test_set_data_updates_and_emits(self, qapp):
        m = self._model(qapp)
        edits = []
        m.translated_title_edited.connect(lambda idx, title: edits.append((idx, title)))
        index = m.index(1, m.TRANSLATED_TITLE_COLUMN)
        assert m.setData(index, "  Chương Hai  ")
        assert m.data(index) == "Chương Hai"  # stored trimmed
        assert edits == [(1, "Chương Hai")]

    def test_set_data_rejects_empty_and_unchanged(self, qapp):
        m = self._model(qapp)
        edits = []
        m.translated_title_edited.connect(lambda idx, title: edits.append((idx, title)))
        index = m.index(1, m.TRANSLATED_TITLE_COLUMN)
        assert not m.setData(index, "   ")
        assert not m.setData(index, "Chương 2")  # same as current value
        assert m.data(index) == "Chương 2"
        assert edits == []


class TestChapterTableModelRewriteMarker:
    """Feature 060 — "đã viết lại" is shown as a suffix, not a new column."""

    def _model(self, qapp):
        from noveltrans.gui.widgets import ChapterTableModel

        model = ChapterTableModel()
        model.set_chapters(
            [
                Chapter(index=0, title="第1章", url="u", content="x", translated="dịch",
                        translator="Google Translate"),  # translated, not rewritten
                Chapter(index=1, title="第2章", url="u", content="x", translated="hay hơn",
                        translator="Google Translate", translated_raw="dịch"),  # rewritten
            ]
        )
        return model

    def test_the_marker_appears_only_on_a_rewritten_chapter(self, qapp):
        m = self._model(qapp)
        col = m.TRANSLATOR_COLUMN
        assert m.data(m.index(0, col)) == "Google Translate"
        assert m.data(m.index(1, col)) == "Google Translate ✍️"

    def test_a_rewritten_chapter_with_no_engine_recorded_has_no_stray_space(self, qapp):
        from noveltrans.gui.widgets import ChapterTableModel

        model = ChapterTableModel()
        model.set_chapters(
            [Chapter(index=0, title="t", url="u", translated="x", translated_raw="y")]
        )
        assert model.data(model.index(0, model.TRANSLATOR_COLUMN)) == "✍️"

    def test_the_marker_carries_a_tooltip_pointing_at_the_undo(self, qapp):
        from PySide6.QtCore import Qt

        m = self._model(qapp)
        col = m.TRANSLATOR_COLUMN
        assert m.data(m.index(0, col), Qt.ItemDataRole.ToolTipRole) is None
        tip = m.data(m.index(1, col), Qt.ItemDataRole.ToolTipRole)
        assert "hoàn tác" in tip

    def test_no_column_was_added(self, qapp):
        # A ninth column would shift RETRANSLATE_COLUMN and every index that follows it
        # across tab_translate.py. The suffix exists precisely to avoid that.
        m = self._model(qapp)
        assert len(m.COLUMNS) == 8
        assert m.RETRANSLATE_COLUMN == 7


class TestDefaultExportName:
    def _meta(self, **overrides):
        from noveltrans.models import NovelMeta

        return NovelMeta(
            url="https://example.com/n/1", site="example", title="斗破苍穹", **overrides
        )

    def test_prefers_translated_title(self, qapp):
        from noveltrans.gui.tab_export import default_export_name

        meta = self._meta(translated_title="Đấu Phá Thương Khung")
        assert default_export_name(meta, True, ".epub") == "dau-pha-thuong-khung.epub"

    def test_original_export_keeps_original_title(self, qapp):
        from noveltrans.gui.tab_export import default_export_name

        meta = self._meta(translated_title="Đấu Phá Thương Khung")
        assert default_export_name(meta, False, ".epub") == "novel.epub"  # CJK slug fallback

    def test_untranslated_falls_back(self, qapp):
        from noveltrans.gui.tab_export import default_export_name

        assert default_export_name(self._meta(), True, ".docx") == "novel.docx"


class TestFormatDuration:
    def test_unset_is_blank(self):
        assert format_duration(0) == ""
        assert format_duration(-1) == ""
        assert format_duration(0.4) == ""  # rounds to 0

    def test_seconds(self):
        assert format_duration(42) == "42s"
        assert format_duration(59.6) == "1m00s"  # rounds up past a minute

    def test_minutes(self):
        assert format_duration(65) == "1m05s"
        assert format_duration(104) == "1m44s"

    def test_hours(self):
        assert format_duration(3725) == "1h02m"


class TestCheckableHeaderView:
    """The "toggle every row" indicator in a table header.

    QHeaderView has no checkable section, so both the indicator's geometry and the
    click-target logic are ours and can regress silently — the painting can't be
    asserted, but where the indicator sits and what counts as a click on it can.
    """

    def _header(self, qapp, column=5, sections=7):
        from PySide6.QtWidgets import QTableWidget

        from noveltrans.gui.widgets import CheckableHeaderView

        table = QTableWidget(0, sections)
        header = CheckableHeaderView(column, table)
        table.setHorizontalHeader(header)
        table.resize(900, 100)
        return table, header

    def test_starts_unchecked(self, qapp):
        from PySide6.QtCore import Qt

        _table, header = self._header(qapp)
        assert header.check_state() == Qt.CheckState.Unchecked

    def test_set_state_round_trips(self, qapp):
        from PySide6.QtCore import Qt

        _table, header = self._header(qapp)
        for state in (
            Qt.CheckState.Checked,
            Qt.CheckState.PartiallyChecked,
            Qt.CheckState.Unchecked,
        ):
            header.set_state(state)
            assert header.check_state() == state

    def test_indicator_sits_inside_its_own_section(self, qapp):
        """A stray indicator would be unclickable, or would hijack the wrong column."""
        from PySide6.QtCore import QRect

        _table, header = self._header(qapp)
        section = QRect(header.sectionViewportPosition(5), 0, header.sectionSize(5), 20)
        indicator = header._indicator_rect(section)
        assert section.contains(indicator)
        assert indicator.width() > 0

    def _click(self, header, x):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        got = []
        header.toggled.connect(got.append)
        point = QPointF(x, header.height() / 2)
        event = QMouseEvent(  # local + global positions: the 5-arg form is deprecated
            QEvent.Type.MouseButtonPress,
            point,
            point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        header.mousePressEvent(event)
        return got

    def test_clicking_the_indicator_asks_to_check_all(self, qapp):
        _table, header = self._header(qapp)
        x = header.sectionViewportPosition(5) + 6
        assert self._click(header, x) == [True]

    def test_clicking_the_indicator_when_all_checked_asks_to_uncheck(self, qapp):
        from PySide6.QtCore import Qt

        _table, header = self._header(qapp)
        header.set_state(Qt.CheckState.Checked)
        x = header.sectionViewportPosition(5) + 6
        assert self._click(header, x) == [False]

    def test_partially_checked_asks_to_check_all(self, qapp):
        """"Some" reads as "not all", so the useful next step is to finish the job."""
        from PySide6.QtCore import Qt

        _table, header = self._header(qapp)
        header.set_state(Qt.CheckState.PartiallyChecked)
        x = header.sectionViewportPosition(5) + 6
        assert self._click(header, x) == [True]

    def test_clicking_another_column_does_not_toggle(self, qapp):
        """Only the indicator counts — toggling every row is too big for a stray click."""
        _table, header = self._header(qapp)
        x = header.sectionViewportPosition(1) + 6
        assert self._click(header, x) == []


class TestChapterTableModelHelpers:
    def _model(self, qapp):
        from noveltrans.gui.widgets import ChapterTableModel

        model = ChapterTableModel()
        model.set_chapters(
            [
                Chapter(index=0, title="Chương 1", url="", content="Nội dung.",
                        status="downloaded"),
                # a chapter added after a delete: index 2 sits at row 1
                Chapter(index=2, title="Chương 3", url="", status="pending"),
            ]
        )
        return model

    def test_row_for_index_is_not_the_identity_across_a_gap(self, qapp):
        m = self._model(qapp)
        assert m.row_for_index(0) == 0
        assert m.row_for_index(2) == 1
        assert m.row_for_index(1) is None  # deleted

    def test_hand_typed_content_reads_as_downloaded(self, qapp):
        # The status column is the one place that reads `status` rather than deriving it
        # from `content`, so a chapter whose text arrived via edit_content must not still
        # say "Chưa tải" while its content is plainly on screen.
        m = self._model(qapp)
        col = m.STATUS_COLUMN
        assert m.data(m.index(0, col)) == "Đã tải"
        assert m.data(m.index(1, col)) == "Chưa tải"


class TestPauseButton:
    """One button class backs both the tab row and the popup row — see 049."""

    def _setup(self, qapp):
        from PySide6.QtCore import QObject, Signal

        from noveltrans.gui.jobs import JobRegistry
        from noveltrans.gui.widgets import PauseButton

        class _Worker(QObject):
            progress = Signal(int, int, str)
            finished = Signal()

            def __init__(self):
                super().__init__()
                self.paused = False

            def isFinished(self):
                return False

            def pause(self):
                self.paused = True

            def resume(self):
                self.paused = False

        registry = JobRegistry()
        worker = _Worker()
        job = registry.register(worker, kind="Dịch", novel="Truyện A")
        return registry, worker, job, PauseButton(job.id, registry=registry)

    def test_it_starts_as_pause_and_enabled(self, qapp):
        _registry, _worker, _job, button = self._setup(qapp)
        assert button.text() == "⏸ Tạm dừng"
        assert button.isEnabled()

    def test_clicking_pauses_the_worker_and_flips_the_label(self, qapp):
        _registry, worker, _job, button = self._setup(qapp)
        button.click()
        assert worker.paused
        assert button.text() == "▶ Tiếp tục"
        assert "Chạy tiếp" in button.toolTip()

    def test_two_buttons_on_one_job_stay_in_step(self, qapp):
        # The tab's button and the popup's are separate widgets on the same Job; neither
        # owns state, so pressing one must relabel the other.
        from noveltrans.gui.widgets import PauseButton

        registry, worker, job, first = self._setup(qapp)
        second = PauseButton(job.id, registry=registry)
        first.click()
        assert worker.paused
        assert second.text() == "▶ Tiếp tục"
        second.click()
        assert not worker.paused
        assert first.text() == "⏸ Tạm dừng"

    def test_it_parks_itself_when_the_job_finishes(self, qapp):
        _registry, worker, _job, button = self._setup(qapp)
        worker.finished.emit()
        assert not button.isEnabled()
        assert button.text() == "⏸ Tạm dừng"

    def test_an_unbound_button_is_disabled_and_inert(self, qapp):
        from noveltrans.gui.widgets import PauseButton

        button = PauseButton()
        assert not button.isEnabled()
        button.click()  # must not raise

    def test_set_job_none_parks_it(self, qapp):
        _registry, _worker, _job, button = self._setup(qapp)
        button.set_job(None)
        assert not button.isEnabled()


class TestProjectPickerLabel:
    """The picker row: "原文  —  Bản dịch — site.com".

    The point of showing both titles is that the two audiences differ: the original is how
    the novel is recognised on the source site, the translation is how the user thinks
    about it. Each half is optional, and the failure that matters is a row that shows a
    dangling separator (or a UUID) when a half is missing.
    """

    def _meta(self, **kw):
        from noveltrans.models import NovelMeta

        base = dict(
            url="https://twkan.com/book/114283/index.html",
            site="twkan",
            title="穿書反派",
        )
        base.update(kw)
        return NovelMeta(**base)

    def test_shows_both_titles_and_the_site(self):
        from noveltrans.gui.widgets import _picker_label

        label = _picker_label(self._meta(translated_title="Xuyên thư thành phản diện"))
        assert label == "Xuyên thư thành phản diện — 穿書反派 — twkan.com"

    def test_an_untranslated_novel_has_no_dangling_separator(self):
        # Before the first translation run there is no Vietnamese title at all.
        from noveltrans.gui.widgets import _picker_label

        assert _picker_label(self._meta()) == "穿書反派 — twkan.com"

    def test_a_whitespace_only_translation_counts_as_none(self):
        from noveltrans.gui.widgets import _picker_label

        assert _picker_label(self._meta(translated_title="   ")) == "穿書反派 — twkan.com"

    def test_the_site_is_the_domain_not_the_adapter_name(self):
        # meta.site is "twkan"; what reads naturally in a list is what the user pasted.
        from noveltrans.gui.widgets import _picker_label

        assert _picker_label(self._meta()).endswith(" — twkan.com")

    def test_www_is_dropped_so_one_site_has_one_spelling(self):
        from noveltrans.gui.widgets import _picker_label

        label = _picker_label(
            self._meta(url="https://www.69shuba.com/book/59024/", site="69shuba")
        )
        assert label == "穿書反派 — 69shuba.com"

    def test_a_local_novel_shows_no_site_suffix(self):
        # Its URL is a synthetic local://<uuid>; the netloc is a UUID, and printing that
        # would be worse than printing nothing.
        from noveltrans.gui.widgets import _picker_label
        from noveltrans.models import new_local_url

        label = _picker_label(
            self._meta(url=new_local_url(), site="local", title="Truyện tự soạn")
        )
        assert label == "Truyện tự soạn"

    def test_the_scrape_tab_header_uses_the_same_naming_minus_the_site(self):
        # ScrapeTab._show_meta and the picker share one helper, so they cannot drift
        # apart. The header drops the site: it sits under the URL box the novel came
        # from, so the domain is already on screen.
        meta = self._meta(translated_title="Xuyên thư thành phản diện")
        assert meta.novel_label(with_source=False) == "Xuyên thư thành phản diện — 穿書反派"

    def test_the_translation_comes_first(self):
        """Feature 068 reversed this. A tab column shows ~20 characters, and spending them
        on text the user does not think in buried the half that identifies the novel."""
        meta = self._meta(translated_title="Xuyên thư thành phản diện")
        assert meta.novel_label().startswith("Xuyên thư thành phản diện")

    def test_a_display_title_override_wins_the_first_slot(self):
        """`display_title` is the user's own name for the novel (feature 025) and exists
        to drop tags like "[ĐM/EDIT] " — so it beats the raw machine translation here."""
        meta = self._meta(
            translated_title="[ĐM/EDIT] Xuyên thư thành phản diện",
            display_title="Xuyên Thư Phản Diện",
        )
        assert meta.novel_label() == "Xuyên Thư Phản Diện — 穿書反派 — twkan.com"

    def test_a_whitespace_only_display_title_falls_through(self):
        meta = self._meta(
            translated_title="Xuyên thư thành phản diện", display_title="   "
        )
        assert meta.novel_label().startswith("Xuyên thư thành phản diện")

    def test_a_local_untranslated_novel_is_just_its_title(self):
        """Every part optional: nothing to translate, no site — no stray separators."""
        from noveltrans.models import new_local_url

        meta = self._meta(url=new_local_url(), site="local", title="Truyện tự soạn")
        assert meta.novel_label() == "Truyện tự soạn"


class TestProjectPickerOrdering:
    """The picker lists novels by what it SHOWS, not by folder name (074).

    `Library.list_projects` sorts on the folder, which is `slugify(meta.title)-<hash>` —
    the ORIGINAL title — while every row shows the translation first. So the dropdown
    looked unsorted: the only key the user could see was never the key it was ordered by.
    """

    def _library(self, tmp_path):
        from noveltrans.models import ChapterRef, NovelMeta
        from noveltrans.storage import NovelProject

        root = tmp_path / "library"
        root.mkdir()
        # Folder order (by original title's slug) is the reverse of label order.
        for original, translated in (
            ("Zhu Ben", "Ánh Sáng"),
            ("Ai Ben", "Zô Cuối"),
            ("Mo Ben", "Muôn Trùng"),
        ):
            meta = NovelMeta(url=f"https://x/{original}", site="x", title=original)
            project = NovelProject.create(
                root, meta, [ChapterRef(index=0, title="C1", url="https://x/1")]
            )
            project.save_meta_translation(translated, "mô tả", "vi")
            project.close()
        return root

    def test_rows_are_ordered_by_their_label(self, qapp, tmp_path):
        from noveltrans.gui.widgets import ProjectPicker

        picker = ProjectPicker()
        picker.refresh(self._library(tmp_path), default_to_first=False)
        from noveltrans.gui.widgets import _fold

        labels = [picker.itemText(i) for i in range(picker.count())]
        # Folded, not raw: "Ánh" sorts after "Z" by codepoint, which is the very thing
        # _fold exists to prevent.
        assert labels == sorted(labels, key=_fold)
        assert labels[0].startswith("Ánh Sáng")  # not "Ai Ben", whose folder sorts first
        assert labels[-1].startswith("Zô Cuối")

    def test_diacritics_file_beside_their_base_letter(self, qapp, tmp_path):
        # "Ánh" belongs next to "A", not after "Z".
        from noveltrans.gui.widgets import _fold

        assert _fold("Ánh Sáng") < _fold("Bản")
        assert _fold("Đấu La") < _fold("Muôn")

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

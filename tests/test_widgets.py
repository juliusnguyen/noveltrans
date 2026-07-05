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

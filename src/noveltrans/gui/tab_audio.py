"""Tab 4 — Audio: read chapters aloud (translation or original) with a local TTS engine."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.gui.widgets import (
    AudioChapterTableModel,
    ProjectPicker,
    RowButtonDelegate,
    enable_cell_copy,
)
from noveltrans.gui.workers import AudioWorker, TtsVoicesWorker
from noveltrans.storage import NovelProject


class AudioTab(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._worker: AudioWorker | None = None
        self._voices_worker: TtsVoicesWorker | None = None

        # --- top row: novel + voice
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._on_project_selected)

        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(240)
        self.voice_combo.setToolTip("Giọng đọc VieNeu-TTS.")
        self._load_voices()

        from noveltrans.tts.convert import ffmpeg_available

        self.format_combo = QComboBox()
        self.format_combo.addItem("MP3 (gọn, ~1 MB/phút)", "mp3")
        self.format_combo.addItem("WAV (48 kHz, ~6 MB/phút)", "wav")
        if not ffmpeg_available():
            index = self.format_combo.findData("mp3")
            self.format_combo.model().item(index).setEnabled(False)
            self.format_combo.setCurrentIndex(self.format_combo.findData("wav"))
            self.format_combo.setToolTip("MP3 cần ffmpeg (brew install ffmpeg).")
        else:
            saved = self.format_combo.findData(config.tts_format)
            self.format_combo.setCurrentIndex(saved if saved >= 0 else 0)

        # --- source: read the translation (default) or the original text
        self.translated_radio = QRadioButton("Bản dịch")
        self.original_radio = QRadioButton("Bản gốc")
        self._source_group = QButtonGroup(self)
        self._source_group.addButton(self.translated_radio)
        self._source_group.addButton(self.original_radio)
        (self.translated_radio if config.tts_use_translation else self.original_radio).setChecked(
            True
        )
        self.original_radio.setToolTip(
            "Đọc thẳng nội dung gốc (chưa dịch). Hợp với nguồn tiếng Việt như "
            "medoctruyen.vn / giatocvuongtai.com — giọng VieNeu là tiếng Việt."
        )
        self._warned_original_lang = False  # toggled connected after the model exists

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Truyện:"))
        top_row.addWidget(self.picker, stretch=1)
        top_row.addWidget(QLabel("Nguồn:"))
        top_row.addWidget(self.translated_radio)
        top_row.addWidget(self.original_radio)
        top_row.addWidget(QLabel("Giọng đọc:"))
        top_row.addWidget(self.voice_combo)
        top_row.addWidget(QLabel("Định dạng:"))
        top_row.addWidget(self.format_combo)

        # --- chapter table
        self.model = AudioChapterTableModel(self)
        self.model.set_source(config.tts_use_translation)
        # connect now that the model exists (setChecked above ran before this)
        self.translated_radio.toggled.connect(self._on_source_changed)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(
            AudioChapterTableModel.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        enable_cell_copy(self.table)  # Ctrl+C / right-click to copy a cell (e.g. errors)
        self.table.setMouseTracking(True)
        self._row_button_delegate = RowButtonDelegate("🔊 Tạo lại", self.table)
        self._row_button_delegate.clicked.connect(self._regenerate_row)
        self.table.setItemDelegateForColumn(
            AudioChapterTableModel.REGENERATE_COLUMN, self._row_button_delegate
        )
        self.table.setColumnWidth(AudioChapterTableModel.REGENERATE_COLUMN, 100)
        self.table.doubleClicked.connect(self._on_row_double_clicked)

        # --- bottom row
        self.generate_button = QPushButton("Tạo audio tất cả")
        self.generate_button.setProperty("primary", True)
        self.generate_button.clicked.connect(lambda: self._start_generate())
        self.regenerate_button = QPushButton("Tạo lại từ đầu")
        self.regenerate_button.setToolTip(
            "Xoá trạng thái audio hiện có rồi tạo lại toàn bộ (dùng khi đổi giọng đọc)."
        )
        self.regenerate_button.clicked.connect(self._regenerate_all)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.open_dir_button = QPushButton("Mở thư mục audio")
        self.open_dir_button.clicked.connect(self._open_audio_dir)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m chương")
        self.status_label = QLabel("")
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.generate_button)
        bottom_row.addWidget(self.regenerate_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addWidget(self.open_dir_button)
        bottom_row.addWidget(self.progress, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(bottom_row)
        layout.addWidget(self.status_label)

    # -------------------------------------------------------------- projects

    def refresh_projects(self, select_path: str = "") -> None:
        self.picker.refresh(self.config.library_dir, select_path)

    def showEvent(self, event) -> None:
        if self._worker is None or not self._worker.isRunning():
            self.refresh_projects()
        super().showEvent(event)

    def _on_project_selected(self, path: str) -> None:
        if self.project is not None:
            self.project.close()
            self.project = None
        if path:
            self.project = NovelProject.open(path)
            self.model.set_chapters(self.project.chapters())
            self._update_status_line()
        else:
            self.model.set_chapters([])
            self.status_label.setText("")

    def _use_translation(self) -> bool:
        return self.translated_radio.isChecked()

    def _update_status_line(self) -> None:
        if self.project is None:
            return
        counts = self.project.counts()
        if self._use_translation():
            ready = f"{counts['translated']}/{counts['total']} chương đã dịch"
        else:
            ready = f"{counts['downloaded']}/{counts['total']} chương đã tải"
        self.status_label.setText(f"{ready}, {counts['audio']} đã có audio.")

    def _on_source_changed(self) -> None:
        use_translation = self._use_translation()
        self.config.tts_use_translation = use_translation
        self.model.set_source(use_translation)
        self._update_status_line()
        # VieNeu is a Vietnamese TTS: warn once if voicing a non-Vietnamese original
        if (
            not use_translation
            and not self._warned_original_lang
            and self.project is not None
            and (self.project.meta.source_lang or "") != "vi"
        ):
            self._warned_original_lang = True
            QMessageBox.warning(
                self,
                "Bản gốc không phải tiếng Việt",
                "Giọng đọc VieNeu là tiếng Việt, nhưng bản gốc của truyện này không "
                "phải tiếng Việt — audio tạo ra có thể không đúng. Nên dùng “Bản dịch”.",
            )

    # ---------------------------------------------------------------- voices

    def _load_voices(self) -> None:
        self._voices_worker = TtsVoicesWorker()
        self._voices_worker.voices_listed.connect(self._on_voices_listed)
        self._voices_worker.start()

    def _on_voices_listed(self, voices: list) -> None:
        saved = self.config.tts_voice
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for label, voice_id in voices:
            self.voice_combo.addItem(label, voice_id)
        index = self.voice_combo.findData(saved)
        self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        self.voice_combo.blockSignals(False)

    # -------------------------------------------------------------- generate

    def _start_generate(self, indices: list[int] | None = None) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        use_translation = self._use_translation()
        counts = self.project.counts()
        if use_translation and counts["translated"] == 0:
            QMessageBox.information(self, "Chưa có bản dịch", "Hãy dịch truyện ở Tab 2 trước.")
            return
        if not use_translation and counts["downloaded"] == 0:
            QMessageBox.information(self, "Chưa tải", "Hãy tải truyện ở Tab 1 trước.")
            return
        voice = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
        out_format = self.format_combo.currentData()
        if indices is None:
            # a voice or source change re-pends chapters voiced differently
            total = len(self.project.pending_audio(voice, use_translation))
            if total == 0:
                nguon = "bản dịch" if use_translation else "bản gốc"
                QMessageBox.information(
                    self, "Đã đủ", f"Mọi chương ({nguon}) đều có audio giọng {voice}."
                )
                return
        else:
            total = len(indices)

        self.config.tts_voice = voice
        self.config.tts_format = out_format

        self.generate_button.setEnabled(False)
        self.regenerate_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.picker.setEnabled(False)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(0)

        self._worker = AudioWorker(
            self.project.path,
            voice=voice,
            out_format=out_format,
            indices=indices,
            use_translation=use_translation,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.chapter_done.connect(self._on_chapter_updated)
        self._worker.chapter_error.connect(lambda idx, _msg: self._on_chapter_updated(idx))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.start()

    def _regenerate_row(self, row: int) -> None:
        chapter = self.model.chapter_at(row)
        if chapter is None or self.project is None:
            return
        if not (chapter.translated if self._use_translation() else chapter.content):
            return
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("Đang có phiên tạo audio chạy — chờ xong rồi thử lại.")
            return
        self._start_generate(indices=[chapter.index])

    def _regenerate_all(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        generated = self.project.counts()["audio"]
        if generated:
            answer = QMessageBox.question(
                self,
                "Tạo lại từ đầu?",
                f"Sẽ tạo lại audio cho toàn bộ {generated} chương đã có. Tiếp tục chứ?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.project.clear_audio()
        self.model.set_chapters(self.project.chapters())
        self._start_generate()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("Đang dừng…")

    # --------------------------------------------------------------- helpers

    def _on_row_double_clicked(self, index) -> None:
        chapter = self.model.chapter_at(index.row())
        if chapter is None or self.project is None or not chapter.has_audio:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.path / chapter.audio_path)))

    def _open_audio_dir(self) -> None:
        if self.project is None:
            return
        self.project.audio_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.audio_dir)))

    def _on_progress(self, done: int, total: int, title: str) -> None:
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        if title:
            self.status_label.setText(f"Đang đọc: {title}")

    def _on_chapter_updated(self, idx: int) -> None:
        if self.project is None:
            return
        chapter = self.project.chapter(idx)
        if chapter is not None:
            self.model.update_chapter(chapter)

    def _on_failed(self, message: str) -> None:
        self._reset_buttons()
        QMessageBox.warning(self, "Không tạo được audio", message)

    def _on_finished(self, ok: int, errors: int) -> None:
        self._reset_buttons()
        message = f"Xong: {ok} chương có audio"
        if errors:
            message += f", {errors} lỗi (bấm 'Tạo audio tất cả' để thử lại)"
        self.status_label.setText(message + ".")

    def _reset_buttons(self) -> None:
        self.generate_button.setEnabled(True)
        self.regenerate_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.picker.setEnabled(True)

    def has_running_workers(self) -> bool:
        # Only TTS generation is user-meaningful work worth a close-confirm; the
        # voices-list fetch is a short background metadata call (shutdown still joins it).
        return self._worker is not None and self._worker.isRunning()

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(120_000)  # a mid-chapter chunk may take a while
        if self._voices_worker is not None and self._voices_worker.isRunning():
            self._voices_worker.wait(5_000)

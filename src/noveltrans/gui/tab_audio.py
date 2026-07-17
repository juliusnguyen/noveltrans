"""Tab 4 — Audio: read chapters aloud (translation or original) with a local TTS engine."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import (
    AudioChapterTableModel,
    ProjectPicker,
    RowButtonDelegate,
    enable_cell_copy,
)
from noveltrans.gui.workers import AudioWorker, MergeWorker, TtsVoicesWorker
from noveltrans.storage import NovelProject


class AudioTab(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._worker: AudioWorker | None = None
        self._voices_worker: TtsVoicesWorker | None = None
        self._merge_worker: MergeWorker | None = None

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
        self.preview_button = QPushButton("Xem trước văn bản")
        self.preview_button.setToolTip(
            "Xem văn bản của chương đang chọn đúng như engine sẽ đọc "
            "(đã làm sạch ký tự đặc biệt nếu bật trong Cài đặt)."
        )
        self.preview_button.clicked.connect(self._preview_text)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m chương")
        self.status_label = QLabel("")
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.generate_button)
        bottom_row.addWidget(self.regenerate_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addWidget(self.open_dir_button)
        bottom_row.addWidget(self.preview_button)
        bottom_row.addWidget(self.progress, stretch=1)

        merge_box = self._build_merge_box()

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(bottom_row)
        layout.addWidget(merge_box)
        layout.addWidget(self.status_label)

    def _build_merge_box(self) -> QGroupBox:
        """The 'Ghép audio' controls: mode (all/range/batch), format, and the button."""
        from noveltrans.tts.convert import ffmpeg_available, ffmpeg_has_encoder

        self.merge_mode = QComboBox()
        self.merge_mode.addItem("Toàn bộ", "all")
        self.merge_mode.addItem("Từ chương … đến …", "range")
        self.merge_mode.addItem("Theo lô", "batch")
        self.merge_mode.currentIndexChanged.connect(self._on_merge_mode_changed)

        self.range_from = QSpinBox()
        self.range_from.setMinimum(1)
        self.range_from.setMaximum(999999)
        self.range_to = QSpinBox()
        self.range_to.setMinimum(1)
        self.range_to.setMaximum(999999)
        self.range_label = QLabel("→")
        self.batch_size = QSpinBox()
        self.batch_size.setMinimum(1)
        self.batch_size.setMaximum(999999)
        self.batch_size.setValue(10)
        self.batch_label = QLabel("chương/lô")

        self.merge_format = QComboBox()
        has_aac = ffmpeg_has_encoder("aac")
        if has_aac:
            self.merge_format.addItem("M4B (có mục lục chương)", "m4b")
        self.merge_format.addItem("MP3 (gộp phẳng)", "mp3")

        self.merge_button = QPushButton("Ghép audio")
        self.merge_button.clicked.connect(self._start_merge)
        if not ffmpeg_available():
            self.merge_button.setEnabled(False)
            self.merge_button.setToolTip("Cần ffmpeg để ghép audio (brew install ffmpeg).")

        row = QHBoxLayout()
        row.addWidget(QLabel("Chế độ:"))
        row.addWidget(self.merge_mode)
        row.addWidget(self.range_from)
        row.addWidget(self.range_label)
        row.addWidget(self.range_to)
        row.addWidget(self.batch_size)
        row.addWidget(self.batch_label)
        row.addWidget(QLabel("Định dạng:"))
        row.addWidget(self.merge_format)
        row.addWidget(self.merge_button)
        row.addStretch()

        box = QGroupBox("Ghép thành 1 file")
        box.setLayout(row)
        self._on_merge_mode_changed()  # set initial visibility
        return box

    def _on_merge_mode_changed(self) -> None:
        mode = self.merge_mode.currentData()
        is_range = mode == "range"
        is_batch = mode == "batch"
        for w in (self.range_from, self.range_label, self.range_to):
            w.setVisible(is_range)
        for w in (self.batch_size, self.batch_label):
            w.setVisible(is_batch)

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
            total = self.project.counts()["total"]
            self.range_to.setValue(max(total, 1))  # default merge range = whole novel
            self.range_from.setValue(1)
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
            workers=self.config.tts_workers,
            clean_text=self.config.tts_clean_text,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.chapter_done.connect(self._on_chapter_updated)
        self._worker.chapter_error.connect(lambda idx, _msg: self._on_chapter_updated(idx))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_ok.connect(self._on_finished)
        track_worker(self._worker)  # keep the Mac awake while generating audio
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
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.status_label.setText("Đang dừng…")
        if self._merge_worker is not None and self._merge_worker.isRunning():
            self._merge_worker.cancel()
            self.status_label.setText("Đang dừng ghép…")

    # --------------------------------------------------------------- helpers

    def _on_row_double_clicked(self, index) -> None:
        chapter = self.model.chapter_at(index.row())
        if chapter is None or self.project is None or not chapter.has_audio:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.path / chapter.audio_path)))

    def _engine_text_for(self, chapter) -> tuple[str, str, bool]:
        """The (title, text, cleaned) a chapter would be synthesized as, right now.

        Mirrors AudioWorker._title_text_for + synthesize_chapter so the preview shows
        exactly what the engine will receive — same source (translated vs original),
        same title+body join, same cleaning toggle.
        """
        from noveltrans.tts.clean import clean_for_tts

        if self.config.tts_use_translation:
            title = chapter.translated_title or chapter.title
            body = chapter.translated
        else:
            title, body = chapter.title, chapter.content
        text = f"{title}\n\n{body}" if title else body
        cleaned = self.config.tts_clean_text
        if cleaned:
            text = clean_for_tts(text)
        return title, text, cleaned

    def _preview_text(self) -> None:
        """Show the selected chapter's text exactly as the engine will receive it."""
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        index = self.table.currentIndex()
        chapter = self.model.chapter_at(index.row()) if index.isValid() else None
        if chapter is None:
            QMessageBox.information(
                self, "Chưa chọn chương", "Hãy chọn một chương trong bảng để xem trước."
            )
            return

        title, text, cleaned = self._engine_text_for(chapter)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Xem trước — {title}")
        dialog.setMinimumSize(560, 480)
        note = QLabel(
            "✓ Đã làm sạch ký tự đặc biệt (như engine sẽ đọc)."
            if cleaned
            else "⚠️ Chưa làm sạch — bật trong Cài đặt để bỏ ký tự đặc biệt."
        )
        note.setWordWrap(True)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text or "(trống)")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout = QVBoxLayout(dialog)
        layout.addWidget(note)
        layout.addWidget(view, stretch=1)
        layout.addWidget(buttons)
        dialog.exec()

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

    # ------------------------------------------------------------------ merge

    def _start_merge(self) -> None:
        from noveltrans.tts.merge import plan_merge_windows

        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        if self._merge_worker is not None and self._merge_worker.isRunning():
            return
        voice = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
        mode = self.merge_mode.currentData()
        start = self.range_from.value() if mode == "range" else None
        end = self.range_to.value() if mode == "range" else None
        batch = self.batch_size.value() if mode == "batch" else None
        if mode == "range" and start > end:
            QMessageBox.warning(self, "Phạm vi sai", "Chương bắt đầu phải ≤ chương kết thúc.")
            return

        # cheap preview (no ffmpeg) so we can show the file/chapter count before a long run
        windows = plan_merge_windows(
            self.project.chapters(), voice, mode, start=start, end=end, batch=batch
        )
        if not windows:
            QMessageBox.information(
                self,
                "Chưa có audio",
                f"Không có chương nào có audio giọng {voice} trong phạm vi đã chọn.",
            )
            return
        n_chapters = sum(len(w.chapters) for w in windows)
        answer = QMessageBox.question(
            self,
            "Ghép audio",
            f"Sẽ tạo {len(windows)} file từ {n_chapters} chương (giọng {voice}). Tiếp tục?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.merge_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.cancel_button.setEnabled(True)  # let the user stop a long merge
        self.progress.setMaximum(len(windows))
        self.progress.setValue(0)
        self.status_label.setText(f"🔗 Đang ghép audio… ({len(windows)} file, có thể mất vài phút)")
        self._merge_worker = MergeWorker(
            self.project.path,
            voice=voice,
            fmt=self.merge_format.currentData(),
            mode=mode,
            start=start,
            end=end,
            batch=batch,
        )
        self._merge_worker.progress.connect(self._on_merge_progress)
        self._merge_worker.file_done.connect(self._on_merge_file_done)
        self._merge_worker.finished_ok.connect(self._on_merge_finished)
        self._merge_worker.failed.connect(self._on_merge_failed)
        track_worker(self._merge_worker)  # keep the Mac awake while merging
        self._merge_worker.start()

    def _on_merge_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setValue(done)
        if name:
            self.status_label.setText(f"🔗 Đang ghép ({done + 1}/{total}): {name}")

    def _on_merge_file_done(self, path: str) -> None:
        self.progress.setValue(self.progress.value() + 1)

    def _reset_merge_ui(self) -> None:
        self.merge_button.setEnabled(True)
        self.generate_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _on_merge_finished(self, count: int) -> None:
        self._reset_merge_ui()
        if count:
            self.status_label.setText(f"✅ Đã ghép xong {count} file — bấm “Mở thư mục audio”.")
        else:
            self.status_label.setText("Đã dừng ghép audio.")

    def _on_merge_failed(self, message: str) -> None:
        self._reset_merge_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Ghép audio thất bại", message)

    def has_running_workers(self) -> bool:
        # Only TTS generation / merge are user-meaningful work worth a close-confirm; the
        # voices-list fetch is a short background metadata call (shutdown still joins it).
        return (self._worker is not None and self._worker.isRunning()) or (
            self._merge_worker is not None and self._merge_worker.isRunning()
        )

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(120_000)  # a mid-chapter chunk may take a while
        if self._merge_worker is not None and self._merge_worker.isRunning():
            self._merge_worker.cancel()  # stops before the next window; current ffmpeg finishes
            self._merge_worker.wait(120_000)
        if self._voices_worker is not None and self._voices_worker.isRunning():
            self._voices_worker.wait(5_000)

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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QRadioButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig
from noveltrans.models import AUDIO_SOURCE_DOWNLOADED
from noveltrans.gui.jobs import job_registry
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import (
    AudioSourceTableModel,
    audio_source_label,
    PauseButton,
    AudioChapterTableModel,
    ProjectPicker,
    RowButtonDelegate,
    enable_cell_copy,
)
from noveltrans.gui.workers import (
    AudioDownloadWorker,
    AudioManifestWorker,
    AudioWorker,
    MergeWorker,
    TtsVoicesWorker,
)
from noveltrans.scrapers import ADAPTERS
from noveltrans.storage import NovelProject


# Re-voicing is slow (minutes per chapter), so a big batch asks first. One chapter
# never does — that is the per-row 🔊 button's long-standing behaviour.
REGENERATE_CONFIRM_FROM = 5

# Combo entry standing for the site's own audio edition — see `VideoTab`'s copy. Not a
# voice id: a release is not synthesized, so it selects a different SOURCE of parts.
SOURCE_AUDIO_KEY = "__source_audio__"


def _supports_audio_download(url: str) -> bool:
    """Whether this novel's source publishes audio this app can fetch.

    Asks the adapter CLASS, not an instance: no HttpClient is built and no request is
    made, so this is safe to call while painting the tab. Only tieuthuyetmang answers
    yes today; `SiteAdapter` is deliberately not widened for two single-site methods,
    which is exactly why this is a `hasattr` and not an interface check.
    """
    return any(
        cls.matches(url) and hasattr(cls, "fetch_audio_url") for cls in ADAPTERS
    )


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
        self.voice_combo.setMinimumWidth(200)
        self.voice_combo.setToolTip("Giọng đọc (người đọc) VieNeu-TTS.")
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

        # --- view toggle: the novel's chapters, or what the source publishes as audio.
        # Built here because the layout below adds it, but NOT connected yet: addItem
        # moves the index off -1 and would fire the handler before the models exist.
        self.view_combo = QComboBox()
        self.view_combo.addItem("Chương", "chapters")
        self.view_combo.addItem("Audio từ nguồn", "source")
        self.view_combo.setToolTip(
            "Chuyển giữa danh sách chương của truyện và danh sách audio trang nguồn phát hành."
        )
        self.view_combo.setVisible(False)  # only for a source that publishes audio

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Truyện:"))
        top_row.addWidget(self.picker, stretch=1)
        top_row.addWidget(QLabel("Nguồn:"))
        top_row.addWidget(self.translated_radio)
        top_row.addWidget(self.original_radio)
        top_row.addWidget(QLabel("Hiển thị:"))
        top_row.addWidget(self.view_combo)
        top_row.addWidget(QLabel("Giọng đọc:"))
        top_row.addWidget(self.voice_combo)
        top_row.addWidget(QLabel("Định dạng:"))
        top_row.addWidget(self.format_combo)

        # --- chapter table
        self.model = AudioChapterTableModel(self)
        # The audio-list view is a SECOND model over the same table rather than a filter
        # over the first: the source publishes per five-chapter volume at a sparse set of
        # chapter numbers, so its rows are not chapters and have no chapter columns.
        self.source_model = AudioSourceTableModel(self)
        self._manifest: list[dict] = []
        self._manifest_worker: AudioManifestWorker | None = None
        self.model.set_source(config.tts_use_translation)
        # connect now that the model exists (setChecked/addItem above ran before this)
        self.translated_radio.toggled.connect(self._on_source_changed)
        self.view_combo.currentIndexChanged.connect(self._on_view_changed)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(
            AudioChapterTableModel.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        # Stated here, not inherited from enable_cell_copy: the right-click
        # "Tạo lại N chương" below depends on a multi-row selection being possible.
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        # Ctrl+C / right-click to copy a cell (e.g. errors); the same menu also carries
        # "Tạo lại" for every selected chapter via the extra_actions hook — the table
        # owns one context-menu signal and enable_cell_copy holds it (same arrangement
        # as the Tải truyện tab). See _add_regenerate_actions.
        enable_cell_copy(self.table, extra_actions=self._table_context_actions)
        self.table.setMouseTracking(True)
        self._row_button_delegate = RowButtonDelegate("🔊 Tạo lại", self.table)
        self._row_button_delegate.clicked.connect(self._regenerate_row)
        self.table.setItemDelegateForColumn(
            AudioChapterTableModel.REGENERATE_COLUMN, self._row_button_delegate
        )
        # The audio view's own per-row button. It cannot simply live on its column
        # forever: RowButtonDelegate.paint draws NOTHING when UserRole is falsy (it does
        # not chain to the default painter), and the source model's button column is the
        # chapter model's "Lỗi" column — leaving it installed would blank error text in
        # the chapter view. `_on_view_changed` swaps it against `_plain_delegate`.
        self._redownload_delegate = RowButtonDelegate("⬇️ Tải lại", self.table)
        self._redownload_delegate.clicked.connect(self._redownload_row)
        self._plain_delegate = QStyledItemDelegate(self.table)
        self.table.setColumnWidth(AudioChapterTableModel.REGENERATE_COLUMN, 100)
        self.table.setColumnWidth(AudioChapterTableModel.CHARS_COLUMN, 70)
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
        self.download_audio_button = QPushButton("⬇️ Tải audio từ nguồn")
        self.download_audio_button.setToolTip(
            "Tải bản đọc do chính trang nguồn phát hành, thay vì tổng hợp bằng TTS."
        )
        self.download_audio_button.clicked.connect(lambda: self._start_audio_download())
        self.download_audio_button.setVisible(False)  # shown only for a source that has it
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.pause_button = PauseButton()
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
        bottom_row.addWidget(self.download_audio_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addWidget(self.pause_button)
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

        # Which audio to merge. plan_merge_windows selects on audio_voice, and this used
        # to read voice_combo — the *synthesis* voice — so audio made with any other
        # voice was silently unmergeable, and downloaded narration (audio_voice =
        # "tieuthuyetmang") could never be merged at all. Populated from what the project
        # actually holds.
        self.merge_source = QComboBox()

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
        row.addWidget(QLabel("Giọng:"))
        row.addWidget(self.merge_source)
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
            self._refresh_merge_sources()
            self._update_status_line()
        else:
            self.model.set_chapters([])
            self.status_label.setText("")
        self._sync_download_button()

    def _in_source_view(self) -> bool:
        return self.view_combo.currentData() == "source"

    def _on_view_changed(self) -> None:
        """Swap the table between the chapter list and the source's audio list.

        The two models have different columns, so the table's model is replaced rather
        than filtered. Selection does not survive the swap, which is correct: a row number
        means a different thing on each side.
        """
        if self._in_source_view():
            self.table.setModel(self.source_model)
            self.table.setItemDelegateForColumn(
                AudioSourceTableModel.REDOWNLOAD_COLUMN, self._redownload_delegate
            )
            self.table.setColumnWidth(AudioSourceTableModel.REDOWNLOAD_COLUMN, 100)
            self.table.horizontalHeader().setSectionResizeMode(
                AudioSourceTableModel.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch
            )
            self.generate_button.setEnabled(False)
            self.regenerate_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            # Show what the project already knows FIRST — the release list is stored, so
            # opening this view offline (or with the site down) still shows every volume
            # and which of them are on disk. The network call is only for a list we have
            # never fetched.
            self._rebuild_source_rows()
            if not self.source_model.rowCount():
                self._load_audio_manifest()
        else:
            self.table.setModel(self.model)
            self.table.setItemDelegateForColumn(
                AudioSourceTableModel.REDOWNLOAD_COLUMN, self._plain_delegate
            )
            self.table.horizontalHeader().setSectionResizeMode(
                AudioChapterTableModel.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch
            )
            running = self._worker is not None and self._worker.isRunning()
            self.generate_button.setEnabled(not running)
            self.regenerate_button.setEnabled(not running)
            self.preview_button.setEnabled(True)
            self._update_status_line()

    def _load_audio_manifest(self) -> None:
        """Ask the source what audio it publishes. One request, off-thread."""
        if self.project is None or self._manifest_worker is not None:
            return
        url = self.project.meta.url
        cookies = self.config.cookies_for_url(url)
        if not cookies:
            # The listen pages need a session; the landing page does not, so the list
            # itself would load — but every row would then fail to download. Say it once.
            self.status_label.setText(
                "Chưa có cookie — dán Cookie tài khoản vào Cài đặt để tải được audio."
            )
        self.status_label.setText("Đang đọc danh sách audio từ trang nguồn…")
        worker = AudioManifestWorker(url, delay=self.config.request_delay, cookies=cookies)
        worker.listed.connect(self._on_manifest_listed)
        worker.failed.connect(self._on_manifest_failed)
        worker.finished.connect(self._on_manifest_finished)
        self._manifest_worker = worker
        worker.start()

    def _on_manifest_listed(self, entries: list) -> None:
        self._manifest = list(entries)
        if self.project is not None:
            # Recorded, not just displayed: the release list is what the download worker
            # and the video tab both work from, and it must survive closing the app.
            self.project.sync_source_audio(entries)
        self._rebuild_source_rows()

    def _on_manifest_failed(self, message: str) -> None:
        self.status_label.setText(message)
        self.source_model.set_items([])

    def _on_manifest_finished(self) -> None:
        self._manifest_worker = None

    def _rebuild_source_rows(self) -> None:
        """Show the releases this project knows about.

        No join to chapters, deliberately: a release covers a chapter RANGE and belongs to
        a different edition of the work. Pinning it to a chapter row is what used to make
        site audio appear in the chapter list.
        """
        if self.project is None:
            return
        releases = self.project.source_audio()
        self.source_model.set_items(releases)
        have = sum(1 for r in releases if r.has_audio)
        if releases:
            self.status_label.setText(
                f"Trang nguồn có {len(releases)} mục audio — đã tải {have}/{len(releases)}."
            )

    def _sync_download_button(self) -> None:
        """The button and the view toggle exist only for sources that publish narration.

        Hidden rather than disabled: for the six other sites there is nothing to explain
        and a permanently dead button in the row is just noise.
        """
        url = self.project.meta.url if self.project is not None else ""
        supported = bool(url) and _supports_audio_download(url)
        self.download_audio_button.setVisible(supported)
        self.view_combo.setVisible(supported)
        # The manifest belongs to the novel that was showing, so it cannot survive a
        # project change — and neither can the audio view, which would otherwise show the
        # previous novel's releases joined onto this one's chapters.
        self._manifest = []
        self.source_model.set_items([])
        if not supported and self._in_source_view():
            self.view_combo.setCurrentIndex(0)  # fires _on_view_changed, restoring the table
        elif self._in_source_view():
            self._load_audio_manifest()

    def _refresh_merge_sources(self) -> None:
        """Fill the merge 'Giọng' combo with the voices this project actually has audio in.

        Listing what is on disk rather than what the TTS engine offers is the point: a
        voice with no audio merges to nothing, and downloaded narration is not a TTS
        voice at all. Keeps the current pick when it survives the refresh.
        """
        previous = self.merge_source.currentData()
        self.merge_source.clear()
        voices: list[str] = []
        downloaded = 0
        if self.project is not None:
            for chapter in self.project.chapters():
                if chapter.has_audio and chapter.audio_voice not in voices:
                    voices.append(chapter.audio_voice)
            downloaded = sum(1 for r in self.project.source_audio() if r.has_audio)
        if downloaded:
            # A sentinel, not a voice: site audio is a separate edition with no narrator
            # of ours, and it is merged from `source_audio` rather than from chapter rows.
            self.merge_source.addItem(
                f"Audio từ nguồn ({downloaded} mục)", SOURCE_AUDIO_KEY
            )
        if not voices and not downloaded:
            # Nothing merged-able yet; offer the synthesis voice so the combo is never
            # empty and _start_merge can still give its "no audio" message.
            fallback = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
            if fallback:
                voices = [fallback]
        for voice in voices:
            self.merge_source.addItem(audio_source_label(voice), voice)
        if previous:
            at = self.merge_source.findData(previous)
            if at >= 0:
                self.merge_source.setCurrentIndex(at)

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
        message = f"{ready}, {counts['audio']} đã có audio"
        if counts["downloaded_audio"]:
            message += f" (trong đó {counts['downloaded_audio']} tải từ trang)"
        message += "."
        # A self-written Vietnamese novel usually has no translation and doesn't need
        # one — point at the radio instead of letting "0 đã dịch" look like a dead end.
        # Deliberately doesn't flip the radio: that writes a persisted preference.
        if (
            self._use_translation()
            and counts["translated"] == 0
            and counts["downloaded"] > 0
            and (self.project.meta.source_lang or "") == "vi"
        ):
            message += " Chưa có bản dịch — chọn “Bản gốc” để đọc thẳng nội dung."
        self.status_label.setText(message)

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
            # Keep the "· Phong cách X" suffix: since vieneu 3.3.0 the reading style
            # comes from the voice itself, so the label is the only place it shows.
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
            clean_extra_remove=self.config.tts_clean_extra_remove,
            gap_seconds=self.config.tts_gap_seconds,
            speed=self.config.tts_speed,
            volume=self.config.tts_volume,
            temperature=self.config.tts_temperature,
            precision=self.config.tts_precision,
            style=self.config.tts_style,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.chapter_done.connect(self._on_chapter_updated)
        self._worker.chapter_error.connect(lambda idx, _msg: self._on_chapter_updated(idx))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_ok.connect(self._on_finished)
        track_worker(self._worker)  # keep the Mac awake while generating audio
        self._job = job_registry.register(
            self._worker, kind="Nghe audio", novel=self._job_novel()
        )
        self.pause_button.set_job(self._job.id if self._job else None)
        self._worker.start()

    def _redownload_row(self, row: int) -> None:
        """The per-row "⬇️ Tải lại" button: re-fetch exactly this release.

        Forces past the skip-what-we-have rule — that is the whole point of the button.
        """
        item = self.source_model.item_at(row)
        if item is None:
            return
        self._start_audio_download([item.number], force=True)

    def _start_audio_download(
        self, numbers: list[int] | None = None, force: bool = False
    ) -> None:
        """Fetch narration published by the source site instead of synthesizing it.

        Shares `self._worker` with `AudioWorker` on purpose: the two can never run at once
        (both write audio rows for the same project), and doing so gives cancel, pause and
        the job registry for free — `AudioDownloadWorker`'s signals are signature-identical.
        """
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self, "Đang bận", "Đang có phiên audio chạy — chờ xong rồi thử lại."
            )
            return
        url = self.project.meta.url
        if not _supports_audio_download(url):
            QMessageBox.information(
                self, "Không hỗ trợ", "Nguồn của truyện này không phát hành audio để tải."
            )
            return
        if numbers is None and self._in_source_view():
            # An empty selection still means "everything the source offers".
            picked = [
                item.number
                for row in self._selected_rows()
                if (item := self.source_model.item_at(row)) is not None
            ]
            numbers = picked or None
        cookies = self.config.cookies_for_url(url)
        if not cookies:
            # Without a session the site serves an upsell instead of the player, so every
            # chapter would fail the same way. Say so once here rather than N times.
            QMessageBox.information(
                self,
                "Chưa có cookie",
                "Audio chỉ hiện với tài khoản đã đăng nhập (và có gói VIP).\n\n"
                "Dán Cookie của tài khoản vào Cài đặt rồi thử lại.",
            )
            return

        self.generate_button.setEnabled(False)
        self.regenerate_button.setEnabled(False)
        self.download_audio_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.picker.setEnabled(False)
        # The manifest is only known once the worker has fetched it; a busy bar until the
        # first progress signal beats a 0/1 bar that looks stalled.
        self.progress.setMaximum(max(len(numbers or []), 1))
        self.progress.setValue(0)
        self.status_label.setText("Đang tìm audio trên trang nguồn…")

        self._worker = AudioDownloadWorker(
            self.project.path,
            delay=self.config.request_delay,
            cookies=cookies,
            numbers=numbers,
            skip_downloaded=not force,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.item_done.connect(self._on_release_updated)
        self._worker.item_error.connect(lambda number, _msg: self._on_release_updated(number))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_ok.connect(self._on_finished)
        track_worker(self._worker)  # a 1.7 GB novel must not be cut short by a sleep
        self._job = job_registry.register(
            self._worker, kind="Tải audio", novel=self._job_novel()
        )
        self.pause_button.set_job(self._job.id if self._job else None)
        self._worker.start()

    def _table_context_actions(self, menu: QMenu, index) -> None:
        """Both row actions, in the order the two audio sources are offered above."""
        self._add_regenerate_actions(menu, index)
        self._add_download_actions(menu, index)

    def _add_regenerate_actions(self, menu: QMenu, index) -> None:
        """Append "Tạo lại" for the whole selection to the table's right-click menu.

        Built from the *selection*, not from the row under the cursor, so one right-click
        can re-voice every chapter the user highlighted. enable_cell_copy keeps a
        multi-row selection alive when the click lands inside it, which is what makes
        this reachable at all.
        """
        if self._in_source_view():
            return  # a row here is an audio release; TTS acts on chapters
        rows = self._selected_rows()
        if index.isValid() and index.row() not in rows:
            rows = [index.row()]  # right-clicked away from the selection → act on that row
        indices, skipped_no_text, skipped_downloaded = self._regenerable_indices(rows)
        if not indices:
            return  # nothing with source text to read — offering it would only mislead
        menu.addSeparator()
        label = (
            "🔊 Tạo lại chương này" if len(indices) == 1 else f"🔊 Tạo lại {len(indices)} chương"
        )
        action = menu.addAction(label)
        running = self._worker is not None and self._worker.isRunning()
        action.setEnabled(not running)
        hints = []
        if running:
            hints.append("Đang có phiên tạo audio chạy — chờ xong rồi thử lại.")
        if skipped_no_text:
            hints.append(f"Bỏ qua {skipped_no_text} chương chưa có nội dung.")
        if skipped_downloaded:
            hints.append(f"Bỏ qua {skipped_downloaded} chương đang dùng audio tải về.")
        if hints:
            menu.setToolTipsVisible(True)  # QMenu hides action tooltips unless asked
            action.setToolTip(" ".join(hints))
        # Snapshot the rows and re-resolve chapter indices at trigger time, so
        # _regenerable_indices stays the only authority on what is eligible.
        rows_snapshot = list(rows)
        action.triggered.connect(lambda: self._regenerate_rows(rows_snapshot))

    def _add_download_actions(self, menu: QMenu, index) -> None:
        """Append "Tải lại audio" for the selected RELEASES.

        Only in the audio view. A chapter row has no release to re-fetch: site audio is a
        separate edition covering a chapter range, so "re-download this chapter" is not a
        thing that can be asked.
        """
        if self.project is None or not self._in_source_view():
            return
        rows = self._selected_rows()
        if index.isValid() and index.row() not in rows:
            rows = [index.row()]
        numbers = [
            item.number
            for row in rows
            if (item := self.source_model.item_at(row)) is not None
        ]
        if not numbers:
            return
        menu.addSeparator()
        label = (
            "⬇️ Tải lại mục audio này"
            if len(numbers) == 1
            else f"⬇️ Tải lại {len(numbers)} mục audio"
        )
        action = menu.addAction(label)
        running = self._worker is not None and self._worker.isRunning()
        action.setEnabled(not running)
        if running:
            menu.setToolTipsVisible(True)
            action.setToolTip("Đang có phiên audio chạy — chờ xong rồi thử lại.")
        snapshot = list(numbers)
        # "Tải lại" means re-fetch, so it overrides the skip rule exactly as the per-row
        # button does; the batch button is the one that spares what is already on disk.
        action.triggered.connect(lambda: self._start_audio_download(snapshot, force=True))

    def _selected_rows(self) -> list[int]:
        """The rows the user has highlighted, in table order and without duplicates.

        selectedIndexes() yields one index per selected *cell*; with SelectRows that is
        every column of every row, hence the dedup.
        """
        selection = self.table.selectionModel()
        if selection is None:
            return []
        return sorted({index.row() for index in selection.selectedIndexes()})

    def _regenerable_indices(self, rows: list[int]) -> tuple[list[int], int, int]:
        """(indices to re-voice, dropped for no text, dropped as downloaded) for `rows`.

        Rows are table positions; AudioWorker wants chapter.index — not the same number
        once a novel has gaps, so this is the one place that converts. A row is dropped
        when its source (translation or original, per the radio) is empty: the worker
        filters those out anyway, so counting them would overstate the job and leave the
        progress bar's maximum lying. Sorted because AudioWorker reads `indices` in order.

        Rows carrying narration downloaded from the source site are dropped too, and
        counted separately because the two need different wording: "chưa có nội dung" is
        a "do this first", while a downloaded row is deliberately protected — re-voicing
        it would replace real narration with TTS. Explicit regenerate is the one path
        that bypasses pending_audio's guard, so it has to re-apply it here.
        """
        if self.project is None:
            return [], 0, 0
        use_translation = self._use_translation()
        indices: list[int] = []
        skipped_no_text = 0
        skipped_downloaded = 0
        for row in rows:
            chapter = self.model.chapter_at(row)
            if chapter is None:
                continue
            if chapter.audio_source == AUDIO_SOURCE_DOWNLOADED and chapter.has_audio:
                skipped_downloaded += 1
                continue
            if not (chapter.translated if use_translation else chapter.content):
                skipped_no_text += 1
                continue
            indices.append(chapter.index)
        return sorted(dict.fromkeys(indices)), skipped_no_text, skipped_downloaded

    def _regenerate_row(self, row: int) -> None:
        """The per-row 🔊 button — one chapter, no confirmation, as before."""
        self._regenerate_rows([row])

    def _regenerate_rows(self, rows: list[int]) -> None:
        """Re-voice exactly the given rows, whether that is one or a hundred.

        Shared by the per-row button and the context menu so both paths agree on the
        guards. No clear_audio() call is needed: AudioWorker with an explicit `indices`
        list regenerates those chapters regardless of existing audio (unlike the
        pending-only pass it makes when indices is None).
        """
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("Đang có phiên tạo audio chạy — chờ xong rồi thử lại.")
            return
        indices, skipped_no_text, skipped_downloaded = self._regenerable_indices(rows)
        if not indices:
            if skipped_downloaded and not skipped_no_text:
                self.status_label.setText(
                    f"{skipped_downloaded} chương đã chọn đang dùng audio tải về — "
                    "không tạo lại bằng TTS."
                )
                return
            nguon = "bản dịch" if self._use_translation() else "bản gốc"
            self.status_label.setText(
                f"Chương đã chọn chưa có {nguon} để đọc — hãy dịch/tải trước."
            )
            return
        if len(indices) >= REGENERATE_CONFIRM_FROM:
            answer = QMessageBox.question(
                self,
                "Tạo lại audio?",
                f"Sẽ tạo lại audio cho {len(indices)} chương đã chọn "
                "(ghi đè audio hiện có của những chương này). Tiếp tục chứ?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        notes = []
        if skipped_no_text:
            notes.append(f"{skipped_no_text} chương chưa có nội dung")
        if skipped_downloaded:
            notes.append(f"{skipped_downloaded} chương đang dùng audio tải về")
        if notes:
            self.status_label.setText("Bỏ qua " + ", ".join(notes) + ".")
        self._start_generate(indices=indices)

    def _regenerate_all(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        counts = self.project.counts()
        # clear_audio() and pending_audio() both spare downloaded narration, so quoting
        # counts["audio"] here would promise to redo files this pass will not touch.
        generated = counts["audio"] - counts["downloaded_audio"]
        kept = counts["downloaded_audio"]
        if generated:
            note = f" ({kept} chương dùng audio tải về sẽ được giữ nguyên.)" if kept else ""
            answer = QMessageBox.question(
                self,
                "Tạo lại từ đầu?",
                f"Sẽ tạo lại audio cho toàn bộ {generated} chương đã có.{note} Tiếp tục chứ?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif kept:
            self.status_label.setText(
                f"Chỉ có {kept} chương dùng audio tải về — không có gì để tạo lại bằng TTS."
            )
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
        if self._in_source_view():
            item = self.source_model.item_at(index.row())
            chapter = item.chapter if item is not None else None
        else:
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
            text = clean_for_tts(text, self.config.tts_clean_extra_remove)
        return title, text, cleaned

    def _preview_text(self) -> None:
        """Show the selected chapter's text exactly as the engine will receive it."""
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        if self._in_source_view():
            QMessageBox.information(
                self, "Đang xem audio", "Chuyển “Hiển thị” về “Chương” để xem trước văn bản."
            )
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

    def _on_release_updated(self, number: int) -> None:
        if self.project is None:
            return
        release = self.project.source_audio_at(number)
        if release is not None:
            self.source_model.update_item(release)

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
        self._refresh_merge_sources()  # a new voice may now have audio to merge
        message = f"Xong: {ok} chương có audio"
        if errors:
            message += f", {errors} lỗi (bấm 'Tạo audio tất cả' để thử lại)"
        # Only the download worker skips anything, and saying so matters: without it a
        # batch over an already-fetched novel reports "0 chương" and reads as a failure.
        skipped = getattr(self._worker, "skipped", 0)
        if skipped:
            message += f", bỏ qua {skipped} mục đã tải"
        self.status_label.setText(message + ".")

    def _reset_buttons(self) -> None:
        # The two TTS buttons act on chapters, so they stay off while the audio list is
        # showing — re-enabling them here is what would put the tab back in a state the
        # view does not support.
        chapters_view = not self._in_source_view()
        self.generate_button.setEnabled(chapters_view)
        self.regenerate_button.setEnabled(chapters_view)
        self.download_audio_button.setEnabled(True)
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
        voice = self.merge_source.currentData() or ""
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
            source_audio=voice == SOURCE_AUDIO_KEY,
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
        self._job = job_registry.register(
            self._merge_worker, kind="Ghép audio", novel=self._job_novel()
        )
        self.pause_button.set_job(self._job.id if self._job else None)
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

    def _job_novel(self) -> str:
        """The novel label for the menu-bar job row — this tab's own project.

        Deliberately not Workspace.current_title(): each tab has an independent picker,
        so an audio job on a different novel would be labelled with the scrape tab's.
        """
        return self.project.meta.display_name() if self.project is not None else ""

    def has_running_workers(self) -> bool:
        # Only TTS generation / merge are user-meaningful work worth a close-confirm; the
        # voices-list fetch is a short background metadata call (shutdown still joins it).
        return (
            (self._worker is not None and self._worker.isRunning())
            or (self._merge_worker is not None and self._merge_worker.isRunning())
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

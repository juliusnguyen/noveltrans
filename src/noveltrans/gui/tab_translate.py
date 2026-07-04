"""Tab 2 — Translate: pick a novel, an engine and a language; translate chapters."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import TARGET_LANGS, AppConfig, translator_labels
from noveltrans.gui.widgets import (
    ChapterTableModel,
    ProjectPicker,
    RetranslateButtonDelegate,
)
from noveltrans.gui.workers import CliModelsWorker, LmStudioModelsWorker, TranslateWorker
from noveltrans.storage import NovelProject


class TranslateTab(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._worker: TranslateWorker | None = None
        self._models_worker: CliModelsWorker | LmStudioModelsWorker | None = None
        self._model_suggestions: dict[str, list[str]] = {}  # binary/url -> model labels

        # --- top row: novel + engine + language
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._on_project_selected)

        self.engine_combo = QComboBox()
        self._populate_engines(select=config.translator)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)

        # server address — only for LM Studio, persisted across sessions
        self.url_label = QLabel("Reachable at:")
        self.url_edit = QLineEdit()
        self.url_edit.setMinimumWidth(180)
        self.url_edit.setPlaceholderText(config.lmstudio_url)
        self.url_edit.setToolTip("Địa chỉ server LM Studio (Developer → Start Server).")
        self.url_edit.editingFinished.connect(self._on_url_edited)

        # model box — for CLI engines and LM Studio, editable so any model works
        self.model_label = QLabel("Model:")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(200)
        self.model_combo.lineEdit().setPlaceholderText("mặc định của CLI")
        self.model_combo.setToolTip(
            "Model cho CLI Agent (thêm --model vào lệnh). Để trống = model mặc định."
        )

        self.lang_combo = QComboBox()
        for key, label in TARGET_LANGS.items():
            self.lang_combo.addItem(label, key)
        self.lang_combo.setCurrentIndex(self.lang_combo.findData(config.target_lang))

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Truyện:"))
        top_row.addWidget(self.picker, stretch=1)
        top_row.addWidget(QLabel("Dịch bằng:"))
        top_row.addWidget(self.engine_combo)
        top_row.addWidget(self.url_label)
        top_row.addWidget(self.url_edit)
        top_row.addWidget(self.model_label)
        top_row.addWidget(self.model_combo)
        top_row.addWidget(QLabel("Sang:"))
        top_row.addWidget(self.lang_combo)

        # --- chapter table + side-by-side preview
        self.model = ChapterTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        for column in (ChapterTableModel.TITLE_COLUMN, ChapterTableModel.TRANSLATED_TITLE_COLUMN):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setMouseTracking(True)  # hover state for the row buttons
        self._row_button_delegate = RetranslateButtonDelegate(self.table)
        self._row_button_delegate.retranslate_clicked.connect(self._retranslate_row)
        self.table.setItemDelegateForColumn(
            ChapterTableModel.RETRANSLATE_COLUMN, self._row_button_delegate
        )
        self.table.setColumnWidth(ChapterTableModel.RETRANSLATE_COLUMN, 100)

        self.original_view = QPlainTextEdit()
        self.original_view.setReadOnly(True)
        self.original_view.setPlaceholderText("Bản gốc (tiếng Trung)")
        self.translated_view = QPlainTextEdit()
        self.translated_view.setReadOnly(True)
        self.translated_view.setPlaceholderText("Bản dịch")
        preview = QSplitter(Qt.Orientation.Horizontal)
        preview.addWidget(self.original_view)
        preview.addWidget(self.translated_view)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        # --- bottom row
        self.translate_button = QPushButton("Dịch tất cả")
        self.translate_button.clicked.connect(lambda: self._start_translate())
        self.retranslate_button = QPushButton("Dịch lại từ đầu")
        self.retranslate_button.setToolTip(
            "Xoá toàn bộ bản dịch hiện có rồi dịch lại (dùng khi đổi engine/cách dịch tên)."
        )
        self.retranslate_button.clicked.connect(self._retranslate_all)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m chương")
        self.status_label = QLabel("")
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.translate_button)
        bottom_row.addWidget(self.retranslate_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addWidget(self.progress, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(splitter, stretch=1)
        layout.addLayout(bottom_row)
        layout.addWidget(self.status_label)

        self.table.selectionModel().currentRowChanged.connect(self._on_row_selected)
        self._on_engine_changed()

    # -------------------------------------------------------------- projects

    def _populate_engines(self, select: str = "") -> None:
        """(Re)build the engine combo — the CLI entry names its actual command."""
        current = select or self.engine_combo.currentData() or ""
        self.engine_combo.blockSignals(True)
        self.engine_combo.clear()
        for key, label in translator_labels(self.config).items():
            self.engine_combo.addItem(label, key)
        index = self.engine_combo.findData(current)
        self.engine_combo.setCurrentIndex(index if index >= 0 else 0)
        self.engine_combo.blockSignals(False)
        if hasattr(self, "model_combo"):  # skip during __init__, called again at the end
            self._on_engine_changed()

    # ------------------------------------------------------------- model box

    def _cli_binary_for(self, engine: str) -> str:
        parts = self.config.cli_command_for(engine).split()
        return parts[0] if parts else ""

    def _on_engine_changed(self, *_args) -> None:
        engine = self.engine_combo.currentData()
        is_cli = engine in ("cli", "claude_cli")
        is_lmstudio = engine == "lmstudio"
        self.url_label.setVisible(is_lmstudio)
        self.url_edit.setVisible(is_lmstudio)
        self.model_label.setVisible(is_cli or is_lmstudio)
        self.model_combo.setVisible(is_cli or is_lmstudio)
        if is_lmstudio:
            url = self.config.lmstudio_url
            self.url_edit.setText(url)
            source = url
        elif is_cli:
            source = self._cli_binary_for(engine)
            if engine == "claude_cli":
                self._model_suggestions.setdefault(source, ["haiku", "sonnet", "opus"])
        else:
            return
        self._set_model_items(self._model_suggestions.get(source, []))
        self.model_combo.setEditText(self.config.cli_model_for(engine))
        if source and source not in self._model_suggestions:
            self._fetch_models(source, lmstudio=is_lmstudio)

    def _on_url_edited(self) -> None:
        """Persist the LM Studio address and refresh its model list."""
        url = self.url_edit.text().strip() or self.config.lmstudio_url
        self.url_edit.setText(url)
        if url != self.config.lmstudio_url:
            self.config.lmstudio_url = url
            self._model_suggestions.pop(url, None)
        if url not in self._model_suggestions:
            self._fetch_models(url, lmstudio=True)

    def _set_model_items(self, models: list[str]) -> None:
        text = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("")  # "" = the CLI's default model
        self.model_combo.addItems(models)
        self.model_combo.setEditText(text)
        self.model_combo.blockSignals(False)

    def _fetch_models(self, source: str, lmstudio: bool = False) -> None:
        if self._models_worker is not None and self._models_worker.isRunning():
            return
        worker_cls = LmStudioModelsWorker if lmstudio else CliModelsWorker
        self._models_worker = worker_cls(source)
        self._models_worker.models_listed.connect(self._on_models_listed)
        self._models_worker.start()

    def _on_models_listed(self, source: str, models: list) -> None:
        self._model_suggestions[source] = list(models)
        engine = self.engine_combo.currentData()
        if engine == "lmstudio":
            current = self.config.lmstudio_url
        elif engine in ("cli", "claude_cli"):
            current = self._cli_binary_for(engine)
        else:
            return
        if current == source:
            self._set_model_items(models)

    def refresh_projects(self, select_path: str = "") -> None:
        self.picker.refresh(self.config.library_dir, select_path)

    def showEvent(self, event) -> None:  # refresh lists every time the tab appears
        if self._worker is None or not self._worker.isRunning():
            self.refresh_projects()
            self._populate_engines()  # pick up a changed CLI command in Settings
        super().showEvent(event)

    def _on_project_selected(self, path: str) -> None:
        if self.project is not None:
            self.project.close()
            self.project = None
        if path:
            self.project = NovelProject.open(path)
            self.model.set_chapters(self.project.chapters())
            counts = self.project.counts()
            self.status_label.setText(
                f"{counts['downloaded']}/{counts['total']} chương đã tải, "
                f"{counts['translated']} đã dịch."
            )
        else:
            self.model.set_chapters([])
            self.status_label.setText("")
        self.original_view.clear()
        self.translated_view.clear()

    # --------------------------------------------------------------- preview

    def _on_row_selected(self, current, _previous) -> None:
        chapter = self.model.chapter_at(current.row()) if current.isValid() else None
        if chapter is None or self.project is None:
            return
        fresh = self.project.chapter(chapter.index)
        if fresh is None:
            return
        self.original_view.setPlainText(f"{fresh.title}\n\n{fresh.content}")
        self.translated_view.setPlainText(
            f"{fresh.translated_title}\n\n{fresh.translated}" if fresh.translated else ""
        )

    # ------------------------------------------------------------- translate

    def _start_translate(self, indices: list[int] | None = None) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        target = self.lang_combo.currentData()
        if indices is None:
            pending = self.project.pending_translation(target)
            meta_done = self.project.meta.translated_lang == target
            if not pending and meta_done:
                QMessageBox.information(self, "Đã đủ", "Không còn gì cần dịch.")
                return
            total = len(pending)
        else:
            total = len(indices)

        engine = self.engine_combo.currentData()
        base_url = ""
        if engine in ("cli", "claude_cli", "lmstudio"):
            model = self.model_combo.currentText().strip()
            self.config.set_cli_model_for(engine, model)
            if engine == "lmstudio":
                self.config.lmstudio_url = self.url_edit.text()
                base_url = self.config.lmstudio_url
        else:
            model = self.config.claude_model
        # remember choices for next time
        self.config.translator = engine
        self.config.target_lang = target

        self.translate_button.setEnabled(False)
        self.retranslate_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.picker.setEnabled(False)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(0)

        self._worker = TranslateWorker(
            self.project.path,
            engine,
            target,
            api_key=self.config.claude_api_key,
            model=model,
            request_delay=self.config.request_delay,
            cli_command=self.config.cli_command_for(engine),
            base_url=base_url,
            indices=indices,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.chapter_done.connect(self._on_chapter_updated)
        self._worker.chapter_error.connect(lambda idx, _msg: self._on_chapter_updated(idx))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.start()

    def _retranslate_row(self, row: int) -> None:
        """Re-translate exactly one chapter (the per-row '↻ Dịch lại' button)."""
        chapter = self.model.chapter_at(row)
        if chapter is None or self.project is None or not chapter.content:
            return
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("Đang có phiên dịch chạy — chờ xong rồi thử lại.")
            return
        self._start_translate(indices=[chapter.index])

    def _retranslate_all(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        translated = self.project.counts()["translated"]
        if translated:
            answer = QMessageBox.question(
                self,
                "Dịch lại từ đầu?",
                f"Sẽ xoá {translated} chương đã dịch và dịch lại toàn bộ. Tiếp tục chứ?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.project.clear_translations()
        self.model.set_chapters(self.project.chapters())
        self._start_translate()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("Đang dừng sau chương hiện tại…")

    def _on_progress(self, done: int, total: int, title: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if title:
            self.status_label.setText(f"Đang dịch: {title}")

    def _on_chapter_updated(self, idx: int) -> None:
        if self.project is None:
            return
        chapter = self.project.chapter(idx)
        if chapter is not None:
            self.model.update_chapter(chapter)

    def _on_failed(self, message: str) -> None:
        self._reset_buttons()
        QMessageBox.warning(self, "Không dịch được", message)

    def _on_finished(self, ok: int, errors: int) -> None:
        self._reset_buttons()
        message = f"Dịch xong: {ok} chương thành công"
        if errors:
            message += f", {errors} lỗi (bấm 'Dịch tất cả' để thử lại)"
        self.status_label.setText(message + ".")

    def _reset_buttons(self) -> None:
        self.translate_button.setEnabled(True)
        self.retranslate_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.picker.setEnabled(True)

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(60_000)
        if self._models_worker is not None and self._models_worker.isRunning():
            self._models_worker.wait(20_000)  # bounded by the 15s subprocess timeout

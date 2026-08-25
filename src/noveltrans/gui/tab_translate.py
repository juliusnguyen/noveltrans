"""Tab 2 — Translate: pick a novel, an engine and a language; translate chapters."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QTextCursor, QTextDocument
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
from noveltrans.find_replace import FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE
from noveltrans.gui.find_replace_dialog import FindReplaceDialog
from noveltrans.gui.jobs import job_registry
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.rewrite_dialog import RewriteDialog
from noveltrans.gui.widgets import (
    PauseButton,
    ChapterTableModel,
    ProjectPicker,
    CellEditorDelegate,
    RetranslateButtonDelegate,
    enable_cell_copy,
)
from noveltrans.gui.workers import (
    CliModelsWorker,
    LmStudioModelsWorker,
    RewriteWorker,
    TranslateWorker,
    chapters_to_rewrite,
)
from noveltrans.models import Chapter
from noveltrans.storage import NovelProject


class TranslateTab(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._preview_idx: int | None = None  # chapter shown in the preview panes
        self._worker: TranslateWorker | None = None
        # A second batch worker on the same tab. Every lifecycle site below — _busy,
        # _cancel, _reset_buttons, has_running_workers, shutdown — has to account for it,
        # or quitting mid-rewrite abandons a running QThread.
        self._rewrite_worker: RewriteWorker | None = None
        self._rewrite_dialog: RewriteDialog | None = None
        # Modeless, unlike the rewrite dialog: it stays up while the user fixes
        # matches by hand in the panes, so the tab has to track and steer it.
        self._find_dialog: FindReplaceDialog | None = None
        self._progress_verb = "Đang dịch"
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
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        # Ctrl+C / right-click to copy a cell (e.g. errors); the same menu also carries
        # the per-chapter "Viết lại" / "Hoàn tác" actions via the extra_actions hook —
        # RETRANSLATE_COLUMN already holds the table's only row-button slot.
        enable_cell_copy(self.table, extra_actions=self._table_context_actions)
        self.table.setMouseTracking(True)  # hover state for the row buttons
        self.model.translated_title_edited.connect(self._on_translated_title_edited)
        # Same clipping as the chapter title in Tải truyện: the styled QLineEdit
        # editor is taller than the row unless its padding is stripped.
        self.table.setItemDelegate(CellEditorDelegate(self.table))
        self._row_button_delegate = RetranslateButtonDelegate(self.table)
        self._row_button_delegate.clicked.connect(self._retranslate_row)
        self.table.setItemDelegateForColumn(
            ChapterTableModel.RETRANSLATE_COLUMN, self._row_button_delegate
        )
        self.table.setColumnWidth(ChapterTableModel.RETRANSLATE_COLUMN, 100)

        self.original_view = QPlainTextEdit()
        self.original_view.setReadOnly(True)  # editable once a chapter loads
        self.original_view.setPlaceholderText("Bản gốc (bấm vào để sửa/dán, tự lưu khi rời ô)")
        self.original_view.setToolTip(
            "Dán hoặc sửa nội dung gốc — dòng đầu là tên chương, cách một dòng trống rồi "
            "đến nội dung. Đây là chỗ nhập nội dung cho truyện tự viết."
        )
        self.original_view.installEventFilter(self)  # save edits on focus-out
        self.translated_view = QPlainTextEdit()
        self.translated_view.setReadOnly(True)  # editable once a translated chapter loads
        self.translated_view.setPlaceholderText("Bản dịch (bấm vào để sửa, tự lưu khi rời ô)")
        self.translated_view.setToolTip(
            "Sửa trực tiếp bản dịch — dòng đầu là tên chương, phần sau là nội dung."
        )
        self.translated_view.installEventFilter(self)  # save edits on focus-out
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
        self.translate_button.setProperty("primary", True)
        self.translate_button.clicked.connect(lambda: self._start_translate())
        self.retranslate_button = QPushButton("Dịch lại từ đầu")
        self.retranslate_button.setToolTip(
            "Xoá toàn bộ bản dịch hiện có rồi dịch lại (dùng khi đổi engine/cách dịch tên)."
        )
        self.retranslate_button.clicked.connect(self._retranslate_all)
        self.find_replace_button = QPushButton("Tìm & thay thế")
        self.find_replace_button.setToolTip(
            "Thay thế hàng loạt trong bản dịch/bản gốc (ví dụ: sửa một tên nhân vật)."
        )
        self.find_replace_button.clicked.connect(self._open_find_replace)
        self.rewrite_button = QPushButton("✍️ Viết lại văn phong")
        self.rewrite_button.setToolTip(
            "Dùng AI viết lại bản dịch cho đúng văn phong tiếng Việt (truyện convert dịch "
            "word-by-word). Không đổi tên riêng, không đổi xưng hô. Có thể hoàn tác."
        )
        self.rewrite_button.clicked.connect(self._open_rewrite)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.pause_button = PauseButton()
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m chương")
        self.status_label = QLabel("")
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.translate_button)
        bottom_row.addWidget(self.retranslate_button)
        bottom_row.addWidget(self.find_replace_button)
        bottom_row.addWidget(self.rewrite_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addWidget(self.pause_button)
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
        if not self._busy():
            self.refresh_projects()
            self._populate_engines()  # pick up a changed CLI command in Settings
        super().showEvent(event)

    def _on_project_selected(self, path: str) -> None:
        self._save_preview_edits()
        self._save_original_edits()
        # It holds this project and would be scanning a closed handle a line from now.
        self._close_find_replace()
        if self.project is not None:
            self.project.close()
            self.project = None
        if path:
            self.project = NovelProject.open(path)
            self.model.set_chapters(self.project.chapters())
            counts = self.project.counts()
            message = (
                f"{counts['downloaded']}/{counts['total']} chương đã tải, "
                f"{counts['translated']} đã dịch."
            )
            if self.project.meta.is_local:
                message = (
                    f"Truyện tự viết — {counts['downloaded']}/{counts['total']} chương đã có "
                    f"nội dung, {counts['translated']} đã dịch. Chọn một chương rồi dán nội "
                    "dung vào ô “Bản gốc” bên trái."
                )
            self.status_label.setText(message)
        else:
            self.model.set_chapters([])
            self.status_label.setText("")
        self.original_view.clear()
        self.original_view.setReadOnly(True)
        self.translated_view.clear()
        self.translated_view.setReadOnly(True)
        self._preview_idx = None

    # --------------------------------------------------------------- preview

    def _on_row_selected(self, current, _previous) -> None:
        self._save_preview_edits()
        self._save_original_edits()
        chapter = self.model.chapter_at(current.row()) if current.isValid() else None
        if chapter is None or self.project is None:
            return
        self._load_preview(self.project.chapter(chapter.index))
        if self._find_dialog is not None:
            self._find_dialog.set_current_chapter(self._preview_idx)

    def _load_preview(self, fresh: Chapter | None) -> None:
        if fresh is None:
            return
        self._preview_idx = fresh.index
        self.original_view.setPlainText(f"{fresh.title}\n\n{fresh.content}")
        self.original_view.setReadOnly(False)
        # setPlainText already clears the modified flag, so this is belt-and-braces: it
        # states the invariant the save-on-blur guard depends on — a freshly loaded pane
        # has nothing pending, so moving through chapters writes nothing back.
        self.original_view.document().setModified(False)
        if fresh.translated:
            self.translated_view.setPlainText(f"{fresh.translated_title}\n\n{fresh.translated}")
            self.translated_view.setReadOnly(False)
        else:
            self.translated_view.clear()
            self.translated_view.setReadOnly(True)
        self.translated_view.document().setModified(False)

    def _save_preview_edits(self) -> None:
        """Persist manual edits typed into the translated preview pane."""
        if (
            self.project is None
            or self._preview_idx is None
            or not self.translated_view.document().isModified()
        ):
            return
        raw = self.translated_view.toPlainText()
        if not raw.strip():  # an emptied pane is treated as an accidental clear
            return
        title, sep, body = raw.partition("\n\n")
        if not sep:  # blank line removed — fall back to first line = title
            title, _, body = raw.partition("\n")
        self.project.edit_translation(self._preview_idx, title=title.strip(), text=body.strip())
        self.translated_view.document().setModified(False)
        chapter = self.project.chapter(self._preview_idx)
        if chapter is not None:
            self.model.update_chapter(chapter)

    def _save_original_edits(self) -> None:
        """Persist manual edits typed into the original preview pane.

        Mirrors `_save_preview_edits` deliberately — same first-line-is-the-title
        convention, same "an emptied pane is an accident" guard — because this is the
        only place a hand-written novel gets its text, and two different rules for two
        panes sitting side by side would be a trap.
        """
        if (
            self.project is None
            or self._preview_idx is None
            or not self.original_view.document().isModified()
        ):
            return
        raw = self.original_view.toPlainText()
        if not raw.strip():  # an emptied pane is treated as an accidental clear
            return
        title, sep, body = raw.partition("\n\n")
        if not sep:  # blank line removed — fall back to first line = title
            title, _, body = raw.partition("\n")
        self.project.edit_content(self._preview_idx, body.strip())
        title = title.strip()
        stored = self.project.chapter(self._preview_idx)
        # Only on a real change: edit_title sets title_custom, and marking every blur as
        # a rename would make "Lấy lại tên gốc" appear on chapters nobody renamed.
        if title and stored is not None and title != stored.title:
            self.project.edit_title(self._preview_idx, title)
        self.original_view.document().setModified(False)
        chapter = self.project.chapter(self._preview_idx)
        if chapter is not None:
            self.model.update_chapter(chapter)

    def _on_translated_title_edited(self, idx: int, title: str) -> None:
        if self.project is None:
            return
        self.project.edit_translation(idx, title=title)
        if idx == self._preview_idx:
            self._load_preview(self.project.chapter(idx))

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.FocusOut:
            if obj is self.translated_view:
                self._save_preview_edits()
            elif obj is self.original_view:
                self._save_original_edits()
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------- translate

    def _start_translate(self, indices: list[int] | None = None) -> None:
        self._save_preview_edits()
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        # The buttons are already disabled during a run, but "Dịch lại từ đầu" and the
        # per-row retranslate reach this by other routes — and a translation racing a
        # rewrite would have them writing the same rows.
        if self._busy():
            self.status_label.setText(self._busy_message())
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

        self._progress_verb = "Đang dịch"
        self._close_find_replace()
        self.translate_button.setEnabled(False)
        self.retranslate_button.setEnabled(False)
        self.find_replace_button.setEnabled(False)
        self.rewrite_button.setEnabled(False)
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
        track_worker(self._worker)  # keep the Mac awake while translating
        self._job = job_registry.register(
            self._worker, kind="Dịch", novel=self._job_novel()
        )
        self.pause_button.set_job(self._job.id if self._job else None)
        self._worker.start()

    def _retranslate_row(self, row: int) -> None:
        """Re-translate exactly one chapter (the per-row '↻ Dịch lại' button)."""
        chapter = self.model.chapter_at(row)
        if chapter is None or self.project is None or not chapter.content:
            return
        if self._busy():
            self.status_label.setText(self._busy_message())
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

    def _open_find_replace(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        if self._busy():
            self.status_label.setText(self._busy_message())
            return
        if self._find_dialog is not None:  # already up — one search at a time
            self._find_dialog.raise_()
            self._find_dialog.activateWindow()
            return
        # Flush a half-typed manual edit to disk FIRST, so the scan sees the latest text
        # and a later focus-out can't overwrite the replacement. Both panes — a replace
        # can target the original text too (FIELD_CONTENT). The dialog is modeless, so
        # later edits are covered by its own staleness check instead.
        self._save_preview_edits()
        self._save_original_edits()

        dialog = FindReplaceDialog(self.project, self._preview_idx, self)
        dialog.applied.connect(self._on_replacements_applied)
        dialog.chapter_activated.connect(self._jump_to_match)
        dialog.finished.connect(lambda _result: setattr(self, "_find_dialog", None))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._find_dialog = dialog
        dialog.show()

    def _close_find_replace(self) -> None:
        """Shut the dialog before a batch run — they write the same rows.

        Clears the handle here rather than waiting for `finished`: a dialog that was
        never actually shown emits nothing on close, and a stale handle would make the
        button think a dialog is still up.
        """
        dialog, self._find_dialog = self._find_dialog, None
        if dialog is not None:
            dialog.close()

    def _jump_to_match(self, index: int, field: str, search: str, case_sensitive: bool) -> None:
        """Open a chapter from the dialog's list and select its first match there.

        Selecting the row loads the panes through the normal path (which also flushes
        pending edits), then the pane re-finds the text rather than trusting an offset
        from the scan: a pane shows "title\n\nbody", so a raw field offset would land in
        the wrong place — and for a title match, in the wrong text entirely.
        """
        if self.project is None:
            return
        row = self.model.row_for_index(index)
        if row is None:
            return  # deleted since the preview — nothing to open
        was_open = self._preview_idx
        self.table.selectRow(row)  # → _on_row_selected loads the preview panes
        self.table.scrollTo(self.model.index(row, 0))
        view = (
            self.translated_view
            if field in (FIELD_TRANSLATED, FIELD_TRANSLATED_TITLE)
            else self.original_view
        )
        flags = (
            QTextDocument.FindFlag.FindCaseSensitively
            if case_sensitive
            else QTextDocument.FindFlag(0)
        )
        if was_open != index:
            self._move_to_start(view)  # a freshly loaded pane starts from the top
        # Otherwise search on from the cursor, so double-clicking the same row again
        # walks a chapter with several hits instead of pinning the first one.
        if not view.find(search, flags):
            self._move_to_start(view)
            view.find(search, flags)  # wrap; a real miss just leaves the cursor at the top
        view.ensureCursorVisible()
        # The dialog has keyboard focus right now — hand it to the pane, or the user's
        # first keystroke on the sentence they came to fix lands in the search box.
        self.window().activateWindow()
        view.setFocus()

    @staticmethod
    def _move_to_start(view: QPlainTextEdit) -> None:
        cursor = view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        view.setTextCursor(cursor)

    def _on_replacements_applied(self, indices: set) -> None:
        if self.project is None:
            return
        # Titles and the preview panes may both have changed; refresh the table, then
        # reload the open chapter. _load_preview clears the modified flag, so the
        # reloaded pane won't trigger a redundant save-on-blur.
        self.model.set_chapters(self.project.chapters())
        if self._preview_idx is not None and self._preview_idx in indices:
            self._load_preview(self.project.chapter(self._preview_idx))

    # --------------------------------------------------------------- rewrite

    def _open_rewrite(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy tải một truyện ở Tab 1 trước.")
            return
        if self._busy():
            self.status_label.setText(self._busy_message())
            return
        # Flush half-typed manual edits FIRST, so the rewrite reads the latest text and a
        # later focus-out cannot overwrite its result. The modal then blocks further pane
        # edits while it is open.
        self._save_preview_edits()
        self._save_original_edits()

        dialog = RewriteDialog(self.project, self.config, self._preview_idx, self)
        self._rewrite_dialog = dialog
        dialog.start_requested.connect(self._start_rewrite)
        dialog.preview_requested.connect(self._start_rewrite)
        dialog.undo_requested.connect(self._undo_rewrite)
        dialog.applied.connect(self._on_replacements_applied)  # same refresh either way
        try:
            dialog.exec()
        finally:
            self._rewrite_dialog = None

    def _start_rewrite(self, params: dict) -> None:
        """Run a rewrite batch, or a one-chapter preview when `dry_run` is set.

        One worker for both: a preview that took a different path would prove nothing
        about the hours-long run it exists to justify.
        """
        if self.project is None or self._busy():
            return
        engine = params.get("engine_name", "")
        dry_run = bool(params.get("dry_run"))
        worker = RewriteWorker(
            self.project.path,
            engine,
            params.get("target_lang", "vi"),
            api_key=self.config.claude_api_key,
            model=params.get("model", ""),
            cli_command=self.config.cli_command_for(engine),
            base_url=self.config.lmstudio_url if engine == "lmstudio" else "",
            indices=params.get("indices"),
            start_idx=params.get("start_idx", 0),
            end_idx=params.get("end_idx"),
            force=bool(params.get("force")),
            dry_run=dry_run,
        )
        self._rewrite_worker = worker
        worker.failed.connect(self._on_rewrite_failed)

        if dry_run:
            # Writes nothing, so it stays out of the progress bar, the job registry and
            # the wake-lock; its results go straight back to the dialog that asked.
            dialog = self._rewrite_dialog
            if dialog is not None:
                worker.preview_ready.connect(dialog.show_preview)
                worker.chapter_error.connect(dialog.show_preview_error)
                worker.finished_ok.connect(lambda *_: dialog.preview_finished())
            worker.start()
            return

        total = len(
            chapters_to_rewrite(
                self.project,
                params.get("target_lang", "vi"),
                indices=params.get("indices"),
                start_idx=params.get("start_idx", 0),
                end_idx=params.get("end_idx"),
                force=bool(params.get("force")),
            )
        )
        self._progress_verb = "Đang viết lại"
        self._close_find_replace()
        self.translate_button.setEnabled(False)
        self.retranslate_button.setEnabled(False)
        self.find_replace_button.setEnabled(False)
        self.rewrite_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.picker.setEnabled(False)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(0)

        worker.progress.connect(self._on_progress)
        worker.chapter_done.connect(self._on_chapter_updated)
        worker.chapter_error.connect(lambda idx, _msg: self._on_chapter_updated(idx))
        worker.finished_ok.connect(self._on_rewrite_finished)
        track_worker(worker)  # a whole novel is hours — don't let the Mac sleep
        self._job = job_registry.register(worker, kind="Viết lại", novel=self._job_novel())
        self.pause_button.set_job(self._job.id if self._job else None)
        worker.start()

    def _rewrite_rows(self, indices: list[int]) -> None:
        """Rewrite exactly these chapters (the table's right-click menu)."""
        if not indices or self.project is None:
            return
        if self._busy():
            self.status_label.setText(self._busy_message())
            return
        self._save_preview_edits()
        self._start_rewrite(
            {
                "engine_name": self.config.rewrite_ai_engine,
                "model": self.config.rewrite_ai_model,
                "target_lang": "vi",
                "indices": indices,
            }
        )

    def _undo_rewrite(self, idx) -> None:
        """Undo a rewrite: `idx` is a chapter index, or None for the whole novel."""
        if self.project is None:
            return
        restored = self.project.restore_translation(idx)
        if not restored:
            return
        self.model.set_chapters(self.project.chapters())
        if self._preview_idx is not None:
            self._load_preview(self.project.chapter(self._preview_idx))
        self.status_label.setText(f"Đã hoàn tác viết lại {restored} chương.")

    def _undo_rewrite_rows(self, indices: list[int]) -> None:
        for index in indices:
            self._undo_rewrite(index)

    def _table_context_actions(self, menu, index) -> None:
        """Per-chapter "Viết lại" / "Hoàn tác" on the table's right-click menu.

        Built from the *selection* rather than the row under the cursor, so one
        right-click can act on everything highlighted; `enable_cell_copy` keeps a
        multi-row selection alive when the click lands inside it.
        """
        if self.project is None:
            return
        selection = self.table.selectionModel()
        rows = sorted({i.row() for i in selection.selectedIndexes()}) if selection else []
        if index.isValid() and index.row() not in rows:
            rows = [index.row()]  # right-clicked away from the selection → act on that row
        chapters = [c for c in (self.model.chapter_at(row) for row in rows) if c is not None]
        rewritable = [c.index for c in chapters if c.translated]
        undoable = [c.index for c in chapters if c.is_rewritten]
        if not rewritable and not undoable:
            return  # nothing translated here — offering it would only mislead

        menu.addSeparator()
        busy = self._busy()
        if rewritable:
            label = (
                "✍️ Viết lại chương này"
                if len(rewritable) == 1
                else f"✍️ Viết lại {len(rewritable)} chương"
            )
            action = menu.addAction(label)
            action.setEnabled(not busy)
            action.triggered.connect(lambda _=False, idxs=rewritable: self._rewrite_rows(idxs))
        if undoable:
            label = (
                "↩︎ Hoàn tác viết lại"
                if len(undoable) == 1
                else f"↩︎ Hoàn tác viết lại {len(undoable)} chương"
            )
            action = menu.addAction(label)
            action.setEnabled(not busy)
            action.triggered.connect(
                lambda _=False, idxs=undoable: self._undo_rewrite_rows(idxs)
            )
        if busy:
            menu.setToolTipsVisible(True)  # QMenu hides action tooltips unless asked

    def _on_rewrite_failed(self, message: str) -> None:
        self._reset_buttons()
        if self._rewrite_dialog is not None:
            self._rewrite_dialog.preview_finished()
        QMessageBox.warning(self, "Không viết lại được", message)

    def _on_rewrite_finished(self, ok: int, errors: int) -> None:
        self._reset_buttons()
        message = f"Viết lại xong: {ok} chương"
        if errors:
            message += (
                f", {errors} chương giữ nguyên bản dịch cũ vì bản viết lại không đạt "
                "(xem cột Lỗi)"
            )
        self.status_label.setText(message + ".")

    # -------------------------------------------------------------- lifecycle

    def _busy_message(self) -> str:
        """Which run is in the way. Naming it beats a generic "something is running":
        the two batches take very different amounts of time to finish."""
        if self._rewrite_worker is not None and self._rewrite_worker.isRunning():
            return "Đang có phiên viết lại chạy — chờ xong rồi thử lại."
        return "Đang có phiên dịch chạy — chờ xong rồi thử lại."

    def _busy(self) -> bool:
        """True while either batch is running.

        The two must never overlap: they write the same rows, and a rewrite reads
        `translated` while a translation is replacing it.
        """
        return any(
            worker is not None and worker.isRunning()
            for worker in (self._worker, self._rewrite_worker)
        )

    def _cancel(self) -> None:
        cancelled = False
        for worker in (self._worker, self._rewrite_worker):
            if worker is not None:
                worker.cancel()
                cancelled = True
        if cancelled:
            self.status_label.setText("Đang dừng sau chương hiện tại…")

    def _on_progress(self, done: int, total: int, title: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if title:
            self.status_label.setText(f"{self._progress_verb}: {title}")

    def _on_chapter_updated(self, idx: int) -> None:
        if self.project is None:
            return
        chapter = self.project.chapter(idx)
        if chapter is not None:
            self.model.update_chapter(chapter)
            if idx == self._preview_idx:  # fresh translation replaces the stale pane
                self._load_preview(chapter)

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
        self.find_replace_button.setEnabled(True)
        self.rewrite_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.picker.setEnabled(True)

    def _job_novel(self) -> str:
        """The novel label for the menu-bar job row — this tab's own project.

        Deliberately not Workspace.current_title(): each tab has an independent picker,
        so an audio job on a different novel would be labelled with the scrape tab's.
        """
        return self.project.meta.display_name() if self.project is not None else ""

    def has_running_workers(self) -> bool:
        # Only the batch runs are user-meaningful work worth a close-confirm; the
        # models-list fetch is a short background metadata call (shutdown still joins it).
        return self._busy()

    def shutdown(self) -> None:
        self._save_preview_edits()
        self._save_original_edits()
        for worker in (self._worker, self._rewrite_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(60_000)
        if self._models_worker is not None and self._models_worker.isRunning():
            self._models_worker.wait(20_000)  # bounded by the 15s subprocess timeout

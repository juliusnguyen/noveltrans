"""Tab 5 — Video: render per-chapter audio into music-player videos, with auto-generated
title / description / thumbnail / tags for each part.

Split out of the audio tab (feature 025): it owns its own project picker, voice selector,
and status/progress/cancel widgets. When exporting, each produced part-video gets, written
next to the `.mp4`:
  * `<name>.title.txt`  — "{tên truyện} - Phần {N}"
  * `<name>.txt`        — the YouTube description (original+VN title/author, chapter count,
                           the chapter timestamp table, "Tạo bởi: …")
  * `<name>.tags.txt`   — the novel-level YouTube tags (LLM-generated, like "2. Dịch")
  * `<name>.jpg`        — a thumbnail composited from a chosen base image + styled text
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig, translator_labels
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import ProjectPicker
from noveltrans.gui.workers import (
    CompletionWorker,
    TagsWorker,
    TtsVoicesWorker,
    VideoPreviewWorker,
    VideoWorker,
)
from noveltrans.storage import NovelProject

# Engines that can generate tags (LLMs). Google translate-only is excluded.
_TAG_ENGINES = ("cli", "claude_cli", "claude", "lmstudio")
_IMAGE_FILTER = "Ảnh (*.png *.jpg *.jpeg *.webp *.bmp)"


class VideoTab(QWidget):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project: NovelProject | None = None
        self._video_worker: VideoWorker | None = None
        self._preview_worker: VideoPreviewWorker | None = None
        self._voices_worker: TtsVoicesWorker | None = None
        self._tags_worker: TagsWorker | None = None
        self._image_prompt_worker: CompletionWorker | None = None
        self._render_after_tags = False  # auto-generate tags, then start the render
        # a persistent, non-modal preview window so the color can be tuned live
        self._preview_dialog: QDialog | None = None
        self._preview_label: QLabel | None = None
        self._preview_status: QLabel | None = None
        self._preview_color_button: QPushButton | None = None
        self._preview_controls: list = []

        # --- top row: novel + voice
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._on_project_selected)

        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(200)
        self.voice_combo.setToolTip("Giọng đọc của audio dùng để tạo video.")
        self._load_voices()

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Truyện:"))
        top_row.addWidget(self.picker, stretch=1)
        top_row.addWidget(QLabel("Giọng đọc:"))
        top_row.addWidget(self.voice_combo)

        self.status_label = QLabel("")
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m video")

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addLayout(self._build_engine_row())  # one AI engine for tags + image prompt
        layout.addWidget(self._build_video_box())
        layout.addWidget(self._build_video_list_box(), stretch=1)
        layout.addWidget(self._build_thumbnail_box())
        layout.addWidget(self._build_image_prompt_box())
        layout.addWidget(self._build_tags_box())
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)

    # ---------------------------------------------------------------- boxes

    def _build_video_box(self) -> QGroupBox:
        """The 'Xuất video' controls: mode + quality + font + background image + buttons."""
        from noveltrans.tts.convert import ffmpeg_available
        from noveltrans.tts.video import VIDEO_FONTS

        self.video_mode = QComboBox()
        self.video_mode.addItem("Toàn bộ", "all")
        self.video_mode.addItem("Từ chương … đến …", "range")
        self.video_mode.addItem("Theo lô", "batch")
        self.video_mode.setCurrentIndex(self.video_mode.findData("batch"))  # sane default
        self.video_mode.currentIndexChanged.connect(self._on_video_mode_changed)

        self.video_quality = QComboBox()
        self.video_quality.addItem("Cao — 1080p", "high")
        self.video_quality.addItem("Cao — 1080p, không đĩa xoay", "high_static")
        self.video_quality.addItem("Nhanh — 720p", "fast")
        self.video_quality.addItem("Nhanh nhất — 720p, không đĩa xoay", "fastest")
        self.video_quality.setToolTip(
            "Cao: 1080p, đẹp nhất, chậm nhất.\n"
            "Cao, không đĩa xoay: 1080p nhưng bỏ đĩa xoay → nhanh hơn đáng kể.\n"
            "Nhanh: 720p.\n"
            "Nhanh nhất: 720p + 15fps + không đĩa xoay."
        )
        idx = self.video_quality.findData(self.config.video_quality)
        self.video_quality.setCurrentIndex(idx if idx >= 0 else 0)
        self.video_quality.currentIndexChanged.connect(self._on_video_quality_changed)

        self.video_font = QComboBox()
        for key, spec in VIDEO_FONTS.items():
            self.video_font.addItem(spec["label"], key)
        fidx = self.video_font.findData(self.config.video_font)
        self.video_font.setCurrentIndex(fidx if fidx >= 0 else 0)
        self.video_font.setToolTip("Phông chữ cho tên truyện/chương trong video và ảnh bìa.")
        self.video_font.currentIndexChanged.connect(self._on_video_font_changed)

        self.video_range_from = QSpinBox()
        self.video_range_from.setRange(1, 999999)
        self.video_range_to = QSpinBox()
        self.video_range_to.setRange(1, 999999)
        self.video_range_label = QLabel("→")
        self.video_batch_size = QSpinBox()
        self.video_batch_size.setRange(1, 999999)
        self.video_batch_size.setValue(10)
        self.video_batch_label = QLabel("chương/video")

        # background color for the player skin ("" = the default pastel gradient)
        self.bg_color = self.config.video_bg_color
        self.bg_color_button = QPushButton("Chọn màu…")
        self.bg_color_button.setToolTip("Màu nền cho khung trình phát (để trống = màu mặc định).")
        self.bg_color_button.clicked.connect(self._pick_bg_color)
        self.bg_reset_button = QPushButton("Mặc định")
        self.bg_reset_button.setToolTip("Dùng lại màu nền mặc định (gradient pastel).")
        self.bg_reset_button.clicked.connect(self._reset_bg_color)

        self.video_image_edit = QLineEdit(self.config.video_image_path)
        self.video_image_edit.setPlaceholderText("Ảnh nền cho video…")
        self.video_image_edit.setReadOnly(True)
        self.video_image_edit.setMinimumWidth(180)
        self.video_image_button = QPushButton("Chọn ảnh…")
        self.video_image_button.clicked.connect(self._pick_video_image)

        self.video_preview_button = QPushButton("Xem trước")
        self.video_preview_button.clicked.connect(self._start_preview)
        self.video_button = QPushButton("Tạo video")
        self.video_button.setProperty("primary", True)
        self.video_button.clicked.connect(self._start_video)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        if not ffmpeg_available():
            for b in (self.video_button, self.video_preview_button):
                b.setEnabled(False)
                b.setToolTip("Cần ffmpeg để tạo video (brew install ffmpeg).")
        self.open_video_dir_button = QPushButton("Mở thư mục video")
        self.open_video_dir_button.clicked.connect(self._open_video_dir)

        row = QHBoxLayout()
        row.addWidget(QLabel("Chế độ:"))
        row.addWidget(self.video_mode)
        row.addWidget(self.video_range_from)
        row.addWidget(self.video_range_label)
        row.addWidget(self.video_range_to)
        row.addWidget(self.video_batch_size)
        row.addWidget(self.video_batch_label)
        row.addWidget(QLabel("Chất lượng:"))
        row.addWidget(self.video_quality)
        row.addWidget(QLabel("Phông chữ:"))
        row.addWidget(self.video_font)
        row.addWidget(QLabel("Màu nền:"))
        row.addWidget(self.bg_color_button)
        row.addWidget(self.bg_reset_button)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Ảnh nền:"))
        row2.addWidget(self.video_image_edit, stretch=1)
        row2.addWidget(self.video_image_button)
        row2.addWidget(self.video_preview_button)
        row2.addWidget(self.video_button)
        row2.addWidget(self.cancel_button)
        row2.addWidget(self.open_video_dir_button)

        inner = QVBoxLayout()
        inner.addLayout(row)
        inner.addLayout(row2)
        box = QGroupBox("Xuất video (trình phát nhạc: ảnh + cột sóng + tên chương)")
        box.setLayout(inner)
        self._on_video_mode_changed()  # set initial visibility
        self._update_bg_swatch()
        return box

    def _build_video_list_box(self) -> QGroupBox:
        """A table of the planned part-videos + their created/not-created status.

        Mirrors the audio tab's chapter table: each row is one part, with a per-row
        "Tạo"/"Tạo lại" button, so the user can render only the missing parts.
        """
        self.video_list = QTableWidget(0, 6)
        self.video_list.setHorizontalHeaderLabels(
            ["Phần", "Chương", "Thời lượng", "Tiêu đề", "Trạng thái", "Thao tác"]
        )
        self.video_list.verticalHeader().setVisible(False)
        self.video_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.video_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.video_list.setAlternatingRowColors(True)
        header = self.video_list.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # thời lượng
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # tiêu đề
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        # ResizeToContents ignores cell *widgets*, so the action column must be sized
        # explicitly or the three buttons get crushed unreadably narrow.
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.video_list.setColumnWidth(5, 250)
        self.video_list.verticalHeader().setDefaultSectionSize(34)

        # refresh the list whenever the selection that defines the parts changes
        self.voice_combo.currentIndexChanged.connect(self._refresh_video_list)
        self.video_mode.currentIndexChanged.connect(self._refresh_video_list)
        self.video_batch_size.valueChanged.connect(self._refresh_video_list)
        self.video_range_from.valueChanged.connect(self._refresh_video_list)
        self.video_range_to.valueChanged.connect(self._refresh_video_list)

        box = QGroupBox("Danh sách phần video (theo lô) — trạng thái & tạo tiếp phần còn thiếu")
        inner = QVBoxLayout()
        inner.addWidget(self.video_list)
        box.setLayout(inner)
        return box

    def _windows_for_current_selection(self) -> list:
        """The parts (`MergeWindow`s) implied by the current voice/mode/range/batch."""
        if self.project is None:
            return []
        from noveltrans.tts.merge import plan_merge_windows

        voice = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
        mode = self.video_mode.currentData()
        start = self.video_range_from.value() if mode == "range" else None
        end = self.video_range_to.value() if mode == "range" else None
        batch = self.video_batch_size.value() if mode == "batch" else None
        if mode == "range" and start and end and start > end:
            return []
        return plan_merge_windows(
            self.project.chapters(), voice, mode, start=start, end=end, batch=batch
        )

    def _part_output_path(self, window, *, whole_novel: bool):
        """The .mp4 path a given window would render to (for the exists check)."""
        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        slug = slugify(self.project.meta.translated_title or self.project.meta.title)
        name = video_part_name(
            slug, window.first_num, window.last_num, whole_novel=whole_novel
        )
        return self.project.video_dir / name

    def _part_sidecar(self, window, whole_novel: bool, ext: str):
        """Path of a companion file (`.title.txt` / `.txt` / `.tags.txt` / `.jpg`) for a part."""
        out = self._part_output_path(window, whole_novel=whole_novel)
        return out.parent / (out.stem + ext)

    def _part_title(self, part_num) -> str:
        from noveltrans.tts.video import build_upload_title

        novel_title = self.project.meta.translated_title or self.project.meta.title
        return build_upload_title(novel_title, part_num)

    @staticmethod
    def _format_hms(seconds: float) -> str:
        """Format a duration as `H:MM:SS` (or `M:SS` under an hour)."""
        total = int(round(seconds))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # YouTube caps a single video at 12 hours.
    _YOUTUBE_MAX_SECONDS = 12 * 3600

    def _duration_item(self, window) -> QTableWidgetItem:
        """A table item with the part's total audio duration, flagged red past YouTube's 12h."""
        seconds = sum(c.audio_seconds for c in window.chapters)
        item = QTableWidgetItem(self._format_hms(seconds))
        if seconds > self._YOUTUBE_MAX_SECONDS:
            item.setForeground(QColor("#e06c75"))
            item.setText("⚠️ " + item.text())
            item.setToolTip("Vượt 12 giờ — YouTube giới hạn 12h/video. Nên chia lô nhỏ hơn.")
        return item

    def _refresh_video_list(self) -> None:
        """Rebuild the parts table with each part's title + created/not-created status."""
        if not hasattr(self, "video_list"):
            return
        self.video_list.setRowCount(0)
        if self.project is None:
            return
        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        total = len(windows)
        self.video_list.setRowCount(total)
        for i, window in enumerate(windows):
            whole_novel = total == 1 and mode == "all"
            part_num = None if whole_novel else (i + 1)
            exists = self._part_output_path(window, whole_novel=whole_novel).is_file()
            label = "Toàn bộ" if whole_novel else f"Phần {i + 1}"
            self.video_list.setItem(i, 0, QTableWidgetItem(label))
            self.video_list.setItem(
                i, 1,
                QTableWidgetItem(
                    f"chương {window.first_num}–{window.last_num} "
                    f"({len(window.chapters)} chương)"
                ),
            )
            self.video_list.setItem(i, 2, self._duration_item(window))
            self.video_list.setItem(i, 3, QTableWidgetItem(self._part_title(part_num)))
            self.video_list.setItem(
                i, 4, QTableWidgetItem("✅ Đã tạo" if exists else "⬜ Chưa tạo")
            )
            self.video_list.setCellWidget(
                i, 5, self._build_row_actions(window, part_num, whole_novel, exists)
            )

    def _build_row_actions(self, window, part_num, whole_novel, exists):
        """The per-row action buttons: (re)render, copyable detail, open thumbnail."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)

        make = QPushButton("Tạo lại" if exists else "Tạo")
        make.clicked.connect(lambda _=False, w=window: self._render_one(w))
        detail = QPushButton("Chi tiết")
        detail.setToolTip("Xem và copy tiêu đề, mô tả, tags để dán lên YouTube.")
        detail.clicked.connect(
            lambda _=False, w=window, pn=part_num, wn=whole_novel: self._show_part_detail(w, pn, wn)
        )
        thumb = QPushButton("Ảnh bìa")
        thumb.setToolTip("Mở ảnh bìa (thumbnail) đã tạo.")
        thumb.setEnabled(self._part_sidecar(window, whole_novel, ".jpg").is_file())
        thumb.clicked.connect(
            lambda _=False, w=window, wn=whole_novel: self._open_part_thumbnail(w, wn)
        )
        # compact padding + a sensible min width so the labels never truncate
        for b, min_w in ((make, 58), (detail, 66), (thumb, 66)):
            b.setStyleSheet("padding: 3px 8px;")
            b.setMinimumWidth(min_w)
            row.addWidget(b)
        return container

    def _compute_part_description(self, window, novel_title: str) -> str:
        """Build a part's description on the fly (before it's rendered) from stored audio."""
        from noveltrans.tts.merge import MergeSegment, chapter_marker_title
        from noveltrans.tts.video import build_video_description

        segments = [
            MergeSegment(path="", seconds=c.audio_seconds, title=chapter_marker_title(c))
            for c in window.chapters
        ]
        return build_video_description(
            segments,
            original_title=self.project.meta.title,
            vn_title=novel_title,
            original_author=self.project.meta.author,
            vn_author=self.project.meta.translated_author,
            total_chapters=self.project.counts()["total"],
            credit=self.credit_edit.text().strip() or "Fox Novel",
        )

    def _part_metadata(self, window, part_num, whole_novel) -> tuple[str, str, str]:
        """(title, description, tags) for a part — from the rendered sidecars if they exist,
        otherwise computed so the text is available even before the video is made."""
        novel_title = self.project.meta.translated_title or self.project.meta.title
        title_p = self._part_sidecar(window, whole_novel, ".title.txt")
        desc_p = self._part_sidecar(window, whole_novel, ".txt")
        tags_p = self._part_sidecar(window, whole_novel, ".tags.txt")

        title = (
            title_p.read_text(encoding="utf-8").strip()
            if title_p.is_file() else self._part_title(part_num)
        )
        description = (
            desc_p.read_text(encoding="utf-8")
            if desc_p.is_file() else self._compute_part_description(window, novel_title)
        )
        tags = (
            tags_p.read_text(encoding="utf-8").strip()
            if tags_p.is_file() else self.tags_edit.toPlainText().strip()
        )
        return title, description, tags

    def _show_part_detail(self, window, part_num, whole_novel) -> None:
        """A dialog with the part's title / description / tags (each copyable) + open buttons."""
        if self.project is None:
            return
        title, description, tags = self._part_metadata(window, part_num, whole_novel)

        dialog = QDialog(self)
        dialog.setWindowTitle("Chi tiết phần — copy để đăng YouTube")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        status = QLabel("")

        def copy(text: str, what: str) -> None:
            QApplication.clipboard().setText(text)
            status.setText(f"✅ Đã copy {what}.")

        title_edit = QLineEdit(title)
        title_edit.setReadOnly(True)
        title_copy = QPushButton("Copy")
        title_copy.clicked.connect(lambda: copy(title, "tiêu đề"))
        title_row = QHBoxLayout()
        title_row.addWidget(title_edit, 1)
        title_row.addWidget(title_copy)
        layout.addWidget(QLabel("Tiêu đề:"))
        layout.addLayout(title_row)

        desc_edit = QPlainTextEdit(description)
        desc_edit.setReadOnly(True)
        desc_copy = QPushButton("Copy mô tả")
        desc_copy.clicked.connect(lambda: copy(description, "mô tả"))
        layout.addWidget(QLabel("Mô tả:"))
        layout.addWidget(desc_edit, 1)
        layout.addWidget(desc_copy)

        tags_edit = QPlainTextEdit(tags)
        tags_edit.setReadOnly(True)
        tags_edit.setMaximumHeight(70)
        tags_copy = QPushButton("Copy tags")
        tags_copy.clicked.connect(lambda: copy(tags, "tags"))
        layout.addWidget(QLabel("Tags:"))
        layout.addWidget(tags_edit)
        layout.addWidget(tags_copy)

        open_thumb = QPushButton("Mở ảnh bìa")
        open_thumb.setEnabled(self._part_sidecar(window, whole_novel, ".jpg").is_file())
        open_thumb.clicked.connect(lambda: self._open_part_thumbnail(window, whole_novel))
        open_dir = QPushButton("Mở thư mục video")
        open_dir.clicked.connect(self._open_video_dir)
        close = QPushButton("Đóng")
        close.clicked.connect(dialog.close)
        bottom = QHBoxLayout()
        bottom.addWidget(open_thumb)
        bottom.addWidget(open_dir)
        bottom.addWidget(status)
        bottom.addStretch()
        bottom.addWidget(close)
        layout.addLayout(bottom)

        dialog.exec()

    def _open_part_thumbnail(self, window, whole_novel) -> None:
        thumb = self._part_sidecar(window, whole_novel, ".jpg")
        if thumb.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(thumb)))
        else:
            QMessageBox.information(
                self, "Chưa có ảnh bìa",
                "Phần này chưa được tạo video nên chưa có ảnh bìa — bấm “Tạo” trước.",
            )

    def _build_thumbnail_box(self) -> QGroupBox:
        """Thumbnail base image + tagline + credit for the auto-generated metadata."""
        self.thumb_image_edit = QLineEdit(self.config.video_thumbnail_image)
        self.thumb_image_edit.setPlaceholderText("Dùng chung ảnh nền video nếu để trống…")
        self.thumb_image_edit.setReadOnly(True)
        self.thumb_image_edit.setMinimumWidth(160)
        self.thumb_image_button = QPushButton("Chọn ảnh bìa…")
        self.thumb_image_button.clicked.connect(self._pick_thumb_image)

        self.tagline_edit = QLineEdit(self.config.video_tagline)
        self.tagline_edit.setPlaceholderText("Câu tagline dưới 'PHẦN N' (tuỳ chọn)…")
        self.tagline_edit.editingFinished.connect(
            lambda: setattr(self.config, "video_tagline", self.tagline_edit.text())
        )

        self.credit_edit = QLineEdit(self.config.video_credit)
        self.credit_edit.setPlaceholderText("Fox Novel")
        self.credit_edit.setMaximumWidth(140)
        self.credit_edit.editingFinished.connect(
            lambda: setattr(self.config, "video_credit", self.credit_edit.text().strip() or "Fox Novel")
        )

        row = QHBoxLayout()
        row.addWidget(QLabel("Ảnh bìa:"))
        row.addWidget(self.thumb_image_edit, stretch=1)
        row.addWidget(self.thumb_image_button)
        row.addWidget(QLabel("Tagline:"))
        row.addWidget(self.tagline_edit, stretch=1)
        row.addWidget(QLabel("Tạo bởi:"))
        row.addWidget(self.credit_edit)

        box = QGroupBox("Ảnh bìa (thumbnail) & metadata")
        box.setLayout(row)
        return box

    def _build_engine_row(self) -> QHBoxLayout:
        """One LLM engine + model picker shared by all AI helpers (tags, image prompt)."""
        self.ai_engine_combo = QComboBox()
        for key, label in translator_labels(self.config).items():
            if key in _TAG_ENGINES:
                self.ai_engine_combo.addItem(label, key)
        eidx = self.ai_engine_combo.findData(self.config.video_ai_engine)
        self.ai_engine_combo.setCurrentIndex(eidx if eidx >= 0 else 0)
        self.ai_engine_combo.setToolTip(
            "Engine LLM dùng chung cho mọi tính năng AI của tab (tags, prompt ảnh bìa) — "
            "giống “2. Dịch”. Google chỉ dịch nên không dùng được ở đây."
        )
        self.ai_engine_combo.currentIndexChanged.connect(
            lambda: setattr(self.config, "video_ai_engine", self.ai_engine_combo.currentData())
        )

        self.ai_model_edit = QLineEdit(self.config.video_ai_model)
        self.ai_model_edit.setPlaceholderText("model (để trống = mặc định)")
        self.ai_model_edit.setMaximumWidth(220)
        self.ai_model_edit.editingFinished.connect(
            lambda: setattr(self.config, "video_ai_model", self.ai_model_edit.text().strip())
        )

        row = QHBoxLayout()
        row.addWidget(QLabel("Engine AI:"))
        row.addWidget(self.ai_engine_combo)
        row.addWidget(QLabel("Model:"))
        row.addWidget(self.ai_model_edit)
        row.addStretch()
        return row

    def _build_image_prompt_box(self) -> QGroupBox:
        """'Tạo prompt' button + editable AI image-generation prompt for the thumbnail."""
        self.image_prompt_button = QPushButton("Tạo prompt")
        self.image_prompt_button.setToolTip(
            "Sinh prompt (tiếng Anh) để tạo ảnh bìa bằng AI (Midjourney/SD/DALL·E), "
            "khớp nội dung truyện."
        )
        self.image_prompt_button.clicked.connect(self._generate_image_prompt)

        self.image_prompt_save_button = QPushButton("Lưu")
        self.image_prompt_save_button.clicked.connect(self._save_image_prompt)

        self.image_prompt_edit = QPlainTextEdit()
        self.image_prompt_edit.setPlaceholderText(
            "Prompt tạo ảnh bìa bằng AI — bấm 'Tạo prompt' để sinh tự động theo nội dung "
            "truyện, rồi dán vào Midjourney / Stable Diffusion / DALL·E để tạo ảnh nền."
        )
        self.image_prompt_edit.setMaximumHeight(90)

        row = QHBoxLayout()
        row.addWidget(self.image_prompt_button)
        row.addWidget(self.image_prompt_save_button)
        row.addStretch()

        inner = QVBoxLayout()
        inner.addLayout(row)
        inner.addWidget(self.image_prompt_edit)
        box = QGroupBox("Prompt tạo ảnh bìa (AI) — khớp nội dung truyện")
        box.setLayout(inner)
        return box

    def _build_tags_box(self) -> QGroupBox:
        """'Tạo tags' button + an editable tag list (uses the shared AI engine)."""
        self.tags_button = QPushButton("Tạo tags")
        self.tags_button.setToolTip("Sinh tags YouTube bằng LLM (engine chọn ở trên).")
        self.tags_button.clicked.connect(lambda: self._generate_tags(then_render=False))

        self.tags_edit = QPlainTextEdit()
        self.tags_edit.setPlaceholderText(
            "Tags YouTube (phân tách bằng dấu phẩy) — bấm 'Tạo tags' để sinh tự động, "
            "hoặc tự nhập/sửa. Lưu vào truyện khi bấm 'Lưu tags'."
        )
        self.tags_edit.setMaximumHeight(90)

        self.tags_save_button = QPushButton("Lưu tags")
        self.tags_save_button.clicked.connect(self._save_tags)

        button_row = QHBoxLayout()
        button_row.addWidget(self.tags_button)
        button_row.addWidget(self.tags_save_button)
        button_row.addStretch()

        inner = QVBoxLayout()
        inner.addLayout(button_row)
        inner.addWidget(self.tags_edit)
        box = QGroupBox("Tags (YouTube) — sinh bằng LLM như “2. Dịch”")
        box.setLayout(inner)
        return box

    # ---------------------------------------------------------- mode/config

    def _on_video_mode_changed(self) -> None:
        mode = self.video_mode.currentData()
        for w in (self.video_range_from, self.video_range_label, self.video_range_to):
            w.setVisible(mode == "range")
        for w in (self.video_batch_size, self.video_batch_label):
            w.setVisible(mode == "batch")

    def _on_video_quality_changed(self) -> None:
        self.config.video_quality = self.video_quality.currentData()

    def _on_video_font_changed(self) -> None:
        self.config.video_font = self.video_font.currentData()

    def _pick_video_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn ảnh nền", self.config.video_image_path or "", _IMAGE_FILTER
        )
        if path:
            self.video_image_edit.setText(path)
            self.config.video_image_path = path

    def _pick_bg_color(self) -> None:
        initial = QColor(self.bg_color) if self.bg_color else QColor("#e9d5ff")
        color = QColorDialog.getColor(initial, self, "Chọn màu nền video")
        if color.isValid():
            self.bg_color = color.name()  # "#rrggbb"
            self.config.video_bg_color = self.bg_color
            self._update_bg_swatch()
            self._maybe_refresh_preview()  # live-update the open preview, if any

    def _reset_bg_color(self) -> None:
        self.bg_color = ""
        self.config.video_bg_color = ""
        self._update_bg_swatch()
        self._maybe_refresh_preview()

    def _style_color_button(self, button: QPushButton, default_text: str) -> None:
        """Paint `button` with the current bg color as a swatch (or a neutral default)."""
        if self.bg_color:
            c = QColor(self.bg_color)
            lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            fg = "#000000" if lum > 140 else "#ffffff"
            button.setStyleSheet(f"background-color: {self.bg_color}; color: {fg};")
            button.setText(self.bg_color)
        else:
            button.setStyleSheet("")
            button.setText(default_text)

    def _update_bg_swatch(self) -> None:
        self._style_color_button(self.bg_color_button, "Chọn màu…")
        if self._preview_color_button is not None:
            self._style_color_button(self._preview_color_button, "Đổi màu nền…")

    def _maybe_refresh_preview(self) -> None:
        """Re-render the preview in place if the (non-modal) preview window is open."""
        if self._preview_dialog is not None and self._preview_dialog.isVisible():
            self._start_preview()

    def _pick_thumb_image(self) -> None:
        start = self.config.video_thumbnail_image or self.config.video_image_path or ""
        path, _ = QFileDialog.getOpenFileName(self, "Chọn ảnh bìa", start, _IMAGE_FILTER)
        if path:
            self.thumb_image_edit.setText(path)
            self.config.video_thumbnail_image = path

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
            label = re.sub(r"\s*·\s*Phong cách.*$", "", label)
            self.voice_combo.addItem(label, voice_id)
        index = self.voice_combo.findData(saved)
        self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        self.voice_combo.blockSignals(False)

    # -------------------------------------------------------------- projects

    def refresh_projects(self, select_path: str = "") -> None:
        self.picker.refresh(self.config.library_dir, select_path)

    def showEvent(self, event) -> None:
        if not self.has_running_workers():
            self.refresh_projects()
        super().showEvent(event)

    def _on_project_selected(self, path: str) -> None:
        if self.project is not None:
            self.project.close()
            self.project = None
        if path:
            self.project = NovelProject.open(path)
            total = self.project.counts()["total"]
            self.video_range_to.setValue(max(total, 1))
            self.video_range_from.setValue(1)
            self.tags_edit.setPlainText(self.project.meta.tags)
            self.image_prompt_edit.setPlainText(self.project.meta.thumbnail_prompt)
            self._update_status_line()
        else:
            self.tags_edit.setPlainText("")
            self.image_prompt_edit.setPlainText("")
            self.status_label.setText("")
        self._refresh_video_list()

    def _update_status_line(self) -> None:
        if self.project is None:
            return
        counts = self.project.counts()
        self.status_label.setText(
            f"{counts['audio']}/{counts['total']} chương đã có audio."
        )

    # ----------------------------------------------------------------- tags

    def _ai_engine_params(self) -> dict:
        """Engine params for the shared AI helpers (from the top engine+model picker)."""
        engine = self.ai_engine_combo.currentData()
        model = self.ai_model_edit.text().strip()
        if not model:
            if engine == "claude":
                model = self.config.claude_model
            elif engine in ("cli", "claude_cli"):
                model = self.config.cli_model_for(engine)
        return {
            "engine_name": engine,
            "api_key": self.config.claude_api_key,
            "model": model,
            "cli_command": self.config.cli_command_for(engine),
            "base_url": self.config.lmstudio_url,
        }

    def _generate_tags(self, *, then_render: bool) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        if self._tags_worker is not None and self._tags_worker.isRunning():
            return
        self._render_after_tags = then_render
        self.tags_button.setEnabled(False)
        self.status_label.setText("🏷️ Đang tạo tags…")
        self._tags_worker = TagsWorker(self.project.path, **self._ai_engine_params())
        self._tags_worker.finished_ok.connect(self._on_tags_ready)
        self._tags_worker.failed.connect(self._on_tags_failed)
        track_worker(self._tags_worker)
        self._tags_worker.start()

    def _on_tags_ready(self, tags: str) -> None:
        self.tags_button.setEnabled(True)
        self.tags_edit.setPlainText(tags)
        self.status_label.setText("✅ Đã tạo tags.")
        if self._render_after_tags:
            self._render_after_tags = False
            self._launch_video(skip_existing=True)

    def _on_tags_failed(self, message: str) -> None:
        self.tags_button.setEnabled(True)
        if self._render_after_tags:
            # auto-generation before a render failed — proceed without tags
            self._render_after_tags = False
            self.status_label.setText("⚠️ Không tạo được tags — tạo video không kèm tags.")
            self._launch_video()
        else:
            self.status_label.setText("")
            QMessageBox.warning(self, "Tạo tags thất bại", message)

    def _save_tags(self) -> None:
        if self.project is None:
            return
        from noveltrans.tts.tags import format_tags, parse_tags

        tags = format_tags(parse_tags(self.tags_edit.toPlainText()))
        self.project.save_tags(tags)
        self.tags_edit.setPlainText(tags)
        self.status_label.setText("✅ Đã lưu tags.")

    # -------------------------------------------------- thumbnail image prompt

    def _generate_image_prompt(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        if self._image_prompt_worker is not None and self._image_prompt_worker.isRunning():
            return
        from noveltrans.tts.tags import build_thumbnail_image_prompt

        meta = self.project.meta
        prompt = build_thumbnail_image_prompt(
            vn_title=meta.translated_title or meta.title,
            original_title=meta.title,
            vn_description=meta.translated_description,
            tagline=self.tagline_edit.text().strip(),
        )
        self.image_prompt_button.setEnabled(False)
        self.status_label.setText("🎨 Đang tạo prompt ảnh bìa…")
        self._image_prompt_worker = CompletionWorker(prompt=prompt, **self._ai_engine_params())
        self._image_prompt_worker.finished_ok.connect(self._on_image_prompt_ready)
        self._image_prompt_worker.failed.connect(self._on_image_prompt_failed)
        track_worker(self._image_prompt_worker)
        self._image_prompt_worker.start()

    def _on_image_prompt_ready(self, prompt: str) -> None:
        self.image_prompt_button.setEnabled(True)
        self.image_prompt_edit.setPlainText(prompt)
        if self.project is not None:
            self.project.save_thumbnail_prompt(prompt)
        self.status_label.setText("✅ Đã tạo prompt ảnh bìa.")

    def _on_image_prompt_failed(self, message: str) -> None:
        self.image_prompt_button.setEnabled(True)
        self.status_label.setText("")
        QMessageBox.warning(self, "Tạo prompt thất bại", message)

    def _save_image_prompt(self) -> None:
        if self.project is None:
            return
        prompt = self.image_prompt_edit.toPlainText().strip()
        self.project.save_thumbnail_prompt(prompt)
        self.status_label.setText("✅ Đã lưu prompt ảnh bìa.")

    # ---------------------------------------------------------------- preview

    def _start_preview(self) -> None:
        from pathlib import Path

        from noveltrans.tts.video import video_font, video_preset

        image = self.video_image_edit.text().strip()
        if not image or not Path(image).is_file():
            QMessageBox.warning(self, "Chưa chọn ảnh", "Hãy chọn một ảnh nền hợp lệ để xem trước.")
            return
        if self._preview_worker is not None and self._preview_worker.isRunning():
            return
        preset = video_preset(self.video_quality.currentData())
        family = video_font(self.video_font.currentData())["family"]
        novel_title = "Tên truyện"
        if self.project is not None:
            novel_title = self.project.meta.translated_title or self.project.meta.title

        self.video_preview_button.setEnabled(False)
        self.status_label.setText("🖼️ Đang tạo ảnh xem trước…")
        if self._preview_dialog is not None and self._preview_dialog.isVisible():
            self._preview_status.setText("⏳ Đang cập nhật…")
            self._set_preview_controls_enabled(False)
        self._preview_worker = VideoPreviewWorker(
            image, novel_title, "Chương 1: Chương mẫu",
            width=preset["width"], height=preset["height"],
            spin_vinyl=preset["spin_vinyl"], font=family, bg_color=self.bg_color,
        )
        self._preview_worker.done.connect(self._on_preview_ready)
        self._preview_worker.failed.connect(self._on_preview_failed)
        self._preview_worker.start()

    def _build_preview_dialog(self) -> None:
        """Create the persistent, non-modal preview window with live color controls."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Xem trước video — chỉnh màu nền trực tiếp")
        dialog.setModal(False)

        self._preview_label = QLabel()
        self._preview_status = QLabel("")
        self._preview_color_button = QPushButton("Đổi màu nền…")
        self._preview_color_button.setToolTip("Đổi màu nền và cập nhật ngay ảnh xem trước.")
        self._preview_color_button.clicked.connect(self._pick_bg_color)
        reset_button = QPushButton("Mặc định")
        reset_button.clicked.connect(self._reset_bg_color)
        refresh_button = QPushButton("Cập nhật")
        refresh_button.setToolTip("Render lại ảnh xem trước.")
        refresh_button.clicked.connect(self._start_preview)
        close_button = QPushButton("Đóng")
        close_button.clicked.connect(dialog.close)
        self._preview_controls = [self._preview_color_button, reset_button, refresh_button]

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Màu nền:"))
        controls.addWidget(self._preview_color_button)
        controls.addWidget(reset_button)
        controls.addWidget(refresh_button)
        controls.addWidget(self._preview_status)
        controls.addStretch()
        controls.addWidget(close_button)

        layout = QVBoxLayout(dialog)
        layout.addWidget(self._preview_label)
        layout.addLayout(controls)

        dialog.finished.connect(self._on_preview_dialog_closed)
        self._preview_dialog = dialog

    def _set_preview_controls_enabled(self, enabled: bool) -> None:
        for w in self._preview_controls:
            w.setEnabled(enabled)

    def _on_preview_dialog_closed(self, *_args) -> None:
        self._preview_dialog = None
        self._preview_label = None
        self._preview_status = None
        self._preview_color_button = None
        self._preview_controls = []

    def _on_preview_ready(self, png_path: str) -> None:
        self.video_preview_button.setEnabled(True)
        self.status_label.setText("")
        pix = QPixmap(png_path).scaled(
            900, 520, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        if self._preview_dialog is None:
            self._build_preview_dialog()
        self._preview_label.setPixmap(pix)
        self._preview_status.setText("")
        self._set_preview_controls_enabled(True)
        self._update_bg_swatch()  # paints the in-dialog swatch too
        self._preview_dialog.show()
        self._preview_dialog.raise_()
        self._preview_dialog.activateWindow()

    def _on_preview_failed(self, message: str) -> None:
        self.video_preview_button.setEnabled(True)
        self.status_label.setText("")
        if self._preview_dialog is not None and self._preview_dialog.isVisible():
            self._preview_status.setText("")
            self._set_preview_controls_enabled(True)
        QMessageBox.warning(self, "Không tạo được xem trước", message)

    # ------------------------------------------------------------ render video

    def _start_video(self) -> None:
        from pathlib import Path

        from noveltrans.tts.merge import plan_merge_windows
        from noveltrans.tts.video import video_preset

        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        if self._video_worker is not None and self._video_worker.isRunning():
            return
        image = self.video_image_edit.text().strip()
        if not image or not Path(image).is_file():
            QMessageBox.warning(self, "Chưa chọn ảnh", "Hãy chọn một ảnh nền hợp lệ cho video.")
            return
        voice = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
        mode = self.video_mode.currentData()
        start = self.video_range_from.value() if mode == "range" else None
        end = self.video_range_to.value() if mode == "range" else None
        if mode == "range" and start > end:
            QMessageBox.warning(self, "Phạm vi sai", "Chương bắt đầu phải ≤ chương kết thúc.")
            return
        batch = self.video_batch_size.value() if mode == "batch" else None

        windows = plan_merge_windows(
            self.project.chapters(), voice, mode, start=start, end=end, batch=batch
        )
        if not windows:
            QMessageBox.information(
                self, "Chưa có audio",
                f"Không có chương nào có audio giọng {voice} trong phạm vi đã chọn.",
            )
            return

        # skip parts whose .mp4 already exists — only render the missing ones
        whole = len(windows) == 1 and mode == "all"
        pending = [
            w for w in windows
            if not self._part_output_path(w, whole_novel=whole).is_file()
        ]
        existing = len(windows) - len(pending)
        if not pending:
            QMessageBox.information(
                self, "Đã tạo hết",
                "Tất cả phần trong phạm vi này đã có video. Dùng nút “Tạo lại” ở từng "
                "dòng trong danh sách nếu muốn làm lại.",
            )
            return

        preset = video_preset(self.video_quality.currentData())
        n_chapters = sum(len(w.chapters) for w in pending)
        total_secs = sum(c.audio_seconds for w in pending for c in w.chapters)
        hours = total_secs / 3600
        render_hours = hours / preset["speed"]
        est = f"~{render_hours * 60:.0f} phút" if render_hours < 1 else f"~{render_hours:.1f} giờ"
        skip_note = f" (bỏ qua {existing} phần đã có)" if existing else ""
        answer = QMessageBox.question(
            self, "Tạo video",
            f"Sẽ tạo {len(pending)} video{skip_note} từ {n_chapters} chương "
            f"(giọng {voice}), tổng ~{hours:.1f} giờ audio.\n\n"
            f"Chất lượng: {self.video_quality.currentText()} "
            f"({preset['width']}×{preset['height']}).\n"
            f"Ước tính thời gian render: {est} (chưa tính máy nóng/tải khác).\n\n"
            f"Mỗi video sẽ kèm tiêu đề, mô tả, ảnh bìa và tags. Tiếp tục?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # auto-generate tags once if the novel has none yet, then render (skipping existing)
        if not self.tags_edit.toPlainText().strip():
            self._generate_tags(then_render=True)
        else:
            self._launch_video(skip_existing=True)

    def _render_one(self, window) -> None:
        """Render (or re-render) just one part, via a range-mode worker for its chapters."""
        if self.project is None:
            return
        if self._video_worker is not None and self._video_worker.isRunning():
            QMessageBox.information(
                self, "Đang bận", "Đang tạo một video khác — hãy đợi hoặc bấm “Dừng”."
            )
            return
        self._launch_video(
            mode="range", start=window.first_num, end=window.last_num, skip_existing=False
        )

    def _launch_video(
        self, *, mode: str | None = None, start=None, end=None, batch=None,
        skip_existing: bool = False,
    ) -> None:
        from pathlib import Path

        from noveltrans.tts.video import video_font, video_preset

        if self.project is None:
            return
        if self._video_worker is not None and self._video_worker.isRunning():
            return
        image = self.video_image_edit.text().strip()
        if not image or not Path(image).is_file():
            QMessageBox.warning(self, "Chưa chọn ảnh", "Hãy chọn một ảnh nền hợp lệ cho video.")
            return
        voice = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
        if mode is None:  # default: whatever the tab currently has selected
            mode = self.video_mode.currentData()
            start = self.video_range_from.value() if mode == "range" else None
            end = self.video_range_to.value() if mode == "range" else None
            batch = self.video_batch_size.value() if mode == "batch" else None
        preset = video_preset(self.video_quality.currentData())
        font_key = self.video_font.currentData()
        font_family = video_font(font_key)["family"]
        tags = self.tags_edit.toPlainText().strip()

        self.video_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        self.status_label.setText("🎬 Đang tạo video… (có thể mất lâu)")
        self._video_worker = VideoWorker(
            self.project.path, voice=voice, mode=mode, image_path=image,
            start=start, end=end, batch=batch,
            width=preset["width"], height=preset["height"], fps=preset["fps"],
            spin_vinyl=preset["spin_vinyl"], font=font_family, font_key=font_key,
            bg_color=self.bg_color, skip_existing=skip_existing,
            credit=self.credit_edit.text().strip() or "Fox Novel",
            tagline=self.tagline_edit.text().strip(),
            thumb_image_path=self.thumb_image_edit.text().strip(),
            tags=tags,
        )
        self._video_worker.progress.connect(self._on_video_progress)
        self._video_worker.file_done.connect(self._on_video_file_done)
        self._video_worker.finished_ok.connect(self._on_video_finished)
        self._video_worker.failed.connect(self._on_video_failed)
        track_worker(self._video_worker)  # keep the Mac awake while encoding
        self._video_worker.start()

    def _on_video_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if name:
            self.status_label.setText(f"🎬 Đang tạo video ({done + 1}/{total}): {name}")

    def _on_video_file_done(self, path: str) -> None:
        self.progress.setValue(self.progress.value() + 1)

    def _reset_video_ui(self) -> None:
        self.video_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _on_video_finished(self, count: int) -> None:
        self._reset_video_ui()
        self._refresh_video_list()  # update the created/not-created statuses
        if count:
            self.status_label.setText(
                f"✅ Đã tạo {count} video (kèm tiêu đề, mô tả, ảnh bìa, tags) — "
                "bấm “Mở thư mục video”."
            )
        else:
            self.status_label.setText("Đã dừng tạo video.")

    def _on_video_failed(self, message: str) -> None:
        self._reset_video_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Tạo video thất bại", message)

    # --------------------------------------------------------------- helpers

    def _cancel(self) -> None:
        if self._video_worker is not None and self._video_worker.isRunning():
            self._video_worker.cancel()
            self.status_label.setText("Đang dừng tạo video…")

    def _open_video_dir(self) -> None:
        if self.project is None:
            return
        self.project.video_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.video_dir)))

    # ------------------------------------------------------- lifecycle (host)

    def has_running_workers(self) -> bool:
        return self._video_worker is not None and self._video_worker.isRunning()

    def shutdown(self) -> None:
        if self._preview_dialog is not None:
            self._preview_dialog.close()
        if self._video_worker is not None and self._video_worker.isRunning():
            self._video_worker.cancel()
            self._video_worker.wait(120_000)
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.wait(60_000)
        if self._tags_worker is not None and self._tags_worker.isRunning():
            self._tags_worker.wait(60_000)
        if self._image_prompt_worker is not None and self._image_prompt_worker.isRunning():
            self._image_prompt_worker.wait(60_000)
        if self._voices_worker is not None and self._voices_worker.isRunning():
            self._voices_worker.wait(5_000)

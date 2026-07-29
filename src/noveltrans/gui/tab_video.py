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

from PySide6.QtCore import QDateTime, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import AppConfig, translator_labels
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import CheckableHeaderView, ProjectPicker
from noveltrans.gui.workers import (
    CompletionWorker,
    TagsWorker,
    TtsVoicesWorker,
    VideoPreviewWorker,
    PlaylistFetchWorker,
    PlaylistSyncWorker,
    SubtitleWorker,
    VideoWorker,
    YouTubeThumbnailWorker,
    YouTubeUploadWorker,
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
        self._upload_worker: YouTubeUploadWorker | None = None
        self._thumbnail_worker: YouTubeThumbnailWorker | None = None
        self._playlist_worker: PlaylistSyncWorker | None = None
        self._playlist_fetch_worker: PlaylistFetchWorker | None = None
        self._subtitle_worker: SubtitleWorker | None = None
        self._preview_worker: VideoPreviewWorker | None = None
        self._voices_worker: TtsVoicesWorker | None = None
        self._tags_worker: TagsWorker | None = None
        self._image_prompt_worker: CompletionWorker | None = None
        self._render_after_tags = False  # auto-generate tags, then start the render
        # guards the "Đã tải lên" checkbox handler while the table is being repopulated
        self._suppress_upload_toggle = False
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

        # Six group boxes is more than a laptop window is tall. Without somewhere to
        # overflow to, Qt's only move is squeezing widgets below their sizeHint — and the
        # parts table, owning the sole stretch, collapsed first and hardest (down to one
        # clipped row on a 75-part project) before the fixed boxes started getting sliced
        # through too. The scroll area gives the overflow a destination.
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addLayout(top_row)
        layout.addLayout(self._build_engine_row())  # one AI engine for tags + image prompt
        layout.addWidget(self._build_video_box())
        layout.addWidget(self._build_video_list_box(), stretch=1)
        layout.addWidget(self._build_upload_box())
        layout.addWidget(self._build_thumbnail_box())
        layout.addWidget(self._build_image_prompt_box())
        layout.addWidget(self._build_tags_box())

        self.scroll = QScrollArea()
        self.scroll.setWidget(content)
        # The content fills the viewport when there's room and grows past it when there
        # isn't — which is what turns "clipped" into "scrollable".
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)  # no second border inside the tab
        # AsNeeded, never Off: Off would clip silently if some content turned out wider
        # than the viewport, which is the exact failure this change exists to end.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll, stretch=1)
        # Pinned outside the scroll: a render or an upload batch runs for minutes to hours,
        # and progress you have to go looking for is progress you don't see — least of all
        # while you're scrolled down reading the parts table.
        outer.addWidget(self.progress)
        outer.addWidget(self.status_label)

    # ---------------------------------------------------------------- boxes

    def _build_video_box(self) -> QGroupBox:
        """The 'Xuất video' controls: mode + quality + font + background image + buttons."""
        from noveltrans.tts.convert import ffmpeg_available
        from noveltrans.tts.video import VIDEO_FONTS

        self.video_mode = QComboBox()
        self.video_mode.addItem("Toàn bộ", "all")
        self.video_mode.addItem("Từ chương … đến …", "range")
        self.video_mode.addItem("Theo lô", "batch")
        midx = self.video_mode.findData(self.config.video_mode)  # remembered between sessions
        self.video_mode.setCurrentIndex(midx if midx >= 0 else self.video_mode.findData("batch"))
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
        self.video_batch_size.setValue(self.config.video_batch_size)  # remembered between sessions
        self.video_batch_size.valueChanged.connect(
            lambda v: setattr(self.config, "video_batch_size", v)
        )
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
        self.burn_subs_check = QCheckBox("Chèn phụ đề")
        self.burn_subs_check.setChecked(self.config.video_burn_subtitles)
        self.burn_subs_check.setToolTip(
            "Vẽ lời đọc thành phụ đề CỐ ĐỊNH ở đáy video (không tắt được). File .srt vẫn "
            "được tạo dù bật hay tắt.\n\nTăng ~25% dung lượng file, và muốn sửa thì phải "
            "tạo lại video. Chỉ áp dụng cho phần có audio tạo sau bản 040."
        )
        self.burn_subs_check.toggled.connect(
            lambda on: setattr(self.config, "video_burn_subtitles", on)
        )

        self.video_button = QPushButton("Tạo video")
        self.video_button.setProperty("primary", True)
        self.video_button.clicked.connect(self._start_video)
        self.redo_all_button = QPushButton("Tạo lại tất cả video")
        self.redo_all_button.setToolTip(
            "Render lại MỌI phần trong phạm vi đang chọn, kể cả phần đã có video — "
            "dùng khi đổi ảnh nền, màu, phông chữ hay chất lượng."
        )
        self.redo_all_button.clicked.connect(self._redo_all_videos)
        self.cancel_button = QPushButton("Dừng")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        if not ffmpeg_available():
            for b in (self.video_button, self.redo_all_button, self.video_preview_button):
                b.setEnabled(False)
                b.setToolTip("Cần ffmpeg để tạo video (brew install ffmpeg).")
        self.subtitle_button = QPushButton("Tạo phụ đề (.srt)")
        self.subtitle_button.setToolTip(
            "Ghi file .srt cho mọi phần trong phạm vi đang chọn — KHÔNG tạo lại video.\n\n"
            "Với audio tạo trước đây (chưa có mốc thời gian), ứng dụng sẽ dò các khoảng "
            "lặng trong file audio để khôi phục mốc. Chương nào không khớp sẽ được bỏ qua "
            "thay vì đoán."
        )
        self.subtitle_button.clicked.connect(self._start_subtitles)

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
        row.addWidget(self.burn_subs_check)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Ảnh nền:"))
        row2.addWidget(self.video_image_edit, stretch=1)
        row2.addWidget(self.video_image_button)
        row2.addWidget(self.video_preview_button)
        row2.addWidget(self.video_button)
        row2.addWidget(self.redo_all_button)
        row2.addWidget(self.cancel_button)
        row2.addWidget(self.subtitle_button)
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
        self.video_list = QTableWidget(0, 7)
        self.video_list.setHorizontalHeaderLabels(
            ["Phần", "Chương", "Thời lượng", "Tiêu đề", "Trạng thái", "Đã tải lên", "Thao tác"]
        )
        self.video_list.verticalHeader().setVisible(False)
        self.video_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.video_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.video_list.setAlternatingRowColors(True)
        # A check-all indicator in the "Đã tải lên" header. Installed before the resize
        # modes below, since setting a header view resets them.
        self.upload_header = CheckableHeaderView(5, self.video_list)
        self.video_list.setHorizontalHeader(self.upload_header)
        self.upload_header.toggled.connect(self._on_upload_header_toggled)
        header = self.video_list.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # thời lượng
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # tiêu đề
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # đã tải lên
        # ResizeToContents ignores cell *widgets*, so the action column must be sized
        # explicitly or the five buttons get crushed unreadably narrow.
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.video_list.setColumnWidth(6, 420)
        self.video_list.verticalHeader().setDefaultSectionSize(34)
        # ~6 rows + header. Without a floor the table is the first thing Qt squeezes —
        # it owns the only stretch in the tab — and on a short window it collapsed to a
        # single clipped row. It keeps its own scrollbar rather than growing to fit all
        # 75 parts, so a wheel gesture over the table never fights the page's scroll.
        self.video_list.setMinimumHeight(240)

        # the "Đã tải lên" tick is a control, not just a status — see _on_upload_toggled
        self.video_list.itemChanged.connect(self._on_upload_toggled)

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

    def _build_upload_box(self) -> QGroupBox:
        """Controls for the YouTube upload run: visibility/schedule, playlist, go.

        Deliberately per-run rather than per-project settings: the release plan for a
        novel ("một phần mỗi tối 20h từ thứ hai") is a decision the user makes once at
        upload time, not a property of the project worth persisting.
        """
        self.upload_visibility = QComboBox()
        # Private first, and the default: an accidental click can't publish anything.
        for label, key in (
            ("Riêng tư", "private"),
            ("Không công khai (unlisted)", "unlisted"),
            ("Công khai ngay", "public"),
            ("Hẹn giờ đăng", "schedule"),
        ):
            self.upload_visibility.addItem(label, key)
        self.upload_visibility.currentIndexChanged.connect(self._on_visibility_changed)

        self.upload_start = QDateTimeEdit()
        self.upload_start.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.upload_start.setCalendarPopup(True)
        # Tomorrow evening: YouTube rejects a schedule in the past, and by the time a
        # multi-GB part finishes uploading "in an hour" can easily be one.
        tomorrow = QDateTime.currentDateTime().addDays(1)
        tomorrow.setTime(tomorrow.time().fromString("20:00", "HH:mm"))
        self.upload_start.setDateTime(tomorrow)
        self.upload_start.dateTimeChanged.connect(self._refresh_schedule_preview)

        self.upload_spacing = QSpinBox()
        self.upload_spacing.setRange(0, 30)
        self.upload_spacing.setValue(1)
        self.upload_spacing.setSuffix(" ngày")
        self.upload_spacing.setToolTip(
            "Khoảng cách giữa các phần. 0 = đăng tất cả cùng một thời điểm."
        )
        self.upload_spacing.valueChanged.connect(self._refresh_schedule_preview)

        sched_row = QHBoxLayout()
        sched_row.addWidget(QLabel("Chế độ hiển thị:"))
        sched_row.addWidget(self.upload_visibility)
        sched_row.addSpacing(12)
        self.upload_start_label = QLabel("Bắt đầu:")
        sched_row.addWidget(self.upload_start_label)
        sched_row.addWidget(self.upload_start)
        self.upload_spacing_label = QLabel("Mỗi phần cách nhau:")
        sched_row.addWidget(self.upload_spacing_label)
        sched_row.addWidget(self.upload_spacing)
        sched_row.addStretch(1)

        # Editable, deliberately: the list is an aid, not a gate. A name that isn't on
        # the channel yet can still be typed and gets created on upload, exactly as before
        # — so nothing that worked stops working, and nobody has to fetch before uploading.
        self.upload_playlist = QComboBox()
        self.upload_playlist.setEditable(True)
        self.upload_playlist.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.upload_playlist.lineEdit().setPlaceholderText(
            "Tên danh sách phát (để trống = không thêm vào playlist)"
        )
        self.upload_playlist.setToolTip(
            "Mỗi phần sẽ được thêm vào danh sách phát này; nếu chưa có, YouTube sẽ tạo mới."
        )

        self.upload_button = QPushButton("⬆️ Tải lên YouTube")
        self.upload_button.setToolTip(
            "Tải mọi phần đã tạo mà chưa tải lên, tuần tự trong một cửa sổ trình duyệt."
        )
        self.upload_button.clicked.connect(self._start_upload)
        self.upload_reset_button = QPushButton("Đặt lại trạng thái…")
        self.upload_reset_button.setToolTip(
            "Xoá trạng thái của những phần bị gián đoạn, hoặc bỏ đánh dấu những phần bị "
            "ghi nhầm là đã tải lên, để có thể tải lên lại."
        )
        self.upload_reset_button.clicked.connect(self._reset_all_upload_states)
        # Lives here rather than in the thumbnail box: it opens the browser and shares
        # the one Chrome profile with the upload run, so it belongs with the other
        # YouTube actions and under the same "Dừng".
        self.thumbnail_update_button = QPushButton("🖼️ Cập nhật ảnh bìa")
        self.thumbnail_update_button.setToolTip(
            "Đẩy ảnh bìa mới lên MỌI phần đã có video trên YouTube trong phạm vi đang "
            "chọn — dùng sau khi “Tạo lại tất cả ảnh bìa”. Không tải lại video."
        )
        self.thumbnail_update_button.clicked.connect(self._start_thumbnail_update)
        self.playlist_fetch_button = QPushButton("Tải danh sách…")
        self.playlist_fetch_button.setToolTip(
            "Đọc các danh sách phát đang có trên kênh đã đăng nhập và đưa vào ô bên trái. "
            "Sẽ mở một cửa sổ Chrome trong vài giây."
        )
        self.playlist_fetch_button.clicked.connect(self._fetch_playlists)

        self.playlist_sync_button = QPushButton("Thêm vào danh sách phát")
        self.playlist_sync_button.setToolTip(
            "XOÁ HẾT danh sách phát đang chọn rồi thêm lại mọi phần đã tải lên theo đúng "
            "thứ tự. Dùng khi thứ tự trong danh sách phát bị sai."
        )
        self.playlist_sync_button.clicked.connect(self._start_playlist_sync)

        self.upload_cancel_button = QPushButton("Dừng")
        self.upload_cancel_button.setEnabled(False)
        self.upload_cancel_button.clicked.connect(self._cancel_upload)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Danh sách phát:"))
        action_row.addWidget(self.upload_playlist, stretch=1)
        action_row.addWidget(self.playlist_fetch_button)
        action_row.addWidget(self.playlist_sync_button)
        action_row.addWidget(self.upload_reset_button)
        action_row.addWidget(self.thumbnail_update_button)
        action_row.addWidget(self.upload_button)
        action_row.addWidget(self.upload_cancel_button)

        self.schedule_preview = QLabel("")
        self.schedule_preview.setProperty("muted", True)
        self.schedule_preview.setWordWrap(True)

        hint = QLabel(
            "Một cửa sổ Chrome riêng sẽ mở ra và tự điền form YouTube Studio — đừng "
            "bấm vào nó khi đang chạy. Cần “Đăng nhập YouTube” trong Settings một lần "
            "trước đó. Phần nào đã tải lên sẽ được bỏ qua. “Cập nhật ảnh bìa” chỉ đổi "
            "ảnh bìa của video đã đăng — không tải lại video."
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)

        box = QGroupBox("Tải lên YouTube (tự động qua trình duyệt)")
        inner = QVBoxLayout()
        inner.addLayout(sched_row)
        inner.addWidget(self.schedule_preview)
        inner.addLayout(action_row)
        inner.addWidget(hint)
        box.setLayout(inner)
        self._on_visibility_changed()
        return box

    def _on_visibility_changed(self) -> None:
        """Show the schedule controls only when they mean something."""
        scheduling = self.upload_visibility.currentData() == "schedule"
        for widget in (
            self.upload_start_label,
            self.upload_start,
            self.upload_spacing_label,
            self.upload_spacing,
        ):
            widget.setVisible(scheduling)
        self.schedule_preview.setVisible(scheduling)
        self._refresh_schedule_preview()

    def _refresh_schedule_preview(self) -> None:
        """Spell out when each pending part would actually go live.

        The arithmetic is trivial but the mistake it prevents isn't: "cách nhau 1 ngày"
        from a start date already in the past silently means YouTube refuses several
        parts mid-run, and seeing the real dates catches that before the browser opens.
        """
        if not hasattr(self, "schedule_preview"):
            return
        if self.upload_visibility.currentData() != "schedule":
            self.schedule_preview.setText("")
            return
        pending = self._pending_upload_rows()
        if not pending:
            self.schedule_preview.setText("Không có phần nào cần tải lên.")
            return
        from noveltrans.youtube_upload import schedule_times

        times = schedule_times(
            self.upload_start.dateTime().toPython(),
            len(pending),
            self.upload_spacing.value(),
        )
        shown = [
            f"{label}: {when:%d/%m %H:%M}"
            for (_, label, _, _), when in list(zip(pending, times))[:4]
        ]
        more = f" … (+{len(pending) - 4} phần)" if len(pending) > 4 else ""
        self.schedule_preview.setText("→ " + "   ".join(shown) + more)

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
        """The .mp4 path a given window would render to (for the exists check).

        Each part lives in its OWN subfolder — `video_dir/<stem>/<stem>.mp4` — so a single
        video (with its sidecars) can be uploaded without hunting through the others. Older
        parts rendered flat in `video_dir` are still recognised, so they keep showing as
        “đã tạo” and their thumbnail/detail keep opening after this change.
        """
        from pathlib import Path

        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import video_part_name

        # NOT display_name(): the slug decides <stem>.mp4 and every sidecar beside it
        # (including <stem>.upload.json), so keying it to an editable title would
        # strand every rendered part and orphan its upload record.
        slug = slugify(self.project.meta.translated_title or self.project.meta.title)
        name = video_part_name(
            slug, window.first_num, window.last_num, whole_novel=whole_novel
        )
        per_folder = self.project.video_dir / Path(name).stem / name
        legacy = self.project.video_dir / name
        if not per_folder.is_file() and legacy.is_file():
            return legacy
        return per_folder

    def _part_sidecar(self, window, whole_novel: bool, ext: str):
        """Path of a companion file (`.title.txt` / `.txt` / `.tags.txt` / `.jpg`) for a part."""
        out = self._part_output_path(window, whole_novel=whole_novel)
        return out.parent / (out.stem + ext)

    def _part_title(self, part_num) -> str:
        from noveltrans.tts.video import build_upload_title

        novel_title = self.project.meta.display_name()
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
        # Populating the table sets check states, which would re-enter the toggle handler
        # and pop a confirmation for a change the user never made.
        self._suppress_upload_toggle = True
        try:
            self._rebuild_video_rows()
        finally:
            self._suppress_upload_toggle = False
        self._sync_upload_header()

    def _upload_rows(self) -> list:
        """(row, path) for every listed part that has a rendered video."""
        from pathlib import Path

        rows = []
        for row in range(self.video_list.rowCount()):
            item = self.video_list.item(row, 5)
            if item is None:
                continue
            path = Path(item.data(Qt.ItemDataRole.UserRole) or "")
            if path.name and path.is_file():
                rows.append((row, path))
        return rows

    def _sync_upload_header(self) -> None:
        """Point the header indicator at all / none / some of the rows being uploaded."""
        from noveltrans.youtube_upload import is_published

        if not hasattr(self, "upload_header"):
            return
        rows = self._upload_rows()
        marked = sum(1 for _row, path in rows if is_published(path))
        if not rows or marked == 0:
            state = Qt.CheckState.Unchecked
        elif marked == len(rows):
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self.upload_header.set_state(state)

    def _on_upload_header_toggled(self, check_all: bool) -> None:
        """Mark every listed part uploaded / not-uploaded, from the header indicator.

        Toggling one row is consequential; toggling thirty is more so, so this confirms
        once with the count and only touches rows that would actually change.
        """
        from noveltrans.youtube_upload import (
            clear_upload_state,
            is_published,
            mark_uploaded_by_hand,
        )

        targets = [
            path for _row, path in self._upload_rows() if is_published(path) != check_all
        ]
        if not targets:
            QMessageBox.information(
                self,
                "Đã tải lên",
                "Tất cả các phần đã ở đúng trạng thái rồi."
                if self._upload_rows()
                else "Chưa có phần nào đã tạo video.",
            )
            return

        if check_all:
            message = (
                f"Đánh dấu {len(targets)} phần là ĐÃ TẢI LÊN?\n\n"
                "Dùng khi bạn đã tự tải chúng lên YouTube. Ứng dụng sẽ bỏ qua các phần "
                "này trong những lần tải lên sau."
            )
        else:
            message = (
                f"Bỏ đánh dấu {len(targets)} phần đang được ghi là ĐÃ TẢI LÊN?\n\n"
                "Chỉ làm vậy nếu các video đó KHÔNG thực sự có trên kênh.\n\n"
                "⚠️ Nếu video vẫn còn trên YouTube và bạn tải lại, kênh sẽ có HAI bản "
                "cho mỗi phần."
            )
        if QMessageBox.question(self, "Đã tải lên", message) != (
            QMessageBox.StandardButton.Yes
        ):
            return

        for path in targets:
            if check_all:
                mark_uploaded_by_hand(path)
            else:
                clear_upload_state(path)
        self._refresh_video_list()
        self.status_label.setText(
            f"Đã đánh dấu {len(targets)} phần là đã tải lên."
            if check_all
            else f"Đã bỏ đánh dấu {len(targets)} phần — có thể tải lên lại."
        )

    def _rebuild_video_rows(self) -> None:
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
            self.video_list.setItem(i, 5, self._upload_item(window, whole_novel))
            self.video_list.setCellWidget(
                i, 6, self._build_row_actions(window, part_num, whole_novel, exists)
            )

    def _upload_item(self, window, whole_novel: bool) -> QTableWidgetItem:
        """The "Đã tải lên" cell: a tick the user can toggle, plus a short status.

        The tick *is* the control — untick a part to mark it not-uploaded (when the
        record is wrong), tick one to record an upload done by hand. Both directions go
        through a confirmation in `_on_upload_toggled`; the checkbox only shows state.

        An interrupted attempt stays unticked but is flagged in red rather than reading
        as a plain "chưa tải": it's the one state that needs a human to go look at the
        channel, and showing it as simply not-uploaded would invite exactly the duplicate
        the sidecar exists to prevent.
        """
        from noveltrans.youtube_upload import is_published, needs_attention, read_upload_state

        path = self._part_output_path(window, whole_novel=whole_novel)
        state = read_upload_state(path)
        published = is_published(path)
        if published:
            when = (state.get("published_at") or "")[:10]
            item = QTableWidgetItem(when or "Đã tải")
            item.setToolTip(
                (state.get("url") or "")
                + ("\n\n" if state.get("url") else "")
                + "Bỏ tick nếu video không thực sự có trên kênh."
            )
        elif needs_attention(path):
            # `needs_attention` owns the "unresolved" set (started / draft / committed /
            # unknown); duplicating it here is how the two drift apart and a `committed`
            # part quietly starts reading as "chưa tải".
            item = QTableWidgetItem("⚠️ Dở dang")
            item.setForeground(QColor("#e06c75"))
            item.setToolTip(
                "Lần tải trước bị gián đoạn. Kiểm tra bản nháp trên kênh, rồi bấm "
                "“Đặt lại” để tải lại."
                + (f"\n{state['url']}" if state.get("url") else "")
            )
        else:
            item = QTableWidgetItem("Chưa tải")
            item.setToolTip("Tick nếu bạn đã tự tải phần này lên YouTube.")

        # A thumbnail push leaves no other trace, so this tooltip is the only place
        # "did that actually do anything?" gets an answer. Not part of the column text:
        # it's a detail about the upload, not a state of its own.
        pushed = (state.get("thumbnail_updated_at") or "")[:10]
        if pushed:
            item.setToolTip(item.toolTip() + f"\nẢnh bìa cập nhật: {pushed}")

        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
        )
        item.setCheckState(
            Qt.CheckState.Checked if published else Qt.CheckState.Unchecked
        )
        # The path travels with the cell so the handler never has to re-derive which part
        # a row is — the row→window mapping shifts whenever the batch size changes.
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        return item

    def _on_upload_toggled(self, item: QTableWidgetItem) -> None:
        """Handle the user ticking / unticking "Đã tải lên" on a row.

        Both directions are consequential, so both confirm first and both revert the tick
        if declined: unticking a live video invites a duplicate re-upload, and ticking one
        that was never uploaded silently excludes it from every future batch.
        """
        from pathlib import Path

        from noveltrans.youtube_upload import (
            clear_upload_state,
            has_remote_draft,
            is_published,
            mark_uploaded_by_hand,
            read_upload_state,
        )

        if item.column() != 5 or self._suppress_upload_toggle:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole) or "")
        if not path.name:
            return
        wanted = item.checkState() == Qt.CheckState.Checked
        if wanted == is_published(path):
            return  # nothing actually changed

        state = read_upload_state(path)
        link = state.get("url") or state.get("video_id") or ""
        if wanted:
            title = "Đánh dấu đã tải lên"
            message = (
                "Đánh dấu phần này là ĐÃ TẢI LÊN?\n\n"
                "Dùng khi bạn đã tự tải nó lên YouTube. Ứng dụng sẽ bỏ qua phần này "
                "trong các lần tải lên sau."
            )
        elif is_published(path) and link:
            title = "Bỏ đánh dấu đã tải lên"
            message = (
                f"Phần này đang được đánh dấu ĐÃ TẢI LÊN.\n{link}\n\n"
                "Chỉ bỏ tick nếu video KHÔNG thực sự có trên kênh — ví dụ lần tải trước "
                "bị dừng giữa chừng nên video không hoàn tất.\n\n"
                "⚠️ Nếu video vẫn còn trên YouTube và bạn tải lại, kênh sẽ có HAI bản.\n\n"
                "Đã kiểm tra trên kênh và vẫn muốn bỏ tick?"
            )
        else:
            title = "Bỏ đánh dấu đã tải lên"
            message = (
                "Bỏ đánh dấu để có thể tải phần này lên lại?"
                + ("\n\n⚠️ Kiểm tra kênh trước — có thể đã có bản nháp." if has_remote_draft(path) else "")
            )

        if QMessageBox.question(self, title, message) != QMessageBox.StandardButton.Yes:
            # Put the tick back where it was; the refresh below would do it anyway, but
            # doing it here keeps the cell honest even if the refresh is a no-op.
            self._set_check_silently(item, not wanted)
            return

        if wanted:
            mark_uploaded_by_hand(path)
        else:
            clear_upload_state(path)
        self._refresh_video_list()
        self.status_label.setText(
            "Đã đánh dấu là đã tải lên." if wanted else "Đã bỏ đánh dấu — có thể tải lên lại."
        )

    def _set_check_silently(self, item: QTableWidgetItem, checked: bool) -> None:
        """Set a check state without re-entering `_on_upload_toggled`."""
        self._suppress_upload_toggle = True
        try:
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        finally:
            self._suppress_upload_toggle = False

    def _build_row_actions(self, window, part_num, whole_novel, exists):
        """Per-row actions: (re)render, copyable detail, open thumbnail, upload."""
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
        # Two shapes for one slot: "Đặt lại" clears an interrupted record so the part can
        # be retried, otherwise "Tải lên". Un-marking a *published* part is the tick in
        # the "Đã tải lên" column rather than a button here — the checkbox already shows
        # that state, so toggling it is the obvious way to change it.
        from noveltrans.youtube_upload import is_published, needs_attention

        path = self._part_output_path(window, whole_novel=whole_novel)
        if exists and needs_attention(path):
            upload = QPushButton("Đặt lại")
            upload.setToolTip(
                "Xoá trạng thái “dở dang” để có thể tải lên lại. Kiểm tra kênh trước "
                "nếu lần tải trước đã kịp tạo video."
            )
            upload.clicked.connect(
                lambda _=False, w=window, wn=whole_novel: self._reset_upload_state(w, wn)
            )
        else:
            upload = QPushButton("Tải lên")
            upload.setToolTip(
                "Tải riêng phần này lên YouTube."
                if not is_published(path)
                else "Phần này đã tải lên — bỏ tick ở cột “Đã tải lên” nếu muốn tải lại."
            )
            # A published part is un-marked by unticking its cell, not by a button.
            upload.setEnabled(exists and not is_published(path))
            upload.clicked.connect(
                lambda _=False, w=window, pn=part_num, wn=whole_novel: self._upload_one(w, pn, wn)
            )

        # Push the current cover onto the video that is already on the channel. Appended
        # last so the existing button positions don't shift.
        from noveltrans.youtube_upload import uploaded_video_id

        jpg = self._part_sidecar(window, whole_novel, ".jpg")
        video_id = uploaded_video_id(path)
        cover = QPushButton("Cập nhật bìa")
        cover.setEnabled(bool(video_id) and jpg.is_file())
        # Two distinct disabled tooltips, because the two blockers have different fixes —
        # a dead control with no explanation is what the "Đã tải lên" column exists to avoid.
        if not video_id:
            cover.setToolTip("Phần này chưa có video trên YouTube — tải lên trước đã.")
        elif not jpg.is_file():
            cover.setToolTip(
                "Chưa có ảnh bìa cho phần này — bấm “Tạo lại tất cả ảnh bìa” trước."
            )
        else:
            cover.setToolTip(
                "Thay ảnh bìa của video đã đăng trên YouTube bằng ảnh bìa mới nhất "
                f"({jpg.name}). Không tải lại video."
            )
        cover.clicked.connect(
            lambda _=False, w=window, pn=part_num, wn=whole_novel: self._update_thumbnail_one(
                w, pn, wn
            )
        )

        # compact padding + a sensible min width so the labels never truncate
        for b, min_w in ((make, 58), (detail, 66), (thumb, 66), (upload, 66), (cover, 92)):
            b.setStyleSheet("padding: 3px 8px;")
            b.setMinimumWidth(min_w)
            row.addWidget(b)
        return container

    def _part_uploaded(self, window, whole_novel: bool) -> bool:
        from noveltrans.youtube_upload import is_published

        return is_published(self._part_output_path(window, whole_novel=whole_novel))

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
        novel_title = self.project.meta.display_name()
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
        regen_thumb = QPushButton("Tạo lại ảnh bìa")
        regen_thumb.setToolTip(
            "Vẽ lại ảnh bìa với phông chữ / tagline / ảnh hiện tại — không cần render lại video."
        )

        def _regen() -> None:
            if self._regen_part_thumbnail(window, part_num, whole_novel):
                open_thumb.setEnabled(True)
                status.setText("✅ Đã tạo lại ảnh bìa.")

        regen_thumb.clicked.connect(_regen)
        open_dir = QPushButton("Mở thư mục video")
        open_dir.setToolTip("Mở đúng thư mục riêng của phần này (video + tiêu đề/mô tả/tags/ảnh bìa).")
        open_dir.clicked.connect(lambda: self._open_part_dir(window, whole_novel))
        close = QPushButton("Đóng")
        close.clicked.connect(dialog.close)
        bottom = QHBoxLayout()
        bottom.addWidget(open_thumb)
        bottom.addWidget(regen_thumb)
        bottom.addWidget(open_dir)
        bottom.addWidget(status)
        bottom.addStretch()
        bottom.addWidget(close)
        layout.addLayout(bottom)

        dialog.exec()

    def _open_part_dir(self, window, whole_novel) -> None:
        """Open the folder holding just this part's video + sidecars (created if missing)."""
        if self.project is None:
            return
        folder = self._part_output_path(window, whole_novel=whole_novel).parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _open_part_thumbnail(self, window, whole_novel) -> None:
        thumb = self._part_sidecar(window, whole_novel, ".jpg")
        if thumb.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(thumb)))
        else:
            QMessageBox.information(
                self, "Chưa có ảnh bìa",
                "Phần này chưa được tạo video nên chưa có ảnh bìa — bấm “Tạo” trước.",
            )

    # -------------------------------------------- regenerate the cover only

    def _thumbnail_base_image(self) -> str | None:
        """The base photo for a cover: the dedicated 'Ảnh bìa' if set, else the video 'Ảnh nền'.
        Warns and returns None when neither is a valid file."""
        from pathlib import Path

        base = self.thumb_image_edit.text().strip() or self.video_image_edit.text().strip()
        if not base or not Path(base).is_file():
            QMessageBox.warning(
                self, "Chưa chọn ảnh",
                "Hãy chọn 'Ảnh bìa' hoặc 'Ảnh nền' hợp lệ trước khi tạo lại ảnh bìa.",
            )
            return None
        return base

    def _render_thumbnail_now(self, window, part_num, whole_novel, *, base, font_dir) -> None:
        """Render one part's `.jpg` cover from the saved cover settings (font + title/part
        positions) — pure Pillow, no ffmpeg, so it's near-instant and needs no video render."""
        from noveltrans.tts.thumbnail import render_thumbnail
        from noveltrans.tts.video import video_font

        novel_title = self.project.meta.display_name()
        font_file = video_font(self.config.video_thumbnail_font)["file"]
        render_thumbnail(
            base,
            self._part_sidecar(window, whole_novel, ".jpg"),
            vn_title=novel_title,
            part_num=part_num or 1,
            tagline=self.tagline_edit.text().strip(),
            font_path=font_dir / font_file,
            width=1280, height=720,
            title_pos=self.config.video_thumbnail_title_pos,
            part_pos=self.config.video_thumbnail_part_pos,
            title_scale=self.config.video_thumbnail_title_scale,
            part_scale=self.config.video_thumbnail_part_scale,
            tagline_scale=self.config.video_thumbnail_tagline_scale,
            title_align=self.config.video_thumbnail_title_align,
        )

    def _regen_part_thumbnail(self, window, part_num, whole_novel) -> bool:
        """Regenerate just this part's cover. Returns True on success (for callers to react)."""
        if self.project is None:
            return False
        from noveltrans.tts.video import font_dir_context

        base = self._thumbnail_base_image()
        if base is None:
            return False
        try:
            with font_dir_context() as font_dir:
                self._render_thumbnail_now(window, part_num, whole_novel, base=base, font_dir=font_dir)
        except Exception as exc:  # noqa: BLE001 — surface a bad image/font to the user
            QMessageBox.warning(self, "Tạo ảnh bìa thất bại", str(exc))
            return False
        self.status_label.setText("✅ Đã tạo lại ảnh bìa.")
        self._refresh_video_list()  # re-enables the "Ảnh bìa" open button on the row
        return True

    def _regen_all_thumbnails(self) -> None:
        """Regenerate the covers for every part in the current selection (font/tagline/image
        applied to all at once) — no video re-render."""
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        from noveltrans.tts.video import font_dir_context

        windows = self._windows_for_current_selection()
        if not windows:
            QMessageBox.information(
                self, "Không có phần nào", "Chưa có phần video nào trong phạm vi đã chọn."
            )
            return
        base = self._thumbnail_base_image()
        if base is None:
            return
        mode = self.video_mode.currentData()
        total = len(windows)
        done = 0
        # Counted before the loop: `processEvents()` below can repopulate the voice combo
        # mid-run, and the answer we want is about the selection we are re-rendering.
        live = len(self._thumbnail_update_rows())
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with font_dir_context() as font_dir:
                for i, window in enumerate(windows):
                    whole_novel = total == 1 and mode == "all"
                    part_num = None if whole_novel else (i + 1)
                    self.status_label.setText(f"🖼️ Đang tạo lại ảnh bìa ({i + 1}/{total})…")
                    QApplication.processEvents()
                    try:
                        self._render_thumbnail_now(
                            window, part_num, whole_novel, base=base, font_dir=font_dir
                        )
                        done += 1
                    except Exception:  # noqa: BLE001 — one bad part must not stop the rest
                        pass
        finally:
            QApplication.restoreOverrideCursor()
        # Re-rendering a cover changes nothing on YouTube — the same trap `_redo_all_videos`
        # warns about, and the discovery path for the button that fixes it.
        self.status_label.setText(
            f"✅ Đã tạo lại {done}/{total} ảnh bìa."
            + (
                f" ⚠️ {live} phần đã có video trên YouTube — bấm “🖼️ Cập nhật ảnh bìa” "
                "để đẩy bìa mới lên."
                if live
                else ""
            )
        )
        self._refresh_video_list()

    def _open_thumbnail_editor(self) -> None:
        """Open the live cover editor (drag title/part, try fonts) — saves to config and can
        apply to every part at once."""
        if self.project is None:
            QMessageBox.information(self, "Chưa chọn truyện", "Hãy chọn một truyện trước.")
            return
        from noveltrans.gui.thumbnail_editor import ThumbnailEditorDialog

        base = self.thumb_image_edit.text().strip() or self.video_image_edit.text().strip()
        novel_title = self.project.meta.display_name()
        dialog = ThumbnailEditorDialog(
            self.config,
            base_image=base,
            novel_title=novel_title,
            part_num=1,
            tagline=self.tagline_edit.text().strip(),
            on_apply_all=self._regen_all_thumbnails,
            parent=self,
        )
        dialog.exec()
        # reflect any font change made in the editor back into the box's font combo
        fidx = self.thumb_font.findData(self.config.video_thumbnail_font)
        if fidx >= 0 and fidx != self.thumb_font.currentIndex():
            self.thumb_font.blockSignals(True)
            self.thumb_font.setCurrentIndex(fidx)
            self.thumb_font.blockSignals(False)

    def _build_thumbnail_box(self) -> QGroupBox:
        """Thumbnail base image + font + tagline + credit for the auto-generated metadata."""
        from noveltrans.tts.video import VIDEO_FONTS

        self.thumb_image_edit = QLineEdit(self.config.video_thumbnail_image)
        self.thumb_image_edit.setPlaceholderText("Dùng chung ảnh nền video nếu để trống…")
        self.thumb_image_edit.setReadOnly(True)
        self.thumb_image_edit.setMinimumWidth(160)
        self.thumb_image_button = QPushButton("Chọn ảnh bìa…")
        self.thumb_image_button.clicked.connect(self._pick_thumb_image)

        # Font for the thumbnail text — chosen independently of the video font (same picker
        # style as “Xuất video”), so the cover can use a softer, Vietnamese-friendly face.
        self.thumb_font = QComboBox()
        for key, spec in VIDEO_FONTS.items():
            self.thumb_font.addItem(spec["label"], key)
        tfidx = self.thumb_font.findData(self.config.video_thumbnail_font)
        self.thumb_font.setCurrentIndex(tfidx if tfidx >= 0 else 0)
        self.thumb_font.setToolTip(
            "Phông chữ cho chữ trên ảnh bìa (thumbnail) — nên chọn phông mềm mại, "
            "hỗ trợ tiếng Việt (mặc định Nunito bo tròn)."
        )
        self.thumb_font.currentIndexChanged.connect(
            lambda: setattr(self.config, "video_thumbnail_font", self.thumb_font.currentData())
        )

        # Per-NOVEL, unlike everything else in this box: it lives in meta.json, because
        # "bỏ [ĐM/EDIT]" is true of this novel and nonsense for the next one.
        self.display_title_edit = QLineEdit()
        self.display_title_edit.setToolTip(
            "Tên truyện dùng trên ảnh bìa, tiêu đề video và mô tả — ví dụ bỏ tiền tố "
            "“[ĐM/EDIT] ”. Để trống = dùng tên đã dịch. Không đổi tên file video đã tạo, "
            "và không đổi được tiêu đề video đã đăng lên YouTube."
        )
        self.display_title_edit.editingFinished.connect(self._save_display_title)

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

        # Live cover editor: drag the title / PHẦN N, try fonts with a preview, then save +
        # apply to all — the visual way to get the layout right.
        self.thumb_edit_button = QPushButton("Tùy chỉnh ảnh bìa…")
        self.thumb_edit_button.setToolTip(
            "Mở cửa sổ chỉnh ảnh bìa: kéo để đặt vị trí tiêu đề / PHẦN N, đổi phông xem trực "
            "tiếp, rồi lưu và áp dụng cho mọi phần."
        )
        self.thumb_edit_button.clicked.connect(self._open_thumbnail_editor)

        # Regenerate every cover from the saved font/tagline/positions/image — near-instant
        # (Pillow only), so changes apply without re-rendering any video.
        self.thumb_regen_button = QPushButton("Tạo lại tất cả ảnh bìa")
        self.thumb_regen_button.setToolTip(
            "Tạo lại ảnh bìa cho mọi phần trong phạm vi đang chọn theo phông chữ / tagline / "
            "vị trí / ảnh hiện tại — không cần render lại video."
        )
        self.thumb_regen_button.clicked.connect(self._regen_all_thumbnails)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Tên hiển thị:"))
        title_row.addWidget(self.display_title_edit, stretch=1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Ảnh bìa:"))
        row.addWidget(self.thumb_image_edit, stretch=1)
        row.addWidget(self.thumb_image_button)
        row.addWidget(QLabel("Phông bìa:"))
        row.addWidget(self.thumb_font)
        row.addWidget(QLabel("Tagline:"))
        row.addWidget(self.tagline_edit, stretch=1)
        row.addWidget(QLabel("Tạo bởi:"))
        row.addWidget(self.credit_edit)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self.thumb_edit_button)
        action_row.addWidget(self.thumb_regen_button)

        inner = QVBoxLayout()
        inner.addLayout(title_row)
        inner.addLayout(row)
        inner.addLayout(action_row)
        box = QGroupBox("Ảnh bìa (thumbnail) & metadata")
        box.setLayout(inner)
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
        self.config.video_mode = mode  # remember the choice between sessions
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
            self._sync_display_title()
            self._update_status_line()
        else:
            self.tags_edit.setPlainText("")
            self.image_prompt_edit.setPlainText("")
            self.display_title_edit.clear()
            self.display_title_edit.setPlaceholderText("")
            self.status_label.setText("")
        self._refresh_video_list()

    def _sync_display_title(self) -> None:
        """Show the override, with the current fallback as the placeholder.

        The placeholder is what makes an empty box read as "dùng tên đã dịch" rather than
        "there is no title" — the box is empty in the common case, and an empty box with
        no hint invites people to paste the title in just to see something there.
        """
        meta = self.project.meta
        self.display_title_edit.setText(meta.display_title)
        self.display_title_edit.setPlaceholderText(
            meta.translated_title or meta.title or "Tên truyện"
        )

    def _save_display_title(self) -> None:
        """Persist the title override and re-render the parts table's titles."""
        if self.project is None:
            return
        wanted = self.display_title_edit.text().strip()
        if wanted == self.project.meta.display_title:
            return
        self.project.save_display_title(wanted)
        self._sync_display_title()
        self._refresh_video_list()  # the "Tiêu đề" column is built from display_name()
        self.status_label.setText(
            f"Tên hiển thị: “{self.project.meta.display_name()}”. Bấm “Tạo lại tất cả "
            "ảnh bìa” để áp dụng lên ảnh bìa đã tạo."
        )

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
            vn_title=meta.display_name(),
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
            novel_title = self.project.meta.display_name()

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

    def _redo_all_videos(self) -> None:
        """Re-render every part in the current range, overwriting the videos already there.

        The counterpart to "Tạo video", which only fills in what's missing. This is the
        button you want after changing the background image, colour, font or quality —
        those settings are baked into the .mp4, so the existing files are simply wrong
        and there's no per-part change to hunt for.
        """
        from noveltrans.tts.merge import plan_merge_windows
        from noveltrans.tts.video import video_preset

        if self.project is None:
            QMessageBox.information(self, "Tạo lại tất cả video", "Chọn truyện trước.")
            return
        if self._video_worker is not None and self._video_worker.isRunning():
            QMessageBox.information(
                self, "Đang bận", "Đang tạo một video khác — hãy đợi hoặc bấm “Dừng”."
            )
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

        whole = len(windows) == 1 and mode == "all"
        existing = sum(
            1 for w in windows if self._part_output_path(w, whole_novel=whole).is_file()
        )
        preset = video_preset(self.video_quality.currentData())
        total_secs = sum(c.audio_seconds for w in windows for c in w.chapters)
        hours = total_secs / 3600
        render_hours = hours / preset["speed"]
        est = f"~{render_hours * 60:.0f} phút" if render_hours < 1 else f"~{render_hours:.1f} giờ"

        # Re-rendering a part that is already on YouTube does NOT change the published
        # video — the user would have to re-upload, and the upload sidecar will keep
        # saying "đã tải lên". Worth saying out loud before an hours-long render.
        uploaded = sum(1 for w in windows if self._part_uploaded(w, whole))
        uploaded_note = (
            f"\n\n⚠️ {uploaded} phần đã tải lên YouTube. Render lại KHÔNG đổi video đã "
            "đăng — muốn thay thì phải xoá trạng thái tải lên và tải lại thủ công."
            if uploaded
            else ""
        )
        overwrite_note = (
            f" Ghi đè {existing} video đã có." if existing else " (chưa phần nào có video)"
        )
        answer = QMessageBox.question(
            self, "Tạo lại tất cả video",
            f"Sẽ render lại toàn bộ {len(windows)} phần "
            f"({sum(len(w.chapters) for w in windows)} chương, giọng {voice}).{overwrite_note}\n\n"
            f"Chất lượng: {self.video_quality.currentText()} "
            f"({preset['width']}×{preset['height']}).\n"
            f"Ước tính thời gian render: {est} (chưa tính máy nóng/tải khác).\n\n"
            f"Tiếp tục?{uploaded_note}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._launch_video(skip_existing=False)

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
        self.redo_all_button.setEnabled(False)
        # An upload reads the .mp4 files this worker is about to overwrite, and a
        # thumbnail push reads the .jpg it also rewrites — neither can run alongside.
        self.upload_button.setEnabled(False)
        self.thumbnail_update_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        self.status_label.setText("🎬 Đang tạo video… (có thể mất lâu)")
        self._video_worker = VideoWorker(
            self.project.path, voice=voice, mode=mode, image_path=image,
            start=start, end=end, batch=batch,
            width=preset["width"], height=preset["height"], fps=preset["fps"],
            spin_vinyl=preset["spin_vinyl"], font=font_family, font_key=font_key,
            thumb_font_key=self.config.video_thumbnail_font,
            thumb_title_pos=self.config.video_thumbnail_title_pos,
            thumb_part_pos=self.config.video_thumbnail_part_pos,
            thumb_title_scale=self.config.video_thumbnail_title_scale,
            thumb_part_scale=self.config.video_thumbnail_part_scale,
            thumb_tagline_scale=self.config.video_thumbnail_tagline_scale,
            thumb_title_align=self.config.video_thumbnail_title_align,
            burn_subtitles=self.burn_subs_check.isChecked(),
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
        self.redo_all_button.setEnabled(True)
        self.upload_button.setEnabled(True)
        self.thumbnail_update_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _subtitle_coverage(self) -> tuple[int, int]:
        """(parts with an .srt, parts rendered) in the current selection.

        Audio voiced before feature 040 carries no timing cues, so those parts render
        without subtitles. Saying so is the whole point: silence would read as a broken
        feature rather than as "that audio predates it".
        """
        if self.project is None:
            return 0, 0
        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        whole = len(windows) == 1 and mode == "all"
        rendered = [
            self._part_output_path(w, whole_novel=whole)
            for w in windows
            if self._part_output_path(w, whole_novel=whole).is_file()
        ]
        return sum(1 for p in rendered if p.with_suffix(".srt").is_file()), len(rendered)

    def _on_video_finished(self, count: int) -> None:
        self._reset_video_ui()
        self._refresh_video_list()  # update the created/not-created statuses
        if count:
            subs, total = self._subtitle_coverage()
            note = (
                ""
                if subs == total
                else (
                    f" ⚠️ Phụ đề: {subs}/{total} phần — audio tạo trước đây chưa có mốc "
                    "thời gian, hãy tạo lại audio nếu muốn phụ đề đầy đủ."
                )
            )
            self.status_label.setText(
                f"✅ Đã tạo {count} video (kèm tiêu đề, mô tả, ảnh bìa, tags, phụ đề .srt) — "
                "bấm “Mở thư mục video”." + note
            )
        else:
            self.status_label.setText("Đã dừng tạo video.")

    def _on_video_failed(self, message: str) -> None:
        self._reset_video_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Tạo video thất bại", message)

    # ------------------------------------------------------ upload to YouTube

    def _pending_upload_rows(self) -> list:
        """The parts that are rendered and not yet uploaded, as (window, label, num, whole).

        Rendered-but-not-uploaded is the whole selection rule: a part with no `.mp4` has
        nothing to send, and one already `published` must never go up twice. Parts left
        `⚠️ Dở dang` by an interrupted run are excluded too — they need a human, and the
        core module would refuse them anyway.
        """
        if self.project is None:
            return []
        from noveltrans.youtube_upload import needs_attention

        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        total = len(windows)
        rows = []
        for i, window in enumerate(windows):
            whole_novel = total == 1 and mode == "all"
            part_num = None if whole_novel else (i + 1)
            path = self._part_output_path(window, whole_novel=whole_novel)
            if not path.is_file():
                continue
            if self._part_uploaded(window, whole_novel) or needs_attention(path):
                continue
            label = "Toàn bộ" if whole_novel else f"Phần {i + 1}"
            rows.append((window, label, part_num, whole_novel))
        return rows

    def _read_sidecar(self, window, whole_novel: bool, ext: str) -> str:
        """Text of a part's sidecar, or "" if it was never written."""
        path = self._part_sidecar(window, whole_novel, ext)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _upload_request(self, window, label, part_num, whole_novel, *, publish_at):
        """Build one `UploadRequest` from the sidecars already sitting beside the .mp4.

        Falls back to computing the title/description when a sidecar is missing (parts
        rendered before those were written) so an older project can still be uploaded.
        """
        from noveltrans.youtube_upload import UploadRequest

        video = self._part_output_path(window, whole_novel=whole_novel)
        thumbnail = self._part_sidecar(window, whole_novel, ".jpg")
        novel_title = self.project.meta.display_name()
        return UploadRequest(
            video=video,
            title=self._read_sidecar(window, whole_novel, ".title.txt")
            or self._part_title(part_num),
            description=self._read_sidecar(window, whole_novel, ".txt")
            or self._compute_part_description(window, novel_title),
            tags=self._read_sidecar(window, whole_novel, ".tags.txt")
            or (self.project.meta.tags or ""),
            thumbnail=thumbnail if thumbnail.is_file() else None,
            playlist=self.upload_playlist.currentText().strip(),
            visibility=self.upload_visibility.currentData(),
            publish_at=publish_at,
            label=label,
        )

    def _start_upload(self) -> None:
        """Upload every rendered-but-not-yet-uploaded part, in one browser session."""
        if self.project is None:
            QMessageBox.information(self, "Tải lên YouTube", "Chọn truyện trước.")
            return
        rows = self._pending_upload_rows()
        if not rows:
            QMessageBox.information(
                self,
                "Tải lên YouTube",
                "Không có phần nào để tải lên (chưa tạo video, hoặc đã tải lên hết).",
            )
            return
        self._launch_upload(rows)

    def _upload_one(self, window, part_num, whole_novel) -> None:
        """Upload a single part — the retry path for one row."""
        label = "Toàn bộ" if whole_novel else f"Phần {part_num}"
        self._launch_upload([(window, label, part_num, whole_novel)])

    def _reset_upload_state(self, window, whole_novel) -> None:
        """Clear one part's “dở dang” record so it can be queued again.

        The app refuses to retry those states by itself because it can't tell whether a
        video exists on the channel. This is where a human answers that — so the warning
        has to be specific about which case they're in.
        """
        from noveltrans.youtube_upload import (
            clear_upload_state,
            has_remote_draft,
            is_published,
            read_upload_state,
        )

        path = self._part_output_path(window, whole_novel=whole_novel)
        state = read_upload_state(path)
        link = state.get("url") or state.get("video_id") or ""
        if is_published(path):
            # The strongest case: we believe this part is live. The record is only wrong
            # when the publish click landed but the transfer didn't — which is exactly
            # what a batch that abandoned its uploads leaves behind.
            title, warning = (
                "Bỏ đánh dấu đã tải lên",
                f"Phần này đang được đánh dấu ĐÃ TẢI LÊN.\n{link}\n\n"
                "Chỉ bỏ đánh dấu nếu video KHÔNG thực sự có trên kênh — ví dụ lần tải "
                "trước bị dừng giữa chừng nên video không hoàn tất.\n\n"
                "⚠️ Nếu video vẫn còn trên YouTube và bạn tải lại, kênh sẽ có HAI bản.\n\n"
                "Đã kiểm tra trên kênh và vẫn muốn bỏ đánh dấu?",
            )
        elif has_remote_draft(path):
            # Something really is on the channel; re-uploading would duplicate it.
            title, warning = (
                "Đặt lại trạng thái tải lên",
                f"Lần tải trước ĐÃ tạo video trên kênh:\n{link}\n\n"
                "Hãy mở link đó kiểm tra trước. Nếu video vẫn còn và bạn tải lại, kênh sẽ "
                "có HAI bản. Xoá video đó trên YouTube trước rồi hãy đặt lại.\n\nVẫn đặt lại?",
            )
        else:
            # No video id was ever recorded → nothing reached YouTube. Safe.
            title, warning = (
                "Đặt lại trạng thái tải lên",
                "Lần tải trước dừng trước khi gửi được file, nên nhiều khả năng KHÔNG có "
                "video nào trên kênh.\n\nĐặt lại để tải phần này lên lại?",
            )
        if QMessageBox.question(self, title, warning) != QMessageBox.StandardButton.Yes:
            return
        clear_upload_state(path)
        self._refresh_video_list()
        self.status_label.setText("Đã đặt lại trạng thái — có thể tải lên lại phần này.")

    def _upload_state_groups(self) -> tuple[list, list]:
        """(stuck, published) parts in the current selection, as (label, path) pairs.

        The two categories carry very different risk, so they are counted separately and
        never cleared by the same click.
        """
        from noveltrans.youtube_upload import is_published, needs_attention

        if self.project is None:
            return [], []
        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        whole = len(windows) == 1 and mode == "all"
        stuck: list = []
        published: list = []
        for i, window in enumerate(windows):
            path = self._part_output_path(window, whole_novel=whole)
            label = "Toàn bộ" if whole else f"Phần {i + 1}"
            if needs_attention(path):
                stuck.append((label, path))
            elif is_published(path):
                published.append((label, path))
        return stuck, published

    def _reset_all_upload_states(self) -> None:
        """Bulk-clear upload records, with the two risk levels chosen separately.

        A failed batch strands every part at once — clicking through 30 rows is the chore
        that sends people to delete sidecar files by hand. But "dở dang" and "đã tải lên"
        must not share a button: the first is nearly always safe, the second means we
        believe the video is live and clearing it invites a duplicate. So each is its own
        opt-in checkbox, and the dangerous one starts unticked.
        """
        from noveltrans.youtube_upload import clear_upload_state, has_remote_draft

        stuck, published = self._upload_state_groups()
        if not stuck and not published:
            QMessageBox.information(
                self,
                "Đặt lại trạng thái tải lên",
                "Không có phần nào ở trạng thái “dở dang” hay “đã tải lên”.",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Đặt lại trạng thái tải lên")
        layout = QVBoxLayout(dialog)

        stuck_check = QCheckBox(f"Đặt lại {len(stuck)} phần “dở dang”")
        stuck_check.setChecked(bool(stuck))
        stuck_check.setEnabled(bool(stuck))
        layout.addWidget(stuck_check)
        if stuck:
            with_draft = [label for label, path in stuck if has_remote_draft(path)]
            note = QLabel(
                f"    ⚠️ {len(with_draft)} phần đã kịp tạo video trên kênh "
                f"({', '.join(with_draft)}) — kiểm tra và xoá trên YouTube trước."
                if with_draft
                else "    Không phần nào kịp tạo video trên kênh — đặt lại là an toàn."
            )
            note.setProperty("muted", True)
            note.setWordWrap(True)
            layout.addWidget(note)

        published_check = QCheckBox(f"Bỏ đánh dấu {len(published)} phần “đã tải lên”")
        published_check.setChecked(False)  # dangerous: never pre-ticked
        published_check.setEnabled(bool(published))
        layout.addWidget(published_check)
        if published:
            warn = QLabel(
                "    ⚠️ Chỉ dùng khi các video này KHÔNG thực sự có trên kênh (ví dụ lần "
                "tải trước bị dừng giữa chừng). Nếu video vẫn còn và bạn tải lại, kênh "
                "sẽ có HAI bản."
            )
            warn.setProperty("muted", True)
            warn.setWordWrap(True)
            layout.addWidget(warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(480)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = (stuck if stuck_check.isChecked() else []) + (
            published if published_check.isChecked() else []
        )
        if not chosen:
            return
        for _label, path in chosen:
            clear_upload_state(path)
        self._refresh_video_list()
        self.status_label.setText(f"Đã đặt lại {len(chosen)} phần — có thể tải lên lại.")

    def _launch_upload(self, rows: list) -> None:
        if self._upload_worker is not None and self._upload_worker.isRunning():
            QMessageBox.information(self, "Tải lên YouTube", "Đang có phiên tải lên chạy.")
            return

        from noveltrans.youtube_upload import YouTubeUploadError, schedule_times

        scheduling = self.upload_visibility.currentData() == "schedule"
        times = (
            schedule_times(
                self.upload_start.dateTime().toPython(), len(rows), self.upload_spacing.value()
            )
            if scheduling
            else [None] * len(rows)
        )

        try:
            requests = [
                self._upload_request(w, label, pn, wn, publish_at=when)
                for (w, label, pn, wn), when in zip(rows, times)
            ]
            for request in requests:  # surface a bad input before opening a browser
                request.validate()
        except YouTubeUploadError as exc:
            QMessageBox.warning(self, "Tải lên YouTube", str(exc))
            return

        names = ", ".join(r.label for r in requests[:5])
        more = f" (+{len(requests) - 5})" if len(requests) > 5 else ""
        when_text = (
            f"\nHẹn giờ: {times[0]:%d/%m %H:%M}"
            + (f" → {times[-1]:%d/%m %H:%M}" if len(times) > 1 else "")
            if scheduling
            else f"\nChế độ: {self.upload_visibility.currentText()}"
        )
        # An upload is public and effectively irreversible once it publishes, so it gets
        # a confirmation the render step doesn't need.
        confirm = QMessageBox.question(
            self,
            "Tải lên YouTube",
            f"Sẽ tải {len(requests)} phần lên kênh đã đăng nhập:\n{names}{more}{when_text}\n\n"
            "Một cửa sổ Chrome sẽ mở ra và tự thao tác — đừng dùng nó khi đang chạy. Tiếp tục?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._upload_worker = YouTubeUploadWorker(requests, self)
        self._upload_worker.progress.connect(self._on_upload_progress)
        self._upload_worker.part_done.connect(self._on_upload_part_done)
        self._upload_worker.finished_ok.connect(self._on_upload_finished)
        self._upload_worker.failed.connect(self._on_upload_failed)
        self._upload_worker.needs_login.connect(self._on_upload_needs_login)
        track_worker(self._upload_worker)  # don't let the Mac sleep mid-upload

        self.upload_button.setEnabled(False)
        # Rendering would overwrite the very files being uploaded — keep them apart.
        self.video_button.setEnabled(False)
        self.redo_all_button.setEnabled(False)
        # One Chrome profile, one session: the two browser runs can never overlap.
        self.thumbnail_update_button.setEnabled(False)
        self.upload_cancel_button.setEnabled(True)
        self.progress.setMaximum(len(requests))
        self.progress.setValue(0)
        self.status_label.setText("⬆️ Bắt đầu tải lên YouTube…")
        self._upload_worker.start()

    def _on_upload_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if message:
            self.status_label.setText(f"⬆️ ({done}/{total}) {message}")

    def _on_upload_part_done(self, index: int, url: str, error: str) -> None:
        # Refresh as each part lands so the "Đã tải lên" column tracks reality during a
        # long run, not only at the end.
        self._refresh_video_list()

    def _reset_upload_ui(self) -> None:
        self.upload_button.setEnabled(True)
        self.video_button.setEnabled(True)
        self.redo_all_button.setEnabled(True)
        self.thumbnail_update_button.setEnabled(True)
        self.upload_cancel_button.setEnabled(False)
        self._refresh_video_list()

    def _on_upload_finished(self, uploaded: int, errors: int) -> None:
        self._reset_upload_ui()
        if uploaded and not errors:
            self.status_label.setText(f"✅ Đã tải {uploaded} phần lên YouTube.")
        elif uploaded:
            self.status_label.setText(
                f"⚠️ Đã tải {uploaded} phần, {errors} phần lỗi — xem cột “Đã tải lên”."
            )
        else:
            self.status_label.setText("Không tải lên được phần nào.")

    def _on_upload_failed(self, message: str) -> None:
        self._reset_upload_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Tải lên YouTube thất bại", message)

    def _on_upload_needs_login(self, message: str) -> None:
        self._reset_upload_ui()
        self.status_label.setText("")
        QMessageBox.information(
            self,
            "Cần đăng nhập YouTube",
            message + "\n\nVào Settings → “Đăng nhập YouTube”, rồi thử lại.",
        )

    def _cancel_upload(self) -> None:
        """Stop whichever browser run is live.

        One button for both, because only one can ever be running: they share the single
        persistent `.youtube-profile`, and Chromium refuses a second instance on one
        user-data-dir. A second "Dừng" would be a second control for one piece of state.
        """
        if self._upload_worker is not None and self._upload_worker.isRunning():
            self._upload_worker.cancel()
            self.status_label.setText("Đang dừng tải lên (chờ bước hiện tại kết thúc)…")
        elif self._thumbnail_worker is not None and self._thumbnail_worker.isRunning():
            self._thumbnail_worker.cancel()
            self.status_label.setText("Đang dừng cập nhật ảnh bìa…")
        elif self._playlist_worker is not None and self._playlist_worker.isRunning():
            self._playlist_worker.cancel()
            self.status_label.setText("Đang dừng sắp xếp danh sách phát…")

    # ------------------------------------------------------------- phụ đề .srt

    def _start_subtitles(self) -> None:
        """Write each part's .srt, recovering missing cues from the audio first.

        No ffmpeg render: the sidecar needs only the segment list and the cues, and going
        through `render_video` for one would cost ~26 minutes and ~250 MB per part to
        produce a 40 KB text file.
        """
        if self.project is None:
            QMessageBox.information(self, "Tạo phụ đề", "Chọn truyện trước.")
            return
        if self._subtitle_worker is not None and self._subtitle_worker.isRunning():
            return
        voice = self.voice_combo.currentData() or self.voice_combo.currentText().strip()
        mode = self.video_mode.currentData()

        self._subtitle_worker = SubtitleWorker(
            self.project.path,
            voice,
            mode,
            start=self.video_range_from.value() if mode == "range" else None,
            end=self.video_range_to.value() if mode == "range" else None,
            batch=self.video_batch_size.value() if mode == "batch" else None,
            use_translation=self.config.tts_use_translation,
            clean_text=self.config.tts_clean_text,
            clean_extra_remove=self.config.tts_clean_extra_remove,
            gap_seconds=self.config.tts_gap,
            speed=self.config.tts_speed,
            parent=self,
        )
        self._subtitle_worker.progress.connect(self._on_subtitle_progress)
        self._subtitle_worker.finished_ok.connect(self._on_subtitles_finished)
        self._subtitle_worker.failed.connect(self._on_subtitles_failed)
        track_worker(self._subtitle_worker)

        self.subtitle_button.setEnabled(False)
        self.progress.setValue(0)
        self.status_label.setText("💬 Đang tạo phụ đề…")
        self._subtitle_worker.start()

    def _on_subtitle_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if message:
            self.status_label.setText(f"💬 ({done}/{total}) {message}")

    def _on_subtitles_finished(self, written: int, backfilled: int, skipped: int) -> None:
        self.subtitle_button.setEnabled(True)
        parts = [f"✅ Đã ghi {written} file .srt"]
        if backfilled:
            parts.append(f"khôi phục mốc thời gian cho {backfilled} chương")
        if skipped:
            # Named, not hidden: a skipped part means the audio's silence pattern didn't
            # match the text, and guessing would have been worse than saying so.
            parts.append(
                f"⚠️ {skipped} phần chưa có phụ đề (không dò được mốc — tạo lại audio "
                "nếu cần)"
            )
        self.status_label.setText(" — ".join(parts) + ".")

    def _on_subtitles_failed(self, message: str) -> None:
        self.subtitle_button.setEnabled(True)
        self.status_label.setText("")
        QMessageBox.warning(self, "Tạo phụ đề thất bại", message)

    # -------------------------------------------------- danh sách phát YouTube

    def _fetch_playlists(self) -> None:
        """Read the channel's playlists into the combo, keeping whatever was typed."""
        if self._playlist_fetch_worker is not None and self._playlist_fetch_worker.isRunning():
            return
        self.playlist_fetch_button.setEnabled(False)
        self.status_label.setText("Đang đọc danh sách phát trên kênh…")
        self._playlist_fetch_worker = PlaylistFetchWorker(self)
        self._playlist_fetch_worker.fetched.connect(self._on_playlists_fetched)
        self._playlist_fetch_worker.failed.connect(self._on_playlists_failed)
        self._playlist_fetch_worker.needs_login.connect(self._on_upload_needs_login)
        self._playlist_fetch_worker.finished.connect(
            lambda: self.playlist_fetch_button.setEnabled(True)
        )
        self._playlist_fetch_worker.start()

    def _on_playlists_fetched(self, titles: list) -> None:
        """Repopulate the combo without losing what the user had typed.

        Losing a half-typed name to a background fetch would be its own small betrayal,
        and the combo is editable precisely so an unlisted name stays usable.
        """
        typed = self.upload_playlist.currentText()
        self.upload_playlist.clear()
        self.upload_playlist.addItems(list(titles))
        self.upload_playlist.setCurrentText(typed)
        self.status_label.setText(
            f"Đã đọc {len(titles)} danh sách phát trên kênh."
            if titles
            else "Kênh chưa có danh sách phát nào — gõ tên mới để tạo khi tải lên."
        )

    def _on_playlists_failed(self, message: str) -> None:
        self.status_label.setText("")
        QMessageBox.warning(self, "Không đọc được danh sách phát", message)

    def _playlist_sync_rows(self) -> list:
        """Parts with a video on the channel, in part order — same rule as 034's push."""
        if self.project is None:
            return []
        from noveltrans.youtube_upload import uploaded_video_id

        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        total = len(windows)
        rows = []
        for i, window in enumerate(windows):
            whole_novel = total == 1 and mode == "all"
            path = self._part_output_path(window, whole_novel=whole_novel)
            if not uploaded_video_id(path):
                continue
            rows.append((path, "Toàn bộ" if whole_novel else f"Phần {i + 1}"))
        return rows

    def _start_playlist_sync(self) -> None:
        """Empty the chosen playlist, then re-add every uploaded part in order."""
        from noveltrans.youtube_upload import PlaylistSyncRequest, YouTubeUploadError

        if self.project is None:
            QMessageBox.information(self, "Danh sách phát", "Chọn truyện trước.")
            return
        for worker in (self._playlist_worker, self._upload_worker, self._thumbnail_worker):
            if worker is not None and worker.isRunning():
                QMessageBox.information(
                    self, "Danh sách phát", "Đang có phiên trình duyệt khác chạy."
                )
                return

        playlist = self.upload_playlist.currentText().strip()
        if not playlist:
            QMessageBox.information(
                self, "Danh sách phát", "Chọn hoặc gõ tên danh sách phát trước."
            )
            return
        rows = self._playlist_sync_rows()
        if not rows:
            QMessageBox.information(
                self, "Danh sách phát", "Chưa có phần nào đã tải lên YouTube."
            )
            return

        requests = [
            PlaylistSyncRequest(video=path, label=label) for path, label in rows
        ]
        try:
            for request in requests:
                request.validate()
        except YouTubeUploadError as exc:
            QMessageBox.warning(self, "Danh sách phát", str(exc))
            return

        names = ", ".join(r.label for r in requests[:5])
        more = f" (+{len(requests) - 5})" if len(requests) > 5 else ""
        # This EMPTIES something viewers may be watching. The trade-off was accepted when
        # the feature was chosen; the dialog still has to state it, because agreeing to it
        # once is not the same as remembering it at the moment of clicking.
        confirm = QMessageBox.question(
            self,
            "Thêm vào danh sách phát",
            f"⚠️ Sẽ XOÁ HẾT video đang có trong danh sách phát “{playlist}”, rồi thêm lại "
            f"{len(requests)} phần theo đúng thứ tự:\n{names}{more}\n\n"
            "Mọi thứ tự sắp xếp thủ công sẽ mất, và danh sách phát sẽ trống trong lúc "
            "chạy — người xem có thể thấy điều đó.\n\n"
            "Một cửa sổ Chrome sẽ mở ra và tự thao tác. Tiếp tục?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._playlist_worker = PlaylistSyncWorker(playlist, requests, self)
        self._playlist_worker.progress.connect(self._on_playlist_progress)
        self._playlist_worker.finished_ok.connect(self._on_playlist_finished)
        self._playlist_worker.failed.connect(self._on_playlist_failed)
        self._playlist_worker.needs_login.connect(self._on_playlist_needs_login)
        track_worker(self._playlist_worker)

        self.playlist_sync_button.setEnabled(False)
        self.playlist_fetch_button.setEnabled(False)
        self.upload_button.setEnabled(False)
        self.thumbnail_update_button.setEnabled(False)
        self.video_button.setEnabled(False)
        self.redo_all_button.setEnabled(False)
        self.upload_cancel_button.setEnabled(True)
        self.progress.setMaximum(len(requests))
        self.progress.setValue(0)
        self.status_label.setText("▶️ Bắt đầu sắp xếp danh sách phát…")
        self._playlist_worker.start()

    def _on_playlist_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if message:
            self.status_label.setText(f"▶️ ({done}/{total}) {message}")

    def _reset_playlist_ui(self) -> None:
        self.playlist_sync_button.setEnabled(True)
        self.playlist_fetch_button.setEnabled(True)
        self.upload_button.setEnabled(True)
        self.thumbnail_update_button.setEnabled(True)
        self.video_button.setEnabled(True)
        self.redo_all_button.setEnabled(True)
        self.upload_cancel_button.setEnabled(False)

    def _on_playlist_finished(self, removed: int, added: int, errors: int) -> None:
        self._reset_playlist_ui()
        note = f"✅ Danh sách phát: gỡ {removed}, thêm lại {added} phần theo thứ tự."
        self.status_label.setText(note if not errors else f"{note} {errors} phần lỗi.")

    def _on_playlist_failed(self, message: str) -> None:
        self._reset_playlist_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Danh sách phát thất bại", message)

    def _on_playlist_needs_login(self, message: str) -> None:
        self._reset_playlist_ui()
        self._on_upload_needs_login(message)

    # ------------------------------------------- cập nhật ảnh bìa trên YouTube

    def _thumbnail_update_rows(self) -> list:
        """Parts with a video on the channel AND a cover to push, as (window, label, whole).

        Eligibility is `uploaded_video_id()`, deliberately not `is_published()`: a
        scheduled or private video still has an editable thumbnail, and replacing one
        cannot duplicate anything — so the paranoia that guards `_pending_upload_rows`
        has nothing to protect here. Parts marked uploaded by hand carry no video id and
        drop out on their own, which is right: we don't know which video they mean.
        """
        if self.project is None:
            return []
        from noveltrans.youtube_upload import uploaded_video_id

        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        total = len(windows)
        rows = []
        for i, window in enumerate(windows):
            whole_novel = total == 1 and mode == "all"
            path = self._part_output_path(window, whole_novel=whole_novel)
            if not uploaded_video_id(path):
                continue
            if not self._part_sidecar(window, whole_novel, ".jpg").is_file():
                continue
            label = "Toàn bộ" if whole_novel else f"Phần {i + 1}"
            rows.append((window, label, whole_novel))
        return rows

    def _thumbnail_request(self, window, label, whole_novel):
        """One `ThumbnailRequest` from the .jpg sidecar beside the part's .mp4."""
        from noveltrans.youtube_upload import ThumbnailRequest, uploaded_video_id

        video = self._part_output_path(window, whole_novel=whole_novel)
        return ThumbnailRequest(
            video=video,
            thumbnail=self._part_sidecar(window, whole_novel, ".jpg"),
            video_id=uploaded_video_id(video),
            label=label,
        )

    def _start_thumbnail_update(self) -> None:
        """Push the current cover onto every already-uploaded part in the selection.

        The batch case is the real one: "Tạo lại tất cả ảnh bìa" and the cover editor's
        "áp dụng cho mọi phần" both rewrite every part's .jpg at once, so one font change
        leaves every published part wearing the old cover simultaneously.
        """
        if self.project is None:
            QMessageBox.information(self, "Cập nhật ảnh bìa", "Chọn truyện trước.")
            return
        rows = self._thumbnail_update_rows()
        if not rows:
            QMessageBox.information(
                self,
                "Cập nhật ảnh bìa",
                "Không có phần nào để cập nhật ảnh bìa (chưa tải lên YouTube, hoặc "
                "chưa có ảnh bìa).",
            )
            return
        self._launch_thumbnail_update(rows)

    def _update_thumbnail_one(self, window, part_num, whole_novel) -> None:
        """Push one part's cover — the per-row path.

        Built straight from the row's own window, like `_upload_one`, rather than by
        looking the part up in `_thumbnail_update_rows()`: that method re-plans the
        windows from scratch on every call, so the fresh objects would never match the
        one this row captured.
        """
        label = "Toàn bộ" if whole_novel else f"Phần {part_num}"
        self._launch_thumbnail_update([(window, label, whole_novel)])

    def _launch_thumbnail_update(self, rows: list) -> None:
        from noveltrans.youtube_upload import (
            YouTubeUploadError,
            is_published,
            thumbnail_is_current,
        )

        if self._thumbnail_worker is not None and self._thumbnail_worker.isRunning():
            QMessageBox.information(
                self, "Cập nhật ảnh bìa", "Đang có phiên cập nhật ảnh bìa chạy."
            )
            return
        if self._upload_worker is not None and self._upload_worker.isRunning():
            QMessageBox.information(
                self, "Cập nhật ảnh bìa", "Đang có phiên tải lên chạy — chờ xong đã."
            )
            return

        try:
            requests = [self._thumbnail_request(w, label, wn) for (w, label, wn) in rows]
            for request in requests:  # surface a bad image before opening a browser
                request.validate()
        except YouTubeUploadError as exc:
            QMessageBox.warning(self, "Cập nhật ảnh bìa", str(exc))
            return

        names = ", ".join(r.label for r in requests[:5])
        more = f" (+{len(requests) - 5})" if len(requests) > 5 else ""
        public = sum(1 for r in requests if is_published(r.video))
        fresh = sum(1 for r in requests if thumbnail_is_current(r.video, r.thumbnail))
        # It touches videos the public may already see, so it confirms — but the tone is
        # informational, not the two-copies klaxon of `_reset_upload_state`: pushing a
        # thumbnail is idempotent and re-doable by pushing the old image back.
        warn = (
            f"\n\n⚠️ {public} phần đang hiển thị trên kênh — người xem sẽ thấy ảnh bìa "
            "mới (YouTube có thể mất vài phút để cập nhật)."
            if public
            else ""
        )
        already = f"\n{fresh} phần đã dùng đúng ảnh bìa này rồi (sẽ đẩy lại)." if fresh else ""
        confirm = QMessageBox.question(
            self,
            "Cập nhật ảnh bìa",
            f"Sẽ đổi ảnh bìa của {len(requests)} video đã đăng trên kênh:\n"
            f"{names}{more}{already}{warn}\n\n"
            "Một cửa sổ Chrome sẽ mở ra và tự thao tác — đừng dùng nó khi đang chạy. "
            "Tiếp tục?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._thumbnail_worker = YouTubeThumbnailWorker(requests, self)
        self._thumbnail_worker.progress.connect(self._on_thumbnail_progress)
        self._thumbnail_worker.part_done.connect(self._on_thumbnail_part_done)
        self._thumbnail_worker.finished_ok.connect(self._on_thumbnail_finished)
        self._thumbnail_worker.failed.connect(self._on_thumbnail_failed)
        self._thumbnail_worker.needs_login.connect(self._on_thumbnail_needs_login)
        track_worker(self._thumbnail_worker)  # don't let the Mac sleep mid-run

        self.thumbnail_update_button.setEnabled(False)
        # One browser profile between them, and a render would rewrite the very .jpg
        # being pushed — all three have to stay apart.
        self.upload_button.setEnabled(False)
        self.video_button.setEnabled(False)
        self.redo_all_button.setEnabled(False)
        self.thumb_regen_button.setEnabled(False)
        self.upload_cancel_button.setEnabled(True)
        self.progress.setMaximum(len(requests))
        self.progress.setValue(0)
        self.status_label.setText("🖼️ Bắt đầu cập nhật ảnh bìa…")
        self._thumbnail_worker.start()

    def _on_thumbnail_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if message:
            self.status_label.setText(f"🖼️ ({done}/{total}) {message}")

    def _on_thumbnail_part_done(self, index: int, url: str, error: str) -> None:
        # Refresh as each part lands so the tooltip tracks reality during a long run.
        self._refresh_video_list()

    def _reset_thumbnail_ui(self) -> None:
        self.thumbnail_update_button.setEnabled(True)
        self.upload_button.setEnabled(True)
        self.video_button.setEnabled(True)
        self.redo_all_button.setEnabled(True)
        self.thumb_regen_button.setEnabled(True)
        self.upload_cancel_button.setEnabled(False)
        self._refresh_video_list()

    def _on_thumbnail_finished(self, updated: int, errors: int) -> None:
        self._reset_thumbnail_ui()
        if updated and not errors:
            self.status_label.setText(
                f"✅ Đã đổi ảnh bìa {updated} video. YouTube có thể mất vài phút mới "
                "hiện ảnh mới."
            )
        elif updated:
            self.status_label.setText(
                f"⚠️ Đã đổi ảnh bìa {updated} video, {errors} phần lỗi."
            )
        else:
            self.status_label.setText("Không đổi được ảnh bìa phần nào.")

    def _on_thumbnail_failed(self, message: str) -> None:
        self._reset_thumbnail_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Cập nhật ảnh bìa thất bại", message)

    def _on_thumbnail_needs_login(self, message: str) -> None:
        self._reset_thumbnail_ui()
        self.status_label.setText("")
        QMessageBox.information(
            self,
            "Cần đăng nhập YouTube",
            message + "\n\nVào Settings → “Đăng nhập YouTube”, rồi thử lại.",
        )

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
        if self._thumbnail_worker is not None and self._thumbnail_worker.isRunning():
            self._thumbnail_worker.cancel()
            self._thumbnail_worker.wait(60_000)
        if self._playlist_worker is not None and self._playlist_worker.isRunning():
            self._playlist_worker.cancel()
            self._playlist_worker.wait(60_000)
        if self._playlist_fetch_worker is not None and self._playlist_fetch_worker.isRunning():
            self._playlist_fetch_worker.wait(60_000)
        if self._subtitle_worker is not None and self._subtitle_worker.isRunning():
            self._subtitle_worker.cancel()
            self._subtitle_worker.wait(60_000)
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.wait(60_000)
        if self._tags_worker is not None and self._tags_worker.isRunning():
            self._tags_worker.wait(60_000)
        if self._image_prompt_worker is not None and self._image_prompt_worker.isRunning():
            self._image_prompt_worker.wait(60_000)
        if self._voices_worker is not None and self._voices_worker.isRunning():
            self._voices_worker.wait(5_000)

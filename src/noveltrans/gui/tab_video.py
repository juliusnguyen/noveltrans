"""Tab 5 — Video: render per-chapter audio into music-player videos, with auto-generated
title / description / thumbnail / tags for each part.

Split out of the audio tab (feature 025): it owns its own project picker, voice selector,
and status/progress/cancel widgets. When exporting, each produced part-video gets, written
next to the `.mp4`:
  * `<name>.title.txt`  — "{tên truyện} - Phần {N}"
  * `<name>.txt`        — the YouTube description (original+VN title/author, chapter count,
                           the chapter timestamp table, "Tạo bởi: …"), capped to YouTube's
                           5000 characters
  * `<name>.tags.txt`   — the novel-level YouTube tags (LLM-generated, like "2. Dịch")
  * `<name>.jpg`        — a thumbnail composited from a chosen base image + styled text

The `.txt` is not write-once: opening a novel re-syncs it from the database for every
rendered part, so renaming a chapter updates what an already-rendered part will upload
(`_resync_description_sidecars`). The exception is a description replaced by "Shorten by
AI" in "Chi tiết phần" — its shortened titles can't be rebuilt from the database, so a
stale one is flagged on the row instead of overwritten.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
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

from noveltrans import video_settings
from noveltrans.config import LLM_ENGINES, AppConfig, translator_labels
from noveltrans.gui.job_popup import BROWSER_PAUSE_HINT
from noveltrans.gui.jobs import job_registry
from noveltrans.gui.keep_awake import track_worker
from noveltrans.gui.widgets import (
    CheckableHeaderView,
    PauseButton,
    ProjectPicker,
    audio_source_label,
)
from noveltrans.gui.workers import (
    CompletionWorker,
    ShortenTitlesWorker,
    TagsWorker,
    TtsVoicesWorker,
    VideoPreviewWorker,
    PlaylistFetchWorker,
    PlaylistSyncWorker,
    SubtitleUploadWorker,
    SubtitleWorker,
    VideoWorker,
    YouTubeThumbnailWorker,
    YouTubeUploadWorker,
)
from noveltrans.storage import NovelProject
from noveltrans.tts.description import (
    YOUTUBE_DESCRIPTION_CHAR_LIMIT,
    description_length,
    was_truncated,
)

# Engines that can generate tags (LLMs). Google translate-only is excluded.
_TAG_ENGINES = LLM_ENGINES
_IMAGE_FILTER = "Ảnh (*.png *.jpg *.jpeg *.webp *.bmp)"


# Combo entry for the site's own audio edition. A sentinel rather than a voice id
# because a release is not synthesized and has no voice: it selects a different SOURCE
# of parts (`plan_source_windows`), not a different narrator.
SOURCE_AUDIO_KEY = "__source_audio__"


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
        self._subtitle_upload_worker: SubtitleUploadWorker | None = None
        self._preview_worker: VideoPreviewWorker | None = None
        self._voices_worker: TtsVoicesWorker | None = None
        # The TTS engine's catalogue, kept only as a FALLBACK for a project that has no
        # audio yet. What the combo normally lists is what the project actually holds —
        # see `_refresh_audio_sources`.
        self._tts_voices: list[tuple[str, str]] = []
        self._tags_worker: TagsWorker | None = None
        self._image_prompt_worker: CompletionWorker | None = None
        self._shorten_worker: ShortenTitlesWorker | None = None
        self._render_after_tags = False  # auto-generate tags, then start the render
        # guards the "Trạng thái" / "Đã tải lên" checkbox handlers while the table is
        # being repopulated
        self._suppress_status_toggle = False
        # Populated by `_locked_batch_windows` (batch mode only), consumed by
        # `_part_number` / `_chapter_range_item` so they don't each re-scan the video
        # directory — see `plan_locked_video_windows` for why part numbers can't be pure
        # grid arithmetic once a window is locked.
        self._locked_part_numbers: dict[int, int] = {}
        self._locked_committed: dict[int, int] = {}
        self._locked_manual: dict[int, int] = {}
        # Part folder names whose `.txt` description no longer matches the database but
        # was customised (AI-shortened), so `_resync_description_sidecars` refused to
        # overwrite it. Consumed by `_chapter_range_item` and the detail dialog.
        self._stale_descriptions: set[str] = set()
        # a persistent, non-modal preview window so the color can be tuned live
        self._preview_dialog: QDialog | None = None
        self._preview_label: QLabel | None = None
        self._preview_status: QLabel | None = None
        self._preview_color_button: QPushButton | None = None
        self._preview_controls: list = []
        # The open novel's effective video settings (see noveltrans.video_settings).
        # Every render/preview reads from here rather than from `config`, so one novel's
        # background image can never reach another novel's video. Seeded from the global
        # values so the widgets built below still have something to show before any novel
        # is picked.
        self._video_settings: dict = video_settings.effective({}, config)
        # Set while pushing settings into widgets, so the change handlers those widgets
        # emit don't write the values straight back out (and, worse, onto whichever novel
        # happens to be open).
        self._loading_video_settings = False

        # --- top row: novel + voice
        self.picker = ProjectPicker()
        self.picker.project_selected.connect(self._on_project_selected)

        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(200)
        self.voice_combo.setToolTip(
            "Nguồn audio dùng để tạo video: giọng TTS đã tạo, hoặc bản đọc tải từ trang."
        )
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

        # The boxes above seed their widgets from `config` as they are built, which for
        # identity settings means the last-used values. Now that every widget exists,
        # resolve them properly: no novel is open yet, so identity resets to defaults.
        self._load_video_settings_for_project()

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
            lambda v: self._save_video_setting("video_batch_size", v)
        )
        self.video_batch_size.setToolTip(
            "Số chương gộp vào một video. Mô tả YouTube tối đa 5000 ký tự, nên mục lục "
            "chương chỉ chứa được khoảng 70–110 chương; phần dư sẽ gộp thành một dòng "
            "“… còn N chương nữa”."
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
            lambda on: self._save_video_setting("video_burn_subtitles", on)
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
        self.pause_button = PauseButton()
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
        row2.addWidget(self.pause_button)
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
        # Row selection exists only to drive the split/merge context menu below — select
        # one row to split it, two adjacent rows to merge them.
        self.video_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.video_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.video_list.setAlternatingRowColors(True)
        self.video_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.video_list.customContextMenuRequested.connect(self._on_video_list_context_menu)
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

        # Both "Trạng thái" and "Đã tải lên" ticks are controls, not just status — see
        # _on_status_toggled. A SINGLE dispatching slot, not one connection per column:
        # either handler may rebuild the table (deleting every QTableWidgetItem) via
        # _refresh_video_list, so a second independent slot touching the same now-stale
        # `item` afterward would be a use-after-free.
        self.video_list.itemChanged.connect(self._on_status_toggled)

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

        The playlist and visibility CHOICE are per-novel (`NovelMeta.upload_playlist` /
        `.upload_visibility`), loaded/saved in `_on_project_selected` / `_save_upload_
        playlist_choice` / `_on_visibility_changed` — so switching novels doesn't leak one
        novel's selection into another's. The actual SCHEDULE (`upload_start`/
        `upload_spacing`) stays per-run and un-persisted: "một phần mỗi tối 20h từ thứ hai"
        starting from *today* is a decision made fresh each time, not a fact about the
        project — reusing a stale date/time would be actively wrong, not just inconvenient.
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
        # Per-novel, like the background image — so picking a playlist for this novel
        # doesn't leave it selected the next time a DIFFERENT novel is opened.
        self.upload_playlist.currentTextChanged.connect(self._save_upload_playlist_choice)

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

        self.subtitle_upload_button = QPushButton("💬 Tải phụ đề lên")
        self.subtitle_upload_button.setToolTip(
            "Tải file .srt của những phần ĐÃ có video trên YouTube lên làm phụ đề. "
            "Không tải lại video, không đổi ảnh bìa.\n\nCần bấm “Tạo phụ đề (.srt)” trước."
        )
        self.subtitle_upload_button.clicked.connect(self._start_subtitle_upload)

        self.upload_cancel_button = QPushButton("Dừng")
        self.upload_cancel_button.setEnabled(False)
        self.upload_cancel_button.clicked.connect(self._cancel_upload)
        # The browser runs get their own pause, next to their own Dừng. One shared button
        # in the render row would sit in the wrong half of the tab AND get stolen from a
        # running render the moment an upload started.
        self.upload_pause_button = PauseButton()
        self.upload_pause_button.set_extra_hint(BROWSER_PAUSE_HINT)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Danh sách phát:"))
        action_row.addWidget(self.upload_playlist, stretch=1)
        action_row.addWidget(self.playlist_fetch_button)
        action_row.addWidget(self.playlist_sync_button)
        action_row.addWidget(self.upload_reset_button)
        action_row.addWidget(self.thumbnail_update_button)
        action_row.addWidget(self.subtitle_upload_button)
        action_row.addWidget(self.upload_button)
        action_row.addWidget(self.upload_cancel_button)
        action_row.addWidget(self.upload_pause_button)

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
        """Show the schedule controls only when they mean something, and remember the
        choice on the open novel (per-novel, not the old always-reset-to-private default —
        see NovelMeta.upload_visibility)."""
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
        if self.project is not None:
            self.project.save_upload_visibility(self.upload_visibility.currentData())

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

    def _voice_label(self, voice: str) -> str:
        """How to name this edition in a dialog — never the raw `SOURCE_AUDIO_KEY`.

        The sentinel is an internal key; "giọng __source_audio__" in a confirm box reads
        as a bug. The site's own audio has no voice at all, so it gets named, not voiced.
        """
        return "audio từ nguồn" if voice == SOURCE_AUDIO_KEY else f"giọng {voice}"

    def _unit_label(self, voice: str) -> str:
        """What a window's members are called. A source window groups RELEASES (volumes),
        not chapters — `plan_source_windows` keys them by `SourceAudio.index` — so counting
        them as "chương" in a dialog overstates the run by a wide margin (21 mục ≠ 21 chương).
        """
        return "mục" if voice == SOURCE_AUDIO_KEY else "chương"

    def _nothing_selected_message(self, voice: str) -> str:
        """Why the plan came out empty — mirrors `VideoWorker._nothing_message()`.

        The two editions need different advice: "chưa voice hoá chương nào" is useless
        when the user picked the site's own audio and simply hasn't downloaded it yet.
        """
        if voice == SOURCE_AUDIO_KEY:
            return "Chưa tải mục audio nào từ trang nguồn trong phạm vi đã chọn."
        return f"Không có chương nào có audio giọng {voice} trong phạm vi đã chọn."

    def _windows_for_current_selection(self, *, honor_committed: bool = True) -> list:
        """The parts (`MergeWindow`s) implied by the current voice/mode/range/batch.

        `honor_committed=False` is the redo-all view: manual split/merge boundaries still
        hold (they encode a real constraint that outlives a re-render), but already-"đã
        tạo" commits do not — the same asymmetry `VideoWorker.run()` applies when
        `skip_existing` is False, so the confirm dialog counts exactly what gets rendered.
        """
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
        if voice == SOURCE_AUDIO_KEY:
            from noveltrans.tts.merge import plan_source_windows

            # Numbering counts RELEASES here, so "phần 1..N" runs 1..21 for a novel with
            # 21 volumes rather than following the chapter numbers they cover.
            #
            # Nothing is locked for the source edition (see `_locked_batch_windows` and
            # VideoWorker's own source branch), and the caches are per-plan: leaving a
            # chapter-voice plan's entries behind would make `_part_number` hand a release
            # window a chapter part number — disagreeing with the number the worker derives
            # — and `_chapter_range_item` paint a phantom "bị khoá" warning on its row.
            self._locked_part_numbers = {}
            self._locked_committed = {}
            self._locked_manual = {}
            return plan_source_windows(
                self.project.source_audio(), mode, start=start, end=end, batch=batch
            )
        if mode == "batch":
            return self._locked_batch_windows(voice, batch, honor_committed=honor_committed)
        return plan_merge_windows(
            self.project.chapters(), voice, mode, start=start, end=end, batch=batch
        )

    def _locked_batch_windows(
        self, voice: str, batch: int, *, honor_committed: bool = True
    ) -> list:
        """Batch windows, honoring already-"đã tạo" commits AND manual split/merge
        boundaries — see `noveltrans.tts.video.plan_locked_video_windows`.

        A part rendered (or manually marked "đã tạo") before translation caught up keeps
        its original, shorter chapter span — new chapters start the *next* part instead of
        silently growing an already-"đã tạo" one out from under an already-uploaded video.
        A part the user split or merged by hand (see `noveltrans.video_windows`) keeps that
        exact boundary too — manual entries win over an auto-discovered commit at the same
        starting chapter, since the user's explicit choice is more authoritative than
        whatever happens to be sitting on disk.

        Caches each window's true part number (`_locked_part_numbers`), the raw commit map
        (`_locked_committed`) and the manual map (`_locked_manual`) so `_part_number` /
        `_chapter_range_item` / the split-merge context menu don't each re-derive them —
        see `plan_locked_video_windows` for why a locked window's part number can deviate
        from plain grid arithmetic.
        """
        from noveltrans.storage.project import slugify
        from noveltrans.tts.video import (
            discover_committed_video_windows,
            plan_locked_video_windows,
        )
        from noveltrans.video_windows import read_manual_windows

        slug = slugify(self.project.meta.translated_title or self.project.meta.title)
        committed = (
            discover_committed_video_windows(self.project.video_dir, slug)
            if honor_committed
            else {}
        )
        manual = read_manual_windows(self.project.path)
        plan = plan_locked_video_windows(
            self.project.chapters(), voice, batch, {**committed, **manual}
        )
        # Only the table's own view (honor_committed=True) may refresh the caches. A
        # redo-all preview deliberately ignores commits, and writing that narrower plan
        # here would mis-number a per-row "Tạo lại" clicked before the next refresh —
        # `_render_one` reads `_part_number` without re-planning first.
        if honor_committed:
            self._locked_part_numbers = {w.first_num: part_num for part_num, w in plan}
            self._locked_committed = committed
            self._locked_manual = manual
        return [w for _, w in plan]

    def _part_number(self, window) -> int:
        """This window's real part number, from its chapter range — see merge.part_number.

        Never the row's index: the parts list, the covers and the upload rows are all built
        from the *current selection*, so numbering by position renamed a part the moment it
        was viewed or re-rendered on its own.

        Batch mode looks this up from `_locked_part_numbers` (populated by the most recent
        `_locked_batch_windows` call — always freshly recomputed before this is consulted,
        since every caller of `_part_number` first calls `_windows_for_current_selection`
        in the same method). Range/whole-novel mode has no batch grid to lock, so it keeps
        the plain arithmetic.
        """
        if self.video_mode.currentData() == "batch":
            cached = self._locked_part_numbers.get(window.first_num)
            if cached is not None:
                return cached
        from noveltrans.tts.merge import part_number

        return part_number(window.first_num, self.video_batch_size.value())

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

    def _novel_slug(self) -> str:
        """The slug every rendered folder and sidecar of this novel is named after.

        NOT `display_name()` — same reason `_part_output_path` spells this out: the slug is
        baked into names already on disk, so keying it to an editable title would strand
        every rendered part.
        """
        from noveltrans.storage.project import slugify

        return slugify(self.project.meta.translated_title or self.project.meta.title)

    def _part_dir_name(self, window, whole_novel: bool = False) -> str:
        """This part's folder name — the key `_stale_descriptions` is stored under."""
        from noveltrans.tts.video import video_part_dir_name

        if self.project is None:
            return ""
        return video_part_dir_name(
            self._novel_slug(), window.first_num, window.last_num, whole_novel=whole_novel
        )

    # ------------------------------------------------------------ split / merge parts

    def _on_video_list_context_menu(self, pos) -> None:
        """Right-click 1+ part rows: render just the selection, split one row, merge 2
        adjacent rows, or bulk-set "Trạng thái" / "Đã tải lên" for every selected row.

        Split/merge are batch-mode only — "range"/"all" has no fixed grid of parts to
        split a boundary out of or merge a boundary away (and never has more than one row
        to select from in the first place). "Tạo video" and the bulk status actions apply
        in any mode, same as each row's own button/checkbox.
        """
        row = self.video_list.rowAt(pos.y())
        if row < 0 or self.project is None:
            return
        selection = self.video_list.selectionModel()
        selected_rows = sorted({idx.row() for idx in selection.selectedRows()})
        # Right-clicking outside the current multi-selection starts a fresh single one —
        # the usual behavior, and what lets a lone right-click always mean "this row".
        if row not in selected_rows:
            self.video_list.selectRow(row)
            selected_rows = [row]

        windows = self._windows_for_current_selection()
        if not selected_rows or any(r >= len(windows) for r in selected_rows):
            return
        selected_windows = [windows[r] for r in selected_rows]
        mode = self.video_mode.currentData()
        whole_novel = mode == "all" and len(windows) == 1
        paths = [self._part_output_path(w, whole_novel=whole_novel) for w in selected_windows]

        menu = QMenu(self)
        render_action = menu.addAction("Tạo video")
        render_action.triggered.connect(lambda: self._render_selected_parts(selected_windows))
        menu.addSeparator()
        # Not for the source edition: `video_manual_windows.json` is keyed by CHAPTER
        # number, while these windows are keyed by release ordinal. Writing one into the
        # other's map would silently reshape the chapter plan — a "split" of releases 1-10
        # would come back as a split of chương 1-10. See `_windows_for_current_selection`.
        splittable = mode == "batch" and self.voice_combo.currentData() != SOURCE_AUDIO_KEY
        if splittable and len(selected_windows) == 1:
            window = selected_windows[0]
            action = menu.addAction("Tách phần…")
            action.setEnabled(len(window.chapters) >= 2)
            action.triggered.connect(lambda: self._split_part(window))
            menu.addSeparator()
        elif splittable and len(selected_windows) == 2:
            window_a, window_b = selected_windows
            adjacent = window_a.last_num + 1 == window_b.first_num
            action = menu.addAction("Gộp 2 phần liền kề")
            action.setEnabled(adjacent)
            if not adjacent:
                action.setToolTip("Chỉ gộp được 2 phần liền kề nhau (không có khoảng trống).")
            action.triggered.connect(lambda: self._merge_parts(window_a, window_b))
            menu.addSeparator()

        created_on = menu.addAction("Đánh dấu \"Đã tạo\"")
        created_on.triggered.connect(lambda: self._bulk_set_created(paths, True))
        created_off = menu.addAction("Đánh dấu \"Chưa tạo\"")
        created_off.triggered.connect(lambda: self._bulk_set_created(paths, False))
        menu.addSeparator()
        upload_on = menu.addAction("Đánh dấu \"Đã tải lên\"")
        upload_on.triggered.connect(lambda: self._bulk_set_uploaded(paths, True))
        upload_off = menu.addAction("Đánh dấu \"Chưa tải lên\"")
        upload_off.triggered.connect(lambda: self._bulk_set_uploaded(paths, False))

        menu.exec(self.video_list.viewport().mapToGlobal(pos))

    def _split_part(self, window) -> None:
        """Split one part into two, the last N chapters becoming a new part.

        The boundary is remembered for this novel (`noveltrans.video_windows`) — every
        future "Tạo video" (and even "Tạo lại tất cả video") keeps honoring it, since a
        split typically exists to stay under YouTube's 12h cap and that constraint doesn't
        go away.
        """
        total = len(window.chapters)
        if total < 2:
            QMessageBox.information(
                self, "Tách phần", "Phần này chỉ có 1 chương — không thể tách."
            )
            return
        default = min(5, total - 1)
        tail, ok = QInputDialog.getInt(
            self, "Tách phần",
            f"Phần này có {total} chương (chương {window.first_num}–{window.last_num}).\n"
            "Số chương CUỐI muốn cắt ra thành phần mới:",
            default, 1, total - 1,
        )
        if not ok:
            return

        if not self._confirm_restructure(
            [window], title="Tách phần",
            action_desc=f"tách thành 2 phần (chương {window.first_num}–"
            f"{window.last_num - tail} và {window.last_num - tail + 1}–{window.last_num})",
        ):
            return

        from noveltrans.video_windows import split_window

        split_window(self.project.path, window.first_num, window.last_num, tail)
        self._delete_rendered_part(window)
        self._refresh_video_list()
        self.status_label.setText(
            f"Đã tách phần chương {window.first_num}–{window.last_num} thành 2 phần."
        )

    def _merge_parts(self, window_a, window_b) -> None:
        """Merge two adjacent parts into one — the inverse of `_split_part`.

        Also how an earlier split gets undone: merging its two halves back together.
        """
        if not self._confirm_restructure(
            [window_a, window_b], title="Gộp phần",
            action_desc=f"gộp thành 1 phần (chương {window_a.first_num}–{window_b.last_num})",
        ):
            return

        from noveltrans.video_windows import merge_windows

        merge_windows(
            self.project.path,
            window_a.first_num, window_a.last_num,
            window_b.first_num, window_b.last_num,
        )
        self._delete_rendered_part(window_a)
        self._delete_rendered_part(window_b)
        self._refresh_video_list()
        self.status_label.setText(
            f"Đã gộp chương {window_a.first_num}–{window_a.last_num} và "
            f"{window_b.first_num}–{window_b.last_num} thành 1 phần."
        )

    def _confirm_restructure(self, windows: list, *, title: str, action_desc: str) -> bool:
        """One confirmation covering every affected part, warning louder if any is
        already uploaded — splitting/merging changes each part's file name, so an
        already-published video on YouTube does NOT update; the old upload just orphans."""
        from noveltrans.youtube_upload import is_published

        rendered = [
            w for w in windows
            if self._part_output_path(w, whole_novel=False).is_file()
        ]
        uploaded = [
            w for w in windows
            if is_published(self._part_output_path(w, whole_novel=False))
        ]
        message = f"Sẽ {action_desc}."
        if rendered:
            message += (
                f"\n\n{len(rendered)} phần trong đó đã có video — file cũ sẽ bị XOÁ, "
                "cần tạo lại video cho (các) phần mới."
            )
        if uploaded:
            message += (
                f"\n\n⚠️ {len(uploaded)} phần đã tải lên YouTube. Video cũ trên kênh sẽ "
                "KHÔNG tự cập nhật theo ranh giới mới — bạn cần tự xử lý (xoá/thay) trên "
                "YouTube nếu muốn."
            )
        message += "\n\nTiếp tục?"
        return QMessageBox.question(self, title, message) == QMessageBox.StandardButton.Yes

    def _delete_rendered_part(self, window, *, whole_novel: bool = False) -> None:
        """Remove a part's rendered file + every sidecar, after its boundary changed.

        The whole per-part subfolder is removed in one go (video + title/description/tags/
        thumbnail/upload/created sidecars all live there — see feature 026). A legacy flat
        render (predates the per-folder layout) has no dedicated folder to remove; only its
        .mp4 is deleted and its sidecars are left, matching how legacy renders are already
        treated as a read-only compatibility case elsewhere (`discover_committed_video_windows`).
        """
        import shutil

        out = self._part_output_path(window, whole_novel=whole_novel)
        if out.parent.name == out.stem and out.parent.is_dir():
            shutil.rmtree(out.parent)
        elif out.is_file():
            out.unlink()

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

    def _chapter_range_item(
        self, window, mode: str, total_chapters: int = 0, novel_title: str = "",
        whole_novel: bool = False,
    ) -> QTableWidgetItem:
        """The "Chương" cell — flagged when something about this part's span needs saying.

        Three warnings share the one ⚠️ + amber treatment, joined into one tooltip:

        1. The batch window is *locked short*. A window counts as locked once
           `_locked_batch_windows` found it either already "đã tạo" (rendered, or manually
           ticked) on disk, or manually split/merged by hand — see
           `noveltrans.tts.video.plan_locked_video_windows`. If it also has fewer chapters
           than the configured batch size, it's frozen there permanently: new chapters will
           never grow it, they start the next part instead. Worth calling out, since without
           this a stuck-at-8-of-10 part looks identical to one that's simply still waiting
           for its 9th and 10th chapters to get audio.
        2. The part holds more chapters than a 5000-character YouTube description can index.
           This is the batch-size pre-warning: the batch spinbox already re-runs
           `_refresh_video_list`, so it appears and disappears live as the user drags it.
           Flagged, never enforced — the safe maximum depends on how long this part's
           chapter titles happen to be, and the 12h video cap next door is only flagged too.
        3. The part's `.txt` description went stale (a chapter was renamed) but had been
           AI-shortened, so `_resync_description_sidecars` left it alone rather than
           destroying titles that can't be rebuilt from the database.
        """
        text = f"chương {window.first_num}–{window.last_num} ({len(window.chapters)} chương)"
        item = QTableWidgetItem(text)
        batch_size = self.video_batch_size.value()
        manual = window.first_num in self._locked_manual
        locked = (
            mode == "batch"
            and (manual or window.first_num in self._locked_committed)
            and len(window.chapters) < batch_size
        )
        warnings: list[str] = []
        if locked:
            reason = (
                "bị khoá ở mức này vì đã tách/gộp thủ công."
                if manual
                else "bị khoá ở mức này vì đã tạo trước khi đủ chương."
            )
            warnings.append(
                f"Phần này có {len(window.chapters)}/{batch_size} chương — {reason} "
                "Chương mới sẽ vào phần kế tiếp, không tự thêm vào đây."
            )
        if total_chapters:
            _text, dropped = self._description_result(window, novel_title, total_chapters)
            if dropped:
                kept = len(window.chapters) - dropped
                warnings.append(
                    f"Mô tả YouTube tối đa {YOUTUBE_DESCRIPTION_CHAR_LIMIT} ký tự — mục lục "
                    f"của phần này chỉ liệt kê {kept}/{len(window.chapters)} chương, "
                    f"{dropped} chương còn lại gộp thành một dòng “… còn {dropped} chương "
                    "nữa”. Giảm số chương/video, hoặc bấm “Shorten by AI” trong Chi tiết, "
                    "nếu muốn mục lục đầy đủ."
                )
        if self._part_dir_name(window, whole_novel) in self._stale_descriptions:
            warnings.append(
                "Tên chương đã đổi sau khi tạo video, nhưng mô tả của phần này đã được rút "
                "gọn bằng AI nên không tự cập nhật — mở “Chi tiết” rồi bấm “Shorten by AI” "
                "lại, hoặc “Khôi phục mô tả gốc”."
            )
        if warnings:
            item.setForeground(QColor("#e5c07b"))
            item.setText("⚠️ " + text)
            item.setToolTip("\n\n".join(warnings))
        return item

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
        self._suppress_status_toggle = True
        try:
            self._rebuild_video_rows()
        finally:
            self._suppress_status_toggle = False
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
        """Mark every listed part uploaded / not-uploaded, from the header indicator."""
        if not self._upload_rows():
            QMessageBox.information(self, "Đã tải lên", "Chưa có phần nào đã tạo video.")
            return
        self._bulk_set_uploaded([path for _row, path in self._upload_rows()], check_all)

    def _bulk_set_uploaded(self, paths: list, wanted: bool) -> None:
        """Mark every given part's video uploaded / not-uploaded — the header "check all"
        indicator and the right-click multi-select menu both funnel through here.

        Toggling one row is consequential; toggling many is more so, so this confirms once
        with the count and only touches paths that would actually change.
        """
        from noveltrans.youtube_upload import (
            clear_upload_state,
            is_published,
            mark_uploaded_by_hand,
        )

        targets = [path for path in paths if is_published(path) != wanted]
        if not targets:
            QMessageBox.information(
                self, "Đã tải lên", "Tất cả các phần đã chọn đã ở đúng trạng thái rồi."
            )
            return

        if wanted:
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
            if wanted:
                mark_uploaded_by_hand(path)
            else:
                clear_upload_state(path)
        self._refresh_video_list()
        self.status_label.setText(
            f"Đã đánh dấu {len(targets)} phần là đã tải lên."
            if wanted
            else f"Đã bỏ đánh dấu {len(targets)} phần — có thể tải lên lại."
        )

    def _bulk_set_created(self, paths: list, wanted: bool) -> None:
        """Mark every given part's "Trạng thái" tick đã tạo / chưa tạo — the right-click
        multi-select menu's version of ticking each row's checkbox one at a time.

        Mirrors `_on_created_toggled`'s messaging, but confirms once for the whole
        selection and only touches paths that would actually change.
        """
        from noveltrans.video_state import created_override, set_created_override

        targets = []
        for path in paths:
            exists = path.is_file()
            override = created_override(path)
            current = exists if override is None else override
            if current != wanted:
                targets.append((path, exists))
        if not targets:
            QMessageBox.information(
                self, "Trạng thái", "Tất cả các phần đã chọn đã ở đúng trạng thái rồi."
            )
            return

        if wanted:
            message = (
                f"Đánh dấu {len(targets)} phần là ĐÃ TẠO?\n\n"
                "Dùng khi các phần này được quản lý ở nơi khác. Ứng dụng sẽ không tự sửa "
                "lại trạng thái cho tới khi bạn tạo video hoặc bỏ tick."
            )
        else:
            message = (
                f"Bỏ đánh dấu {len(targets)} phần đang ĐÃ TẠO?\n\n"
                "Dùng khi muốn coi các phần này là chưa hoàn tất (ví dụ cần tạo lại). "
                "File .mp4 (nếu có) sẽ KHÔNG bị xoá."
            )
        if QMessageBox.question(self, "Trạng thái", message) != (
            QMessageBox.StandardButton.Yes
        ):
            return

        for path, exists in targets:
            set_created_override(path, wanted, file_exists=exists)
        self._refresh_video_list()
        self.status_label.setText(
            f"Đã đánh dấu {len(targets)} phần là đã tạo."
            if wanted
            else f"Đã bỏ đánh dấu {len(targets)} phần."
        )

    def _rebuild_video_rows(self) -> None:
        self.video_list.setRowCount(0)
        if self.project is None:
            return
        windows = self._windows_for_current_selection()
        mode = self.video_mode.currentData()
        total = len(windows)
        self.video_list.setRowCount(total)
        # Hoisted out of the loop: `counts()` runs eight COUNT(*) queries, and the
        # description each row's warning is derived from needs both of these.
        total_chapters = self.project.counts()["total"]
        novel_title = self.project.meta.display_name()
        for i, window in enumerate(windows):
            whole_novel = total == 1 and mode == "all"
            part_num = None if whole_novel else self._part_number(window)
            exists = self._part_output_path(window, whole_novel=whole_novel).is_file()
            label = "Toàn bộ" if whole_novel else f"Phần {part_num}"
            self.video_list.setItem(i, 0, QTableWidgetItem(label))
            self.video_list.setItem(
                i, 1,
                self._chapter_range_item(
                    window, mode, total_chapters, novel_title, whole_novel
                ),
            )
            self.video_list.setItem(i, 2, self._duration_item(window))
            self.video_list.setItem(i, 3, QTableWidgetItem(self._part_title(part_num)))
            self.video_list.setItem(i, 4, self._created_item(window, whole_novel))
            self.video_list.setItem(i, 5, self._upload_item(window, whole_novel))
            self.video_list.setCellWidget(
                i, 6, self._build_row_actions(window, part_num, whole_novel, exists)
            )

    def _created_item(self, window, whole_novel: bool) -> QTableWidgetItem:
        """The "Trạng thái" cell: normally just the .mp4's existence, but tickable.

        The tick lets the user override the automatic status — mark a part "đã tạo" that
        was rendered/managed outside this app, or flag one that exists on disk as not
        actually finished. The override only sticks while it disagrees with disk; the
        moment it agrees again (a real render finished, or the file got deleted) it's
        cleared automatically, so it can never permanently fight reality.
        """
        from noveltrans.video_state import created_override

        path = self._part_output_path(window, whole_novel=whole_novel)
        exists = path.is_file()
        override = created_override(path)
        effective = exists if override is None else override

        item = QTableWidgetItem("✅ Đã tạo" if effective else "⬜ Chưa tạo")
        if override is not None and override != exists:
            item.setToolTip("Đánh dấu thủ công — không khớp với file trên đĩa.")
        else:
            item.setToolTip("Tick để đánh dấu thủ công phần này là đã/chưa tạo.")

        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
        )
        item.setCheckState(
            Qt.CheckState.Checked if effective else Qt.CheckState.Unchecked
        )
        # The path travels with the cell so the handler never has to re-derive which part
        # a row is — the row→window mapping shifts whenever the batch size changes.
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        return item

    def _on_status_toggled(self, item: QTableWidgetItem) -> None:
        """Dispatch a tick on "Trạng thái" or "Đã tải lên" to its handler.

        A single slot for both checkable columns: either handler below may rebuild the
        whole table (deleting every `QTableWidgetItem`) via `_refresh_video_list`, so two
        independent slots connected to the same `itemChanged` signal would leave the
        second one running against an already-deleted `item` — a crash, not just a bug.
        """
        if self._suppress_status_toggle:
            return
        column = item.column()
        if column == 4:
            self._on_created_toggled(item)
        elif column == 5:
            self._on_upload_toggled(item)

    def _on_created_toggled(self, item: QTableWidgetItem) -> None:
        """Handle the user ticking / unticking "Trạng thái" on a row.

        Both directions are consequential enough to confirm when they'd disagree with
        disk — ticking a part that has no file hides it from "còn thiếu" bookkeeping,
        unticking one that has a file makes a finished part look unfinished. Reverting to
        agreement with disk needs no confirmation: it's just clearing the override.
        """
        from pathlib import Path

        from noveltrans.video_state import created_override, set_created_override

        path = Path(item.data(Qt.ItemDataRole.UserRole) or "")
        if not path.name:
            return
        exists = path.is_file()
        override = created_override(path)
        current = exists if override is None else override
        wanted = item.checkState() == Qt.CheckState.Checked
        if wanted == current:
            return  # nothing actually changed

        if wanted == exists:
            # Reverting to what disk already says — no confirmation, just clear it.
            set_created_override(path, wanted, file_exists=exists)
            self._refresh_video_list()
            self.status_label.setText("Đã bỏ đánh dấu thủ công — theo trạng thái file.")
            return

        if wanted:
            message = (
                "Đánh dấu phần này là ĐÃ TẠO dù chưa có file video?\n\n"
                "Dùng khi bạn tự quản lý video này ở nơi khác. Ứng dụng sẽ không tự sửa "
                "lại trạng thái này cho tới khi bạn tạo video hoặc bỏ tick."
            )
        else:
            message = (
                "Bỏ đánh dấu ĐÃ TẠO dù file video vẫn còn trên đĩa?\n\n"
                "Chỉ làm vậy nếu bạn muốn coi phần này là chưa hoàn tất (ví dụ cần tạo "
                "lại). File .mp4 sẽ KHÔNG bị xoá."
            )
        if QMessageBox.question(self, "Trạng thái", message) != (
            QMessageBox.StandardButton.Yes
        ):
            self._set_check_silently(item, current)
            return

        set_created_override(path, wanted, file_exists=exists)
        self._refresh_video_list()
        self.status_label.setText(
            "Đã đánh dấu thủ công là đã tạo." if wanted else "Đã bỏ đánh dấu là đã tạo."
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
        """Set a check state without re-entering `_on_upload_toggled`/`_on_created_toggled`."""
        self._suppress_status_toggle = True
        try:
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        finally:
            self._suppress_status_toggle = False

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
        from noveltrans.video_state import created_override
        from noveltrans.youtube_upload import is_published, needs_attention

        path = self._part_output_path(window, whole_novel=whole_novel)
        override = created_override(path)
        if override is not None and override != exists:
            make.setToolTip(
                "Đang đánh dấu thủ công là đã tạo — bấm để tạo file video thật."
                if override
                else "Đang đánh dấu thủ công là chưa tạo dù file đã có trên đĩa."
            )
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

    def _part_segments(self, window) -> list:
        """A part's `MergeSegment`s built from stored audio — what the description reads."""
        from noveltrans.tts.merge import MergeSegment, chapter_marker_title

        return [
            MergeSegment(path="", seconds=c.audio_seconds, title=chapter_marker_title(c))
            for c in window.chapters
        ]

    def _description_result(
        self, window, novel_title: str, total_chapters: int
    ) -> tuple[str, int]:
        """`(description, chapters dropped from the index)` for a part, computed fresh.

        `total_chapters` is a parameter rather than a `self.project.counts()` call so the
        table refresh can hoist it: `counts()` runs eight `COUNT(*)` queries, and doing that
        once per row on a 200-part list is the one real cost in rebuilding the table.
        """
        from noveltrans.tts.video import fit_video_description

        return fit_video_description(
            self._part_segments(window),
            original_title=self.project.meta.title,
            vn_title=novel_title,
            original_author=self.project.meta.author,
            vn_author=self.project.meta.translated_author,
            total_chapters=total_chapters,
            credit=self.credit_edit.text().strip() or "Fox Novel",
        )

    def _compute_part_description(self, window, novel_title: str) -> str:
        """Build a part's description on the fly (before it's rendered) from stored audio."""
        return self._description_result(
            window, novel_title, self.project.counts()["total"]
        )[0]

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

    def _description_label_text(self, description: str) -> str:
        """The "Mô tả:" caption, with Studio's own character count and a truncation note.

        Its own method rather than an inline f-string because `_show_part_detail` ends in
        `dialog.exec()` and can't be driven from a test.
        """
        text = (
            f"Mô tả: ({description_length(description)}/"
            f"{YOUTUBE_DESCRIPTION_CHAR_LIMIT} ký tự)"
        )
        if was_truncated(description):
            text += " — ⚠️ mục lục chương đã rút gọn cho vừa giới hạn của YouTube."
        return text

    def _short_description_extras(self) -> tuple[list[str], list[str]]:
        """The header/credit lines the short form puts back when they cost no chapters.

        Built from the very same helpers the full description uses, so a shortened
        description that has room reads identically at the top and bottom.
        """
        from noveltrans.tts.video import DEFAULT_VIDEO_CREDIT, description_header_lines

        meta = self.project.meta
        before = description_header_lines(
            original_title=meta.title,
            vn_title=meta.display_name(),
            original_author=meta.author,
            vn_author=meta.translated_author,
        )
        credit = self.credit_edit.text().strip() or DEFAULT_VIDEO_CREDIT
        return before, ["", f"Tạo bởi: {credit}"]

    def _shorten_description(
        self, window, desc_edit, button, status, whole_novel: bool,
        keep_extras: bool = True,
    ) -> None:
        """"Shorten by AI": rebuild the description as a bare, AI-shortened chapter index.

        The chapter NUMBER never goes near the model — `split_chapter_number` strips it, the
        model only ever sees the descriptive half, and `C.N` is reassembled here. Trusting an
        LLM to carry a couple hundred chapter numbers through intact is how you get a
        silently renumbered index, which is a far worse bug than a long description.
        """
        from noveltrans.tts.description import split_chapter_number
        from noveltrans.tts.merge import chapter_marker_title
        from noveltrans.tts.video import _yt_timestamp

        if self.project is None:
            return
        if self._shorten_worker is not None and self._shorten_worker.isRunning():
            return

        stamps: list[str] = []
        numbers: list[int | None] = []
        rests: list[str] = []
        start = 0.0
        for chapter in window.chapters:
            number, rest = split_chapter_number(chapter_marker_title(chapter))
            stamps.append(_yt_timestamp(start))
            # The title's own number wins when it has one (a scrape with gaps stays
            # consistent with what the site published); `index + 1` is what the rest of the
            # app calls this chapter otherwise.
            numbers.append(number if number is not None else chapter.index + 1)
            rests.append(rest)
            start += chapter.audio_seconds

        button.setEnabled(False)
        status.setText("✨ Đang rút gọn bằng AI…")
        worker = ShortenTitlesWorker(rests, **self._ai_engine_params())
        self._shorten_worker = worker
        worker.progress.connect(
            lambda done, total: status.setText(f"✨ Đang rút gọn… {done}/{total}")
        )
        worker.finished_ok.connect(
            lambda titles, fell_back: self._on_shorten_ready(
                window, desc_edit, button, status, whole_novel,
                stamps, numbers, rests, titles, fell_back, keep_extras,
            )
        )
        worker.failed.connect(
            lambda message: self._on_shorten_failed(button, status, message)
        )
        track_worker(worker)
        worker.start()

    def _on_shorten_ready(
        self, window, desc_edit, button, status, whole_novel,
        stamps, numbers, originals, titles, fell_back, keep_extras=True,
    ) -> None:
        from noveltrans.tts.description import build_short_description, short_chapter_label

        button.setEnabled(True)
        if len(titles) != len(originals):  # a worker that lost its place — keep the old text
            self._on_shorten_failed(button, status, "Số dòng trả về không khớp số chương.")
            return
        entries = [
            (stamp, short_chapter_label(number), title)
            for stamp, number, title in zip(stamps, numbers, titles)
        ]
        extras_before, extras_after = (
            self._short_description_extras() if keep_extras else ([], [])
        )
        text, dropped, extras_kept = build_short_description(
            entries,
            total_chapters=self.project.counts()["total"],
            extras_before=extras_before,
            extras_after=extras_after,
        )
        desc_edit.setPlainText(text)
        saved = self._write_description_sidecar(window, whole_novel, text)

        notes = [f"✅ Đã rút gọn {len(titles)} chương."]
        if keep_extras:
            notes.append(
                "Còn chỗ nên giữ lại tên truyện/tác giả/dòng “Tạo bởi”."
                if extras_kept
                else "Không đủ chỗ cho tên truyện/tác giả nên chỉ giữ mục lục."
            )
        if fell_back:
            notes.append(f"⚠️ {fell_back} nhóm giữ nguyên tên gốc.")
        if dropped:
            notes.append(f"⚠️ {dropped} chương vẫn không vừa mục lục.")
        notes.append("Đã lưu vào .txt." if saved else "Chưa tạo video nên chưa lưu.")
        status.setText(" ".join(notes))

    def _on_shorten_failed(self, button, status, message: str) -> None:
        button.setEnabled(True)
        status.setText("")
        QMessageBox.warning(self, "Rút gọn thất bại", message)

    def _restore_generated_description(
        self, window, desc_edit, button, status, whole_novel: bool
    ) -> None:
        """Throw away a customised description and rebuild the full one from the database.

        The deliberate way out of a stale AI-shortened sidecar — the one thing
        `_resync_description_sidecars` refuses to do on its own, because it can't be undone.
        """
        if self.project is None:
            return
        text = self._compute_part_description(window, self.project.meta.display_name())
        desc_edit.setPlainText(text)
        saved = self._write_description_sidecar(window, whole_novel, text)
        self._stale_descriptions.discard(self._part_dir_name(window, whole_novel))
        button.setEnabled(False)
        status.setText(
            "✅ Đã khôi phục mô tả gốc." + ("" if saved else " (chưa tạo video nên chưa lưu)")
        )
        self._refresh_video_list()

    def _write_description_sidecar(self, window, whole_novel: bool, text: str) -> bool:
        """Write a part's `.txt` — only if it's actually been rendered. Returns whether it was.

        An unrendered part has no folder to write into and can't be uploaded yet, so the
        dialog's copy is the whole deliverable there. A rendered one must be written:
        `_upload_request` reads this file, so without it nothing new ever reaches YouTube.
        """
        if not self._part_output_path(window, whole_novel=whole_novel).is_file():
            return False
        self._part_sidecar(window, whole_novel, ".txt").write_text(text, encoding="utf-8")
        return True

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
        # Read the widget, not the captured string: "Shorten by AI" replaces the text in
        # place, and copying the description the dialog opened with would be wrong.
        desc_copy.clicked.connect(lambda: copy(desc_edit.toPlainText(), "mô tả"))
        desc_label = QLabel(self._description_label_text(description))
        desc_edit.textChanged.connect(
            lambda: desc_label.setText(
                self._description_label_text(desc_edit.toPlainText())
            )
        )

        shorten = QPushButton("Shorten by AI")
        shorten.setToolTip(
            "Dùng AI rút gọn tên từng chương (giữ nguyên ý nghĩa), đổi “Chương 1” thành "
            "“C.1”, và bỏ tên truyện / tác giả / dòng “Tạo bởi” — chỉ giữ mục lục chương, "
            f"để nhiều chương vừa trong giới hạn {YOUTUBE_DESCRIPTION_CHAR_LIMIT} ký tự "
            "của YouTube."
        )
        keep_extras = QCheckBox("Giữ tên truyện / tác giả nếu còn chỗ")
        keep_extras.setChecked(True)
        keep_extras.setToolTip(
            "Sau khi rút gọn, nếu mô tả vẫn còn chỗ thì thêm lại “Tên truyện:”, “Tác giả:” "
            "và dòng “Tạo bởi:”. Chỉ thêm khi KHÔNG phải bỏ bớt chương nào khỏi mục lục — "
            "còn nếu chật thì mục lục được ưu tiên."
        )
        shorten.clicked.connect(
            lambda: self._shorten_description(
                window, desc_edit, shorten, status, whole_novel,
                keep_extras.isChecked(),
            )
        )
        restore = QPushButton("Khôi phục mô tả gốc")
        restore.setToolTip(
            "Dựng lại mô tả đầy đủ từ dữ liệu hiện tại (tên truyện, tác giả, mục lục "
            "chương đầy đủ) — dùng khi mô tả đã rút gọn không còn khớp tên chương."
        )
        restore.setEnabled(
            self._part_dir_name(window, whole_novel) in self._stale_descriptions
        )
        restore.clicked.connect(
            lambda: self._restore_generated_description(
                window, desc_edit, restore, status, whole_novel
            )
        )

        desc_row = QHBoxLayout()
        desc_row.addWidget(desc_copy)
        desc_row.addWidget(shorten)
        desc_row.addWidget(keep_extras)
        desc_row.addWidget(restore)
        desc_row.addStretch()
        layout.addWidget(desc_label)
        layout.addWidget(desc_edit, 1)
        layout.addLayout(desc_row)

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
        font_file = video_font(self._video_settings["video_thumbnail_font"])["file"]
        render_thumbnail(
            base,
            self._part_sidecar(window, whole_novel, ".jpg"),
            vn_title=novel_title,
            part_num=part_num or 1,
            tagline=self.tagline_edit.text().strip(),
            font_path=font_dir / font_file,
            width=1280, height=720,
            title_pos=self._video_settings["video_thumbnail_title_pos"],
            part_pos=self._video_settings["video_thumbnail_part_pos"],
            title_scale=self._video_settings["video_thumbnail_title_scale"],
            part_scale=self._video_settings["video_thumbnail_part_scale"],
            tagline_scale=self._video_settings["video_thumbnail_tagline_scale"],
            title_align=self._video_settings["video_thumbnail_title_align"],
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
                    part_num = None if whole_novel else self._part_number(window)
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

        def adopt_editor_layout() -> None:
            """Copy the editor's current layout onto this novel. The cover layout belongs
            to the novel, so it is stored here rather than left in the config the editor
            also writes (which is now only the user's last-used layout)."""
            for key, value in (
                ("video_thumbnail_title_pos", tuple(dialog.title_pos)),
                ("video_thumbnail_part_pos", tuple(dialog.part_pos)),
                ("video_thumbnail_title_scale", dialog.title_scale),
                ("video_thumbnail_part_scale", dialog.part_scale),
                ("video_thumbnail_tagline_scale", dialog.tagline_scale),
                ("video_thumbnail_title_align", dialog.title_align),
                ("video_thumbnail_font", dialog.font_key),
            ):
                self._save_video_setting(key, value)

        def apply_all() -> None:
            # "Áp dụng cho tất cả" fires while the dialog is still open, and the regen
            # reads `_video_settings` — so adopt the edits BEFORE regenerating, or every
            # cover would be rebuilt with the layout the editor started from.
            adopt_editor_layout()
            self._regen_all_thumbnails()

        dialog = ThumbnailEditorDialog(
            self.config,
            base_image=base,
            novel_title=novel_title,
            part_num=1,
            tagline=self.tagline_edit.text().strip(),
            settings=self._video_settings,  # start from THIS novel's cover layout
            on_apply_all=apply_all,
            parent=self,
        )
        dialog.exec()
        adopt_editor_layout()
        # reflect any font change made in the editor back into the box's font combo
        fidx = self.thumb_font.findData(dialog.font_key)
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
            lambda: self._save_video_setting("video_thumbnail_font", self.thumb_font.currentData())
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
            lambda: self._save_video_setting("video_tagline", self.tagline_edit.text())
        )

        self.credit_edit = QLineEdit(self.config.video_credit)
        self.credit_edit.setPlaceholderText("Fox Novel")
        self.credit_edit.setMaximumWidth(140)
        self.credit_edit.editingFinished.connect(
            lambda: self._save_video_setting(
                "video_credit", self.credit_edit.text().strip() or "Fox Novel"
            )
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
            lambda: self._save_video_setting("video_ai_engine", self.ai_engine_combo.currentData())
        )

        self.ai_model_edit = QLineEdit(self.config.video_ai_model)
        self.ai_model_edit.setPlaceholderText("model (để trống = mặc định)")
        self.ai_model_edit.setMaximumWidth(220)
        self.ai_model_edit.editingFinished.connect(
            lambda: self._save_video_setting("video_ai_model", self.ai_model_edit.text().strip())
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

    # ------------------------------------------------- per-novel video settings

    def _save_video_setting(self, key: str, value) -> None:
        """Record one video setting for the open novel (and as the user's habit, if it is one).

        Called from every widget's change handler. A no-op while `_apply_video_settings`
        is pushing values in, otherwise loading novel B would immediately save B's freshly
        shown values over whatever the user had — or worse, save them onto novel A.

        Workflow keys are mirrored into the global config so the *next* new novel inherits
        them; identity keys deliberately are not, because that mirror is exactly how one
        novel's `ảnh nền` used to end up on another's video.
        """
        if self._loading_video_settings:
            return
        self._video_settings[key] = value
        if key in video_settings.WORKFLOW_KEYS:
            setattr(self.config, key, value)
        if self.project is not None:
            self.project.save_video_settings({key: value})

    def _apply_video_settings(self, values: dict) -> None:
        """Push a resolved settings dict into every widget that shows one.

        Signals stay connected but are ignored (see `_loading_video_settings`) rather than
        blocked per-widget: the handlers do more than save — `_on_video_mode_changed` also
        shows/hides the range and batch controls — and that work still needs to happen.
        """
        self._video_settings = dict(values)
        self._loading_video_settings = True
        try:
            self._set_combo(self.video_mode, values["video_mode"], fallback="batch")
            self._set_combo(self.video_quality, values["video_quality"])
            self._set_combo(self.video_font, values["video_font"])
            self._set_combo(self.thumb_font, values["video_thumbnail_font"])
            self.video_batch_size.setValue(int(values["video_batch_size"]))
            self.burn_subs_check.setChecked(bool(values["video_burn_subtitles"]))
            self.video_image_edit.setText(values["video_image_path"])
            self.thumb_image_edit.setText(values["video_thumbnail_image"])
            self.tagline_edit.setText(values["video_tagline"])
            self.credit_edit.setText(values["video_credit"])
            self._set_combo(self.ai_engine_combo, values["video_ai_engine"])
            self.ai_model_edit.setText(values["video_ai_model"])
            self.bg_color = values["video_bg_color"]
        finally:
            self._loading_video_settings = False
        # Outside the guard: these only read state, and the mode handler owns the
        # show/hide of the range and batch rows, which must reflect the novel just loaded.
        self._update_bg_swatch()
        self._on_video_mode_changed()

    @staticmethod
    def _set_combo(combo, value, *, fallback=None) -> None:
        """Select `value` in `combo`, falling back rather than leaving index -1 (blank)."""
        index = combo.findData(value)
        if index < 0 and fallback is not None:
            index = combo.findData(fallback)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _load_video_settings_for_project(self) -> None:
        """Resolve and show the open novel's settings, adopting today's globals if it has none.

        The adoption is the migration: a novel set up and rendered before settings went
        per-novel has nothing saved, and starting it at defaults would silently change what
        its next part looks like. Taking a snapshot keeps its output identical and makes it
        independent from that point on.
        """
        if self.project is None:
            # Workflow habits from config, identity at app defaults — never the last
            # novel's image sitting in the box with no novel open to own it.
            self._apply_video_settings(video_settings.effective({}, self.config))
            return
        stored = self.project.meta.video_settings
        if not stored:
            stored = video_settings.snapshot(self.config)
            # The pre-existing standalone field wins: a novel that had its own image
            # chosen must keep it, not adopt whichever image the globals happen to hold.
            if self.project.meta.video_image_path:
                stored["video_image_path"] = self.project.meta.video_image_path
            self.project.save_video_settings(stored)
        self._apply_video_settings(video_settings.effective(stored, self.config))

    # ---------------------------------------------------------- mode/config

    def _on_video_mode_changed(self) -> None:
        mode = self.video_mode.currentData()
        self._save_video_setting("video_mode", mode)
        for w in (self.video_range_from, self.video_range_label, self.video_range_to):
            w.setVisible(mode == "range")
        for w in (self.video_batch_size, self.video_batch_label):
            w.setVisible(mode == "batch")

    def _on_video_quality_changed(self) -> None:
        self._save_video_setting("video_quality", self.video_quality.currentData())

    def _on_video_font_changed(self) -> None:
        self._save_video_setting("video_font", self.video_font.currentData())

    def _pick_video_image(self) -> None:
        # Opens where this novel's image lives, not where some other novel's did.
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn ảnh nền", self._video_settings.get("video_image_path") or "", _IMAGE_FILTER
        )
        if path:
            self.video_image_edit.setText(path)
            # Per-novel only — deliberately NOT mirrored into the global config, which is
            # what used to carry this image onto the next novel's video.
            self._save_video_setting("video_image_path", path)
            self._maybe_refresh_preview()

    def _save_upload_playlist_choice(self, text: str) -> None:
        """Persist the playlist box's current text onto the open novel, if any.

        Fires on every keystroke/selection (`currentTextChanged`) — cheap (one small
        meta.json rewrite) and simplest way to keep it in sync without a separate save
        step the user could forget, same tradeoff `_pick_video_image` makes.
        """
        if self.project is not None:
            self.project.save_upload_playlist(text)

    def _pick_bg_color(self) -> None:
        initial = QColor(self.bg_color) if self.bg_color else QColor("#e9d5ff")
        color = QColorDialog.getColor(initial, self, "Chọn màu nền video")
        if color.isValid():
            self.bg_color = color.name()  # "#rrggbb"
            self._save_video_setting("video_bg_color", self.bg_color)
            self._update_bg_swatch()
            self._maybe_refresh_preview()  # live-update the open preview, if any

    def _reset_bg_color(self) -> None:
        self.bg_color = ""
        self._save_video_setting("video_bg_color", "")
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
        start = (
            self._video_settings.get("video_thumbnail_image")
            or self._video_settings.get("video_image_path")
            or ""
        )
        path, _ = QFileDialog.getOpenFileName(self, "Chọn ảnh bìa", start, _IMAGE_FILTER)
        if path:
            self.thumb_image_edit.setText(path)
            self._save_video_setting("video_thumbnail_image", path)

    # ---------------------------------------------------------------- voices

    def _load_voices(self) -> None:
        self._voices_worker = TtsVoicesWorker()
        self._voices_worker.voices_listed.connect(self._on_voices_listed)
        self._voices_worker.start()

    def _on_voices_listed(self, voices: list) -> None:
        # Arrives asynchronously, possibly after a project is already open. Writing
        # straight into the combo here is what would overwrite a project-derived list
        # with the engine catalogue, depending purely on which finished first.
        self._tts_voices = [
            (re.sub(r"\s*·\s*Phong cách.*$", "", label), voice_id) for label, voice_id in voices
        ]
        self._refresh_audio_sources()

    def _refresh_audio_sources(self) -> None:
        """Fill the combo with the audio this project actually has.

        This tab never SYNTHESIZES anything — it only consumes audio already on disk — so
        listing the TTS engine's voices was wrong in a way that mattered: narration
        downloaded from the source site is stored under `audio_voice="tieuthuyetmang"`,
        which is not an engine voice and so could never be selected. `plan_merge_windows`
        filters on `audio_voice == voice`, so downloaded audio was structurally
        unreachable from the video tab. Same bug family as the merge step's, fixed the
        same way (059.01).

        Falls back to the engine catalogue only when the project has no audio at all, so
        the combo is never empty before the first chapter is voiced.
        """
        previous = self.voice_combo.currentData() or self.config.tts_voice
        pretty = dict(self._tts_voices)
        have: list[str] = []
        downloaded = 0
        if self.project is not None:
            for chapter in self.project.chapters():
                if chapter.has_audio and chapter.audio_voice not in have:
                    have.append(chapter.audio_voice)
            downloaded = sum(1 for r in self.project.source_audio() if r.has_audio)
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        if downloaded:
            # Listed first: on a novel fetched from the site this is the edition the user
            # actually has, and often the only one.
            self.voice_combo.addItem(f"Audio từ nguồn ({downloaded} mục)", SOURCE_AUDIO_KEY)
        if have:
            for voice in have:
                self.voice_combo.addItem(audio_source_label(voice, pretty), voice)
        elif not downloaded:
            for label, voice_id in self._tts_voices:
                self.voice_combo.addItem(label, voice_id)
        index = self.voice_combo.findData(previous)
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
            # Loads this novel's own image, colour, fonts and cover layout. There is
            # deliberately no global fallback for those: falling back is how one novel's
            # `ảnh nền` used to arrive pre-filled on the next novel and render into its
            # video. Workflow choices (quality, mode, ...) still inherit — see
            # noveltrans.video_settings.
            self._load_video_settings_for_project()
            self.upload_playlist.setCurrentText(self.project.meta.upload_playlist)
            # No global fallback here, unlike the image: an unset/unknown key must land
            # on "private" (index 0), the deliberately-safe default — never on whatever
            # visibility some OTHER novel happened to have chosen last.
            visibility_index = self.upload_visibility.findData(
                self.project.meta.upload_visibility
            )
            self.upload_visibility.setCurrentIndex(max(visibility_index, 0))
            self._sync_display_title()
            self._update_status_line()
        else:
            self.tags_edit.setPlainText("")
            self.image_prompt_edit.setPlainText("")
            # No novel open: show the user's workflow habits, but no novel's identity —
            # an image left in the box here is one a later render could pick up.
            self._load_video_settings_for_project()
            self.upload_playlist.setCurrentText("")
            self.upload_visibility.setCurrentIndex(0)  # back to Riêng tư, no novel open
            self.display_title_edit.clear()
            self.display_title_edit.setPlaceholderText("")
            self.status_label.setText("")
        self._refresh_audio_sources()
        # Once per project open — not from `_refresh_video_list`, which fires on every
        # spinbox tick. Once is enough because a rename made in another tab forces a reopen:
        # returning here runs showEvent → refresh_projects → picker.refresh →
        # project_selected → this method.
        self._resync_descriptions_and_report()
        self._refresh_video_list()

    def _resync_descriptions_and_report(self) -> None:
        """Run the description resync and fold what it did into the status line."""
        if self.project is None:
            return
        rewritten, customised = self._resync_description_sidecars()
        notes = []
        if rewritten:
            notes.append(f"đã cập nhật mô tả {rewritten} phần")
        if customised:
            notes.append(f"⚠️ {customised} phần có mô tả rút gọn đã cũ")
        if notes:
            base = self.status_label.text()
            joined = ", ".join(notes)
            self.status_label.setText(f"{base} ({joined})" if base else joined.capitalize())

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

    def _resync_tags_sidecars(self, tags: str) -> int:
        """Rewrite `.tags.txt` for every already-rendered part with the given tags.

        A part's `.tags.txt` is written once, at render time, and never touched again —
        so regenerating or editing tags afterward left every existing part's sidecar
        stale: `_upload_request` reads tags straight from that file (an already-rendered
        part's *next* upload would still send the old tags), and `_part_metadata` prefers
        the sidecar over the live tags box whenever it exists (so "Chi tiết phần" kept
        showing the old value too). Re-stamping every rendered part's sidecar here keeps
        both in sync with whatever was just saved/generated. Returns how many were updated.
        """
        if self.project is None or not tags.strip():
            return 0
        windows = self._windows_for_current_selection()
        whole = len(windows) == 1 and self.video_mode.currentData() == "all"
        updated = 0
        for window in windows:
            if not self._part_output_path(window, whole_novel=whole).is_file():
                continue
            sidecar = self._part_sidecar(window, whole, ".tags.txt")
            sidecar.write_text(tags + "\n", encoding="utf-8")
            updated += 1
        return updated

    # ------------------------------------------------- keep descriptions fresh

    def _resync_description_sidecars(self) -> tuple[int, int]:
        """Rewrite the `.txt` of every rendered part whose chapter names have changed.

        Same problem `_resync_tags_sidecars` solves, one field over: a part's description is
        written once, at render time, and never touched again — while `_part_metadata`
        prefers that file over recomputing it and `_upload_request` reads it straight off
        disk. So renaming a chapter left an already-rendered part showing (and *uploading*)
        the old name until it was re-rendered.

        Staleness is detected by regenerating and diffing rather than by hooking the rename:
        a title can move through at least six routes (`edit_title` from two tabs,
        `reset_title`, `edit_translation`, Tìm & Thay thế, a re-translation — and
        `chapter_marker_title` reads `translated_title or title`, so all of them count).
        A generated description is a pure function of the database, so comparing against a
        fresh build covers every route at once, including ones added later and renames made
        in a previous session.

        Parts are enumerated from DISK, not from the current window selection: a part's
        description depends on its own chapter span, and the mode/batch/range the user
        happens to have selected need not reproduce the windows that were actually rendered.

        Returns `(rewritten, stale but customised)`. A sidecar that doesn't `looks_generated`
        is left strictly alone and recorded in `_stale_descriptions` — an AI-shortened
        description's titles cannot be rebuilt from the database, so overwriting one would
        be data loss, not a regeneration.
        """
        from noveltrans.tts.description import indexed_chapter_count, looks_generated
        from noveltrans.tts.video import (
            build_youtube_description,
            iter_rendered_part_dirs,
            video_part_dir_name,
        )

        self._stale_descriptions = set()
        if self.project is None:
            return 0, 0
        video_dir = self.project.video_dir
        if not video_dir.is_dir():
            return 0, 0

        slug = self._novel_slug()
        chapters = self.project.chapters()
        total_chapters = self.project.counts()["total"]
        novel_title = self.project.meta.display_name()

        spans = list(iter_rendered_part_dirs(video_dir, slug))
        # A whole-novel render lives in `{slug}/` with no span suffix, so the scan above
        # (which keys on `{slug}-{first}-{last}`) can't see it.
        whole_dir = video_dir / video_part_dir_name(slug, 0, 0, whole_novel=True)
        if whole_dir.is_dir() and chapters:
            spans.append((whole_dir, chapters[0].index + 1, chapters[-1].index + 1))

        rewritten = 0
        customised = 0
        for part_dir, first_num, last_num in spans:
            sidecar = part_dir / f"{part_dir.name}.txt"
            if not sidecar.is_file() or not (part_dir / f"{part_dir.name}.mp4").is_file():
                continue
            try:
                current = sidecar.read_text(encoding="utf-8")
            except OSError:
                continue

            selected = [c for c in chapters if first_num <= c.index + 1 <= last_num]
            if not selected:
                continue
            window = SimpleNamespace(
                first_num=first_num, last_num=last_num, chapters=selected
            )
            fresh, _dropped = self._description_result(
                window, novel_title, total_chapters
            )
            if current == fresh:
                continue
            if not (
                looks_generated(current)
                or current == build_youtube_description(
                    self._part_segments(window), novel_title
                )
            ):
                self._stale_descriptions.add(part_dir.name)
                customised += 1
                continue
            # Chapters deleted since the render would leave the description describing a
            # different set of chapters than the audio in the .mp4 actually contains —
            # timestamps that point nowhere. Renaming, the case this exists for, keeps the
            # count identical; anything else is flagged rather than silently rewritten.
            if indexed_chapter_count(current) != len(selected):
                self._stale_descriptions.add(part_dir.name)
                customised += 1
                continue
            sidecar.write_text(fresh, encoding="utf-8")
            rewritten += 1
        return rewritten, customised

    def _on_tags_ready(self, tags: str) -> None:
        self.tags_button.setEnabled(True)
        self.tags_edit.setPlainText(tags)
        updated = self._resync_tags_sidecars(tags)
        suffix = f" (đã cập nhật {updated} phần đã tạo)" if updated else ""
        self.status_label.setText(f"✅ Đã tạo tags.{suffix}")
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
        updated = self._resync_tags_sidecars(tags)
        suffix = f" (đã cập nhật {updated} phần đã tạo)" if updated else ""
        self.status_label.setText(f"✅ Đã lưu tags.{suffix}")

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

        # Batch mode: the same locked/"đã tạo"-aware windows the table shows — so a part
        # already committed with fewer than a full batch of chapters isn't retroactively
        # grown here, and the render below (also `skip_existing=True`) matches exactly.
        windows = self._windows_for_current_selection()
        if not windows:
            QMessageBox.information(self, "Chưa có audio", self._nothing_selected_message(voice))
            return

        # skip parts already "đã tạo" — either really rendered, or manually ticked so —
        # matching VideoWorker's own skip_existing check so this count/estimate is accurate
        from noveltrans.video_state import effective_created

        whole = len(windows) == 1 and mode == "all"
        pending = [
            w for w in windows
            if not effective_created(self._part_output_path(w, whole_novel=whole))
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
            f"Sẽ tạo {len(pending)} video{skip_note} từ {n_chapters} {self._unit_label(voice)} "
            f"({self._voice_label(voice)}), tổng ~{hours:.1f} giờ audio.\n\n"
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

        # The same planner the table and "Tạo video" use — including the source-audio
        # edition, which has no chapter grid at all. `honor_committed=False` because a
        # redo-all worker is launched with `skip_existing=False`, and that makes it ignore
        # auto-discovered "đã tạo" commits while still honoring manual split/merge
        # boundaries (see the long comment in `VideoWorker.run()`). Planning any other way
        # here would count parts the render doesn't actually produce.
        windows = self._windows_for_current_selection(honor_committed=False)
        if not windows:
            QMessageBox.information(self, "Chưa có audio", self._nothing_selected_message(voice))
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
            f"({sum(len(w.chapters) for w in windows)} {self._unit_label(voice)}, "
            f"{self._voice_label(voice)})"
            f".{overwrite_note}\n\n"
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
        # A range-mode worker has no batch grid to derive a part number from — pass the
        # tab's own answer explicitly, or a re-render titles/thumbnails the video "Phần 1"
        # (`merge.part_number` falls back to 1 with no batch size) regardless of which row
        # was actually clicked. This is what makes a locked window's re-render keep its
        # true number too, since that number can no longer be grid arithmetic either.
        self._launch_video(
            mode="range", start=window.first_num, end=window.last_num, skip_existing=False,
            part_num=self._part_number(window),
        )

    def _render_selected_parts(self, windows: list) -> None:
        """Right-click multi-select "Tạo video": render exactly the selected parts.

        Unlike `_render_one` (always redo, one part), this fills in what's missing among
        the selection — parts already "đã tạo" are skipped, same as the global "Tạo video"
        button — but only within the rows the user actually picked, not the whole current
        range/batch. Useful for rendering a handful of specific pending parts without
        waiting on everything else that's also pending.
        """
        from pathlib import Path

        from noveltrans.tts.video import video_preset
        from noveltrans.video_state import effective_created

        if self.project is None:
            return
        if self._video_worker is not None and self._video_worker.isRunning():
            QMessageBox.information(
                self, "Đang bận", "Đang tạo một video khác — hãy đợi hoặc bấm “Dừng”."
            )
            return
        image = self.video_image_edit.text().strip()
        if not image or not Path(image).is_file():
            QMessageBox.warning(self, "Chưa chọn ảnh", "Hãy chọn một ảnh nền hợp lệ cho video.")
            return

        pending = [
            w for w in windows
            if not effective_created(self._part_output_path(w, whole_novel=False))
        ]
        existing = len(windows) - len(pending)
        if not pending:
            QMessageBox.information(
                self, "Đã tạo hết",
                "Tất cả các phần đã chọn đã có video. Dùng “Tạo lại” ở từng dòng nếu "
                "muốn làm lại.",
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
            f"Sẽ tạo {len(pending)} phần đã chọn{skip_note} từ {n_chapters} "
            f"{self._unit_label(self.voice_combo.currentData() or '')}, "
            f"tổng ~{hours:.1f} giờ audio.\n\n"
            f"Chất lượng: {self.video_quality.currentText()} "
            f"({preset['width']}×{preset['height']}).\n"
            f"Ước tính thời gian render: {est} (chưa tính máy nóng/tải khác).\n\n"
            f"Tiếp tục?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        part_numbers = {w.first_num: self._part_number(w) for w in pending}
        self._launch_video(
            mode="batch", skip_existing=True,
            explicit_windows=pending, explicit_part_numbers=part_numbers,
        )

    def _launch_video(
        self, *, mode: str | None = None, start=None, end=None, batch=None,
        skip_existing: bool = False, part_num: int | None = None,
        explicit_windows: list | None = None, explicit_part_numbers: dict | None = None,
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
            source_audio=voice == SOURCE_AUDIO_KEY,
            width=preset["width"], height=preset["height"], fps=preset["fps"],
            spin_vinyl=preset["spin_vinyl"], font=font_family, font_key=font_key,
            thumb_font_key=self._video_settings["video_thumbnail_font"],
            thumb_title_pos=self._video_settings["video_thumbnail_title_pos"],
            thumb_part_pos=self._video_settings["video_thumbnail_part_pos"],
            thumb_title_scale=self._video_settings["video_thumbnail_title_scale"],
            thumb_part_scale=self._video_settings["video_thumbnail_part_scale"],
            thumb_tagline_scale=self._video_settings["video_thumbnail_tagline_scale"],
            thumb_title_align=self._video_settings["video_thumbnail_title_align"],
            burn_subtitles=self.burn_subs_check.isChecked(),
            bg_color=self.bg_color, skip_existing=skip_existing, part_num=part_num,
            explicit_windows=explicit_windows, explicit_part_numbers=explicit_part_numbers,
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
        self._job = job_registry.register(
            self._video_worker, kind="Tạo video", novel=self._job_novel()
        )
        self.pause_button.set_job(self._job.id if self._job else None)
        self._video_worker.start()

    def _on_video_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if name:
            self.status_label.setText(f"🎬 Đang tạo video ({done + 1}/{total}): {name}")

    def _on_video_file_done(self, path: str) -> None:
        from pathlib import Path

        from noveltrans.video_state import set_created_override

        self.progress.setValue(self.progress.value() + 1)
        # A real render just produced this exact file — any manual override (in either
        # direction) has nothing left to say, so let the automatic status take back over.
        set_created_override(Path(path), True, file_exists=True)

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
            part_num = None if whole_novel else self._part_number(window)
            path = self._part_output_path(window, whole_novel=whole_novel)
            if not path.is_file():
                continue
            if self._part_uploaded(window, whole_novel) or needs_attention(path):
                continue
            label = "Toàn bộ" if whole_novel else f"Phần {part_num}"
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
            label = "Toàn bộ" if whole else f"Phần {self._part_number(window)}"
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
        self._job = job_registry.register(
            self._upload_worker, kind="Tải video lên", novel=self._job_novel()
        )
        self.upload_pause_button.set_job(self._job.id if self._job else None)

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
        elif (
            self._subtitle_upload_worker is not None
            and self._subtitle_upload_worker.isRunning()
        ):
            self._subtitle_upload_worker.cancel()
            self.status_label.setText("Đang dừng tải phụ đề…")

    # ------------------------------------------------- tải phụ đề lên YouTube

    def _subtitle_upload_rows(self) -> list:
        """Parts with BOTH a video on the channel and an .srt on disk, in part order.

        Same eligibility rule as 034's cover push — a recorded `video_id` is exactly
        "there is a video to act on" — plus the sidecar this flow uploads.
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
            srt = path.with_suffix(".srt")
            if not uploaded_video_id(path) or not srt.is_file():
                continue
            rows.append(
                (path, srt, "Toàn bộ" if whole_novel else f"Phần {self._part_number(window)}")
            )
        return rows

    def _start_subtitle_upload(self) -> None:
        """Upload every eligible part's .srt as a YouTube subtitle track."""
        from noveltrans.youtube_upload import SubtitleRequest, YouTubeUploadError

        if self.project is None:
            QMessageBox.information(self, "Tải phụ đề lên", "Chọn truyện trước.")
            return
        for worker in (
            self._subtitle_upload_worker, self._upload_worker,
            self._thumbnail_worker, self._playlist_worker,
        ):
            if worker is not None and worker.isRunning():
                QMessageBox.information(
                    self, "Tải phụ đề lên", "Đang có phiên trình duyệt khác chạy."
                )
                return

        rows = self._subtitle_upload_rows()
        if not rows:
            QMessageBox.information(
                self,
                "Tải phụ đề lên",
                "Không có phần nào đủ điều kiện — cần đã tải video lên YouTube VÀ có file "
                ".srt (bấm “Tạo phụ đề (.srt)” trước).",
            )
            return

        try:
            requests = [
                SubtitleRequest(video=path, subtitle=srt, label=label)
                for path, srt, label in rows
            ]
            for request in requests:
                request.validate()
        except YouTubeUploadError as exc:
            QMessageBox.warning(self, "Tải phụ đề lên", str(exc))
            return

        names = ", ".join(r.label for r in requests[:5])
        more = f" (+{len(requests) - 5})" if len(requests) > 5 else ""
        confirm = QMessageBox.question(
            self,
            "Tải phụ đề lên",
            f"Sẽ tải phụ đề .srt lên {len(requests)} video đã đăng:\n{names}{more}\n\n"
            "Phụ đề cũ cùng ngôn ngữ (nếu có) sẽ bị thay thế. Không đổi video, ảnh bìa "
            "hay tiêu đề.\n\nMột cửa sổ Chrome sẽ mở ra và tự thao tác. Tiếp tục?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._subtitle_upload_worker = SubtitleUploadWorker(requests, self)
        self._subtitle_upload_worker.progress.connect(self._on_subtitle_upload_progress)
        self._subtitle_upload_worker.finished_ok.connect(self._on_subtitle_upload_finished)
        self._subtitle_upload_worker.failed.connect(self._on_subtitle_upload_failed)
        self._subtitle_upload_worker.needs_login.connect(self._on_upload_needs_login)
        track_worker(self._subtitle_upload_worker)
        self._job = job_registry.register(
            self._subtitle_upload_worker, kind="Tải phụ đề lên", novel=self._job_novel()
        )
        self.upload_pause_button.set_job(self._job.id if self._job else None)

        self.subtitle_upload_button.setEnabled(False)
        self.upload_button.setEnabled(False)
        self.thumbnail_update_button.setEnabled(False)
        self.playlist_sync_button.setEnabled(False)
        self.video_button.setEnabled(False)
        self.upload_cancel_button.setEnabled(True)
        self.progress.setMaximum(len(requests))
        self.progress.setValue(0)
        self.status_label.setText("💬 Bắt đầu tải phụ đề lên YouTube…")
        self._subtitle_upload_worker.start()

    def _on_subtitle_upload_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if message:
            self.status_label.setText(f"💬 ({done}/{total}) {message}")

    def _reset_subtitle_upload_ui(self) -> None:
        self.subtitle_upload_button.setEnabled(True)
        self.upload_button.setEnabled(True)
        self.thumbnail_update_button.setEnabled(True)
        self.playlist_sync_button.setEnabled(True)
        self.video_button.setEnabled(True)
        self.upload_cancel_button.setEnabled(False)
        self._refresh_video_list()

    def _on_subtitle_upload_finished(self, uploaded: int, errors: int) -> None:
        self._reset_subtitle_upload_ui()
        if uploaded and not errors:
            self.status_label.setText(f"✅ Đã tải phụ đề lên {uploaded} video.")
        elif uploaded:
            self.status_label.setText(
                f"⚠️ Đã tải phụ đề lên {uploaded} video, {errors} phần lỗi."
            )
        else:
            self.status_label.setText("Không tải được phụ đề lên phần nào.")

    def _on_subtitle_upload_failed(self, message: str) -> None:
        self._reset_subtitle_upload_ui()
        self.status_label.setText("")
        QMessageBox.warning(self, "Tải phụ đề lên thất bại", message)

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
            gap_seconds=self.config.tts_gap_seconds,
            speed=self.config.tts_speed,
            parent=self,
        )
        self._subtitle_worker.progress.connect(self._on_subtitle_progress)
        self._subtitle_worker.finished_ok.connect(self._on_subtitles_finished)
        self._subtitle_worker.failed.connect(self._on_subtitles_failed)
        track_worker(self._subtitle_worker)
        self._job = job_registry.register(
            self._subtitle_worker, kind="Tạo phụ đề", novel=self._job_novel()
        )
        self.pause_button.set_job(self._job.id if self._job else None)

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
            rows.append((path, "Toàn bộ" if whole_novel else f"Phần {self._part_number(window)}"))
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
        self._job = job_registry.register(
            self._playlist_worker, kind="Danh sách phát", novel=self._job_novel()
        )
        self.upload_pause_button.set_job(self._job.id if self._job else None)

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
            label = "Toàn bộ" if whole_novel else f"Phần {self._part_number(window)}"
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
        self._job = job_registry.register(
            self._thumbnail_worker, kind="Đổi ảnh bìa", novel=self._job_novel()
        )
        self.upload_pause_button.set_job(self._job.id if self._job else None)

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

    def _job_novel(self) -> str:
        """The novel label for the menu-bar job row — this tab's own project.

        Deliberately not Workspace.current_title(): each tab has an independent picker,
        so a video job on a different novel would be labelled with the scrape tab's.
        """
        return self.project.meta.display_name() if self.project is not None else ""

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
        if (
            self._subtitle_upload_worker is not None
            and self._subtitle_upload_worker.isRunning()
        ):
            self._subtitle_upload_worker.cancel()
            self._subtitle_upload_worker.wait(60_000)
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.wait(60_000)
        if self._tags_worker is not None and self._tags_worker.isRunning():
            self._tags_worker.wait(60_000)
        if self._image_prompt_worker is not None and self._image_prompt_worker.isRunning():
            self._image_prompt_worker.wait(60_000)
        if self._voices_worker is not None and self._voices_worker.isRunning():
            self._voices_worker.wait(5_000)

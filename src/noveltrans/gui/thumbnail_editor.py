"""A live thumbnail (Ảnh bìa) editor: drag the title / part text, try fonts, save & apply.

The user opens this from the Video tab. It shows a WYSIWYG preview of one cover (the novel
title + "PHẦN N" over the chosen base photo), rendered by the very same `compose_thumbnail`
that makes the real cover — so what they see is what every video gets. They can:

  * drag the title block or the "PHẦN N" block to reposition it (or nudge with the sliders),
  * switch fonts and see the result instantly,

then **save** the positions + font to config so every subsequent render uses them, and
optionally **apply to all** existing parts right away (a callback the tab supplies).

The preview renders at a smaller size for speed; because positions are stored as fractions
of the frame, the small preview and the full 1280×720 cover lay out identically.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# The preview is rendered at this size (16:9, same aspect as the 1280×720 cover).
_PREVIEW_W = 720
_PREVIEW_H = 405


def _pil_to_pixmap(img) -> QPixmap:
    """Convert an RGB/RGBA Pillow image to a QPixmap (copied, so it owns its buffer)."""
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class _DragPreview(QLabel):
    """A fixed-size preview label that reports drags as (x, y) fractions in 0..1."""

    moved = Signal(float, float)

    def __init__(self, w: int, h: int, parent=None):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _emit(self, pos) -> None:
        x = min(max(pos.x() / self.width(), 0.0), 1.0)
        y = min(max(pos.y() / self.height(), 0.0), 1.0)
        self.moved.emit(x, y)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._emit(event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._emit(event.position())


class ThumbnailEditorDialog(QDialog):
    """Reposition the cover text + pick a font live, then save/apply to every cover."""

    def __init__(
        self,
        config,
        *,
        base_image: str,
        novel_title: str,
        part_num: int = 1,
        tagline: str = "",
        on_apply_all: Callable[[], None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.config = config
        self.base_image = base_image or ""
        self.novel_title = novel_title or "Tên truyện"
        self.part_num = part_num or 1
        self.tagline = tagline or ""
        self._on_apply_all = on_apply_all

        # editable state, seeded from the saved config
        self.title_pos = list(config.video_thumbnail_title_pos)
        self.part_pos = list(config.video_thumbnail_part_pos)
        self.font_key = config.video_thumbnail_font
        self._active = "title"  # which block a drag moves

        self.setWindowTitle("Tùy chỉnh ảnh bìa — kéo để đặt vị trí, đổi phông trực tiếp")
        self.setModal(True)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(80)
        self._render_timer.timeout.connect(self._render_preview)

        self._build_ui()
        self._sync_controls_from_state()
        self._render_preview()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        from noveltrans.tts.video import VIDEO_FONTS

        self.preview = _DragPreview(_PREVIEW_W, _PREVIEW_H)
        self.preview.moved.connect(self._on_preview_dragged)

        # which block the drag / sliders move
        self.pick_title = QRadioButton("Tiêu đề truyện")
        self.pick_part = QRadioButton(f"PHẦN {self.part_num}")
        self.pick_title.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.pick_title)
        group.addButton(self.pick_part)
        self.pick_title.toggled.connect(self._on_active_changed)
        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("Kéo trên ảnh để đặt vị trí. Đang chọn:"))
        pick_row.addWidget(self.pick_title)
        pick_row.addWidget(self.pick_part)
        pick_row.addStretch()

        # font picker (live)
        self.font_combo = QComboBox()
        for key, spec in VIDEO_FONTS.items():
            self.font_combo.addItem(spec["label"], key)
        fidx = self.font_combo.findData(self.font_key)
        self.font_combo.setCurrentIndex(fidx if fidx >= 0 else 0)
        self.font_combo.currentIndexChanged.connect(self._on_font_changed)
        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Phông chữ:"))
        font_row.addWidget(self.font_combo)
        font_row.addStretch()

        # fine-tune sliders for the active block (0..100% of the frame)
        self.slider_x = self._make_slider()
        self.slider_y = self._make_slider()
        self.slider_x.valueChanged.connect(lambda v: self._on_slider(0, v))
        self.slider_y.valueChanged.connect(lambda v: self._on_slider(1, v))
        sx = QHBoxLayout()
        sx.addWidget(QLabel("Ngang:"))
        sx.addWidget(self.slider_x)
        sy = QHBoxLayout()
        sy.addWidget(QLabel("Dọc:  "))
        sy.addWidget(self.slider_y)

        reset = QPushButton("Đặt lại vị trí")
        reset.setToolTip("Trả tiêu đề và PHẦN N về vị trí mặc định.")
        reset.clicked.connect(self._reset_positions)

        save = QPushButton("Lưu")
        save.setToolTip("Lưu vị trí + phông chữ cho các video tạo sau này.")
        save.clicked.connect(self._save)
        apply_all = QPushButton("Lưu & áp dụng cho tất cả")
        apply_all.setProperty("primary", True)
        apply_all.setToolTip("Lưu rồi vẽ lại ngay ảnh bìa cho mọi phần đang liệt kê.")
        apply_all.clicked.connect(self._save_and_apply)
        apply_all.setEnabled(self._on_apply_all is not None)
        close = QPushButton("Đóng")
        close.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(reset)
        btns.addStretch()
        btns.addWidget(save)
        btns.addWidget(apply_all)
        btns.addWidget(close)

        controls = QVBoxLayout()
        controls.addLayout(pick_row)
        controls.addLayout(font_row)
        controls.addLayout(sx)
        controls.addLayout(sy)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview, alignment=Qt.AlignmentFlag.AlignCenter)
        controls_box = QWidget()
        controls_box.setLayout(controls)
        layout.addWidget(controls_box)
        layout.addLayout(btns)

    @staticmethod
    def _make_slider() -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(0, 1000)  # 0.0..1.0 in thousandths, for smooth placement
        return s

    # -------------------------------------------------------------- state

    def _active_pos(self) -> list[float]:
        return self.title_pos if self._active == "title" else self.part_pos

    def _on_active_changed(self, _checked: bool = False) -> None:
        self._active = "title" if self.pick_title.isChecked() else "part"
        self._sync_controls_from_state()

    def _sync_controls_from_state(self) -> None:
        """Push the active block's position into the sliders without re-triggering a render."""
        pos = self._active_pos()
        for slider, frac in ((self.slider_x, pos[0]), (self.slider_y, pos[1])):
            slider.blockSignals(True)
            slider.setValue(round(frac * 1000))
            slider.blockSignals(False)

    def _on_slider(self, axis: int, value: int) -> None:
        self._active_pos()[axis] = value / 1000.0
        self._schedule_render()

    def _on_preview_dragged(self, x: float, y: float) -> None:
        pos = self._active_pos()
        pos[0], pos[1] = x, y
        self._sync_controls_from_state()
        self._schedule_render()

    def _on_font_changed(self) -> None:
        self.font_key = self.font_combo.currentData()
        self._schedule_render()

    def _reset_positions(self) -> None:
        from noveltrans.tts.thumbnail import DEFAULT_PART_POS, DEFAULT_TITLE_POS

        self.title_pos = list(DEFAULT_TITLE_POS)
        self.part_pos = list(DEFAULT_PART_POS)
        self._sync_controls_from_state()
        self._render_preview()

    # ------------------------------------------------------------ preview

    def _schedule_render(self) -> None:
        self._render_timer.start()

    def _render_preview(self) -> None:
        from noveltrans.tts.thumbnail import compose_thumbnail
        from noveltrans.tts.video import font_dir_context, video_font

        try:
            with font_dir_context() as font_dir:
                img = compose_thumbnail(
                    self.base_image,
                    vn_title=self.novel_title,
                    part_num=self.part_num,
                    tagline=self.tagline,
                    font_path=font_dir / video_font(self.font_key)["file"],
                    width=_PREVIEW_W, height=_PREVIEW_H,
                    title_pos=tuple(self.title_pos), part_pos=tuple(self.part_pos),
                )
            self.preview.setPixmap(_pil_to_pixmap(img))
        except Exception:  # noqa: BLE001 — a bad base image must not crash the editor
            self.preview.setText("Không tạo được xem trước")

    # ------------------------------------------------------------- persist

    def _save_to_config(self) -> None:
        self.config.video_thumbnail_title_pos = tuple(self.title_pos)
        self.config.video_thumbnail_part_pos = tuple(self.part_pos)
        self.config.video_thumbnail_font = self.font_key

    def _save(self) -> None:
        self._save_to_config()
        self.accept()

    def _save_and_apply(self) -> None:
        self._save_to_config()
        if self._on_apply_all is not None:
            self._on_apply_all()
        self.accept()

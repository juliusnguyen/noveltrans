"""Dialogs for novels the user writes themselves, rather than scrapes.

Two small modals used by the Tải truyện tab:

* `LocalNovelDialog` — collects the metadata for a brand-new hand-written novel. The
  project it feeds gets `site="local"` and a synthetic `local://<uuid>` URL: the URL is
  never fetched, but it IS the project's identity on disk (the folder name hashes it,
  and the library looks projects up by it), so it has to be unique. See
  `noveltrans.models.new_local_url` for what a blank one would have destroyed.
* `AddChaptersDialog` — one chapter name per line. A hand-written novel has no table of
  contents to scan, so this is where chapter rows come from; taking a whole list at once
  means an outline can simply be pasted in.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from noveltrans.models import LOCAL_SITE, NovelMeta, new_local_url

# Source languages a hand-written novel might be in. Vietnamese leads because the whole
# point of writing your own is reading it aloud with the (Vietnamese) VieNeu voices.
SOURCE_LANGS: list[tuple[str, str]] = [
    ("Tiếng Việt", "vi"),
    ("Tiếng Trung", "zh"),
    ("Tiếng Anh", "en"),
]


class LocalNovelDialog(QDialog):
    """Create a novel with no source website. `meta()` is valid once accepted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Truyện tự viết")
        self.setMinimumWidth(460)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Bắt buộc")
        self.author_edit = QLineEdit()
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setFixedHeight(90)
        self.lang_combo = QComboBox()
        for label, code in SOURCE_LANGS:
            self.lang_combo.addItem(label, code)
        self.lang_combo.setToolTip(
            "Ngôn ngữ bạn viết. Tiếng Việt thì tạo audio thẳng từ 'Bản gốc' được luôn."
        )

        form = QFormLayout()
        form.addRow("Tên truyện:", self.title_edit)
        form.addRow("Tác giả:", self.author_edit)
        form.addRow("Mô tả:", self.desc_edit)
        form.addRow("Ngôn ngữ gốc:", self.lang_combo)

        hint = QLabel(
            "Truyện tự viết không có link nguồn — thêm chương bằng tên ở tab Tải truyện, "
            "rồi dán nội dung vào ô “Bản gốc” ở tab Dịch."
        )
        hint.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        # A novel with no title has nowhere to live: the project folder slug and every
        # video/thumbnail label fall back to it.
        self.title_edit.textChanged.connect(self._sync_ok)
        self._sync_ok()

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(self.buttons)

    def _sync_ok(self) -> None:
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(bool(self.title_edit.text().strip()))

    def meta(self) -> NovelMeta:
        """The NovelMeta to create the project from — a fresh URL on every call."""
        return NovelMeta(
            url=new_local_url(),
            site=LOCAL_SITE,
            title=self.title_edit.text().strip(),
            author=self.author_edit.text().strip(),
            description=self.desc_edit.toPlainText().strip(),
            source_lang=self.lang_combo.currentData(),
        )


class AddChaptersDialog(QDialog):
    """Add chapters by name — one per line. `titles()` is valid once accepted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Thêm chương")
        self.setMinimumSize(460, 360)

        self.names_edit = QPlainTextEdit()
        self.names_edit.setPlaceholderText(
            "Mỗi dòng một tên chương, ví dụ:\n\nChương 1: Khởi đầu\nChương 2: Gặp gỡ"
        )
        self.count_label = QLabel("")
        self.names_edit.textChanged.connect(self._sync_count)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._sync_count()

        layout = QVBoxLayout(self)
        layout.addWidget(self.names_edit, stretch=1)
        layout.addWidget(self.count_label)
        layout.addWidget(self.buttons)

    def titles(self) -> list[str]:
        """Non-blank, stripped chapter names in the order they were typed."""
        return [line.strip() for line in self.names_edit.toPlainText().splitlines() if line.strip()]

    def _sync_count(self) -> None:
        count = len(self.titles())
        self.count_label.setText(f"Sẽ thêm {count} chương." if count else "")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(count > 0)

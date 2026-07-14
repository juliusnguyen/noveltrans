"""Settings dialog — library dir, request delay, translator, language, API key."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from noveltrans.config import TARGET_LANGS, AppConfig, translator_labels

_MEDOCTRUYEN_LOGIN_URL = "https://medoctruyen.vn/auth/login"

_COOKIE_HELP = """\
medoctruyen.vn yêu cầu đăng nhập mới đọc được nội dung đầy đủ của chương. \
Hãy lấy cookie phiên đăng nhập của bạn từ trình duyệt:

1. Mở trình duyệt (Chrome / Edge / Cốc Cốc) và ĐĂNG NHẬP vào medoctruyen.vn.

2. Mở một trang chương bất kỳ, ví dụ:
   https://medoctruyen.vn/tu-bao-tien-bon/chuong-1

3. Mở Developer Tools:
   • macOS:  ⌥ + ⌘ + I
   • Windows / Linux:  F12  (hoặc Ctrl + Shift + I)

4. Chọn tab “Network” (Mạng), rồi tải lại trang (⌘R hoặc F5).

5. Bấm vào request đầu tiên trong danh sách (thường trùng tên trang, ví dụ “chuong-1”).

6. Kéo xuống mục “Request Headers”, tìm dòng bắt đầu bằng “cookie:”.

7. Sao chép TOÀN BỘ giá trị phía sau chữ “cookie:” (gồm nhiều cặp tên=giá_trị, \
ngăn cách bằng “; ”).

8. Dán vào ô “Cookie medoctruyen.vn” trong cửa sổ Cài đặt rồi bấm OK.

Lưu ý:
• Phải đang ĐĂNG NHẬP khi sao chép — cookie khi đăng xuất sẽ không mở được nội dung.
• Sao chép ĐẦY ĐỦ dòng cookie, không chỉ một cặp.
• Cookie sẽ hết hạn sau một thời gian; nếu tải chương báo lỗi “cần đăng nhập”, \
hãy lấy lại cookie mới và dán lại.\
"""


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Cài đặt")
        self.setMinimumWidth(480)

        form = QFormLayout()

        # Library directory
        self.library_edit = QLineEdit(str(config.library_dir))
        browse = QPushButton("Chọn…")
        browse.clicked.connect(self._browse_library)
        lib_row = QHBoxLayout()
        lib_row.addWidget(self.library_edit)
        lib_row.addWidget(browse)
        form.addRow("Thư mục thư viện:", lib_row)

        # Request delay
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 30.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setSuffix(" s")
        self.delay_spin.setValue(config.request_delay)
        form.addRow("Giãn cách giữa các request:", self.delay_spin)

        # Translator engine
        self.translator_combo = QComboBox()
        for key, label in translator_labels(config).items():
            self.translator_combo.addItem(label, key)
        self.translator_combo.setCurrentIndex(
            self.translator_combo.findData(config.translator)
        )
        form.addRow("Engine dịch:", self.translator_combo)

        # Target language
        self.lang_combo = QComboBox()
        for key, label in TARGET_LANGS.items():
            self.lang_combo.addItem(label, key)
        self.lang_combo.setCurrentIndex(self.lang_combo.findData(config.target_lang))
        form.addRow("Ngôn ngữ đích:", self.lang_combo)

        # Claude
        self.api_key_edit = QLineEdit(config.claude_api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-ant-…")
        form.addRow("Claude API key:", self.api_key_edit)

        self.model_edit = QLineEdit(config.claude_model)
        form.addRow("Claude model:", self.model_edit)

        # CLI agent engines
        self.cli_edit = QLineEdit(config.cli_command)
        self.cli_edit.setPlaceholderText("agy -p")
        self.cli_edit.setToolTip(
            "Lệnh chạy AI agent ở chế độ headless; nội dung chương sẽ được nối vào cuối lệnh."
        )
        form.addRow("Lệnh CLI Agent:", self.cli_edit)

        self.claude_cli_edit = QLineEdit(config.claude_cli_command)
        self.claude_cli_edit.setPlaceholderText("claude -p   hoặc   claude -p --model haiku")
        self.claude_cli_edit.setToolTip(
            "Lệnh Claude Code headless — dùng subscription Claude sẵn có, không cần API key."
        )
        form.addRow("Lệnh Claude CLI:", self.claude_cli_edit)

        # medoctruyen.vn session cookie — needed to read full chapter bodies
        self.medoctruyen_cookie_edit = QLineEdit(config.medoctruyen_cookies)
        self.medoctruyen_cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.medoctruyen_cookie_edit.setPlaceholderText("__Secure-…=…; session=…")
        self.medoctruyen_cookie_edit.setToolTip(
            "Đăng nhập medoctruyen.vn trên trình duyệt, sao chép header 'Cookie' của "
            "request rồi dán vào đây. Cần thiết để tải nội dung đầy đủ của chương."
        )
        cookie_help = QPushButton("Hướng dẫn")
        cookie_help.setToolTip("Xem các bước lấy cookie medoctruyen.vn")
        cookie_help.clicked.connect(self._show_cookie_help)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(self.medoctruyen_cookie_edit, stretch=1)
        cookie_row.addWidget(cookie_help)
        form.addRow("Cookie medoctruyen.vn:", cookie_row)

        cookie_hint = QLabel(
            'Cần đăng nhập medoctruyen.vn để tải nội dung đầy đủ — bấm “Hướng dẫn” để '
            "xem cách lấy cookie."
        )
        cookie_hint.setProperty("muted", True)
        cookie_hint.setWordWrap(True)
        form.addRow("", cookie_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _show_cookie_help(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Cách lấy cookie medoctruyen.vn")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("Hướng dẫn lấy cookie đăng nhập")
        box.setInformativeText(_COOKIE_HELP)
        open_login = box.addButton("Mở trang đăng nhập", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_login:
            QDesktopServices.openUrl(QUrl(_MEDOCTRUYEN_LOGIN_URL))

    def _browse_library(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục thư viện", self.library_edit.text()
        )
        if path:
            self.library_edit.setText(path)

    def accept(self) -> None:
        self.config.library_dir = self.library_edit.text()
        self.config.request_delay = self.delay_spin.value()
        self.config.translator = self.translator_combo.currentData()
        self.config.target_lang = self.lang_combo.currentData()
        self.config.claude_api_key = self.api_key_edit.text().strip()
        self.config.claude_model = self.model_edit.text().strip()
        self.config.cli_command = self.cli_edit.text().strip()
        self.config.claude_cli_command = self.claude_cli_edit.text().strip()
        self.config.medoctruyen_cookies = self.medoctruyen_cookie_edit.text().strip()
        self.config.sync()
        super().accept()

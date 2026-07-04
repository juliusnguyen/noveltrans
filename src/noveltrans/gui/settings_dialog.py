"""Settings dialog — library dir, request delay, translator, language, API key."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from noveltrans.config import TARGET_LANGS, AppConfig, translator_labels


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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

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
        self.config.sync()
        super().accept()

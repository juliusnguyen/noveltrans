"""Settings dialog — library dir, request delay, translator, language, API key."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from noveltrans.config import (
    DEFAULT_ONEDRIVE_ROOT,
    TARGET_LANGS,
    AppConfig,
    translator_labels,
)
from noveltrans.discord_unlock import valid_channel_url
from noveltrans.gui import keep_awake
from noveltrans.gui.workers import (
    DiscordLoginWorker,
    OneDriveLoginWorker,
    YouTubeLoginWorker,
)
from noveltrans.tts.convert import ffmpeg_available

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


_TIEUTHUYETMANG_LOGIN_URL = "https://tieuthuyetmang.com/dang-nhap"

_TIEUTHUYETMANG_COOKIE_HELP = """\
tieuthuyetmang.com khoá phần lớn chương sau đăng nhập và trả phí. \
Hãy lấy cookie phiên đăng nhập của bạn từ trình duyệt:

1. Mở trình duyệt (Chrome / Edge / Cốc Cốc) và ĐĂNG NHẬP vào tieuthuyetmang.com.

2. Mở một trang chương bất kỳ, ví dụ:
   https://tieuthuyetmang.com/truyen/<tên-truyện>/doc/1

3. Mở Developer Tools:
   • macOS:  ⌥ + ⌘ + I
   • Windows / Linux:  F12  (hoặc Ctrl + Shift + I)

4. Chọn tab “Network” (Mạng), rồi tải lại trang (⌘R hoặc F5).

5. Bấm vào request đầu tiên trong danh sách (thường trùng tên trang, ví dụ “1”).

6. Kéo xuống mục “Request Headers”, tìm dòng bắt đầu bằng “cookie:”.

7. Sao chép TOÀN BỘ giá trị phía sau chữ “cookie:” (gồm nhiều cặp tên=giá_trị, \
ngăn cách bằng “; ”).

8. Dán vào ô “Cookie tieuthuyetmang.com” trong cửa sổ Cài đặt rồi bấm OK.

Lưu ý:
• Phải đang ĐĂNG NHẬP khi sao chép — cookie khi đăng xuất sẽ không mở được nội dung.
• Cookie KHÔNG mở khoá chương trả phí: chương có biểu tượng khoá vẫn cần tài khoản \
đã mua/mở khoá chương đó. Ứng dụng chỉ tải những chương tài khoản của bạn đọc được.
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
        # Editable combo, not a plain field: someone with several libraries can switch
        # between them without re-browsing, and a path that isn't in the list can still be
        # typed or pasted exactly as before.
        self.library_edit = QComboBox()
        self.library_edit.setEditable(True)
        self.library_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.library_edit.addItems(config.library_dir_history)
        self.library_edit.setCurrentText(str(config.library_dir))
        self.library_edit.setToolTip(
            "Thư mục chứa các truyện. Danh sách là những thư mục đã dùng trước đây — "
            "chọn để chuyển thư viện, hoặc gõ/dán đường dẫn mới."
        )
        self.library_edit.setMinimumWidth(320)
        browse = QPushButton("Chọn…")
        browse.clicked.connect(self._browse_library)
        lib_row = QHBoxLayout()
        lib_row.addWidget(self.library_edit, stretch=1)
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

        # tieuthuyetmang.com session cookie — same shape, different site
        self.tieuthuyetmang_cookie_edit = QLineEdit(config.tieuthuyetmang_cookies)
        self.tieuthuyetmang_cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tieuthuyetmang_cookie_edit.setPlaceholderText("__Secure-…=…; session=…")
        self.tieuthuyetmang_cookie_edit.setToolTip(
            "Đăng nhập tieuthuyetmang.com trên trình duyệt, sao chép header 'Cookie' của "
            "request rồi dán vào đây. Cần thiết để tải các chương đã mở khoá."
        )
        ttm_help = QPushButton("Hướng dẫn")
        ttm_help.setToolTip("Xem các bước lấy cookie tieuthuyetmang.com")
        ttm_help.clicked.connect(self._show_tieuthuyetmang_cookie_help)
        ttm_row = QHBoxLayout()
        ttm_row.addWidget(self.tieuthuyetmang_cookie_edit, stretch=1)
        ttm_row.addWidget(ttm_help)
        form.addRow("Cookie tieuthuyetmang.com:", ttm_row)

        ttm_hint = QLabel(
            "Phần lớn chương trên tieuthuyetmang.com là chương trả phí — cookie chỉ tải "
            "được những chương tài khoản của bạn đã mở khoá."
        )
        ttm_hint.setProperty("muted", True)
        ttm_hint.setWordWrap(True)
        form.addRow("", ttm_hint)

        # Auto-unlock: run medoctruyen's Discord /mochuong unlock automatically when
        # the 50-chapters/day cap is hit, so a download batch resumes unattended.
        self.discord_enable = QCheckBox(
            "Tự mở khoá qua Discord khi đạt giới hạn 50 chương/ngày"
        )
        self.discord_enable.setChecked(config.discord_autounlock_enabled)
        form.addRow("Tự mở khoá:", self.discord_enable)

        self.discord_channel_edit = QLineEdit(config.discord_channel_url)
        self.discord_channel_edit.setPlaceholderText(
            "https://discord.com/channels/…/…  (chuột phải kênh #mở-khoá → Copy Link)"
        )
        discord_login = QPushButton("Đăng nhập Discord")
        discord_login.setToolTip(
            "Mở cửa sổ Chrome riêng để đăng nhập tài khoản Discord phụ một lần."
        )
        discord_login.clicked.connect(self._discord_login)
        discord_row = QHBoxLayout()
        discord_row.addWidget(self.discord_channel_edit, stretch=1)
        discord_row.addWidget(discord_login)
        form.addRow("Kênh #mở-khoá:", discord_row)

        discord_hint = QLabel(
            "Dùng một tài khoản Discord PHỤ (không dùng tài khoản chính): tự động hoá "
            "tài khoản Discord là vi phạm điều khoản của Discord. Cần cài Playwright: "
            "pip install 'noveltrans[discord]' rồi playwright install chromium."
        )
        discord_hint.setProperty("muted", True)
        discord_hint.setWordWrap(True)
        form.addRow("", discord_hint)

        # YouTube upload: the Video tab drives Studio in a dedicated browser profile.
        # Only the one-time login lives here; everything else about a run (visibility,
        # schedule) is per-run and belongs next to the parts list in the Video tab.
        youtube_login = QPushButton("Đăng nhập YouTube")
        youtube_login.setToolTip(
            "Mở cửa sổ Chrome riêng để đăng nhập kênh YouTube một lần, cho tính năng "
            "tự tải video lên ở tab Video."
        )
        youtube_login.clicked.connect(self._youtube_login)
        youtube_switch = QPushButton("Đổi kênh…")
        youtube_switch.setToolTip(
            "Đăng xuất kênh hiện tại rồi đăng nhập lại — dùng khi lỡ đăng nhập nhầm kênh."
        )
        youtube_switch.clicked.connect(self._youtube_switch)
        self.youtube_status = QLabel("")
        self.youtube_status.setProperty("muted", True)
        self._refresh_youtube_status()
        youtube_row = QHBoxLayout()
        youtube_row.addWidget(youtube_login)
        youtube_row.addWidget(youtube_switch)
        youtube_row.addWidget(self.youtube_status, stretch=1)
        form.addRow("Tải lên YouTube:", youtube_row)

        youtube_hint = QLabel(
            "Đăng nhập một lần vào kênh sẽ đăng video; phiên được lưu trong profile "
            "riêng của ứng dụng. Đăng nhập nhầm kênh thì bấm “Đổi kênh…”. Tự động hoá "
            "YouTube Studio là vi phạm điều khoản của YouTube — dùng ở mức vừa phải. "
            "Cần cài Playwright: pip install 'noveltrans[browser]' rồi playwright "
            "install chromium."
        )
        youtube_hint.setProperty("muted", True)
        youtube_hint.setWordWrap(True)
        form.addRow("", youtube_hint)

        # OneDrive backup: the Export tab mirrors a whole novel folder up, in its own
        # browser profile. Like YouTube, only the one-time sign-in lives here. There is
        # deliberately no destination field — every novel goes to /NovelTrans/<tên
        # truyện>/, derived from its title, so there is nothing per-run to configure.
        onedrive_login = QPushButton("Đăng nhập OneDrive")
        onedrive_login.setToolTip(
            "Mở cửa sổ Chrome riêng để đăng nhập tài khoản Microsoft một lần, cho tính "
            "năng sao lưu truyện lên OneDrive ở tab Xuất bản."
        )
        onedrive_login.clicked.connect(self._onedrive_login)
        onedrive_switch = QPushButton("Đổi tài khoản…")
        onedrive_switch.setToolTip(
            "Đăng xuất tài khoản hiện tại rồi đăng nhập lại — dùng khi lỡ đăng nhập nhầm."
        )
        onedrive_switch.clicked.connect(self._onedrive_switch)
        self.onedrive_status = QLabel("")
        self.onedrive_status.setProperty("muted", True)
        self._refresh_onedrive_status()
        onedrive_row = QHBoxLayout()
        onedrive_row.addWidget(onedrive_login)
        onedrive_row.addWidget(onedrive_switch)
        onedrive_row.addWidget(self.onedrive_status, stretch=1)
        form.addRow("Sao lưu OneDrive:", onedrive_row)

        # Backs up several novels in one go. The run itself is handed to MainWindow —
        # this dialog is modal, and a sync can take hours; owning it here would lock the
        # whole app for the duration. `sync_requests` is what MainWindow reads.
        self.sync_requests: list = []
        onedrive_sync = QPushButton("Đồng bộ nhiều truyện…")
        onedrive_sync.setToolTip(
            "Xem cả thư viện, chọn những truyện muốn sao lưu, rồi đẩy lần lượt lên "
            "OneDrive."
        )
        onedrive_sync.clicked.connect(self._start_onedrive_sync)
        sync_row = QHBoxLayout()
        sync_row.addWidget(onedrive_sync)
        sync_row.addStretch()
        form.addRow("", sync_row)

        # One destination for the whole library; each novel becomes a subfolder of it.
        # A single choice rather than a per-novel picker, which is what was asked for.
        self.onedrive_root_edit = QLineEdit(config.onedrive_root)
        self.onedrive_root_edit.setPlaceholderText(DEFAULT_ONEDRIVE_ROOT)
        self.onedrive_root_edit.setToolTip(
            "Thư mục trên OneDrive chứa toàn bộ bản sao lưu. Mỗi truyện là một thư mục "
            "con bên trong, đặt theo tên truyện. Ví dụ: /Fox Novel"
        )
        onedrive_browse = QPushButton("Chọn…")
        onedrive_browse.setToolTip(
            "Mở OneDrive và chọn thư mục có sẵn — khỏi phải gõ đúng tên."
        )
        onedrive_browse.clicked.connect(self._browse_onedrive_root)
        onedrive_root_row = QHBoxLayout()
        onedrive_root_row.addWidget(self.onedrive_root_edit, stretch=1)
        onedrive_root_row.addWidget(onedrive_browse)
        form.addRow("Thư mục đích:", onedrive_root_row)

        onedrive_hint = QLabel(
            "Mỗi truyện được đẩy vào <thư mục đích>/<tên truyện>/ trên OneDrive, giữ "
            "nguyên cấu trúc thư mục trên máy. File trùng tên trên OneDrive sẽ bị GHI ĐÈ bằng "
            "bản trên máy — đây là sao lưu một chiều, không phải đồng bộ hai chiều. "
            "Tự động hoá giao diện web OneDrive không được Microsoft hỗ trợ chính thức; "
            "đích đến là kho lưu trữ riêng của bạn nên rủi ro thấp, nhưng vẫn nên dùng ở "
            "mức vừa phải. Cần cài Playwright: pip install 'noveltrans[browser]' rồi "
            "playwright install chromium."
        )
        onedrive_hint.setProperty("muted", True)
        onedrive_hint.setWordWrap(True)
        form.addRow("", onedrive_hint)

        # Vertical is the default: a tab label is now a novel's full name, which needs
        # far more width than a horizontal strip can give each tab once several are open.
        self.vertical_tabs_check = QCheckBox("Xếp tab truyện thành cột dọc bên trái")
        self.vertical_tabs_check.setChecked(config.workspace_tabs_vertical)
        self.vertical_tabs_check.setToolTip(
            "Bật: mỗi truyện là một dòng trong cột bên trái, tên hiển thị ngang. "
            "Tắt: các tab nằm ngang phía trên như trình duyệt. Áp dụng ngay khi bấm OK."
        )
        form.addRow("Thanh tab truyện:", self.vertical_tabs_check)

        # Keep the Mac awake while a job runs so it doesn't idle-sleep mid-download.
        self.keep_awake_check = QCheckBox("Giữ máy thức khi đang chạy (tải/dịch/tạo audio)")
        self.keep_awake_check.setChecked(config.keep_awake_enabled)
        form.addRow("Chống ngủ:", self.keep_awake_check)

        # Parallel TTS workers — each loads its own ~334MB model, so more workers
        # means proportionally more RAM and CPU. Default 1 = current behavior.
        self.tts_workers_spin = QSpinBox()
        self.tts_workers_spin.setRange(1, 6)
        self.tts_workers_spin.setValue(config.tts_workers)
        self.tts_workers_spin.setToolTip(
            "Số luồng tạo audio song song. Mỗi luồng nạp một model VieNeu riêng "
            "(~334 MB RAM/luồng) và dùng thêm CPU. 1 = tuần tự (mặc định). "
            "Chỉ tăng nếu máy nhiều RAM/nhân."
        )
        form.addRow("Luồng tạo audio song song:", self.tts_workers_spin)

        # Strip special characters (emoji, decorative symbols, stray CJK, markdown)
        # before synthesis so the audio reads smoothly. Vietnamese is kept.
        self.tts_clean_check = QCheckBox("Làm sạch ký tự đặc biệt trước khi đọc")
        self.tts_clean_check.setChecked(config.tts_clean_text)
        self.tts_clean_check.setToolTip(
            "Bỏ emoji, ký hiệu trang trí (★ ※ 【】), chữ Hán còn sót và ký tự markdown "
            "khỏi văn bản trước khi tạo audio. Giữ nguyên tiếng Việt và dấu câu. "
            "Chỉ áp dụng cho bản đưa vào engine — không đổi văn bản đã lưu."
        )
        form.addRow("Đọc (TTS):", self.tts_clean_check)

        # Extra characters the user wants stripped on top of the automatic cleaning.
        # Only bites on characters normally KEPT (punctuation) — e.g. "()" so
        # parentheses aren't voiced; anything already stripped is unaffected.
        self.tts_extra_remove_edit = QLineEdit(config.tts_clean_extra_remove)
        self.tts_extra_remove_edit.setPlaceholderText("ví dụ: ()“”—  (dán các ký tự cần bỏ)")
        self.tts_extra_remove_edit.setToolTip(
            "Các ký tự này sẽ bị bỏ thêm, ngoài phần làm sạch tự động. Chỉ có tác dụng "
            "với ký tự vốn được GIỮ (như dấu ngoặc, dấu nháy) — dùng “Xem trước văn bản” "
            "ở tab Audio để thấy ký tự nào còn lại rồi dán vào đây. Không cần liệt kê "
            "emoji/ký hiệu vì chúng đã bị bỏ sẵn."
        )
        self.tts_extra_remove_edit.setEnabled(self.tts_clean_check.isChecked())
        self.tts_clean_check.toggled.connect(self.tts_extra_remove_edit.setEnabled)
        form.addRow("Bỏ thêm ký tự:", self.tts_extra_remove_edit)

        # Output adjustments. Defaults reproduce the app's original audio.
        self.tts_gap_spin = QDoubleSpinBox()
        self.tts_gap_spin.setRange(0.0, 2.0)
        self.tts_gap_spin.setSingleStep(0.1)
        self.tts_gap_spin.setSuffix(" s")
        self.tts_gap_spin.setValue(config.tts_gap_seconds)
        self.tts_gap_spin.setToolTip("Khoảng lặng giữa các đoạn khi đọc. Mặc định 0.4 s.")
        form.addRow("Khoảng lặng giữa đoạn:", self.tts_gap_spin)

        self.tts_speed_spin = QDoubleSpinBox()
        self.tts_speed_spin.setRange(0.5, 2.0)
        self.tts_speed_spin.setSingleStep(0.05)
        self.tts_speed_spin.setSuffix("×")
        self.tts_speed_spin.setValue(config.tts_speed)
        if ffmpeg_available():
            self.tts_speed_spin.setToolTip("Tốc độ đọc (giữ nguyên cao độ). 1.0× = bình thường.")
        else:
            self.tts_speed_spin.setEnabled(False)
            self.tts_speed_spin.setToolTip("Cần ffmpeg để đổi tốc độ (brew install ffmpeg).")
        form.addRow("Tốc độ đọc:", self.tts_speed_spin)

        self.tts_volume_spin = QDoubleSpinBox()
        self.tts_volume_spin.setRange(0.1, 3.0)
        self.tts_volume_spin.setSingleStep(0.1)
        self.tts_volume_spin.setSuffix("×")
        self.tts_volume_spin.setValue(config.tts_volume)
        self.tts_volume_spin.setToolTip("Âm lượng. 1.0× = nguyên bản; trên 1.0× có thể bị rè.")
        form.addRow("Âm lượng:", self.tts_volume_spin)

        self.tts_temperature_spin = QDoubleSpinBox()
        self.tts_temperature_spin.setRange(0.0, 1.5)
        self.tts_temperature_spin.setSingleStep(0.05)
        self.tts_temperature_spin.setValue(config.tts_temperature)
        self.tts_temperature_spin.setSpecialValueText("Mặc định")  # 0.0 = use model default
        self.tts_temperature_spin.setToolTip(
            "Độ biểu cảm của giọng đọc. “Mặc định” (0.0) để model tự quyết; cao hơn = "
            "biểu cảm/đa dạng hơn, thấp hơn = đều/ổn định hơn."
        )
        form.addRow("Độ biểu cảm:", self.tts_temperature_spin)

        # Model precision (ONNX/CPU graph). fp32 is higher quality but slower and pulls
        # a larger one-time model download; int8 is the fast default.
        self.tts_precision_combo = QComboBox()
        self.tts_precision_combo.addItem("Nhanh (int8 — mặc định)", "int8")
        self.tts_precision_combo.addItem("Chất lượng cao (fp32 — chậm hơn)", "fp32")
        idx = self.tts_precision_combo.findData(config.tts_precision)
        self.tts_precision_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.tts_precision_combo.setToolTip(
            "fp32 cho chất lượng cao hơn nhưng đọc chậm hơn và tải thêm model (~1 lần). "
            "Đổi lựa chọn này sẽ tải graph mới ở lần tạo audio kế tiếp."
        )
        form.addRow("Chất lượng giọng:", self.tts_precision_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        # This form has grown past what a small laptop screen fits in one window — without
        # somewhere to overflow to, Qt just pushes the OK/Cancel row off the bottom of the
        # screen. A scroll area gives the overflow a destination instead (same fix as the
        # Video tab, feature 025).
        form_widget = QWidget()
        form_widget.setLayout(form)
        self.scroll = QScrollArea()
        self.scroll.setWidget(form_widget)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)  # no second border inside the dialog

        layout = QVBoxLayout(self)
        layout.addWidget(self.scroll, stretch=1)
        layout.addWidget(buttons)  # pinned outside the scroll — always reachable

        # Cap the initial height to the screen instead of the full form's sizeHint, so the
        # scroll area actually has something to do on a screen too short for every row.
        screen = QApplication.primaryScreen()
        if screen is not None:
            max_height = int(screen.availableGeometry().height() * 0.85)
            self.resize(self.sizeHint().width(), min(self.sizeHint().height(), max_height))

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

    def _show_tieuthuyetmang_cookie_help(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Cách lấy cookie tieuthuyetmang.com")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("Hướng dẫn lấy cookie đăng nhập")
        box.setInformativeText(_TIEUTHUYETMANG_COOKIE_HELP)
        open_login = box.addButton("Mở trang đăng nhập", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_login:
            QDesktopServices.openUrl(QUrl(_TIEUTHUYETMANG_LOGIN_URL))

    def _discord_login(self) -> None:
        """Open the one-time Discord login window for the throwaway account."""
        self._login_worker = DiscordLoginWorker(self)
        self._login_worker.done.connect(
            lambda: QMessageBox.information(
                self,
                "Đăng nhập Discord",
                "Đã đăng nhập xong. Từ giờ ứng dụng có thể tự chạy /mochuong khi bị "
                "giới hạn.",
            )
        )
        self._login_worker.failed.connect(
            lambda msg: QMessageBox.warning(self, "Đăng nhập Discord", msg)
        )
        self._login_worker.start()
        QMessageBox.information(
            self,
            "Đăng nhập Discord",
            "Một cửa sổ trình duyệt riêng sẽ mở ra. Đăng nhập tài khoản Discord phụ "
            "và mở tới server có kênh #mở-khoá, rồi đóng lại.",
        )

    def _refresh_youtube_status(self) -> None:
        """Show whether a channel is currently connected, so a wrong one is noticeable."""
        from noveltrans.youtube_upload import profile_dir

        connected = profile_dir().is_dir()
        self.youtube_status.setText(
            "✅ Đã kết nối một kênh" if connected else "⬜ Chưa đăng nhập kênh nào"
        )

    def _youtube_login(self, *, switch: bool = False) -> None:
        """Open the Google login window for the channel that will publish.

        `switch` drops the saved session first, which is the only way to change channel:
        with a valid session Studio loads straight through and the window closes before
        the user can pick anything.
        """
        if switch:
            confirm = QMessageBox.question(
                self,
                "Đổi kênh YouTube",
                "Sẽ đăng xuất kênh đang kết nối và mở lại cửa sổ đăng nhập để bạn chọn "
                "tài khoản/kênh khác.\n\nCác phần đã tải lên kênh cũ vẫn còn trên "
                "YouTube — việc này chỉ đổi kênh cho những lần tải lên sau. Tiếp tục?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        def _done(channel_id: str, name: str) -> None:
            self._refresh_youtube_status()
            which = name or channel_id or "(không đọc được tên kênh)"
            QMessageBox.information(
                self,
                "Đăng nhập YouTube",
                f"Đã kết nối kênh: {which}\n\nKiểm tra đúng kênh chưa — nếu sai, bấm "
                "“Đổi kênh…” để chọn lại.",
            )

        self._yt_login_worker = YouTubeLoginWorker(self, switch=switch)
        self._yt_login_worker.done.connect(_done)
        self._yt_login_worker.failed.connect(
            lambda msg: (self._refresh_youtube_status(),
                         QMessageBox.warning(self, "Đăng nhập YouTube", msg))
        )
        self._yt_login_worker.start()
        QMessageBox.information(
            self,
            "Đăng nhập YouTube",
            "Một cửa sổ trình duyệt riêng sẽ mở ra. Đăng nhập tài khoản Google của kênh "
            "(nếu tài khoản có nhiều kênh, chọn đúng kênh muốn đăng) và chờ tới khi "
            "YouTube Studio hiện ra, rồi đóng cửa sổ lại.",
        )

    def _youtube_switch(self) -> None:
        self._youtube_login(switch=True)

    def _refresh_onedrive_status(self) -> None:
        """Show which account is connected, so a wrong one is noticeable.

        Reads the recorded account, NOT `profile_dir().is_dir()`. Playwright creates the
        profile folder before the user types anything, so a folder proves only that a
        browser was once opened — an abandoned or failed sign-in leaves one that looks
        exactly like a good one, and reading it claimed "đã kết nối" over a profile that
        had never been logged into.
        """
        account = self.config.onedrive_account
        if not account:
            self.onedrive_status.setText("⬜ Chưa đăng nhập tài khoản nào")
        elif account == "?":
            self.onedrive_status.setText("✅ Đã kết nối (không đọc được tên tài khoản)")
        else:
            self.onedrive_status.setText(f"✅ {account}")

    def _onedrive_login(self, *, switch: bool = False) -> None:
        """Open the Microsoft sign-in window for the account that will hold the backups.

        `switch` drops the saved session first, which is the only reliable way to change
        account: with a valid session OneDrive loads straight through and the window
        closes before the user can pick anything.
        """
        if switch:
            confirm = QMessageBox.question(
                self,
                "Đổi tài khoản OneDrive",
                "Sẽ đăng xuất tài khoản đang kết nối và mở lại cửa sổ đăng nhập để bạn "
                "chọn tài khoản khác.\n\nCác file đã tải lên tài khoản cũ vẫn còn trên "
                "OneDrive — việc này chỉ đổi tài khoản cho những lần sao lưu sau. "
                "Tiếp tục?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        def _done(account: str) -> None:
            # "?" rather than "": the worker only reports success once a file list was
            # actually opened, so a name we could not read still means signed in.
            self.config.onedrive_account = account or "?"
            self.config.sync()
            self._refresh_onedrive_status()
            QMessageBox.information(
                self,
                "Đăng nhập OneDrive",
                f"Đã kết nối tài khoản: {account or '(không đọc được tên tài khoản)'}"
                "\n\nKiểm tra đúng tài khoản chưa — nếu sai, bấm “Đổi tài khoản…” để "
                "chọn lại.",
            )

        def _failed(message: str) -> None:
            # Forget any previous account: the sign-in that just failed may well have
            # dropped the old session on its way (a switch always does), and claiming a
            # connection we no longer have is the failure this whole indicator exists to
            # avoid.
            self.config.onedrive_account = ""
            self.config.sync()
            self._refresh_onedrive_status()
            QMessageBox.warning(self, "Đăng nhập OneDrive", message)

        self._od_login_worker = OneDriveLoginWorker(self, switch=switch)
        self._od_login_worker.done.connect(_done)
        self._od_login_worker.failed.connect(_failed)
        self._od_login_worker.start()
        QMessageBox.information(
            self,
            "Đăng nhập OneDrive",
            "Một cửa sổ trình duyệt riêng sẽ mở ra. Đăng nhập tài khoản Microsoft giữ "
            "bản sao lưu — cửa sổ sẽ TỰ ĐÓNG khi vào được kho file, không cần đóng tay.",
        )

    def _onedrive_switch(self) -> None:
        self._onedrive_login(switch=True)

    def _start_onedrive_sync(self) -> None:
        """Pick the novels here, then close so MainWindow can run them modelessly.

        Settings is modal. Running a multi-hour sync inside it would lock the app, so the
        choice is made here and the run is handed over — `accept()` also saves whatever
        else was edited, including a destination the user just changed for this very sync.
        """
        from noveltrans.gui.onedrive_sync_dialog import OneDriveSyncPickerDialog

        if not self.config.onedrive_account:
            QMessageBox.information(
                self,
                "Chưa đăng nhập OneDrive",
                "Bấm “Đăng nhập OneDrive” trước khi đồng bộ.",
            )
            return
        # Use the field's current text, not the saved value: the user may have just
        # picked a destination and would reasonably expect this sync to honour it.
        root = self.onedrive_root_edit.text().strip() or DEFAULT_ONEDRIVE_ROOT
        picker = OneDriveSyncPickerDialog(self.config.library_dir, root, self)
        if picker.exec() != QDialog.DialogCode.Accepted or not picker.requests:
            return
        self.sync_requests = picker.requests
        self.accept()

    def _browse_onedrive_root(self) -> None:
        """Pick the destination by browsing OneDrive rather than typing it.

        Refuses before there is a session, because the alternative is opening a browser
        that lands on a sign-in page inside a dialog whose only job is to list folders.
        """
        from noveltrans.gui.onedrive_folder_dialog import pick_onedrive_folder

        if not self.config.onedrive_account:
            QMessageBox.information(
                self,
                "Chưa đăng nhập OneDrive",
                "Bấm “Đăng nhập OneDrive” trước, rồi mới chọn được thư mục.",
            )
            return
        chosen = pick_onedrive_folder(self, self.onedrive_root_edit.text().strip())
        if chosen:
            self.onedrive_root_edit.setText(chosen)

    def _browse_library(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục thư viện", self.library_edit.currentText()
        )
        if path:
            self.library_edit.setCurrentText(path)

    def accept(self) -> None:
        self.config.library_dir = self.library_edit.currentText()
        self.config.request_delay = self.delay_spin.value()
        self.config.translator = self.translator_combo.currentData()
        self.config.target_lang = self.lang_combo.currentData()
        self.config.claude_api_key = self.api_key_edit.text().strip()
        self.config.claude_model = self.model_edit.text().strip()
        self.config.cli_command = self.cli_edit.text().strip()
        self.config.claude_cli_command = self.claude_cli_edit.text().strip()
        self.config.medoctruyen_cookies = self.medoctruyen_cookie_edit.text().strip()
        self.config.tieuthuyetmang_cookies = (
            self.tieuthuyetmang_cookie_edit.text().strip()
        )
        channel_url = self.discord_channel_edit.text().strip()
        if (
            self.discord_enable.isChecked()
            and channel_url
            and not valid_channel_url(channel_url)
        ):
            QMessageBox.warning(
                self,
                "Link kênh Discord không hợp lệ",
                "Link kênh #mở-khoá phải có dạng https://discord.com/channels/…/… "
                "(chuột phải kênh → Copy Link). Tự mở khoá sẽ không chạy tới khi link "
                "đúng.",
            )
        self.config.discord_autounlock_enabled = self.discord_enable.isChecked()
        self.config.discord_channel_url = channel_url
        self.config.onedrive_root = self.onedrive_root_edit.text()
        # No live-apply call here, unlike keep_awake below: this is window state, and
        # MainWindow._open_settings re-applies it after exec() returns.
        self.config.workspace_tabs_vertical = self.vertical_tabs_check.isChecked()
        self.config.keep_awake_enabled = self.keep_awake_check.isChecked()
        keep_awake.set_enabled(self.keep_awake_check.isChecked())  # apply live
        self.config.tts_workers = self.tts_workers_spin.value()
        self.config.tts_clean_text = self.tts_clean_check.isChecked()
        self.config.tts_clean_extra_remove = self.tts_extra_remove_edit.text()
        self.config.tts_gap_seconds = self.tts_gap_spin.value()
        self.config.tts_speed = self.tts_speed_spin.value()
        self.config.tts_volume = self.tts_volume_spin.value()
        self.config.tts_temperature = self.tts_temperature_spin.value()
        self.config.tts_precision = self.tts_precision_combo.currentData()
        self.config.sync()
        super().accept()

# NovelTrans

Ứng dụng desktop (PySide6) để **tải → dịch → xuất → nghe** tiểu thuyết mạng tiếng Trung.

| Tab | Chức năng |
|---|---|
| **1. Tải truyện** | Dán URL truyện → Quét metadata (tên, tác giả, mô tả, mục lục) → Tải toàn bộ chương về máy. Sau khi dịch, tên dịch hiện kế bên tên gốc và mô tả hiển thị bản dịch (rê chuột xem bản gốc). Có progress bar, nút Dừng, và tự resume (chạy lại chỉ tải chương còn thiếu). |
| **2. Dịch** | Dịch Trung → Việt/Anh bằng **Google Translate (miễn phí)**, **Claude API**, **CLI Agent** (agy/claude) hoặc **LM Studio** (model local). Xem song song bản gốc/bản dịch. Resume + retry chương lỗi, dịch lại từng chương. Sửa tay bản dịch: nháy đúp cột "Tên dịch" để đổi tên chương, bấm vào ô bản dịch để sửa nội dung (tự lưu). |
| **3. Xuất file** | Xuất bản dịch (hoặc bản gốc) ra **DOCX**, **Markdown**, **EPUB**. Tên file mặc định lấy theo tên truyện đã dịch. |
| **4. Nghe audio** | Đọc bản dịch thành audio bằng **VieNeu-TTS** (chạy local, 10 giọng tiếng Việt). MP3/WAV từng chương, resume, tạo lại từng chương, double-click để nghe. |

## Trang web được hỗ trợ

| Site | URL mẫu |
|---|---|
| 半夏小說 (xbanxia.cc) | `https://www.xbanxia.cc/books/331303.html` |
| 爱下电子书 (ixdzs8.com) | `https://ixdzs8.com/read/620438/` |
| Mê Đọc Truyện (medoctruyen.vn) | `https://medoctruyen.vn/tu-bao-tien-bon` (nội dung tiếng Việt; cần dán cookie đăng nhập trong Cài đặt để tải chương) |

Thêm site mới = thêm 1 file adapter trong `src/noveltrans/scrapers/` (kế thừa `SiteAdapter`, đăng ký bằng `@register`).

## Cài đặt & chạy

Yêu cầu Python ≥ 3.11. Khuyến nghị dùng [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
.venv/bin/noveltrans          # mở ứng dụng
```

Hoặc với pip thường:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
noveltrans
```

## Đóng gói thành app macOS

Tạo `NovelTrans.app` (và file `.dmg` để kéo vào Applications) bằng PyInstaller:

```bash
uv pip install -e ".[tts]"   # để gói kèm engine đọc audio
make dmg                      # → dist/NovelTrans.app + dist/NovelTrans.dmg
# hoặc chỉ tạo .app:  make app
```

App **chưa được ký (unsigned)**, nên lần đầu mở macOS sẽ cảnh báo. Cách mở:
chuột phải vào app → **Open** → **Open**, hoặc chạy `xattr -cr /Applications/NovelTrans.app`.
Model TTS (~334 MB) tải về lần đầu khi dùng tab "Nghe audio" (cần mạng).
Đổi icon: sửa `packaging/make_icon.py` rồi `make icon`.

## Cấu hình

Menu **App → Cài đặt**:

- **Thư mục thư viện** — nơi lưu truyện (mặc định `~/NovelTrans`), mỗi truyện một thư mục gồm `meta.json` + `chapters.db` (SQLite) + `exports/`.
- **Giãn cách giữa các request** — mặc định 1.5s để tránh bị chặn IP.
- **Claude API key** — cần cho engine dịch Claude. ⚠️ Key được lưu **không mã hoá** trong QSettings của hệ điều hành.

## Ghi chú engine dịch

- **Google (miễn phí)**: không cần key; nội dung được cắt thành đoạn ≤1500 ký tự (giới hạn endpoint miễn phí với chữ Hán). Tốc độ ~30–60s/chương. Tên nhân vật được tự động chuyển sang **Hán-Việt** bằng bộ tự điển tích hợp (phát hiện tên lặp lại trong bản gốc, thay trước khi gửi Google).
- **Claude API**: dịch cả chương mỗi request, văn phong tốt hơn hẳn; tốn phí theo token. Model mặc định: Haiku (đổi được trong Cài đặt).
- **CLI Agent**: gọi một AI-agent CLI ở chế độ headless — ví dụ `agy -p` (Antigravity CLI, có Gemini/Claude/GPT-OSS bên trong) hoặc `claude -p` (Claude Code). Dùng subscription/quota sẵn có của CLI, **không cần API key**. Chất lượng ngang Claude API, ~30s/chương. Đổi lệnh trong Cài đặt (ví dụ `agy -p --model "Gemini 3.1 Pro (Low)"`).

## Nghe audio (VieNeu-TTS)

Tab 4 đọc bản dịch tiếng Việt thành audiobook, chạy hoàn toàn local:

```bash
uv pip install -e ".[tts]"    # cài vieneu (ONNX, không cần PyTorch)
```

- Lần chạy đầu tự tải model **~330 MB** từ HuggingFace (chờ hơi lâu, có thông báo).
- 10 giọng đọc có sẵn (Ngọc Lan, Mỹ Duyên, Gia Bảo…); tốc độ ~4× real-time trên Apple Silicon (chương ~7 phút audio tạo trong ~2 phút).
- MP3 cần `ffmpeg` (`brew install ffmpeg`); không có thì dùng WAV (~6 MB/phút).
- File nằm trong `exports/audio/` của từng truyện; đã tạo rồi thì lần sau chỉ tạo chương còn thiếu.

## Phát triển

```bash
.venv/bin/python -m pytest              # test offline (fixtures HTML có sẵn)
.venv/bin/python -m pytest -m live      # test chạm site thật (kiểm tra site đổi giao diện)
.venv/bin/python -m noveltrans.scrapers <url>   # debug một adapter với site thật
.venv/bin/ruff check src tests          # lint
```

Kiến trúc: 3 plugin ABC tách hoàn toàn khỏi GUI — `SiteAdapter` (scrapers/), `Translator` (translators/), `Exporter` (exporters/). GUI (gui/) chỉ ghép các phần qua QThread worker + Signal. Xem `changes/001-NOVEL-TRANSLATOR-GUI/001.02-INITIAL-PLAN.md` để biết chi tiết thiết kế.

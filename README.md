# NovelTrans

Ứng dụng desktop (PySide6) để **tải → dịch → xuất → nghe** tiểu thuyết mạng tiếng Trung.

| Tab | Chức năng |
|---|---|
| **1. Tải truyện** | Dán URL truyện → Quét metadata (tên, tác giả, mô tả, mục lục) → Tải toàn bộ chương về máy. Sau khi dịch, tên dịch hiện kế bên tên gốc và mô tả hiển thị bản dịch (rê chuột xem bản gốc). Có progress bar, nút Dừng, và tự resume (chạy lại chỉ tải chương còn thiếu). Hoặc bấm **✍️ Truyện tự viết** để tạo truyện của chính bạn (không cần link nguồn): thêm chương bằng tên (mỗi dòng một tên), đổi tên chương, chuột phải để xoá chương. Có nút **⏸ Tạm dừng** cạnh nút Dừng. |
| **2. Dịch** | Dịch Trung → Việt/Anh bằng **Google Translate (miễn phí)**, **Claude API**, **CLI Agent** (agy/claude) hoặc **LM Studio** (model local). Xem song song bản gốc/bản dịch. Resume + retry chương lỗi, dịch lại từng chương. Sửa tay cả hai ô: bấm vào ô **Bản gốc** để dán/sửa nội dung gốc (đây cũng là chỗ nhập nội dung cho truyện tự viết), bấm vào ô **Bản dịch** để sửa bản dịch — cả hai tự lưu khi rời ô, dòng đầu là tên chương. Nháy đúp cột "Tên dịch" để đổi tên chương dịch. **Tìm & thay thế** hàng loạt (một chương hoặc cả truyện) — xem trước số khớp rồi mới áp dụng. Có nút **⏸ Tạm dừng** cạnh nút Dừng. |
| **3. Xuất file** | Xuất bản dịch (hoặc bản gốc) ra **DOCX**, **Markdown**, **EPUB**. Tên file mặc định lấy theo tên truyện đã dịch. |
| **4. Nghe audio** | Đọc bản dịch thành audio bằng **VieNeu-TTS** (chạy local, 14 giọng tiếng Việt × 3 phong cách). MP3/WAV từng chương, resume, tạo lại từng chương, double-click để nghe. Chọn **giọng** và **phong cách** riêng, xem trước văn bản engine sẽ đọc, và tinh chỉnh **tốc độ / âm lượng / khoảng lặng / độ biểu cảm / chất lượng** trong Cài đặt. Truyện tự viết bằng tiếng Việt thì chọn nguồn **Bản gốc** để đọc thẳng nội dung (không cần dịch). Có nút **⏸ Tạm dừng** cạnh nút Dừng. |

## Chạy nền ở thanh menu

Việc tải / dịch / tạo audio / render video chạy hàng giờ, nên **đóng cửa sổ (✕) sẽ thu nhỏ
app xuống thanh menu macOS chứ không thoát** — tiến trình vẫn chạy tiếp.

Bấm biểu tượng NovelTrans trên thanh menu để mở bảng tiến trình: mỗi việc đang chạy là một
dòng có tên truyện, thanh tiến trình, số chương, và nút **⏸ Tạm dừng / ▶ Tiếp tục**. Cuối
bảng có **Mở cửa sổ** và **Thoát**. Nút ⏸ cũng nằm sẵn cạnh nút **Dừng** trong từng tab, để
tạm dừng mà không cần đóng cửa sổ.

**Tạm dừng = dừng lại sau khi xong mục đang chạy**, không bỏ dở giữa chừng. Không có file
nào bị ghi dở và chạy tiếp thì tiếp đúng chỗ cũ — đổi lại, nó không tức thì:

| Đang làm | Tạm dừng có hiệu lực sau |
|---|---|
| Tải / dịch chương | gần như ngay (xong chương hiện tại) |
| Tạo audio | khoảng một phút (xong chương đang đọc) |
| Ghép audio / render video | khi xong **file** đang ffmpeg — có thể vài chục phút |
| Tải lên YouTube | xong phần đang tải; cửa sổ Chrome và phiên đăng nhập Google **vẫn mở** cho tới khi chạy tiếp |

Khi thu nhỏ xuống thanh menu, **icon dưới Dock cũng biến mất** — app chỉ còn một biểu tượng
nhỏ trên thanh menu. Mở lại cửa sổ (**Mở cửa sổ**) thì icon Dock hiện lại.

Đổi lại, trong lúc đang ẩn:

- **Bấm biểu tượng trên thanh menu là cách duy nhất để mở lại cửa sổ** (không còn icon Dock
  để bấm).
- **⌘Q không hoạt động** — lúc này app không giữ thanh menu ứng dụng nên phím tắt không tới
  được. Dùng **Thoát** trong bảng tiến trình.
- Báo "đã xong" dựa vào thông báo của macOS (không còn icon Dock để hiện chấm đỏ).

Chỉ **Thoát** mới thật sự đóng app — lúc đó mọi tiến trình đang chạy sẽ bị huỷ. Khi cửa sổ
đang mở thì ⌘Q vẫn thoát như bình thường. Máy nào không có thanh menu (system tray) thì ✕
vẫn thoát như cũ và icon Dock không bị đụng tới.

## Trang web được hỗ trợ

| Site | URL mẫu |
|---|---|
| 半夏小說 (xbanxia.cc) | `https://www.xbanxia.cc/books/331303.html` |
| 爱下电子书 (ixdzs8.com) | `https://ixdzs8.com/read/620438/` |
| 69书吧 (69shuba.com) | `https://www.69shuba.com/book/59024/` (có kiểm tra Cloudflare — app tự mở trình duyệt để vượt qua; cần Google Chrome hoặc `playwright install chromium`) |
| Mê Đọc Truyện (medoctruyen.vn) | `https://medoctruyen.vn/tu-bao-tien-bon` (nội dung tiếng Việt; cần dán cookie đăng nhập trong Cài đặt để tải chương) |
| Tiểu Thuyết Mạng (tieuthuyetmang.com) | `https://tieuthuyetmang.com/truyen/<slug>` (nội dung tiếng Việt; **phần lớn chương là trả phí** — cần dán cookie đăng nhập trong Cài đặt, và chỉ tải được những chương tài khoản của bạn đã mở khoá) |

Thêm site mới = thêm 1 file adapter trong `src/noveltrans/scrapers/` (kế thừa `SiteAdapter`, đăng ký bằng `@register`).

**Không có site cũng được:** truyện do bạn tự viết thì bấm **✍️ Truyện tự viết** ở tab 1 — không cần link nguồn, tự thêm chương và tự nhập nội dung.

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

- Lần chạy đầu tự tải model **~330 MB** (build v3-Turbo) từ HuggingFace (chờ hơi lâu, có thông báo).
- 14 giọng đọc có sẵn (Ngọc Linh, Minh Đức, Phạm Tuyên, Thái Sơn…); tốc độ ~4× real-time trên Apple Silicon (chương ~7 phút audio tạo trong ~2 phút).
- **Giọng** (người đọc) và **phong cách** (Tự nhiên / Kể chuyện / Tin tức) chọn riêng — kết hợp giọng bất kỳ với phong cách bất kỳ.
- **Làm sạch ký tự đặc biệt** trước khi đọc (emoji, ký hiệu, chữ Hán sót, markdown) để audio mượt hơn — giữ nguyên tiếng Việt và dấu câu; có ô "bỏ thêm ký tự" tuỳ chọn. Nút **Xem trước văn bản** cho thấy đúng những gì engine sẽ đọc.
- **Tinh chỉnh trong Cài đặt**: khoảng lặng giữa đoạn, tốc độ đọc (cần ffmpeg), âm lượng, độ biểu cảm (temperature), và chất lượng model (int8 nhanh / fp32 chất lượng cao hơn). Mặc định giữ nguyên như cũ.
- MP3 và **đổi tốc độ** cần `ffmpeg` (`brew install ffmpeg`); không có thì dùng WAV (~6 MB/phút).
- Chạy nhiều luồng song song được (mỗi luồng ~334 MB RAM) để tạo audio nhanh hơn.
- File nằm trong `exports/audio/` của từng truyện; đã tạo rồi thì lần sau chỉ tạo chương còn thiếu.

## Phát triển

```bash
.venv/bin/python -m pytest              # test offline (fixtures HTML có sẵn)
.venv/bin/python -m pytest -m live      # test chạm site thật (kiểm tra site đổi giao diện)
.venv/bin/python -m noveltrans.scrapers <url>   # debug một adapter với site thật
.venv/bin/ruff check src tests          # lint
```

Kiến trúc: 3 plugin ABC tách hoàn toàn khỏi GUI — `SiteAdapter` (scrapers/), `Translator` (translators/), `Exporter` (exporters/). GUI (gui/) chỉ ghép các phần qua QThread worker + Signal. Xem `changes/001-NOVEL-TRANSLATOR-GUI/001.02-INITIAL-PLAN.md` để biết chi tiết thiết kế.

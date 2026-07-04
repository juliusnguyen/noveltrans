# NovelTrans

Ứng dụng desktop (PySide6) để **tải → dịch → xuất** tiểu thuyết mạng tiếng Trung.

| Tab | Chức năng |
|---|---|
| **1. Tải truyện** | Dán URL truyện → Quét metadata (tên, tác giả, mô tả, mục lục) → Tải toàn bộ chương về máy. Có progress bar, nút Dừng, và tự resume (chạy lại chỉ tải chương còn thiếu). |
| **2. Dịch** | Dịch Trung → Việt/Anh bằng **Google Translate (miễn phí)** hoặc **Claude API** (chất lượng cao, cần API key). Xem song song bản gốc/bản dịch. Resume + retry chương lỗi. |
| **3. Xuất file** | Xuất bản dịch (hoặc bản gốc) ra **DOCX**, **Markdown**, **EPUB**. |

## Trang web được hỗ trợ

| Site | URL mẫu |
|---|---|
| 半夏小說 (xbanxia.cc) | `https://www.xbanxia.cc/books/331303.html` |
| 爱下电子书 (ixdzs8.com) | `https://ixdzs8.com/read/620438/` |

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

## Cấu hình

Menu **App → Cài đặt**:

- **Thư mục thư viện** — nơi lưu truyện (mặc định `~/NovelTrans`), mỗi truyện một thư mục gồm `meta.json` + `chapters.db` (SQLite) + `exports/`.
- **Giãn cách giữa các request** — mặc định 1.5s để tránh bị chặn IP.
- **Claude API key** — cần cho engine dịch Claude. ⚠️ Key được lưu **không mã hoá** trong QSettings của hệ điều hành.

## Ghi chú engine dịch

- **Google (miễn phí)**: không cần key; nội dung được cắt thành đoạn ≤1500 ký tự (giới hạn endpoint miễn phí với chữ Hán). Tốc độ ~30–60s/chương. Tên nhân vật được tự động chuyển sang **Hán-Việt** bằng bộ tự điển tích hợp (phát hiện tên lặp lại trong bản gốc, thay trước khi gửi Google).
- **Claude API**: dịch cả chương mỗi request, văn phong tốt hơn hẳn; tốn phí theo token. Model mặc định: Haiku (đổi được trong Cài đặt).
- **CLI Agent**: gọi một AI-agent CLI ở chế độ headless — ví dụ `agy -p` (Antigravity CLI, có Gemini/Claude/GPT-OSS bên trong) hoặc `claude -p` (Claude Code). Dùng subscription/quota sẵn có của CLI, **không cần API key**. Chất lượng ngang Claude API, ~30s/chương. Đổi lệnh trong Cài đặt (ví dụ `agy -p --model "Gemini 3.1 Pro (Low)"`).

## Phát triển

```bash
.venv/bin/python -m pytest              # test offline (fixtures HTML có sẵn)
.venv/bin/python -m pytest -m live      # test chạm site thật (kiểm tra site đổi giao diện)
.venv/bin/python -m noveltrans.scrapers <url>   # debug một adapter với site thật
.venv/bin/ruff check src tests          # lint
```

Kiến trúc: 3 plugin ABC tách hoàn toàn khỏi GUI — `SiteAdapter` (scrapers/), `Translator` (translators/), `Exporter` (exporters/). GUI (gui/) chỉ ghép các phần qua QThread worker + Signal. Xem `changes/001-NOVEL-TRANSLATOR-GUI/001.02-INITIAL-PLAN.md` để biết chi tiết thiết kế.

# NovelTrans

Ứng dụng desktop (PySide6) để **tải → dịch → xuất → nghe → làm video** tiểu thuyết mạng tiếng Trung.

| Tab | Chức năng |
|---|---|
| **1. Tải truyện** | Dán URL truyện → Quét metadata (tên, tác giả, mô tả, mục lục) → Tải toàn bộ chương về máy. Sau khi dịch, tên dịch hiện kế bên tên gốc và mô tả hiển thị bản dịch (rê chuột xem bản gốc). Ô **Truyện gần đây** (có ở cả 5 tab) cũng hiện `tên gốc  —  tên dịch — trang nguồn`, nên nhận ra truyện mà không cần nhớ tên Hán. Có progress bar, nút Dừng, và tự resume (chạy lại chỉ tải chương còn thiếu). Hoặc bấm **✍️ Truyện tự viết** để tạo truyện của chính bạn (không cần link nguồn): thêm chương bằng tên (mỗi dòng một tên), đổi tên chương, chuột phải để xoá chương. Có nút **⏸ Tạm dừng** cạnh nút Dừng. |
| **2. Dịch** | Dịch Trung → Việt/Anh bằng **Google Translate (miễn phí)**, **Claude API**, **CLI Agent** (agy/claude) hoặc **LM Studio** (model local). Xem song song bản gốc/bản dịch. Resume + retry chương lỗi, dịch lại từng chương. Sửa tay cả hai ô: bấm vào ô **Bản gốc** để dán/sửa nội dung gốc (đây cũng là chỗ nhập nội dung cho truyện tự viết), bấm vào ô **Bản dịch** để sửa bản dịch — cả hai tự lưu khi rời ô, dòng đầu là tên chương. Nháy đúp cột "Tên dịch" để đổi tên chương dịch. **Tìm & thay thế** hàng loạt (một chương hoặc cả truyện) — xem trước số khớp rồi mới áp dụng; trong bảng kết quả, **nháy đúp một chương để mở chương đó và nhảy tới chỗ khớp** (nháy đúp tiếp để sang chỗ khớp kế trong cùng chương) rồi sửa tay ngay, cửa sổ tìm kiếm vẫn mở. Bản dịch mới **tự bỏ dòng quảng cáo trang nguồn** (kiểu `… xin truy cập sto9🍀.com`, kể cả khi tên miền bị chèn emoji để né bộ lọc) — chương đã dịch trước đó không đổi, dùng **Tìm & thay thế** nếu muốn dọn. **👤 Tên nhân vật**: danh sách tên riêng của truyện — app tự dò tên hay lặp lại và thay sẵn bằng âm Hán-Việt **trước khi** gửi cho AI, nên cùng một nhân vật được viết giống nhau ở mọi chương (trước đây chỉ Google mới có, các engine AI thì mỗi chương tự đoán lại). Bảng cho sửa cách viết (bảng máy tra có thể khác cách gọi quen thuộc — ví dụ `Nịnh` với `Ninh`), bỏ tick tên dò nhầm, và **＋ Thêm tên** cho tên máy không tìm ra; sửa xong máy hỏi có sửa luôn các chương đã dịch không. Lần dò sau **không ghi đè** cách viết bạn đã sửa. **✍️ Viết lại văn phong**: dùng AI sửa bản dịch kiểu "convert" (dịch từng chữ, giữ trật tự từ tiếng Trung) thành tiếng Việt xuôi tai — xem bên dưới. Có nút **⏸ Tạm dừng** cạnh nút Dừng. |
| **3. Xuất file** | Xuất bản dịch (hoặc bản gốc) ra **DOCX**, **Markdown**, **EPUB**. Tên file mặc định lấy theo tên truyện đã dịch. Cũng là nơi **sao lưu cả truyện lên OneDrive** (xem bên dưới). |
| **4. Nghe audio** | Đọc bản dịch thành audio bằng **VieNeu-TTS** (chạy local, 20 giọng tiếng Việt). MP3/WAV từng chương, resume, tạo lại từng chương, double-click để nghe. Chọn **giọng** (mỗi giọng mang sẵn phong cách tự nhiên / kể chuyện / tin tức), xem trước văn bản engine sẽ đọc, và tinh chỉnh **tốc độ / âm lượng / khoảng lặng / độ biểu cảm / chất lượng** trong Cài đặt. Truyện tự viết bằng tiếng Việt thì chọn nguồn **Bản gốc** để đọc thẳng nội dung (không cần dịch). Có nút **⏸ Tạm dừng** cạnh nút Dừng. |
| **5. Video** | Ghép audio các chương thành **video kiểu trình phát nhạc** (ảnh nền + cột sóng + tên chương) để đăng YouTube. Chọn phạm vi **Toàn bộ / khoảng chương / theo lô** (ví dụ 20 chương một video), chất lượng **1080p hoặc 720p** (có bản “không đĩa xoay” render nhanh hơn nhiều), phông chữ, ảnh nền, màu nền, và **Xem trước** trước khi render. Tạo **phụ đề `.srt`** (kể cả cho audio cũ chưa có mốc thời gian) với tuỳ chọn **chèn phụ đề cố định** vào video; **ảnh bìa (thumbnail)** có cửa sổ chỉnh tay (kéo vị trí tên truyện / PHẦN N, đổi phông, tagline) và tạo lại hàng loạt mà không phải render lại video; **tags YouTube** sinh bằng LLM như tab 2. **Mô tả** của mỗi phần luôn nằm dưới giới hạn **5000 ký tự** của YouTube — lô quá lớn thì mục lục chương bị cắt bớt (có cảnh báo ⚠️ ngay trên bảng phần, đổi số chương/video là thấy liền), và trong **Chi tiết phần** có nút **Shorten by AI** rút gọn tên từng chương (`Chương 1` → `C.1`, bỏ tên truyện/tác giả/dòng “Tạo bởi”) để nhét vừa nhiều chương hơn — rút gọn xong mà vẫn còn chỗ thì tên truyện/tác giả/“Tạo bởi” được thêm lại, nhưng chỉ khi không phải bỏ bớt chương nào. Đổi tên chương sau khi đã render thì mô tả của phần đó **tự cập nhật** lần sau mở truyện. Bảng **danh sách phần** hiện phần nào đã tạo / đã tải lên, tạo tiếp phần còn thiếu, tách hoặc gộp 2 phần liền kề, và tự đánh dấu trạng thái. **⬆️ Tải lên YouTube** chạy tự động qua một cửa sổ Chrome riêng (đăng nhập một lần trong Cài đặt): chọn chế độ hiển thị hoặc **hẹn giờ đăng** cách nhau N ngày, thêm vào **danh sách phát**, cập nhật ảnh bìa và tải phụ đề lên cho video đã đăng. Cần `ffmpeg`. Có nút **⏸ Tạm dừng** cạnh nút Dừng. |

## Chạy nền ở thanh menu

Việc tải / dịch / tạo audio / render video chạy hàng giờ, nên **đóng cửa sổ (✕) sẽ thu nhỏ
app xuống thanh menu macOS (hoặc khay hệ thống Windows) chứ không thoát** — tiến trình vẫn
chạy tiếp.

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
| Sao lưu OneDrive | xong **đợt file** đang gửi (tối đa 20 file / 4 GB); cửa sổ Chrome **vẫn mở** cho tới khi chạy tiếp |

Khi thu nhỏ xuống thanh menu, **icon dưới Dock cũng biến mất** — app chỉ còn một biểu tượng
nhỏ trên thanh menu. Mở lại cửa sổ (**Mở cửa sổ**) thì icon Dock hiện lại. (Chỉ macOS có
Dock — trên Windows, cửa sổ chỉ đơn giản ẩn xuống khay hệ thống, icon taskbar không có gì
để ẩn/hiện.)

Đổi lại, trong lúc đang ẩn:

- **Bấm biểu tượng trên thanh menu / khay hệ thống là cách duy nhất để mở lại cửa sổ**
  (macOS: không còn icon Dock để bấm).
- **⌘Q không hoạt động** trên macOS lúc này — app không giữ thanh menu ứng dụng nên phím
  tắt không tới được. Dùng **Thoát** trong bảng tiến trình. (Không áp dụng cho Windows.)
- Báo "đã xong" dựa vào thông báo của hệ điều hành (macOS: không còn icon Dock để hiện
  chấm đỏ; Windows: thông báo khay hệ thống bình thường).

Chỉ **Thoát** mới thật sự đóng app — lúc đó mọi tiến trình đang chạy sẽ bị huỷ. Khi cửa sổ
đang mở thì ⌘Q (macOS) hoặc nút ✕ vẫn thoát/ẩn như bình thường. Máy nào không có thanh
menu/khay hệ thống thì ✕ vẫn thoát như cũ và icon Dock (macOS) không bị đụng tới.

## Trang web được hỗ trợ

Nguồn tiếng Trung (tải về rồi dịch ở tab 2):

| Site | URL mẫu |
|---|---|
| 半夏小說 (xbanxia.cc) | `https://www.xbanxia.cc/books/331303.html` (nhận cả `xbanxia.com`) |
| 爱下电子书 (ixdzs8.com) | `https://ixdzs8.com/read/620438/` (nhận cả `ixdzs8.tw` và các biến thể số miền, ví dụ `ixdzs.com`) |
| 69书吧 (69shuba.com) | `https://www.69shuba.com/book/59024/` (có kiểm tra Cloudflare — app tự mở trình duyệt để vượt qua; cần Google Chrome hoặc `playwright install chromium`; nhận cả `69shuba.cx`) |
| QQ阅读 (book.qq.com) | `https://book.qq.com/book-detail/58625737` (nhận cả link chương `book-read/<id>/<số>`) — **truyện trên QQ thường chỉ miễn phí một phần**: ví dụ truyện trên có 226 chương thì 77 chương đọc miễn phí (65 chương đầu liền mạch), **149 chương còn lại là chương trả phí**. App báo lỗi "chương trả phí" cho những chương đó thay vì tải — đây là chuyện bình thường, không phải lỗi app. Không cần đăng nhập; NovelTrans không vượt tường phí của QQ. Số chương miễn phí hiện ngay khi quét xong, trước lúc bấm tải. |
| 思兔閱讀 (sto9.com) | `https://sto9.com/book/13908/index.html` (nhận cả `book/<id>.html` và link chương `txt/<id>/<cid>.html` — dán kiểu nào cũng ra cùng một truyện). Không cần đăng nhập hay cookie. Nội dung là **tiếng Trung phồn thể**, tải xong dịch ở tab 2 như bình thường. Trang mục lục của site chỉ hiện ~35 chương đầu/cuối, app tự lấy danh sách đầy đủ từ máy chủ; nếu lấy hụt thì app **báo lỗi chứ không lưu danh sách thiếu** — gặp lỗi đó thì quét lại sau. |
| 台灣小說網 (twkan.com) | `https://twkan.com/book/114283.html` (nhận cả `book/<id>/index.html` và link chương `txt/<id>/<cid>` — dán kiểu nào cũng ra cùng một truyện). **Có kiểm tra Cloudflare** — app tự mở trình duyệt để vượt qua, cần Google Chrome hoặc `playwright install chromium`; **giữ cửa sổ đó mở tới khi tải xong** và **không tải song song nhiều luồng**. Khi tải nhiều chương, thỉnh thoảng app dừng ~3 giây để vượt kiểm tra rồi tự chạy tiếp — đó là bình thường, không phải treo. Không cần đăng nhập hay cookie. Nội dung là **tiếng Trung phồn thể**, tải xong dịch ở tab 2 như bình thường. Trang mục lục của site chỉ hiện ~35 chương đầu/cuối, app tự lấy danh sách đầy đủ từ máy chủ; nếu lấy hụt thì app **báo lỗi chứ không lưu danh sách thiếu** — gặp lỗi đó thì quét lại sau. |
| 提莫小說 (timotxt.com) | `https://www.timotxt.com/2608569069/` (nhận cả link mục lục `<id>/dir` và link chương `<id>/<n>.html` — dán kiểu nào cũng ra cùng một truyện). Không cần đăng nhập hay cookie. Nội dung là **tiếng Trung phồn thể**, tải xong dịch ở tab 2 như bình thường. Trang chủ chỉ hiện 12 chương mới nhất (và xếp ngược), app tự lấy danh sách đầy đủ từ trang `/dir`; nếu danh sách không liền mạch từ chương 1 thì app **báo lỗi chứ không lưu danh sách thiếu** — gặp lỗi đó thì quét lại sau. Site này còn **thay ngẫu nhiên một số chữ Hán bằng ký tự Hàn trông na ná** (mỗi lần tải một kiểu khác) để chống sao chép — app tự giải mã khi tải, và **nếu còn sót chữ nào thì tự tải lại chương đó** rồi ghép hai lần tải để lấy đủ (mỗi lần site xáo trộn chỗ khác nhau). Chương tải trước bản cập nhật này có thể còn sót ký tự lỗi — bấm **Sửa chương lỗi ký tự** ở tab 1 để tìm và tải lại chúng (bản dịch và audio của riêng những chương đó sẽ bị xoá vì được tạo từ nội dung lỗi, cần dịch/đọc lại). |

Nguồn đã là tiếng Việt (tải xong đọc/nghe được ngay, không cần qua tab 2):

| Site | URL mẫu |
|---|---|
| Mê Đọc Truyện (medoctruyen.vn) | `https://medoctruyen.vn/tu-bao-tien-bon` — cần dán cookie đăng nhập trong Cài đặt để tải chương |
| Tiểu Thuyết Mạng (tieuthuyetmang.com) | `https://tieuthuyetmang.com/truyen/<slug>` — **phần lớn chương là trả phí**: cần dán cookie đăng nhập trong Cài đặt, và chỉ tải được những chương tài khoản của bạn đã mở khoá. Đây cũng là site duy nhất **có sẵn audio do trang đọc**: ở tab 4 bấm **⬇️ Tải audio từ nguồn** để tải thẳng bản đọc của site thay vì tạo bằng TTS, rồi chọn giọng **Audio từ nguồn** ở tab 4 / tab video để ghép và render. Mục nào tài khoản chưa mở khoá thì app báo và bỏ qua mục đó |
| Gia Tộc Vương Tài (giatocvuongtai.com) | `https://giatocvuongtai.com/stories/<slug>` — đọc qua JSON API công khai của site, **không cần đăng nhập hay cookie** |
| Web Truyện Dịch (webtruyendich.com) | `https://webtruyendich.com/truyen/dong-kinh-y-do` — bản dịch AI do chính trang tạo ra: app mở trình duyệt (Cloudflare), tự chọn model Gemini "Memory / No Apikey" mà trang đang có rồi bấm **Dịch lại**, và lưu thẳng kết quả làm bản dịch. Cần Google Chrome hoặc `playwright install chromium`. **Không tải song song nhiều luồng** — site bị giới hạn Cloudflare + quota AI |

Truyện lấy từ nhóm thứ hai đã có sẵn bản dịch tiếng Việt nên tab **2. Dịch** dùng để sửa
tay, để **✍️ Viết lại văn phong** (những bản dịch này hay là bản "convert" đọc trúc trắc),
hoặc dịch lại nếu muốn; riêng webtruyendich thì bản dịch được ghi luôn vào ô **Bản dịch**
khi tải, không chạy engine dịch của NovelTrans.

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

**Windows** (PowerShell), dùng [uv](https://docs.astral.sh/uv/):

```powershell
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
.venv\Scripts\noveltrans.exe    # mở ứng dụng
```

Có thể thay các bước trên bằng `.\make.ps1 run` (tự cài venv lần đầu nếu chưa có) — xem
`## Phát triển` để biết các target khác. Không cần cài GNU `make`, script này là thuần
PowerShell.

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
Đổi icon: thay ảnh nguồn trong `design/` rồi chạy `make icon`. Lệnh này dựng lại tất cả
từ hai file: `design/logo-icon.png` → icon app (`packaging/NovelTrans.png`, `.icns`,
`.ico` và `src/noveltrans/gui/assets/app-icon.png`), `design/bar-icon.png` → biểu tượng
thanh menu (`src/noveltrans/gui/assets/tray-glyph.png`).

## Đóng gói thành app Windows

Tạo `NovelTrans.exe` (thư mục portable, không cần cài đặt) bằng PyInstaller:

```powershell
uv pip install -e ".[tts]"        # để gói kèm engine đọc audio
.\make.ps1 zip                     # → dist/NovelTrans-windows.zip
# hoặc chỉ tạo thư mục app, không nén: .\make.ps1 app  → dist/NovelTrans/NovelTrans.exe
```

App **chưa được ký (unsigned)**, nên Windows SmartScreen sẽ cảnh báo lần đầu mở. Cách mở:
**More info** → **Run anyway**.

Build kèm sẵn `ffmpeg.exe`/`ffprobe.exe` nếu bạn đặt chúng vào `packaging/ffmpeg/win/`
trước khi build (không bắt buộc — thiếu thì vẫn build và chạy được, chỉ là tab "Nghe audio"
sẽ giới hạn ở WAV và không đổi được tốc độ đọc, giống hệt máy Mac chưa cài `ffmpeg`). Tải
bản **essentials** (không phải "full", để dung lượng gói cài nhỏ hơn) từ
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/), giải nén rồi copy `ffmpeg.exe` và
`ffprobe.exe` từ thư mục `bin/` vào `packaging/ffmpeg/win/`. Các binary này không được
commit vào git (xem `.gitignore`).

Model TTS (~330 MB) tải về lần đầu khi dùng tab "Nghe audio" (cần mạng), giống macOS.
Icon dùng chung nguồn trong `design/`. `.\make.ps1 icon` dựng lại được mọi thứ trừ
`packaging/NovelTrans.icns` — file đó cần `sips`/`iconutil` (chỉ có trên macOS), nên
phải chạy `make icon` trên macOS rồi commit kết quả.

## Cấu hình

Menu **App → Cài đặt**:

- **Thư mục thư viện** — nơi lưu truyện (mặc định `~/NovelTrans`), mỗi truyện một thư mục gồm `meta.json` + `chapters.db` (SQLite) + `exports/`.
- **Giãn cách giữa các request** — mặc định 1.5s để tránh bị chặn IP.
- **Claude API key** — cần cho engine dịch Claude. ⚠️ Key được lưu **không mã hoá** trong QSettings của hệ điều hành.

## Ghi chú engine dịch

- **Google (miễn phí)**: không cần key; nội dung được cắt thành đoạn ≤1500 ký tự (giới hạn endpoint miễn phí với chữ Hán). Tốc độ ~30–60s/chương. Tên nhân vật được tự động chuyển sang **Hán-Việt** bằng bộ tự điển tích hợp (phát hiện tên lặp lại trong bản gốc, thay trước khi gửi Google).
- **Claude API**: dịch cả chương mỗi request, văn phong tốt hơn hẳn; tốn phí theo token. Model mặc định: Haiku (đổi được trong Cài đặt).
- **CLI Agent**: gọi một AI-agent CLI ở chế độ headless — ví dụ `agy -p` (Antigravity CLI, có Gemini/Claude/GPT-OSS bên trong) hoặc `claude -p` (Claude Code). Dùng subscription/quota sẵn có của CLI, **không cần API key**. Chất lượng ngang Claude API, ~30s/chương. Đổi lệnh trong Cài đặt (ví dụ `agy -p --model "Gemini 3.1 Pro (Low)"`).
- **Khi engine từ chối một chương**: Google có bộ lọc nội dung riêng và thỉnh thoảng từ chối dịch một chương truyện (kể cả chương không có gì nặng — bộ lọc khá thô). App báo lỗi rõ ràng ở chương đó, **các chương còn lại vẫn dịch bình thường**, và **không bao giờ lưu lời từ chối làm bản dịch**. Dịch lại bằng cùng engine sẽ bị từ chối y hệt — cách xử lý là đổi engine (Claude, hoặc LM Studio chạy model ngay trên máy) rồi **chọn riêng chương đó dịch lại**. Lưu ý: nút "dịch các chương" chỉ lấy chương *chưa có* bản dịch, nên chương lỗi kiểu này phải chọn đích danh.

## Viết lại văn phong

Nhiều bản dịch — nhất là truyện lấy từ các trang tiếng Việt, hoặc dịch bằng Google — là
kiểu **"convert"**: máy dịch từng chữ và giữ nguyên trật tự từ của tiếng Trung, nên đọc
rất trúc trắc. Nút **✍️ Viết lại văn phong** ở tab 2 dùng AI sắp xếp lại cho xuôi tai:

> **Gốc:** Hắn nội tâm tràn ngập một loại không cách nào nói nói tư vị.
>
> **Viết lại:** Nội tâm hắn tràn ngập một loại tư vị khó nói.

Đây **không phải dịch lại** — tiếng Việt vào, tiếng Việt ra. Chỉ đổi cách hành văn:

- **Giữ nguyên tên riêng Hán-Việt** (Phó Thanh Từ, Giang Dư…) và **xưng hô** (hắn, nàng,
  y, thị, ngươi) — đổi xưng hô là hỏng cả giọng văn tiên hiệp.
- **Không tóm tắt, không thêm bớt.** Số đoạn văn phải khớp chính xác với bản gốc.
- Chỉ dành cho **bản dịch tiếng Việt**; engine phải là **CLI Agent, Claude API hoặc
  LM Studio** (Google chỉ dịch được, không viết lại được nên không xuất hiện trong danh
  sách). Chọn engine/model riêng cho việc viết lại, độc lập với engine dịch.

**Chương nào viết lại hỏng thì giữ nguyên bản dịch cũ.** Nếu AI trả về bản thiếu đoạn,
bị tóm tắt, hay đổi tên nhân vật, app thử lại tối đa 3 lần rồi bỏ qua chương đó và ghi lý
do vào cột **Lỗi** — bản dịch đang có không bao giờ bị ghi đè bằng một bản kém hơn.

Trước khi chạy cả truyện, bấm **👁 Xem thử 1 chương**: nó chạy đúng quy trình sẽ dùng cho
cả truyện nhưng **không ghi gì**, cho xem trước/sau cạnh nhau. Đáng làm — một truyện 1000
chương là hàng giờ quota hoặc tiền API.

Chương đã viết lại có dấu **✍️** ở cột "Dịch bằng". Bản dịch trước khi viết lại được giữ
lại, nên **↩︎ Hoàn tác viết lại** trả về được — cho cả truyện (trong hộp thoại) hoặc từng
chương (chuột phải vào bảng). Chuột phải cũng là chỗ viết lại riêng vài chương đang chọn.

Lưu ý:

- **Hoàn tác trả về bản dịch tại thời điểm viết lại** — sửa tay hay tìm & thay thế bạn
  làm *sau* lần viết lại sẽ mất.
- **Chương đã tạo audio thì file audio cũ vẫn giữ nguyên**, không tự khớp với bản viết
  lại. Muốn khớp phải tạo lại audio cho những chương đó. Hộp thoại có cảnh báo kèm số
  chương bị ảnh hưởng.
- Viết lại lần nữa là viết lại **trên bản đã viết lại**, mỗi lượt trôi xa bản gốc thêm
  một chút. Muốn đổi engine thì nên Hoàn tác trước rồi viết lại.
- Chạy được **resume** (lần sau chỉ làm chương còn thiếu) và **⏸ Tạm dừng** như mọi tác
  vụ dài khác.

## Nghe audio (VieNeu-TTS)

Tab 4 đọc bản dịch tiếng Việt thành audiobook, chạy hoàn toàn local:

```bash
uv pip install -e ".[tts]"    # cài vieneu (ONNX, không cần PyTorch)
```

- Lần chạy đầu tự tải model **~330 MB** (build v3-Turbo) từ HuggingFace (chờ hơi lâu, có thông báo).
- 20 giọng đọc có sẵn (Ngọc Linh, Minh Đức, Phạm Tuyên, Thái Sơn, Quỳnh Anh, Kim Thanh…); tốc độ ~4× real-time trên Apple Silicon (chương ~7 phút audio tạo trong ~2 phút).
- **Giọng** chọn ngay trên tab 4; mỗi giọng đã mang sẵn phong cách riêng, ghi ngay trong nhãn (ví dụ “Ngọc Linh — Nữ · Bắc · Phong cách kể chuyện”). Từ vieneu 3.3.0, phong cách nằm trong chính giọng mẫu chứ không còn là một tuỳ chọn tách rời, nên ô **Phong cách** cũ đã được bỏ — muốn giọng kể chuyện thì chọn giọng có nhãn “Phong cách đọc truyện” hoặc “kể chuyện”.
- **Làm sạch ký tự đặc biệt** trước khi đọc (emoji, ký hiệu, chữ Hán sót, markdown) để audio mượt hơn — giữ nguyên tiếng Việt và dấu câu; có ô "bỏ thêm ký tự" tuỳ chọn. Nút **Xem trước văn bản** cho thấy đúng những gì engine sẽ đọc.
- **Tinh chỉnh trong Cài đặt**: khoảng lặng giữa đoạn, tốc độ đọc (cần ffmpeg), âm lượng, độ biểu cảm (temperature), và chất lượng model (int8 nhanh / fp32 chất lượng cao hơn). Mặc định giữ nguyên như cũ.
- MP3 và **đổi tốc độ** cần `ffmpeg` (`brew install ffmpeg` trên macOS, `winget install ffmpeg`
  hoặc `scoop install ffmpeg` trên Windows); không có thì dùng WAV (~6 MB/phút).
- Chạy nhiều luồng song song được (mỗi luồng ~334 MB RAM) để tạo audio nhanh hơn.
- File nằm trong `exports/audio/` của từng truyện; đã tạo rồi thì lần sau chỉ tạo chương còn thiếu.

## Sao lưu lên OneDrive

Tab 3 có nhóm **Sao lưu OneDrive**: đẩy **toàn bộ thư mục truyện** lên OneDrive để công
sức không chỉ nằm trên một cái máy.

> ⚠️ **Mới.** Tính năng điều khiển giao diện web của OneDrive, nên khi Microsoft đổi giao
> diện là hỏng. Toàn bộ luồng đã chạy thử trên tài khoản thật — đăng nhập, tạo thư mục,
> tải file lên, và lần chạy thứ hai bỏ qua file không đổi. Nó được viết để **dừng lại và
> nói rõ hỏng ở bước nào**, không bao giờ báo thành công khống. Xem
> `changes/051-ONEDRIVE-UPLOAD/`.

```bash
uv pip install -e ".[browser]"   # cần Playwright
playwright install chromium      # bỏ qua nếu đã có Google Chrome
```

Đăng nhập một lần ở **App → Cài đặt → Sao lưu OneDrive → “Đăng nhập OneDrive”**. Phiên
được lưu trong profile trình duyệt **riêng** của ứng dụng (`~/NovelTrans/.onedrive-profile`),
tách hẳn khỏi profile YouTube và trình duyệt thường ngày.

**Đẩy gì lên:** cả thư mục truyện, giữ nguyên cấu trúc — `meta.json`, `chapters.db`, và
`exports/` với audio, video từng phần cùng mọi file đi kèm (`.title.txt`, `.txt`,
`.tags.txt`, `.jpg`, `.srt`). `chapters.db` được chụp bản sao nhất quán bằng sqlite chứ
không copy thẳng file — copy thẳng khi app đang mở sẽ ra một file **mở được nhưng thiếu
sạch những chương mới nhất**.

**Đẩy vào đâu:** một **thư mục đích duy nhất** cho cả thư viện, chọn một lần ở Cài đặt
(mặc định `/NovelTrans`, đổi được thành ví dụ `/Fox Novel`). Mỗi truyện là một thư mục con
bên trong, đặt theo tên truyện đã dịch — `/Fox Novel/<tên truyện>/`.

Đổi tên truyện, hoặc đổi thư mục đích sau này, **không** làm mất cây thư mục cũ: đường dẫn
được ghi lại từ lần đẩy đầu và những lần sau vẫn dùng nó. Truyện chưa từng đẩy thì vào chỗ
mới. Hộp thoại xác nhận nói rõ khi hai chỗ khác nhau; muốn chuyển hẳn thì bấm **Quên trạng
thái** rồi đẩy lại.

**Một chiều, và ghi đè.** Đây là *sao lưu*, không phải đồng bộ hai chiều: file trùng tên
trên OneDrive sẽ bị ghi đè bằng bản trên máy. Sửa file trên OneDrive thì lần đẩy sau sẽ
mất. Hộp thoại xác nhận nói rõ điều này trước khi chạy, kèm số file và dung lượng —
12 file / 4 GB và 3 200 file / 61 GB là hai câu chuyện rất khác nhau.

**Lần sau chỉ đẩy phần thay đổi.** Ứng dụng ghi lại đã đẩy những gì vào
`.onedrive-upload.json` trong thư mục truyện; file không đổi (cùng dung lượng, không mới
hơn) được bỏ qua. Render lại phần 7 rồi đẩy tiếp thì chỉ 3 GB đi lên, không phải 60 GB.
Muốn đẩy lại tất cả thì bấm **Quên trạng thái** — thao tác này **không xoá gì trên
OneDrive**, chỉ khiến lần sau tải lại từ đầu.

**Chạy dở thì chạy lại.** Không có chuyện file lên "một nửa": mỗi file hoặc lên đủ hoặc
chưa lên. Dừng giữa chừng, mất mạng, hay một thư mục lỗi đều không huỷ cả lượt — những
file đã lên vẫn còn và lần chạy sau bỏ qua chúng. Đổi lại, **không resume giữa một file**:
mất mạng lúc đang gửi file 3 GB thì file đó phải gửi lại từ đầu.

Tự động hoá giao diện web OneDrive không được Microsoft hỗ trợ chính thức. Đích đến là kho
lưu trữ riêng của bạn nên rủi ro thấp, nhưng vẫn nên dùng ở mức vừa phải.

## Phát triển

```bash
.venv/bin/python -m pytest              # test offline (fixtures HTML có sẵn)
.venv/bin/python -m pytest -m live      # test chạm site thật (kiểm tra site đổi giao diện)
.venv/bin/python -m noveltrans.scrapers <url>   # debug một adapter với site thật
.venv/bin/ruff check src tests          # lint
```

Trên Windows, dùng `.venv\Scripts\...` thay cho `.venv/bin/...`, hoặc dùng `make.ps1`
(tương đương `Makefile`, không cần cài GNU `make`):

```powershell
.\make.ps1 test        # test offline
.\make.ps1 test-live   # test chạm site thật
.\make.ps1 lint        # lint
.\make.ps1 app         # đóng gói dist/NovelTrans/NovelTrans.exe
.\make.ps1 zip         # đóng gói + nén dist/NovelTrans-windows.zip
.\make.ps1 clean       # xoá venv và cache
```

Khi một tính năng điều khiển trình duyệt hỏng vì site đổi giao diện, `scripts/` có sẵn
công cụ *quan sát* trước khi đoán selector:

```bash
.venv/bin/python scripts/diagnose_onedrive.py            # chỉ xem, không tạo/xoá gì
.venv/bin/python scripts/diagnose_onedrive.py --upload F # thêm: thử đẩy một file nhỏ
.venv/bin/python scripts/diagnose_subtitles.py <video_id>
```

Chúng mở đúng profile mà ứng dụng dùng, in ra những nút và hook thật sự có trên trang, rồi
báo selector nào khớp và bước nào gãy — dán nguyên output đó ra là đủ để chỉnh lại.

Kiến trúc: 3 plugin ABC tách hoàn toàn khỏi GUI — `SiteAdapter` (scrapers/), `Translator` (translators/), `Exporter` (exporters/). GUI (gui/) chỉ ghép các phần qua QThread worker + Signal. Xem `changes/001-NOVEL-TRANSLATOR-GUI/001.02-INITIAL-PLAN.md` để biết chi tiết thiết kế.

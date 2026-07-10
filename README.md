# Hermes Zalo Plugin (cá nhân)

Plugin kết nối **tài khoản Zalo cá nhân** vào [Hermes Agent](https://github.com/) qua một **sidecar Node.js** dùng thư viện [`zca-js`](https://www.npmjs.com/package/zca-js) (Zalo Web API không chính thức).

> ⚠️ **CẢNH BÁO QUAN TRỌNG**
> - Đây là **API Zalo KHÔNG chính thức**. **KHUYẾN NGHỊ DÙNG SỐ PHỤ**, đừng dùng số Zalo chính.
> - Gửi lời mời kết bạn / nhắn tin hàng loạt **có thể bị Zalo khoá tài khoản**. Dùng có hạn mức, nhỏ giọt. Bạn **tự chịu trách nhiệm**.
> - Tuân thủ điều khoản Zalo và pháp luật về quấy rối / thu thập dữ liệu cá nhân ở nơi bạn sống.

---

## Tính năng

- **Nhận / gửi tin** Zalo (DM + nhóm), gửi **ảnh / file / sticker / reaction**, "đang gõ".
- **Quét thành viên nhóm** theo link `zalo.me/g/...` (không cần tham gia, nếu nhóm không khoá xem thành viên) — **nhỏ giọt nền 24h**, dồn vào **một Google Sheet chung** (tùy chọn).
- **Phễu marketing bán tự động**: tạo lead → kết bạn → nhắn tin (người lạ / bạn bè) → CRM. Nội dung do AI sinh **khác nhau từng người**, gửi **nhỏ giọt theo hạn mức/ngày** (mặc định 30 mời + 40 tin), **tự động chấp nhận** lời mời kết bạn.
- **Thao tác lẻ**: kết bạn / nhắn 1 người theo **uid / số điện thoại / người vừa @tag / tên**; nhắn kèm **nhiều ảnh** (link hoặc ảnh bạn gửi cho bot).
- **Tra số điện thoại** → tài khoản Zalo (uid) hàng loạt.
- **File**: tạo & gửi HTML / PDF / PowerPoint / Excel vào chat.

> Bản chia sẻ này **đã loại bỏ** các tính năng gắn hạ tầng riêng (cảnh báo qua Telegram, Google Slides/Form, publish web riêng, preset thương hiệu).

---

## Yêu cầu

- **Hermes Agent** đang chạy (đây là plugin nền tảng — `kind: platform`).
- **Node.js ≥ 18** (cho sidecar).
- (Tùy chọn) Thư viện Python cho xuất Google Sheet: `google-api-python-client google-auth`.
- (Khuyến nghị) **Proxy dân cư** cùng quốc gia với số Zalo.

---

## Cài đặt

1. **Chép thư mục plugin** `hermes-zalo-plugin/` vào thư mục plugins của Hermes (ví dụ `/opt/data/plugins/zalo-personal/`).
   > Lưu ý: tên thư mục nên là `zalo-personal` (khớp tên plugin nội bộ).

2. **Cài deps cho sidecar**:
   ```bash
   cd <plugins>/zalo-personal/sidecar
   npm install
   ```

3. **Khai báo biến môi trường** (xem `.env.example`). Tối thiểu cần `ZALO_PERSONAL_OWNER_UID`.

4. **Khởi động Hermes** — plugin tự spawn sidecar. Lần đầu chưa có session → cần **đăng nhập QR**:
   ```bash
   # gọi sidecar tạo mã QR (cổng mặc định 3838)
   curl -X POST http://127.0.0.1:3838/login/qr
   # lấy ảnh QR rồi quét bằng app Zalo trên điện thoại (Cài đặt → Zalo Web)
   #   ảnh QR: http://127.0.0.1:3838/qr.png
   ```
   Sau khi quét, session được lưu vào `ZALO_PERSONAL_SESSION_DIR` và tự khôi phục các lần sau.

5. **Lấy `ZALO_PERSONAL_OWNER_UID`**: nhắn bot 1 tin từ số chính của bạn rồi xem log gateway (`from_uid=...`), hoặc dùng công cụ tra UID Zalo.

---

## Ra lệnh cho bot (tiếng Việt tự nhiên)

| Bạn nói | Bot làm |
|---|---|
| "quét nhóm này: \<link\>" | Quét thành viên nhỏ giọt → Google Sheet chung |
| "tra mấy số này trên Zalo: 09..., 03..." | SĐT → tài khoản Zalo |
| "kết bạn với người này" (kèm @tag) / "kết bạn với số 09..." | Gửi lời mời 1 người |
| "gửi lời mời kết bạn cho nhóm này 20 người" | Soạn đợt → bạn duyệt → gửi nhỏ giọt 24h |
| "nhắn tin cho các thành viên nhóm này" / "cho danh sách bạn bè" | Soạn (AI khác nhau từng người) → duyệt → gửi |
| "gửi mấy ảnh này cho khách [tag]" | Nhắn kèm nhiều ảnh |
| "tự động chấp nhận kết bạn với tất cả" | Bật auto-accept |
| "đặt hạn mức 50 lời mời/ngày" / "hôm nay chỉ 10 tin" | Đổi hạn mức |
| "báo cáo chiến dịch" / "cho xem file quản lý tổng" | Báo cáo phễu / link Sheet chung |

---

## An toàn & chống khoá tài khoản

- Dùng **số phụ**. Bật **proxy dân cư** (`ZALO_PERSONAL_PROXY`).
- Giữ **hạn mức thấp**, gửi **nhỏ giọt** (mặc định đã rải đều 24h + nghỉ ngẫu nhiên).
- Quét nhóm chỉ được khi nhóm **không bật khoá xem thành viên**; gọi quá dày sẽ bị Zalo giới hạn tạm (đợi vài phút/giờ).
- Nhắn **người lạ** rủi ro cao hơn nhắn bạn bè — cân nhắc số lượng.

---

## Xuất Google Sheet (tùy chọn)

Phễu marketing có thể đổ lead vào **Google Sheet của chính bạn**:
1. Tạo OAuth (Desktop app) trong Google Cloud, bật **Sheets API + Drive API**, lấy `google_token.json` (scope `spreadsheets` + `drive`).
2. Đặt đường dẫn vào `GOOGLE_TOKEN_PATH`.
3. Cài: `pip install google-api-python-client google-auth`.

Không cấu hình → phễu vẫn chạy, chỉ **bỏ qua** bước tạo Sheet (lead lưu nội bộ JSON).

---

## Cấu trúc

```
zalo-personal/
├── __init__.py          # entrypoint: from .adapter import register
├── adapter.py           # adapter Hermes (lifecycle, routing, tool, phễu marketing)
├── marketing.py         # logic phễu (store/quota/schedule/sheet) — test được độc lập
├── inbound_media.py     # hợp đồng media inbound (magic sniff, normalize, recent-image index)
├── landing_media_bridge.py # upload ảnh Zalo → landing server-to-server (không base64 qua LLM)
├── message_filtering.py # classifier ẩn thông báo vận hành, giữ câu trả lời thật
├── plugin.yaml          # manifest + khai báo env
├── .env.example         # mẫu biến môi trường
├── tests/               # unittest (stdlib) — chạy không cần Hermes/Zalo
└── sidecar/
    ├── server.js        # sidecar Node.js (zca-js)
    ├── media-contract.js # magic sniff + streaming download + cache-root resolver
    └── package.json     # deps Node
```

## Thông báo vận hành & khôi phục

- Plugin KHÔNG hiển thị thông báo lifecycle/context của runtime ra chat Zalo
  (busy/interrupt, "Context too large", "Context length exceeded", "Cannot
  compress further"…) — kể cả owner DM. Chi tiết vẫn có trong **log server**
  (category/counter, `chat_hash`), không log nội dung thô.
- Lỗi kỹ thuật terminal → chỉ 1 câu trấn an tiếng Việt/chat/loại lỗi trong mỗi
  cửa sổ TTL (không lặp, không đè chat khác).
- Câu trả lời thật nằm cạnh thông báo kỹ thuật **được giữ nguyên** (classifier
  bóc đúng đoạn notice, không nuốt cả tin).
- Khi model không còn trả lời được: nhắn **`/new`** để mở phiên mới (cách khôi
  phục chuẩn). Busy-ACK bubble tắt qua Hermes config (`display.busy_ack_enabled=false`)
  ở bước deploy — không đổi ngữ nghĩa queue/interrupt của core.

## Kiểm thử (tests)

Không cần login Zalo hay boot Hermes — module thuần tách riêng để test.

```bash
# Python (adapter helpers)
python3 -m unittest discover -s tests -v
python3 -m compileall -q adapter.py marketing.py inbound_media.py

# Node (sidecar media contract)
cd sidecar && node --check server.js && node --test
```

## Giấy phép / miễn trừ

Cung cấp "nguyên trạng" (as-is), **không bảo hành**. Dùng API Zalo không chính thức có rủi ro khoá tài khoản. Người dùng tự chịu mọi trách nhiệm pháp lý và hậu quả.

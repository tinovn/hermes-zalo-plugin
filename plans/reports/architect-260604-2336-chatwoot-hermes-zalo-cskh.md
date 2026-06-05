# Report: Kiến trúc tích hợp Chatwoot ↔ Hermes ↔ Zalo cho team CSKH Tino

**Type:** architect / khảo sát đấu nối
**Date:** 2026-06-04 23:36
**Scope:** Build hệ CSKH cho Tino — UI chat giống Zalo Web (= Chatwoot), AI Hermes tự trả lời + tự đặt hàng, nút mute cho support nhảy vào. Mục tiêu: **qua tuần chạy thử auto support.**

---

## 1. Quyết định đã chốt (từ user)

| Vấn đề | Chốt |
|---|---|
| UI chat | Dùng **Chatwoot** (không build từ đầu) |
| Bridge Zalo↔Chatwoot | Clone **tinovn/zca-bridge** (fork của diendh/zca-bridge) |
| AI engine | **NousResearch/hermes-agent** (Hermes Agent) |
| Cách Hermes nhận tin | **Chatwoot Agent Bot** (webhook `message_created` → Hermes trả lời qua Chatwoot API) |
| Mute logic | **Cả hai**: auto theo conversation status (human reply → bot im) + label `mute-ai` đè thủ công |
| KHÔNG dùng | `hermes-zalo-plugin` cũ (adapter.py Zalo) — bỏ qua hoàn toàn |

**Luồng cuối:**
```
Khách Zalo → zca-bridge → Chatwoot ──(Agent Bot webhook)──► Hermes plugin (chatwoot)
                              ▲                                      │
                              └────── Chatwoot API (reply) ◄─────────┘
                                          │
                          Chatwoot outgoing webhook → zca-bridge → Khách Zalo

Nếu human (support) reply/assign → conversation status đổi → Hermes KHÔNG trả lời nữa.
```

Điểm hay nhất: **3 hệ tách bạch, ghép qua HTTP/webhook, không sửa core của bên nào.**

---

## 2. Khảo sát zca-bridge (đã đọc kỹ)

Stack: Node 20 · TypeScript ESM · **Fastify** · **PostgreSQL** (riêng, tách DB Chatwoot). Production-grade.

**Đã có sẵn — KHÔNG cần build:**
- Nhắn 2 chiều Zalo ↔ Chatwoot (text, ảnh, file, audio, video, sticker, location)
- **Durable queue** (`job_queue`): lưu trước → xử lý → retry → dead-letter (`src/worker/worker.ts`, `src/store/jobQueueRepo.ts`)
- **Anti-echo 2 chiều** qua `message_map` — chống lặp khi bot/nhân viên gửi (`src/store/mappingRepo.ts`)
- Media archive bền + link token cho file lớn (`src/media/`)
- Quote reply, reaction (→ private note), undo/recall mirror (`src/handlers/events.ts`)
- Hỗ trợ cả **Zalo cá nhân (zca-js, QR login)** lẫn **Zalo OA (API chính thức)**
- Admin UI riêng (`src/admin/`, QR login, settings, logs)

**Luồng inbound:** `zca listener → onInbound() → jobs.enqueue("inbound") → worker dispatch → InboundHandler.handle() → tạo contact/conversation + relay message vào Chatwoot` (`src/main.ts:onInbound`, `src/handlers/inbound.ts`).

**Luồng outbound:** `Chatwoot gửi outgoing → webhook POST /webhooks/chatwoot/<secret> → jobs.enqueue("outbound") → worker → OutboundHandler.handle() → sessions.sendText/sendAttachment → Zalo` (`src/chatwoot/webhookServer.ts`, `src/handlers/outbound.ts`).

→ **Bridge đã lo trọn phần Zalo↔Chatwoot. Ta KHÔNG cần đụng vào nó** ngoài cấu hình. Hermes chỉ làm việc với Chatwoot, không biết gì về Zalo.

**Cảnh báo rủi ro (từ README bridge):** kênh Zalo **cá nhân** dùng zca-js (API không chính thức) → **có thể bị khoá/cấm tài khoản**. Khuyến nghị dùng số phụ, không dùng tài khoản chính. Kênh **OA** dùng API chính thức → an toàn. → Cân nhắc cho Tino: chạy thử nên dùng **số Zalo phụ** hoặc tính chuyển sang **Zalo OA**.

---

## 3. Khảo sát hermes-agent (đã đọc kỹ)

Stack: Python. Có **plugin system cho platform** — `gateway/platforms/ADDING_A_PLATFORM.md`.

**Phát hiện then chốt:** `hermes-zalo-plugin/adapter.py` của user **chính là** một plugin viết cho hermes-agent (import `gateway.platforms.base.BasePlatformAdapter`). Tức là cách "viết plugin Chatwoot vào Hermes" đã có khuôn mẫu rõ ràng.

### Plugin Path (khuyến nghị — zero core changes)
Tạo thư mục plugin với `plugin.yaml` + `adapter.py`, đăng ký qua `register(ctx)` → `ctx.register_platform(...)`. Tham khảo mẫu nhỏ gọn: `plugins/platforms/irc/`, `plugins/platforms/ntfy/`, `plugins/platforms/teams/`.

### Blueprint hoàn hảo: `gateway/platforms/webhook.py`
`WebhookAdapter` chạy **aiohttp HTTP server** nhận POST webhook → dispatch vào agent qua `handle_message(event)` → trả kết quả qua `_deliver()`. **Đây chính xác là pattern Agent Bot cần** (Chatwoot bắn webhook tới, Hermes nhận, xử lý, trả lời).

### Các method adapter cần implement (từ ADDING_A_PLATFORM.md)
- `__init__(config)` → `super().__init__(config, Platform.CHATWOOT)`
- `connect() -> bool` — start aiohttp server nghe webhook Chatwoot
- `disconnect()` — stop server
- `send(chat_id, text, ...)` — gọi **Chatwoot API** tạo outgoing message (Chatwoot sẽ tự bắn outgoing webhook → bridge → Zalo)
- `send_typing`, `send_image`, `get_chat_info`
- `build_source(...)` để tạo `SessionSource`; `handle_message(event)` để đẩy vào agent
- Filter self/echo (chống loop), redact sensitive identifiers

---

## 4. Mô hình Chatwoot Agent Bot (cốt lõi của tích hợp)

Chatwoot có sẵn khái niệm **Agent Bot**:
- Tạo Agent Bot trong Chatwoot → gán vào inbox Zalo (inbox do bridge tạo).
- Chatwoot gửi webhook `message_created` (event của khách) tới URL của bot = **Hermes plugin**.
- Conversation có vòng đời status: **`pending` / `open` / `resolved`**. Agent Bot thường xử lý khi `pending`; khi human agent reply hoặc nhận hội thoại → status chuyển → bot **handoff** (ngừng trả lời). Đây chính là **mute tự nhiên**.

**Mapping với yêu cầu của user:**
| Yêu cầu user | Hiện thực bằng Chatwoot |
|---|---|
| "UI giống Zalo Web để khách click xem lịch sử + chat" | Chatwoot agent dashboard (inbox, lịch sử, gán hội thoại, team) |
| "AI tự chat + tự đặt hàng" | Hermes Agent Bot trả lời khi conversation ở trạng thái bot |
| "Support nhảy vào, bấm mute" | Human reply/assign → status `open` → bot im (auto). + label `mute-ai` để đè thủ công |
| "Train dần cho tự động" | Ban đầu để human trả nhiều (bot mute), dần mở cho bot khi đủ tốt |

---

## 5. Thiết kế plugin Hermes-Chatwoot (đề xuất)

**Tên plugin:** `chatwoot` · **Vị trí:** `plugins/platforms/chatwoot/` trong repo hermes-agent (hoặc `~/.hermes/plugins/chatwoot/`).

**File:**
- `plugin.yaml` — metadata + `requires_env` (CHATWOOT_BASE_URL, CHATWOOT_API_ACCESS_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_BOT_TOKEN, CHATWOOT_WEBHOOK_SECRET, CHATWOOT_PORT...)
- `adapter.py` — `ChatwootAdapter(BasePlatformAdapter)` + `register(ctx)`
  - `connect()`: aiohttp server, route `POST /chatwoot/webhook`
  - `_handle_webhook()`:
    1. Verify HMAC/secret
    2. Parse `message_created`, chỉ xử lý `message_type == "incoming"` (tin khách), bỏ qua outgoing/private
    3. **Mute gate:** nếu conversation status == `open`/`resolved` (human đang xử lý) HOẶC có label `mute-ai` → bỏ qua, không gọi agent
    4. Nếu pass → `build_source(chat_id=conversation_id)` + `handle_message(event)` → agent chạy
  - `send()`: POST `Chatwoot API /conversations/{id}/messages` (message_type=outgoing) → Chatwoot tự bắn outgoing webhook → bridge → Zalo
- `__init__.py`

**chat_id mapping:** `chat_id = chatwoot conversation_id` (đơn giản, 1 conversation = 1 thread Zalo).

**Chống loop:** Khi Hermes `send()` tạo outgoing message, Chatwoot bắn `message_created` outgoing → plugin phải bỏ qua (chỉ xử lý `incoming`). Bridge cũng có anti-echo riêng. → 2 lớp chống loop.

---

## 6. Hạ tầng cần dựng (Chatwoot CHƯA có)

1. **Chatwoot self-host** (docker) — chưa có, cần dựng. Cần Postgres + Redis (Chatwoot tự lo trong compose của nó).
2. **Postgres riêng cho zca-bridge** (tách khỏi DB Chatwoot) — bridge tự migrate.
3. **zca-bridge** (docker hoặc node) — đấu vào Chatwoot.
4. **hermes-agent** + plugin chatwoot — chạy gateway.
5. Mạng/URL: bridge cần `PUBLIC_BASE_URL` reachable; Chatwoot cần reach Hermes webhook + bridge webhook.

**VPS:** user có VPS hermes-agent (103.142.27.98, RAM đã nâng 6G — từ session trước). Cần xác minh đủ RAM cho cả Chatwoot (nặng, ~2-4G) + bridge + hermes + Postgres. **6G có thể chật** nếu chạy tất cả 1 máy.

---

## 7. Các bước triển khai (phác thảo phases — chi tiết sẽ ở plan.md)

- **P1 — Hạ tầng:** dựng Chatwoot self-host, Postgres bridge, mạng/domain/TLS.
- **P2 — Bridge:** clone tinovn/zca-bridge, cấu hình `.env`, QR login số Zalo (phụ), verify Zalo↔Chatwoot 2 chiều chạy tay (chưa có AI).
- **P3 — Plugin Hermes:** viết `plugins/platforms/chatwoot/` (adapter + plugin.yaml), test nhận webhook + reply.
- **P4 — Agent Bot wiring:** tạo Agent Bot trong Chatwoot, gán inbox, set webhook URL → Hermes. Test end-to-end: khách Zalo nhắn → Hermes trả → về Zalo.
- **P5 — Mute:** logic status-based + label `mute-ai`. Test support nhảy vào → bot im.
- **P6 — Train + đặt hàng:** cấu hình prompt/skills cho Hermes (catalog sản phẩm, tone, tool đặt hàng). Chạy thử có giám sát.
- **P7 — Go-live thử nghiệm:** **qua tuần** bật auto support có người trực mute.

---

## 8. Rủi ro & câu hỏi chưa giải quyết

**Rủi ro:**
1. **Khoá tài khoản Zalo** (zca-js không chính thức) — cao nhất. → Dùng số phụ / cân nhắc OA.
2. **RAM VPS 6G** có thể không đủ cho Chatwoot + bridge + hermes + 2 Postgres. → Cần đo, có thể tách máy hoặc nâng RAM.
3. **AI tự đặt hàng** — cần tool/skill đặt hàng thật trong Hermes + guardrail (đặt nhầm đơn). Giai đoạn đầu nên cho bot **soạn nháp**, human xác nhận.
4. **Đồng bộ status mute** — cần test kỹ webhook event nào Chatwoot bắn khi human reply (có thể là `conversation_updated`/`assignee_changed`, không chỉ `message_created`).

**Câu hỏi cần user trả lời trước khi code:**
1. Chatwoot sẽ dựng trên **VPS nào**? Cùng máy hermes (103.142.27.98) hay máy riêng? Có domain + TLS chưa?
2. Số Zalo dùng chạy thử là **số phụ** hay số chính? (ảnh hưởng rủi ro khoá)
3. "Tự đặt hàng" nghĩa là gì cụ thể — ghi đơn vào đâu? (Google Sheet / hệ POS / DB nào?) Có cần human duyệt đơn không?
4. Hermes-agent trên VPS đã chạy gateway chưa, hay cần tao dựng từ đầu?
5. Có cần giữ lại marketing funnel (`marketing.py`) từ plugin cũ không, hay bỏ hẳn?

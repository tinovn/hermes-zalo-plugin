# Phase 03 — Hermes plugin `chatwoot`

**Priority:** P0 · **Status:** ⬜ chưa · **Depends:** none (làm song song P1/P2)

## Mục tiêu
Viết platform plugin `chatwoot` cho hermes-agent: nhận webhook Chatwoot (tin khách) → đẩy vào agent → trả lời qua Chatwoot API. Mute khi human tham gia.

## Khuôn mẫu
Theo `plugins/platforms/ntfy/` (HTTP, httpx, register/connect/send) + `gateway/platforms/webhook.py` (aiohttp server nhận POST). Plugin path = **zero core changes**.

## File: `plugins/chatwoot/`
- `plugin.yaml` — name `chatwoot-platform`, kind `platform`, requires_env.
- `adapter.py` — `ChatwootAdapter(BasePlatformAdapter)`:
  - `__init__`: đọc env CHATWOOT_BASE_URL, CHATWOOT_API_ACCESS_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_WEBHOOK_SECRET, CHATWOOT_PLUGIN_PORT (8088), CHATWOOT_BOT_NAME.
  - `connect()`: aiohttp server, `POST /chatwoot/webhook`, `GET /health`. Port-conflict check.
  - `_handle_webhook()`:
    1. Verify secret (header `X-Chatwoot-Webhook-Token` hoặc path secret).
    2. Parse JSON. Chỉ xử lý `event == "message_created"` & `message_type == "incoming"` (tin khách). Bỏ outgoing/private/activity.
    3. Dedup theo `message.id`.
    4. **MUTE GATE:**
       - status conversation == `open` hoặc `resolved` (human đang xử lý) → skip.
       - labels chứa `mute-ai` → skip.
       - (chỉ trả lời khi status `pending`/`bot` và không mute.)
    5. `build_source(chat_id=conversation_id, chat_type="dm", user_id=contact_id, user_name=sender_name)` → `MessageEvent(TEXT)` → `await self.handle_message(event)`.
  - `send(chat_id, content, ...)`: POST `Chatwoot API /api/v1/accounts/{acc}/conversations/{chat_id}/messages` `{content, message_type:"outgoing"}` (header `api_access_token`). Chatwoot tự bắn outgoing webhook → bridge → Zalo. → chống loop: outgoing không được plugin xử lý lại (chỉ nhận incoming).
  - `send_typing`: POST toggle typing của Chatwoot (tùy chọn, có thể no-op).
  - `send_image(chat_id, url, caption)`: gửi attachment qua Chatwoot API (multipart) — giai đoạn đầu có thể chỉ gửi link.
  - `get_chat_info`: `{name, type:"dm", chat_id}`.
  - `_env_enablement()`, `register(ctx)` theo mẫu ntfy.

## Mute chi tiết
- Đọc `conversation.status` và `conversation.labels`/`conversation.additional_attributes` từ payload webhook (Chatwoot gửi kèm). Không cần gọi thêm API.
- Khi human bấm reply trong Chatwoot, status thường tự sang `open` → từ đó plugin im. Khi resolve/snooze cũng im.
- Label `mute-ai`: support gắn để đè (kể cả khi vẫn pending).

## Env (`plugin.yaml` requires/optional)
- requires: `CHATWOOT_BASE_URL`, `CHATWOOT_API_ACCESS_TOKEN`, `CHATWOOT_ACCOUNT_ID`
- optional: `CHATWOOT_WEBHOOK_SECRET`, `CHATWOOT_PLUGIN_PORT` (8088), `CHATWOOT_PLUGIN_HOST` (127.0.0.1), `CHATWOOT_MUTE_LABEL` (mute-ai), `CHATWOOT_ALLOWED_USERS`, `CHATWOOT_ALLOW_ALL_USERS`, `CHATWOOT_HOME_CHANNEL`

## Success criteria
- POST giả lập webhook incoming → plugin gọi agent → POST reply lên Chatwoot (mock/curl thấy 200).
- Webhook outgoing/private/status=open → plugin skip (log rõ).
- Drop vào `~/.hermes/plugins/chatwoot/` → `hermes gateway status` thấy platform `chatwoot`.

## Test
- Unit: parse webhook (incoming vs outgoing vs private), mute gate (status/label), secret verify.
- Integration: curl webhook giả → kiểm log + outgoing call (mock Chatwoot bằng httpbin/local).

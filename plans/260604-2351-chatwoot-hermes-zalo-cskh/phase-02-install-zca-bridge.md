# Phase 02 — Cài & cấu hình zca-bridge

**Priority:** P0 · **Status:** ⬜ chưa · **Depends:** P1 (cần Chatwoot URL)

## Mục tiêu
Clone tinovn/zca-bridge, build, dựng Postgres riêng cho bridge, chạy systemd, QR-login số Zalo phụ, verify Zalo↔Chatwoot 2 chiều.

## Cài đặt (native, Node 20)
`deploy/install-zca-bridge.sh`:
1. Cài Node 20 (nodesource) nếu chưa có.
2. Tạo Postgres DB riêng `zca` (user/pass) — TÁCH khỏi DB Chatwoot.
3. `git clone https://github.com/tinovn/zca-bridge /opt/zca-bridge` → `npm ci && npm run build`.
4. Tạo `/opt/zca-bridge/.env`:
   - `DATABASE_URL=postgres://zca:<pass>@127.0.0.1:5432/zca`
   - `CHATWOOT_BASE_URL=http://127.0.0.1:3000`
   - `CREDENTIALS_KEY=<openssl rand -hex 32>`
   - `PORT=4000`
   - `PUBLIC_BASE_URL=https://<bridge-domain hoặc domain/bridge>`
   - `CHATWOOT_API_ACCESS_TOKEN=<tạo ở P4>`, `CHATWOOT_ACCOUNT_ID=1`
   - `ADMIN_TOKEN=<openssl rand -hex 24>`, `WEBHOOK_SECRET=<openssl rand -hex 24>`
   - `MEDIA_ARCHIVE_ROOT=/opt/zca-bridge/archive`
5. systemd unit `zca-bridge.service` (bridge tự migrate khi start).
6. Health: `curl http://127.0.0.1:4000/healthz`.

## QR login (số Zalo phụ)
- Bridge admin UI: `https://<domain>/admin/?token=<ADMIN_TOKEN>` → tạo account → quét QR bằng app Zalo (số phụ).
- Hoặc endpoint QR theo admin routes của bridge.

## Wiring Chatwoot (làm ở P4, ghi chú ở đây)
- Trong Chatwoot tạo **API inbox** cho mỗi account Zalo (bridge map qua `chatwoot_inbox_identifier`).
- Set webhook outgoing của Chatwoot → `https://<domain>/webhooks/chatwoot/<WEBHOOK_SECRET>`.

## Success criteria
- Khách nhắn số Zalo phụ → xuất hiện conversation trong Chatwoot.
- Reply tay trong Chatwoot → về Zalo khách.
- Echo/loop không xảy ra (bridge có message_map).

## Rủi ro
- zca-js có thể cần proxy dân cư cùng quốc gia để giảm khoá (bridge hỗ trợ qua env).
- QR session hết hạn → cần login lại; bridge tự set status `expired`.

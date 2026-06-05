# Phase 04 — Caddy TLS + đấu nối Agent Bot

**Priority:** P0 · **Status:** ⬜ chưa · **Depends:** P1, P2, P3

## Mục tiêu
Reverse proxy Caddy auto-TLS, đấu Chatwoot ↔ bridge ↔ Hermes plugin thành luồng end-to-end.

## Caddy (`deploy/install-caddy.sh` + `Caddyfile`)
- Cài Caddy (apt repo chính thức).
- Caddyfile:
  ```
  <domain> {
      handle /webhooks/chatwoot/* { reverse_proxy 127.0.0.1:4000 }
      handle /admin/*             { reverse_proxy 127.0.0.1:4000 }   # bridge admin
      handle /media/*             { reverse_proxy 127.0.0.1:4000 }
      handle                      { reverse_proxy 127.0.0.1:3000 }   # Chatwoot
  }
  ```
- Auto-TLS Let's Encrypt (cần domain trỏ về 103.142.25.24, port 80/443 mở).
- Tắt nginx của Chatwoot installer trước.

## Đấu Agent Bot (Chatwoot)
1. Tạo **Access Token** (Profile → Access Token) → điền `CHATWOOT_API_ACCESS_TOKEN` cho cả bridge (.env P2) lẫn plugin (P3). Lấy `ACCOUNT_ID`.
2. Tạo **Agent Bot** (Super Admin → Bots, hoặc API `/platform/api/v1/agent_bots`): `outgoing_url = http://127.0.0.1:8088/chatwoot/webhook` (nội bộ, không qua Caddy). Lấy bot access token nếu cần.
3. **Gán Agent Bot vào inbox Zalo** (inbox do bridge tạo) → Chatwoot sẽ gửi `message_created` tới plugin.
4. Set **webhook outgoing** Chatwoot (Settings → Integrations → Webhooks) → `https://<domain>/webhooks/chatwoot/<WEBHOOK_SECRET>` (cho bridge gửi tin ra Zalo).
5. Tạo label `mute-ai` trong Chatwoot.

## Drop plugin vào Hermes
- Copy `plugins/chatwoot/` → `~/.hermes/plugins/chatwoot/` trên VPS.
- Set env plugin (CHATWOOT_BASE_URL=http://127.0.0.1:3000, token, account_id, webhook_secret, port=8088).
- Restart Hermes gateway (cần biết service name — **câu hỏi mở**).
- `hermes gateway status` → thấy `chatwoot` connected.

## Luồng end-to-end cần thông
Khách Zalo → bridge → Chatwoot (inbox) → Agent Bot webhook → Hermes plugin → agent → Chatwoot API (outgoing) → Chatwoot webhook → bridge → Zalo khách.

## Success criteria
- Khách Zalo nhắn → nhận được trả lời AI tự động.
- Support reply trong Chatwoot → AI im (status open).
- Gắn `mute-ai` → AI im dù pending.

## Câu hỏi mở
- Domain cụ thể?
- Hermes gateway service name để restart?

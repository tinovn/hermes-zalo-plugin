# Setup CSKH Tino — Chatwoot + zca-bridge + Hermes plugin

Hệ hỗ trợ khách hàng: khách nhắn Zalo → Chatwoot (UI team CSKH) → Hermes AI tự trả lời → về Zalo. Support nhảy vào thì AI tự im (mute).

## Kiến trúc

```
Khách Zalo ⇄ zca-bridge (:4000) ⇄ Chatwoot (:3000)
                                      │ Agent Bot webhook
                                      ▼
                    Hermes plugin chatwoot (:8088, trong gateway)
                                      │ reply qua Chatwoot API
                                      ▼  outgoing webhook → bridge → Zalo
Caddy (:443 auto-TLS) → / → Chatwoot ; /webhooks,/admin,/media → bridge
```

- **Chatwoot** = UI chat giống Zalo Web cho team (inbox, lịch sử, gán hội thoại).
- **zca-bridge** = cầu nối Zalo ↔ Chatwoot (clone `tinovn/zca-bridge`).
- **Hermes plugin `chatwoot`** = Agent Bot AI, chỉ đẩy chat sang Hermes; Hermes tự xử lý (kể cả đặt hàng qua MCP riêng).

## Cài nhanh (VPS 103.142.25.24)

```bash
cd hermes-zalo-plugin/deploy
cp .env.example .env
nano .env            # điền DOMAIN, ACME_EMAIL, admin Chatwoot...
sudo ./install-all.sh
```

Script chạy theo thứ tự: Chatwoot → (nhắc tạo Access Token) → zca-bridge → Caddy → Hermes plugin. Mỗi script chạy lại được (idempotent).

Cài lẻ từng phần:
```bash
sudo ./install-chatwoot.sh
sudo ./install-zca-bridge.sh
sudo ./install-caddy.sh
sudo ./install-hermes-plugin.sh
```

## Việc làm tay sau khi cài

1. **DNS:** trỏ `DOMAIN` (A record) về `103.142.25.24`, mở port 80/443 (Caddy auto-TLS).
2. **Access Token Chatwoot:** login admin → Profile → Access Token → điền `CHATWOOT_API_ACCESS_TOKEN` vào `deploy/.env` (dùng chung bridge + plugin).
3. **QR login Zalo (số phụ):** `https://<DOMAIN>/admin/?token=<ZCA_ADMIN_TOKEN>` → tạo account → quét QR. (Token in ra khi chạy install-zca-bridge.)
4. **Agent Bot (Chatwoot):**
   - Tạo Agent Bot, `outgoing_url = http://127.0.0.1:8088/chatwoot/webhook`.
   - Nếu set `CHATWOOT_WEBHOOK_SECRET`, Chatwoot phải gửi header `X-Chatwoot-Webhook-Token`.
   - Gán Agent Bot vào **inbox Zalo** (do bridge tạo).
5. **Webhook ra Zalo:** Chatwoot → Settings → Integrations → Webhooks → `https://<DOMAIN>/webhooks/chatwoot/<ZCA_WEBHOOK_SECRET>`.
6. **Label mute:** tạo label `mute-ai` trong Chatwoot.
7. **Hermes gateway:** đảm bảo nạp `~/.hermes/plugins/chatwoot/chatwoot.env` (thêm `EnvironmentFile=` vào systemd unit của hermes), restart, kiểm tra `hermes gateway status` thấy `chatwoot`.

## Cơ chế mute (cả 2)

| Cách | Khi nào AI im |
|---|---|
| **Auto theo status** | Human reply/assign → conversation `open` → AI im. Chỉ trả lời khi `pending` (`CHATWOOT_BOT_REPLY_STATUSES`). |
| **Label thủ công** | Gắn `mute-ai` → AI im ngay cả khi `pending`. |

Chống loop: plugin chỉ xử lý tin `incoming` (khách); tin AI gửi ra là `outgoing` → bỏ qua. Bridge có `message_map` chống echo riêng.

## Vận hành

```bash
systemctl status chatwoot-web.1 chatwoot-worker.1 zca-bridge caddy
journalctl -u zca-bridge -f
journalctl -u caddy -f
curl http://127.0.0.1:8088/health    # plugin chatwoot
```

Rollback: tắt Agent Bot khỏi inbox = về 100% human. Tắt plugin = AI im.

## Rủi ro
- **zca-js không chính thức** → rủi ro khoá tài khoản Zalo. Dùng **số phụ**, cân nhắc proxy dân cư (`ZCA_PROXY`), hoặc chuyển Zalo OA.
- **RAM 6G** chật khi chạy hết. Script tự thêm swap 4G; theo dõi `free -h`.

## Test (P5)
Xem `plans/260604-2351-chatwoot-hermes-zalo-cskh/phase-05-test-golive.md`.

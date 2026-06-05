# Plan: Chatwoot + zca-bridge + Hermes plugin cho CSKH Tino

**Mục tiêu:** Dựng hệ CSKH trên VPS hermes — khách Zalo nhắn → Chatwoot (UI team) → Hermes AI tự trả lời → về Zalo. Support nhảy vào thì AI tự im (mute). **Qua tuần chạy thử auto support.**

## Chốt scope (từ user)
- VPS: **103.142.25.24** (cùng máy Hermes-agent đã chạy sẵn)
- Zalo: **số phụ** (tránh rủi ro khoá zca-js)
- "Tự đặt hàng": user tự viết **MCP cho Hermes** → tao **chỉ đẩy chat sang Hermes**, không lo logic đơn
- Việc của tao: **(A)** cài Chatwoot native, **(B)** cài+cấu hình zca-bridge, **(C)** viết Hermes plugin `chatwoot`
- Hermes I/O: **plugin platform trong gateway** (drop vào `~/.hermes/plugins/`)
- Domain/TLS: **Caddy auto-TLS**
- Script: **bash native** (apt/systemd, không Docker)
- Bỏ marketing funnel cũ

## Kiến trúc
```
Khách Zalo ⇄ zca-bridge (systemd, :4000) ⇄ Chatwoot (native, :3000)
                                               │  Agent Bot webhook
                                               ▼
                              Hermes plugin chatwoot (aiohttp :8088 trong gateway)
                                               │ reply qua Chatwoot API
                                               ▼  outgoing webhook → bridge → Zalo
Caddy (:443 auto-TLS) → reverse proxy Chatwoot + bridge admin
```

**Mute:** (1) auto theo conversation status — human reply → status `open` → plugin bỏ qua; (2) label `mute-ai` đè thủ công.

## Phases
| Phase | File | Trạng thái |
|---|---|---|
| P1 | [phase-01-install-chatwoot.md](phase-01-install-chatwoot.md) | ✅ script xong (chờ chạy trên VPS) |
| P2 | [phase-02-install-zca-bridge.md](phase-02-install-zca-bridge.md) | ✅ script xong (chờ chạy trên VPS) |
| P3 | [phase-03-hermes-chatwoot-plugin.md](phase-03-hermes-chatwoot-plugin.md) | ✅ code + test xong (14/14 pass) |
| P4 | [phase-04-wiring-caddy-agentbot.md](phase-04-wiring-caddy-agentbot.md) | ✅ script + Caddyfile xong (chờ đấu nối trên VPS) |
| P5 | [phase-05-test-golive.md](phase-05-test-golive.md) | ⬜ chờ deploy VPS để test |

## Sản phẩm bàn giao (deliverables)
1. `deploy/install-chatwoot.sh` — cài Chatwoot native (Ruby, Postgres, Redis, sidekiq, systemd)
2. `deploy/install-zca-bridge.sh` — clone + build bridge, Postgres riêng, systemd
3. `deploy/install-caddy.sh` + `Caddyfile` — reverse proxy auto-TLS
4. `deploy/install-all.sh` — orchestrate cả 3 + health check + in hướng dẫn QR/Agent Bot
5. `plugins/chatwoot/` — Hermes plugin (adapter.py + plugin.yaml + __init__.py)
6. `deploy/.env.example` + `docs/setup-cskh.md`

## Dependencies / thứ tự
P1 → P2 (bridge cần Chatwoot URL) → P3 (plugin độc lập, làm song song được) → P4 (đấu nối) → P5.

## Rủi ro chính
- RAM 6G chật (Chatwoot ~3G + bridge + 2 Postgres + hermes). → đo, thêm swap, hoặc nâng RAM.
- Khoá Zalo (zca-js) → số phụ.
- Webhook mute: cần test event Chatwoot bắn khi human reply.

## Câu hỏi mở
- Domain cụ thể trỏ về VPS là gì? (cho Caddyfile)
- Hermes gateway hiện chạy bằng gì (systemd? tên service?) để tao biết chỗ drop plugin + restart.
- Plugin chatwoot nghe port nào (mặc định 8088) — có vướng port nào trên VPS không?

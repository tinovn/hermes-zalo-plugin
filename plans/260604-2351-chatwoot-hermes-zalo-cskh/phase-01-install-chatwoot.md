# Phase 01 — Cài Chatwoot native trên VPS

**Priority:** P0 (nền tảng) · **Status:** ⬜ chưa

## Mục tiêu
Cài Chatwoot self-host **native** (không Docker) trên Ubuntu VPS 103.142.25.24, chạy production qua systemd.

## Cách làm (khuyến nghị: dùng installer chính thức)
Chatwoot có script cài native chính thức cho Ubuntu 20.04/22.04/24.04:
```
wget https://get.chatwoot.app/linux/install.sh
chmod +x install.sh && ./install.sh --install
```
Script này tự lo: Ruby (rbenv), Node, Postgres, Redis, nginx (sẽ thay/tắt để dùng Caddy), pgvector, sidekiq, systemd services (`chatwoot-web.1`, `chatwoot-worker.1`).

→ `deploy/install-chatwoot.sh` của tao = wrapper: tải installer chính thức + cấu hình env (FRONTEND_URL, SECRET_KEY_BASE, Postgres pass, Redis), tắt nginx mặc định (Caddy lo TLS ở P4), tạo admin account, health check `:3000`.

## Env Chatwoot cần set (`/home/chatwoot/chatwoot/.env`)
- `FRONTEND_URL=https://<domain>`
- `SECRET_KEY_BASE` (openssl rand -hex 64)
- `POSTGRES_*`, `REDIS_*`
- `RAILS_ENV=production`, `NODE_ENV=production`
- `ENABLE_ACCOUNT_SIGNUP=false`
- SMTP (tùy chọn, để gửi mail mời agent — có thể bỏ giai đoạn thử)

## Steps
1. Check OS (Ubuntu), RAM (cảnh báo nếu <4G khả dụng), thêm swap 4G nếu thiếu.
2. Tải + chạy chatwoot install.sh (mode `--install`).
3. Override `.env` (FRONTEND_URL=domain, signup=false).
4. `rails db:chatwoot_prepare` (installer tự làm), restart services.
5. Tạo super admin qua `rails runner` hoặc UI lần đầu.
6. Health: `curl -f http://127.0.0.1:3000` → 200/302.

## Success criteria
- `systemctl status chatwoot-web.1 chatwoot-worker.1` = active.
- Truy cập được Chatwoot, login super admin.

## Rủi ro
- RAM: Chatwoot (rails+sidekiq+node) ngốn ~2-3G. → swap + đo.
- Installer chính thức cài nginx → cần tắt (`systemctl disable --now nginx`) trước khi Caddy dùng :80/:443.

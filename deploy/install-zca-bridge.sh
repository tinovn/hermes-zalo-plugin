#!/usr/bin/env bash
# Cài zca-bridge (Node 20, native) + Postgres riêng + systemd.
# Usage: sudo ./install-zca-bridge.sh [path/to/.env]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

require_root
require_ubuntu
load_env "${1:-$HERE/.env}"

: "${ZCA_BRIDGE_REPO:?thiếu ZCA_BRIDGE_REPO}"
: "${ZCA_BRIDGE_DIR:=/opt/zca-bridge}"
: "${ZCA_BRIDGE_PORT:=4000}"
gen_secret ZCA_CREDENTIALS_KEY 32
gen_secret ZCA_ADMIN_TOKEN 24
gen_secret ZCA_WEBHOOK_SECRET 24
gen_secret ZCA_DB_PASSWORD 24

c_step "zca-bridge — Node 20"
if ! command -v node >/dev/null || ! node -v | grep -qE 'v(2[0-9]|[3-9][0-9])'; then
  c_info "Cài Node 20 (nodesource)"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt_install nodejs
fi
c_ok "node $(node -v), npm $(npm -v)"

c_step "zca-bridge — Postgres DB riêng ($ZCA_DB_NAME)"
command -v psql >/dev/null || apt_install postgresql
systemctl enable --now postgresql
su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${ZCA_DB_USER}'\"" | grep -q 1 \
  || su - postgres -c "psql -c \"CREATE USER ${ZCA_DB_USER} WITH PASSWORD '${ZCA_DB_PASSWORD}';\""
su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${ZCA_DB_NAME}'\"" | grep -q 1 \
  || su - postgres -c "psql -c \"CREATE DATABASE ${ZCA_DB_NAME} OWNER ${ZCA_DB_USER};\""
c_ok "DB ${ZCA_DB_NAME} sẵn sàng"

c_step "zca-bridge — clone + build"
if [ -d "$ZCA_BRIDGE_DIR/.git" ]; then
  git -C "$ZCA_BRIDGE_DIR" pull --ff-only || c_warn "git pull bỏ qua"
else
  command -v git >/dev/null || apt_install git
  git clone "$ZCA_BRIDGE_REPO" "$ZCA_BRIDGE_DIR"
fi
cd "$ZCA_BRIDGE_DIR"
npm ci >/dev/null 2>&1 || npm install >/dev/null
npm run build >/dev/null
c_ok "Build xong"

c_step "zca-bridge — .env"
BENV="$ZCA_BRIDGE_DIR/.env"
set_env_file "$BENV" DATABASE_URL "postgres://${ZCA_DB_USER}:${ZCA_DB_PASSWORD}@127.0.0.1:5432/${ZCA_DB_NAME}"
set_env_file "$BENV" CHATWOOT_BASE_URL "http://127.0.0.1:3000"
set_env_file "$BENV" CREDENTIALS_KEY "${ZCA_CREDENTIALS_KEY}"
set_env_file "$BENV" PORT "${ZCA_BRIDGE_PORT}"
set_env_file "$BENV" PUBLIC_BASE_URL "https://${DOMAIN}"
set_env_file "$BENV" CHATWOOT_API_ACCESS_TOKEN "${CHATWOOT_API_ACCESS_TOKEN:-}"
set_env_file "$BENV" CHATWOOT_ACCOUNT_ID "${CHATWOOT_ACCOUNT_ID:-1}"
set_env_file "$BENV" ADMIN_TOKEN "${ZCA_ADMIN_TOKEN}"
set_env_file "$BENV" WEBHOOK_SECRET "${ZCA_WEBHOOK_SECRET}"
set_env_file "$BENV" MEDIA_ARCHIVE_ROOT "${ZCA_BRIDGE_DIR}/archive"
[ -n "${ZCA_PROXY:-}" ] && set_env_file "$BENV" ZALO_PROXY "${ZCA_PROXY}"
mkdir -p "${ZCA_BRIDGE_DIR}/archive"
c_ok "Đã ghi $BENV"

c_step "zca-bridge — systemd"
cat > /etc/systemd/system/zca-bridge.service <<EOF
[Unit]
Description=zca-bridge (Zalo <-> Chatwoot)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
WorkingDirectory=${ZCA_BRIDGE_DIR}
EnvironmentFile=${ZCA_BRIDGE_DIR}/.env
ExecStart=/usr/bin/node ${ZCA_BRIDGE_DIR}/dist/main.js
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now zca-bridge
c_step "zca-bridge — health (:${ZCA_BRIDGE_PORT})"
wait_http "http://127.0.0.1:${ZCA_BRIDGE_PORT}/healthz" 20 3 \
  || c_warn "Bridge chưa lên — journalctl -u zca-bridge -n 50"

c_ok "zca-bridge xong."
echo
c_info "QR LOGIN số Zalo phụ: mở https://${DOMAIN}/admin/?token=${ZCA_ADMIN_TOKEN}"
c_info "WEBHOOK_SECRET bridge = ${ZCA_WEBHOOK_SECRET}"
c_info "→ Trong Chatwoot set webhook outgoing: https://${DOMAIN}/webhooks/chatwoot/${ZCA_WEBHOOK_SECRET}"

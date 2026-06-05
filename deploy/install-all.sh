#!/usr/bin/env bash
# Orchestrator: cài toàn bộ CSKH stack trên VPS hermes (103.142.25.24).
# Thứ tự: Chatwoot -> zca-bridge -> Caddy -> Hermes plugin.
#
# LƯU Ý: Cần tạo Chatwoot Access Token GIỮA CHỪNG (sau Chatwoot, trước bridge)
# nếu muốn bridge/plugin có token ngay. Script sẽ tạm dừng nhắc nếu thiếu.
#
# Usage: sudo ./install-all.sh [path/to/.env]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

require_root
require_ubuntu
ENV_FILE="${1:-$HERE/.env}"
load_env "$ENV_FILE"

c_step "[1/4] Chatwoot"
bash "$HERE/install-chatwoot.sh" "$ENV_FILE"

if [ -z "${CHATWOOT_API_ACCESS_TOKEN:-}" ]; then
  c_warn "Chưa có CHATWOOT_API_ACCESS_TOKEN."
  c_info "Mở Chatwoot (https://${DOMAIN}), login admin, vào Profile -> Access Token, copy token."
  read -rp "Dán Access Token vào đây (Enter để bỏ qua, điền sau): " _tok || true
  if [ -n "${_tok:-}" ]; then
    set_env_file "$ENV_FILE" CHATWOOT_API_ACCESS_TOKEN "$_tok"
    load_env "$ENV_FILE"
    c_ok "Đã lưu token vào $ENV_FILE"
  fi
fi

c_step "[2/4] zca-bridge"
bash "$HERE/install-zca-bridge.sh" "$ENV_FILE"

c_step "[3/4] Caddy"
bash "$HERE/install-caddy.sh" "$ENV_FILE"

c_step "[4/4] Hermes plugin chatwoot"
bash "$HERE/install-hermes-plugin.sh" "$ENV_FILE"

c_step "XONG — tóm tắt"
cat <<EOF
Chatwoot   : https://${DOMAIN}            (systemctl status chatwoot-web.1)
zca-bridge : :${ZCA_BRIDGE_PORT:-4000}    (systemctl status zca-bridge)
Caddy      : :443 auto-TLS                (systemctl status caddy)
Plugin     : ${HERMES_PLUGINS_DIR:-/root/.hermes/plugins}/chatwoot

CÒN LẠI (làm tay):
  - QR login số Zalo phụ: https://${DOMAIN}/admin/?token=<ZCA_ADMIN_TOKEN>
  - Chatwoot: tạo Agent Bot (outgoing_url http://127.0.0.1:${CHATWOOT_PLUGIN_PORT:-8088}/chatwoot/webhook),
    gán vào inbox Zalo, tạo label 'mute-ai'.
  - Chatwoot: webhook outgoing -> https://${DOMAIN}/webhooks/chatwoot/<ZCA_WEBHOOK_SECRET>
  - Restart Hermes gateway (nạp ${HERMES_PLUGINS_DIR:-/root/.hermes/plugins}/chatwoot/chatwoot.env),
    kiểm tra: hermes gateway status -> thấy 'chatwoot'.

Xem chi tiết: docs/setup-cskh.md
EOF

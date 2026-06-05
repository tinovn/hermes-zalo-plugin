#!/usr/bin/env bash
# Cài Caddy + Caddyfile reverse proxy auto-TLS cho Chatwoot + zca-bridge.
# Usage: sudo ./install-caddy.sh [path/to/.env]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

require_root
require_ubuntu
load_env "${1:-$HERE/.env}"
: "${DOMAIN:?thiếu DOMAIN}"
: "${ZCA_BRIDGE_PORT:=4000}"

c_step "Caddy — cài qua apt repo chính thức"
if ! command -v caddy >/dev/null; then
  apt_install debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt_install caddy
fi
c_ok "$(caddy version | head -1)"

c_step "Caddy — Caddyfile"
# Bridge endpoints (webhook/admin/media) đi 4000; còn lại đi Chatwoot 3000.
cat > /etc/caddy/Caddyfile <<EOF
{
    email ${ACME_EMAIL:-admin@${DOMAIN}}
}

${DOMAIN} {
    handle /webhooks/chatwoot/* {
        reverse_proxy 127.0.0.1:${ZCA_BRIDGE_PORT}
    }
    handle /admin/* {
        reverse_proxy 127.0.0.1:${ZCA_BRIDGE_PORT}
    }
    handle /media/* {
        reverse_proxy 127.0.0.1:${ZCA_BRIDGE_PORT}
    }
    handle {
        reverse_proxy 127.0.0.1:3000
    }
}
EOF
c_ok "Đã ghi /etc/caddy/Caddyfile (domain: ${DOMAIN})"

caddy validate --config /etc/caddy/Caddyfile || die "Caddyfile không hợp lệ."
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy
c_ok "Caddy chạy. TLS tự cấp khi ${DOMAIN} trỏ đúng về VPS + mở port 80/443."

#!/usr/bin/env bash
# Deploy plugin chatwoot vào Hermes (~/.hermes/plugins/) + ghi env plugin.
# Hermes-agent đã chạy sẵn trên VPS; script này chỉ drop plugin + nhắc restart.
# Usage: sudo ./install-hermes-plugin.sh [path/to/.env]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

require_root
load_env "${1:-$HERE/.env}"
: "${HERMES_PLUGINS_DIR:=/root/.hermes/plugins}"
gen_secret CHATWOOT_WEBHOOK_SECRET 24

SRC="$REPO_ROOT/plugins/chatwoot"
[ -d "$SRC" ] || die "Không thấy plugin nguồn: $SRC"

c_step "Hermes plugin — copy vào $HERMES_PLUGINS_DIR/chatwoot"
mkdir -p "$HERMES_PLUGINS_DIR"
rm -rf "$HERMES_PLUGINS_DIR/chatwoot"
cp -r "$SRC" "$HERMES_PLUGINS_DIR/chatwoot"
# Bỏ test khỏi bản deploy (không cần ở runtime).
rm -f "$HERMES_PLUGINS_DIR/chatwoot/test_webhook_parser.py"
c_ok "Đã copy plugin"

c_step "Hermes plugin — ghi env (drop-in)"
# Ghi 1 file env để admin source vào systemd của hermes hoặc shell.
PENV="$HERMES_PLUGINS_DIR/chatwoot/chatwoot.env"
cat > "$PENV" <<EOF
# Env cho plugin chatwoot — source vào tiến trình Hermes gateway.
CHATWOOT_BASE_URL=http://127.0.0.1:3000
CHATWOOT_API_ACCESS_TOKEN=${CHATWOOT_API_ACCESS_TOKEN:-}
CHATWOOT_ACCOUNT_ID=${CHATWOOT_ACCOUNT_ID:-1}
CHATWOOT_WEBHOOK_SECRET=${CHATWOOT_WEBHOOK_SECRET}
CHATWOOT_PLUGIN_HOST=${CHATWOOT_PLUGIN_HOST:-127.0.0.1}
CHATWOOT_PLUGIN_PORT=${CHATWOOT_PLUGIN_PORT:-8088}
CHATWOOT_MUTE_LABEL=${CHATWOOT_MUTE_LABEL:-mute-ai}
CHATWOOT_BOT_REPLY_STATUSES=${CHATWOOT_BOT_REPLY_STATUSES:-pending}
CHATWOOT_ALLOW_ALL_USERS=${CHATWOOT_ALLOW_ALL_USERS:-true}
EOF
c_ok "Đã ghi $PENV"

if [ -z "${CHATWOOT_API_ACCESS_TOKEN:-}" ]; then
  c_warn "CHATWOOT_API_ACCESS_TOKEN còn trống — điền vào $PENV sau khi tạo token Chatwoot."
fi

c_step "HOÀN TẤT — việc cần làm tay"
cat <<EOF
1) Đảm bảo Hermes gateway nạp env: source $PENV
   (thêm 'EnvironmentFile=$PENV' vào systemd unit của hermes, hoặc export trước khi chạy).
2) Restart Hermes gateway, rồi kiểm tra:
     hermes gateway status     # phải thấy platform 'chatwoot'
3) Tạo Agent Bot trong Chatwoot, outgoing_url:
     http://127.0.0.1:${CHATWOOT_PLUGIN_PORT:-8088}/chatwoot/webhook
   (nếu set webhook secret, Chatwoot phải gửi header X-Chatwoot-Webhook-Token=${CHATWOOT_WEBHOOK_SECRET})
4) Gán Agent Bot vào inbox Zalo; tạo label 'mute-ai'.
EOF

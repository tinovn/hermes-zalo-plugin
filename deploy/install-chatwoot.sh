#!/usr/bin/env bash
# Cài Chatwoot native (Ubuntu) qua installer chính thức, cấu hình cho CSKH Tino.
# Dùng Caddy cho TLS (P4) nên TẮT nginx mà installer dựng.
#
# Usage: sudo ./install-chatwoot.sh [path/to/.env]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

require_root
require_ubuntu
load_env "${1:-$HERE/.env}"

c_step "Chatwoot — chuẩn bị hệ thống"
ensure_swap 4096
warn_low_ram 4096
apt-get update -qq
apt_install curl ca-certificates openssl

gen_secret CHATWOOT_SECRET_KEY_BASE 64
gen_secret CHATWOOT_POSTGRES_PASSWORD 24

CW_HOME="/home/chatwoot/chatwoot"
CW_ENV="$CW_HOME/.env"

c_step "Chatwoot — chạy installer chính thức"
if [ -d "$CW_HOME" ]; then
  c_ok "Chatwoot đã tồn tại ở $CW_HOME — bỏ qua cài mới, chỉ cập nhật .env"
else
  # Installer chính thức: cài Ruby/Node/Postgres/Redis/sidekiq + systemd units.
  # -s -- --install : chế độ cài tự động.
  c_info "Tải get.chatwoot.app/linux/install.sh ..."
  TMP_INST="$(mktemp)"
  curl -fsSL https://get.chatwoot.app/linux/install.sh -o "$TMP_INST"
  chmod +x "$TMP_INST"
  # Truyền domain để installer cấu hình FRONTEND_URL ban đầu.
  CW_DOMAIN="${DOMAIN:-}" bash "$TMP_INST" --install || die "Installer Chatwoot lỗi."
  rm -f "$TMP_INST"
fi

[ -f "$CW_ENV" ] || die "Không thấy $CW_ENV sau khi cài."

c_step "Chatwoot — ghi cấu hình production"
set_env_file "$CW_ENV" FRONTEND_URL "https://${DOMAIN}"
set_env_file "$CW_ENV" SECRET_KEY_BASE "${CHATWOOT_SECRET_KEY_BASE}"
set_env_file "$CW_ENV" RAILS_ENV "production"
set_env_file "$CW_ENV" NODE_ENV "production"
set_env_file "$CW_ENV" ENABLE_ACCOUNT_SIGNUP "false"
set_env_file "$CW_ENV" DEFAULT_LOCALE "vi"
# Chatwoot lắng nghe loopback; Caddy proxy vào (P4).
set_env_file "$CW_ENV" RAILS_MAX_THREADS "5"
c_ok "Đã ghi $CW_ENV"

c_step "Chatwoot — tắt nginx (Caddy sẽ lo TLS ở P4)"
systemctl disable --now nginx 2>/dev/null || c_info "nginx không chạy (ok)"

c_step "Chatwoot — restart services"
systemctl restart chatwoot.target 2>/dev/null \
  || systemctl restart chatwoot-web.1 chatwoot-worker.1 2>/dev/null \
  || c_warn "Không restart được service tự động — kiểm tra tên unit."

c_step "Chatwoot — chờ web sẵn sàng (:3000)"
wait_http "http://127.0.0.1:3000" 40 3 || c_warn "Chatwoot chưa lên — xem: journalctl -u chatwoot-web.1 -n 50"

c_step "Chatwoot — tạo super admin (nếu chưa có)"
if [ -n "${CHATWOOT_ADMIN_EMAIL:-}" ] && [ -n "${CHATWOOT_ADMIN_PASSWORD:-}" ]; then
  su - chatwoot -c "cd $CW_HOME && RAILS_ENV=production bundle exec rails runner \"
    e='${CHATWOOT_ADMIN_EMAIL}'; p='${CHATWOOT_ADMIN_PASSWORD}'; n='${CHATWOOT_ADMIN_NAME:-Admin}';
    u=User.find_by(email:e);
    if u.nil?
      acc=Account.first || Account.create!(name:'Tino');
      u=User.create!(name:n,email:e,password:p,password_confirmation:p,confirmed_at:Time.now);
      AccountUser.create!(account:acc,user:u,role::administrator);
      puts 'CREATED admin '+e
    else
      puts 'admin exists '+e
    end
  \"" 2>&1 | sed 's/^/  /' || c_warn "Tạo admin lỗi — tạo tay qua UI."
else
  c_warn "Chưa set CHATWOOT_ADMIN_EMAIL/PASSWORD — tạo admin qua UI."
fi

c_ok "Chatwoot xong. UI nội bộ: http://127.0.0.1:3000 (qua Caddy: https://${DOMAIN})"
echo
c_info "TIẾP THEO: tạo Access Token trong Chatwoot (Profile -> Access Token),"
c_info "điền CHATWOOT_API_ACCESS_TOKEN vào deploy/.env, rồi chạy install-zca-bridge.sh"

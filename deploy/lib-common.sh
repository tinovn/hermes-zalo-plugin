#!/usr/bin/env bash
# Hàm dùng chung cho các script install. Source bởi install-*.sh.
# Không chạy trực tiếp.

set -euo pipefail

# ── Logging ──────────────────────────────────────────────────────────────
c_info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
c_ok()    { printf '\033[1;32m[ OK ]\033[0m  %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
c_err()   { printf '\033[1;31m[FAIL]\033[0m  %s\n' "$*" >&2; }
c_step()  { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

die() { c_err "$*"; exit 1; }

# ── Yêu cầu môi trường ───────────────────────────────────────────────────
require_root() {
  [ "$(id -u)" -eq 0 ] || die "Phải chạy bằng root (dùng sudo)."
}

require_ubuntu() {
  [ -f /etc/os-release ] || die "Không xác định được OS."
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) c_ok "OS: ${PRETTY_NAME:-$ID}" ;;
    *) c_warn "OS ${ID:-unknown} chưa được kiểm thử (script hỗ trợ Ubuntu/Debian)." ;;
  esac
}

# Nạp deploy/.env nếu có, export toàn bộ biến.
load_env() {
  local env_file="${1:?env path required}"
  [ -f "$env_file" ] || die "Thiếu file env: $env_file (copy từ .env.example)."
  set -a
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
  c_ok "Đã nạp env: $env_file"
}

# Sinh secret nếu biến rỗng. gen_secret VAR_NAME [hex_bytes]
gen_secret() {
  local var="$1" bytes="${2:-32}"
  local cur="${!var:-}"
  if [ -z "$cur" ]; then
    local val; val="$(openssl rand -hex "$bytes")"
    export "$var=$val"
    c_info "Đã sinh $var (random)"
  fi
}

# Đảm bảo có swap tối thiểu (MB). ensure_swap 4096
ensure_swap() {
  local want_mb="${1:-4096}"
  local cur_kb; cur_kb="$(awk '/SwapTotal/{print $2}' /proc/meminfo)"
  local cur_mb=$(( cur_kb / 1024 ))
  if [ "$cur_mb" -ge "$want_mb" ]; then
    c_ok "Swap hiện có ${cur_mb}MB (>= ${want_mb}MB)"
    return
  fi
  c_warn "Swap ${cur_mb}MB < ${want_mb}MB — tạo swapfile ${want_mb}MB"
  local sf=/swapfile
  if [ ! -f "$sf" ]; then
    fallocate -l "${want_mb}M" "$sf" || dd if=/dev/zero of="$sf" bs=1M count="$want_mb"
    chmod 600 "$sf"; mkswap "$sf"
  fi
  swapon "$sf" || true
  grep -q "$sf" /etc/fstab || echo "$sf none swap sw 0 0" >> /etc/fstab
  c_ok "Đã bật swap"
}

# Cảnh báo RAM khả dụng thấp.
warn_low_ram() {
  local want_mb="${1:-4096}"
  local total_mb; total_mb="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
  if [ "$total_mb" -lt "$want_mb" ]; then
    c_warn "RAM ${total_mb}MB < khuyến nghị ${want_mb}MB. Chatwoot + bridge + hermes có thể chật. Đã bù swap."
  else
    c_ok "RAM: ${total_mb}MB"
  fi
}

apt_install() {
  c_info "apt install: $*"
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" >/dev/null
}

# Đợi 1 URL trả 2xx/3xx. wait_http URL [retries] [sleep_s]
wait_http() {
  local url="$1" retries="${2:-30}" sleep_s="${3:-3}"
  for i in $(seq 1 "$retries"); do
    if curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
      c_ok "Sẵn sàng: $url"; return 0
    fi
    sleep "$sleep_s"
  done
  c_warn "Chưa phản hồi sau $((retries*sleep_s))s: $url"
  return 1
}

# Ghi env KEY=VALUE vào file (upsert).
set_env_file() {
  local file="$1" key="$2" val="$3"
  touch "$file"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    echo "${key}=${val}" >> "$file"
  fi
}

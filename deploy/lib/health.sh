#!/usr/bin/env bash
set -euo pipefail

verify_root_privileges() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "Please run installer as root: sudo ./broadcast.sh"
  fi
}

verify_python_version() {
  require_cmd python3
  local v
  v="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  log_info "python version=${v}"
}

verify_domain_dns() {
  require_cmd curl
  local server_ip domain_ip
  server_ip="$(curl -fsS ifconfig.me 2>/dev/null || true)"
  if command -v dig >/dev/null 2>&1; then
    domain_ip="$(dig +short "$DOMAIN" | tail -n1)"
  else
    domain_ip="$(getent ahosts "$DOMAIN" 2>/dev/null | awk '/STREAM/ {print $1; exit}')"
  fi
  if [[ -n "$server_ip" && -n "$domain_ip" && "$server_ip" == "$domain_ip" ]]; then
    log_ok "DNS verified for ${DOMAIN} -> ${domain_ip}"
  else
    log_warn "Domain does not resolve to this server (${DOMAIN}: ${domain_ip:-unknown}, server: ${server_ip:-unknown})"
  fi
}

verify_port_access() {
  for p in 80 443 3478 5349; do
    if command -v nc >/dev/null 2>&1; then
      nc -z 127.0.0.1 "$p" >/dev/null 2>&1 || true
    fi
  done
}


wait_for_backend_health() {
  local url="${1:-http://127.0.0.1:8000/health}"
  local tries="${2:-90}"
  for ((i=1; i<=tries; i++)); do
    local code
    code="$(curl --noproxy '*' -k -sS -m 2 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" =~ ^(2|3) ]]; then
      echo "[installer] backend ready url=$url code=$code"
      return 0
    fi
    if (( i == 1 || i % 10 == 0 )); then
      echo "[installer] waiting for backend url=$url attempt=$i/$tries code=${code:-connect}"
      systemctl is-active --quiet broadcast.service && echo "[installer] broadcast.service active" || true
    fi
    sleep 1
  done
  echo "[installer] backend health timeout url=$url"
  systemctl status broadcast.service --no-pager -l || true
  journalctl -u broadcast -n 80 --no-pager || true
  return 1
}

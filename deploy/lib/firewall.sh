#!/usr/bin/env bash
set -euo pipefail

verify_firewall_ports_impl() {
  local ports=(80 443 3478 5349)
  for p in "${ports[@]}"; do
    if nc -z localhost "$p" >/dev/null 2>&1; then
      log_ok "Port ${p} reachable locally"
    else
      log_warn "Port ${p} is not open locally"
    fi
  done
}

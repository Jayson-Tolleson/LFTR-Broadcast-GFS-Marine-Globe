#!/usr/bin/env bash
set -euo pipefail

create_systemd_services_impl() {
  local PYTHON_BIN="${PYTHON_BIN:-python3}"
  local unit="/etc/systemd/system/broadcast.service"
  cat > "$unit" <<EOF
[Unit]
Description=Broadcast Weather Globe
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/config/google.env
Environment=PYTHONUNBUFFERED=1
Environment=MALLOC_ARENA_MAX=2
Environment=OMP_NUM_THREADS=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
ExecStart=${APP_DIR}/venv/bin/${PYTHON_BIN} -m hypercorn --bind ${APP_BIND_HOST}:${APP_BIND_PORT} --workers ${APP_WORKERS} main:app
Restart=always
RestartSec=3
LimitNOFILE=20000
MemoryMax=10G

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable broadcast.service
  systemctl restart broadcast.service
}

verify_installation_impl() {
  local fail=0
  curl -fsS "http://localhost:${APP_BIND_PORT}/" >/dev/null || { log_warn "backend root failed"; fail=1; }
  curl -fsS "http://localhost:${APP_BIND_PORT}/gfs" >/dev/null || { log_warn "backend /gfs failed"; fail=1; }
  curl -kfsS "https://${DOMAIN}" >/dev/null || log_warn "public https endpoint check failed"

  printf '\n=== INSTALL SUMMARY ===\n'
  printf 'App dir: %s\n' "$APP_DIR"
  printf 'Service: broadcast.service\n'
  printf 'Domain: %s\n' "$DOMAIN"
  printf 'Routes expected: /, /broadcast, /watch, /gfs, /static/*, /api/*\n'
  if [[ $fail -eq 0 ]]; then
    log_ok "Core local health checks passed"
  fi
}

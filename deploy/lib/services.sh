#!/usr/bin/env bash
set -euo pipefail

create_systemd_services_impl() {
  local unit="/etc/systemd/system/broadcast.service"
  cat > "$unit" <<EOF
[Unit]
Description=LFTR Broadcast / GFS Marine Globe
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
Environment=APP_DIR=${APP_DIR}
Environment=VENV_DIR=${APP_DIR}/venv
Environment=APP_USER=${APP_USER}
Environment=APP_GROUP=${APP_GROUP}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=MALLOC_ARENA_MAX=2
Environment=OMP_NUM_THREADS=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
Environment=PYTHONMALLOC=malloc
Environment=HDF5_USE_FILE_LOCKING=FALSE
Environment=GFS_TILE_WARM_WORKERS=1
Environment=GFS_TILE_READ_WORKERS=1
Environment=GFS_ALLOW_SYNTHETIC_FALLBACK=0
Environment=ALLOW_SYNTHETIC_FALLBACK=0
EnvironmentFile=/etc/broadcast/install.env
ExecStartPre=/bin/bash -lc 'test -x ${APP_DIR}/venv/bin/python && ${APP_DIR}/venv/bin/python -c "import hypercorn, quart"'
ExecStart=${APP_DIR}/scripts/run_broadcast_service.sh
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=25
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

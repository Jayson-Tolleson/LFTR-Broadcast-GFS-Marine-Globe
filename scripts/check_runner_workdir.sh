#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"

echo "[workdir-check] pwd/app"
pwd
test -d "$APP_DIR" && echo "[workdir-check] app_dir exists: $APP_DIR"

echo
echo "[workdir-check] systemd"
systemctl cat broadcast | grep -Ei 'WorkingDirectory|Environment=APP_DIR|KillMode|TimeoutStopSec|ExecStart|User|Group' || true

echo
echo "[workdir-check] runner markers"
grep -nEi 'ensure_app_dir|starting hypercorn|cwd=|setsid|APP_DIR' "$APP_DIR/scripts/run_broadcast_service.sh" | head -80 || true

echo
echo "[workdir-check] health"
curl -fsS --max-time 10 http://127.0.0.1:8000/health || curl -fsS --max-time 10 http://127.0.0.1:8000/api/health || true

echo
echo "[workdir-check] recent journal"
journalctl -u broadcast -n 180 --no-pager | grep -Ei 'cwd=|FileNotFoundError|os.getcwd|starting hypercorn|startup ready|Running on|State .*timed out|SIGKILL|hypercorn exited' || true

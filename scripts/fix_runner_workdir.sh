#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"

echo "[workdir-fix] app_dir=$APP_DIR"
if [ ! -d "$APP_DIR" ]; then
  echo "[workdir-fix] ERROR: missing app dir $APP_DIR" >&2
  exit 1
fi

cd "$APP_DIR"

echo "[workdir-fix] installing systemd WorkingDirectory override"
sudo mkdir -p /etc/systemd/system/broadcast.service.d
sudo tee /etc/systemd/system/broadcast.service.d/10-working-directory.conf >/dev/null <<EOF
[Service]
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
KillMode=control-group
TimeoutStopSec=18
EOF

echo "[workdir-fix] ensuring runner is executable"
sudo chmod +x "$APP_DIR/scripts/run_broadcast_service.sh"

echo "[workdir-fix] daemon reload + restart"
sudo systemctl daemon-reload
sudo systemctl restart broadcast

echo "[workdir-fix] status"
sleep 3
systemctl cat broadcast | grep -Ei 'WorkingDirectory|Environment=APP_DIR|KillMode|TimeoutStopSec|ExecStart' || true
journalctl -u broadcast -n 120 --no-pager | grep -Ei 'starting hypercorn|cwd=|FileNotFoundError|os.getcwd|startup ready|Running on|hypercorn exited' || true

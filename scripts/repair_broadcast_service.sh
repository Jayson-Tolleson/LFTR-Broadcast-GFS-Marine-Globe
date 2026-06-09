#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"
VENV_DIR="${VENV_DIR:-$APP_DIR/venv}"
APP_USER="${APP_USER:-jayson_tolleson}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
cd "$APP_DIR"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "[repair] recreating missing virtualenv: $VENV_DIR"
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt" netCDF4 pydap cfgrib eccodes
"$VENV_DIR/bin/python" -c 'import hypercorn, quart; print("[repair] hypercorn/quart ok")'
cat >/etc/systemd/system/broadcast.service <<EOF
[Unit]
Description=Broadcast API Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
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
ExecStartPre=/bin/bash -lc 'test -x $VENV_DIR/bin/python && $VENV_DIR/bin/python -c "import hypercorn, quart"'
ExecStart=$APP_DIR/scripts/run_broadcast_service.sh
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=25
LimitNOFILE=20000
MemoryMax=10G

[Install]
WantedBy=multi-user.target
EOF

"$VENV_DIR/bin/python" - <<'PY'
import builtins
import sitecustomize  # noqa: F401
import server.gfs_service as svc
from server.gfs.inland_water import _centroid, _temperature_point, _enrich_inland_feature
assert getattr(builtins, "ALLOW_SYNTHETIC_FALLBACK", None) is False
assert getattr(svc, "ALLOW_SYNTHETIC_FALLBACK", None) is False
assert callable(_centroid)
print("[repair] compat guards ok: ALLOW_SYNTHETIC_FALLBACK=False, inland private helpers exported")
PY
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR" 2>/dev/null || true
systemctl daemon-reload
if [[ "${GFS_CLEAR_CACHE_ON_REPAIR:-1}" == "1" && -x "$APP_DIR/scripts/clear_gfs_runtime_cache.sh" ]]; then
  APP_USER="$APP_USER" APP_GROUP="$APP_GROUP" bash "$APP_DIR/scripts/clear_gfs_runtime_cache.sh" || true
fi
systemctl reset-failed broadcast.service 2>/dev/null || true
systemctl enable broadcast.service
systemctl restart broadcast.service
systemctl status broadcast.service --no-pager

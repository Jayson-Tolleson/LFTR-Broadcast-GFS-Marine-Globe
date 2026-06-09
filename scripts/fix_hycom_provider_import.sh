#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"
HYCOM="$APP_DIR/server/gfs/providers/hycom.py"

echo "[hycom-import-fix] patching $HYCOM"
if [ ! -f "$HYCOM" ]; then
  echo "[hycom-import-fix] ERROR: missing $HYCOM" >&2
  exit 1
fi

sudo tee "$HYCOM" >/dev/null <<'PY'
"""HYCOM ocean provider facade."""

from __future__ import annotations

from server.gfs.providers.rtofs import RtofsProvider as _LegacyRtofsProvider


class HycomProvider(_LegacyRtofsProvider):
    """First-class HYCOM provider facade around the legacy implementation."""

    provider_name = "hycom"
    provider_contract = "hycom_first_class_ocean_provider_sst_sss_ssu_ssv"


RtofsProvider = HycomProvider
OceanProvider = HycomProvider
PY

sudo chown jayson_tolleson:jayson_tolleson "$HYCOM" 2>/dev/null || true

echo "[hycom-import-fix] compile/import sanity"
cd "$APP_DIR"
./venv/bin/python - <<'PY'
from server.gfs.providers.hycom import HycomProvider, OceanProvider
print("ok", HycomProvider, OceanProvider)
PY

echo "[hycom-import-fix] restart"
sudo systemctl restart broadcast
sleep 3
journalctl -u broadcast -n 80 --no-pager | grep -Ei 'ImportError|HycomProvider|startup ready|Running on|health|Traceback' || true

#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

echo "[hycom-import-health] python import"
python3 - <<'PY'
from server.gfs.providers.hycom import HycomProvider, OceanProvider
p = HycomProvider()
print("provider", getattr(p, "provider_name", None), getattr(p, "provider_contract", None))
PY

echo
echo "[hycom-import-health] service health"
curl -fsS --max-time 10 "$BASE/health" || curl -fsS --max-time 10 "$BASE/api/health" || true

echo
echo "[hycom-import-health] recent journal"
journalctl -u broadcast -n 160 --no-pager | grep -Ei 'ImportError|circular import|HycomProvider|startup ready|Running on|Traceback|health' || true

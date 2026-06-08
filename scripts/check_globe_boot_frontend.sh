#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

echo "[globe-boot] service health"
curl -fsS --max-time 10 "$BASE/health" || curl -fsS --max-time 10 "$BASE/api/health" || true

echo
echo "[globe-boot] JS syntax check"
if command -v node >/dev/null 2>&1; then
  find static/js/gfs -name '*.js' -print0 | xargs -0 -n1 node --check
else
  echo "node not installed; skipping JS syntax check"
fi

echo
echo "[globe-boot] layer lazy-import markers"
grep -nE 'lazy layer import|globe_boot_continues|renderBoatsLayerSafe|renderInlandWaterLayerSafe' static/js/gfs/main.js || true

echo
echo "[globe-boot] recent journal"
journalctl -u broadcast -n 180 --no-pager | grep -Ei 'startup ready|Running on|ImportError|Traceback|health|hypercorn exited|HycomProvider' || true

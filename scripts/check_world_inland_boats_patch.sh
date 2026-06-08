#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[patch-check] boats normalizeDeg declarations"
grep -RIn "function normalizeDeg" static/js/gfs/boats.js || true
COUNT=$(grep -R "function normalizeDeg" static/js/gfs/boats.js | wc -l | tr -d ' ')
echo "normalizeDeg_count=$COUNT"
test "$COUNT" = "1"

echo

echo "[patch-check] world inland gate markers"
grep -RIn "world-layer-filter\|world_zoom_gate\|world_zoom_gated\|_filter_world_inland_layers" static/js/gfs/main.js server/gfs/routes.py server/gfs_service.py | head -80

echo

echo "[patch-check] syntax"
python3 -m py_compile server/gfs/routes.py server/gfs_service.py
node --check static/js/gfs/boats.js >/dev/null
node --check static/js/gfs/main.js >/dev/null
node --check static/js/gfs/world_subscription_renderer.js >/dev/null
echo "ok"

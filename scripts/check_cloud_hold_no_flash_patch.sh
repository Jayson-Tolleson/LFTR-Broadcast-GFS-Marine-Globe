#!/usr/bin/env bash
set -euo pipefail
cd "${APP_DIR:-$(pwd)}"
file="static/js/gfs/cloud-zones.js"
main="static/js/gfs/main.js"
echo "[cloud-hold-check] app=$(pwd)"
grep -n "cloudSceneStops\|registerCloudSceneStop\|stopAllCloudAdvection\|__gfsKeepExisting" "$file"
grep -n "valid_time/source_time/resolved_time\|__GFS_CLOUD_MIN_RENDER_INTERVAL_MS || 180000" "$main"
echo "[cloud-hold-check] syntax"
python3 -m py_compile scripts/run_hypercorn_single.py
echo "[cloud-hold-check] ok"

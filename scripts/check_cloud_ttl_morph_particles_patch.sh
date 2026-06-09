#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[cloud-patch] checking cloud morph/particle throttle patch"

grep -n "minRenderIntervalMs: Number(window.__GFS_CLOUD_MIN_RENDER_INTERVAL_MS || 90000)" static/js/gfs/main.js
grep -n "Ignore cache ttl/warmed_at/cache version churn" static/js/gfs/main.js
grep -n "function fadeInCloudElements" static/js/gfs/cloud-zones.js
grep -n "transitionScale" static/js/gfs/cloud-zones.js
grep -n "replacingExistingClouds" static/js/gfs/cloud-zones.js
grep -n "DESKTOP_MAX_CLOUD_PARTICLES.*1800" static/js/gfs/cloud-zones.js
grep -n "MOBILE_MAX_CLOUD_PARTICLES.*650" static/js/gfs/cloud-zones.js

python3 -m py_compile server/gfs/routes.py >/dev/null || true
node --check static/js/gfs/cloud-zones.js
node --check static/js/gfs/main.js

echo "[cloud-patch] OK"

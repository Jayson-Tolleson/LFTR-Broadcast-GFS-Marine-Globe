#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-$(pwd)}"
cd "$ROOT"
echo "[check] JS syntax"
for f in static/js/gfs/main.js static/js/gfs/world_subscription_renderer.js static/js/gfs/inland-water.js static/js/gfs/cloud-zones.js static/js/gfs/boats.js; do
  node --check "$f" >/dev/null
  echo "  ok $f"
done

echo "[check] Python compile"
python3 -m compileall -q server main.py scripts/*.py

echo "[check] inland overview policy markers"
grep -RIn "world/regional overview renders lake outlines" static/js/gfs/main.js >/dev/null
grep -RIn "largest-lake outline/temp per tile" server/gfs/routes.py >/dev/null
grep -RIn "world zoom samples largest closed lake" server/gfs_service_parts/lightning_cache_media.py >/dev/null

echo "[check] inland bait remains gated by payload flag"
grep -RIn "inland_bait_render_allowed" static/js/gfs/inland-water.js server/gfs_service_parts/lightning_cache_media.py server/gfs/routes.py >/dev/null

echo "[check] cloud morph/particle throttle markers"
grep -RIn "ttl/cache heartbeat" static/js/gfs/cloud-zones.js >/dev/null || grep -RIn "flash" static/js/gfs/cloud-zones.js >/dev/null
grep -RIn "cloudParticleGovernorCap" static/js/gfs/cloud-zones.js >/dev/null

echo "[check] HYCOM large-bbox no one-shot policy"
grep -RIn "no_large_one_shot_hycom_ncss" server/gfs_service_parts/ocean_bait_frame.py >/dev/null

echo "[check] runner single-process/static preflight"
grep -RIn "ensure_static_dir" scripts/run_broadcast_service.sh >/dev/null
grep -RIn "run_hypercorn_single" scripts/run_broadcast_service.sh >/dev/null

echo "[ok] overview/cloud/provider patch present"

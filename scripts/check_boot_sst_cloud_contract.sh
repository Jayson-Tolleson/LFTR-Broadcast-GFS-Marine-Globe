#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
echo "[boot-contract] force ocean layers"
curl -fsS --max-time 45 "$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world&layers=bait,boater,shark-intel&force=1&reason=manual_boot_sst_gate" | python3 -m json.tool | grep -Ei '"bait"|"boater"|"shark"|"scheduled"|"throttled"|"force_empty_repair"' || true
echo
echo "[boot-contract] cloud/boat/bait frame"
curl -fsS --max-time 120 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=clouds,rain,bait,boater,shark-intel&mode=refresh&refresh=1&provider_jobs=1&reason=manual_boot_sst_cloud_contract" | python3 -m json.tool | grep -Ei '"max_cloud_shells"|"max_cloud_particles"|"validTime"|"valid_time"|"ocean_point_count"|"min_ocean_points_to_render"|"renderable_count_hint"|"source"|"status"|"sst"|"boats"|"polygons"|"points"|"cloud"'
echo
echo "[journal]"
journalctl -u broadcast -n 420 --no-pager | grep -Ei 'boats/(reconcile|preserve)|sstReadiness|oceanPointCount|hycom|clouds/render|maxCloudBodies|maxCloudParticles|rendered bodies|scene_cache_empty_boater|validTime' | tail -180 || true

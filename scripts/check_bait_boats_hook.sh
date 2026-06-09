#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
LAYERS="bait,boater"
echo "[hook-check] force refresh bait+boater"
curl -fsS --max-time 45 "$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world&layers=$LAYERS&force=1&reason=manual_empty_placeholder_repair" | python3 -m json.tool | grep -Ei '"bait"|"boater"|"scheduled"|"throttled"|"force_empty_repair"|"empty_placeholder"|"min_gap_seconds"'
echo
echo "[hook-check] fast scene frame"
curl -fsS --max-time 90 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=$LAYERS&mode=fast&refresh=0&provider_jobs=1&reason=manual_bait_boats_hook_check" | python3 -m json.tool | grep -Ei '"bait"|"boater"|"oceanPoints"|"ocean_point_count"|"ocean_points"|"bait_score"|"polygons"|"boats"|"points"|"source"|"status"'
echo
echo "[journal]"
journalctl -u broadcast -n 300 --no-pager | grep -Ei 'empty-repair|force_empty|scene-cache-(bait|boater)|hycom|bait/reconcile|boats/(reconcile|preserve)|oceanPointRows|ocean_point_count|throttled' | tail -120 || true

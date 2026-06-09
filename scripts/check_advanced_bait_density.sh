#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
echo "[advanced-bait-density] force bait"
curl -fsS --max-time 45 "$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world&layers=bait&force=1&reason=manual_advanced_bait_density_force" | python3 -m json.tool | grep -Ei '"bait"|"scheduled"|"throttled"|"force_empty_repair"' || true
echo
echo "[advanced-bait-density] scene frame"
curl -fsS --max-time 120 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=bait,boater,shark-intel&mode=refresh&refresh=1&provider_jobs=1&reason=manual_advanced_bait_density_scene" \
| python3 -m json.tool \
| grep -Ei '"advancedBaitRows"|"advanced_bait_rows"|"advanced_bait_row_count"|"dense_bait_field"|"bait_score"|"outer_polygons"|"inner_polygons"|"core_polygons"|"polygon_caps"|"derived_resolution_deg"|"detail_multiplier"|"grid_cap"|"ocean_point_count"|"shared_ocean_point_count"|"source"|"status"'
echo
echo "[journal]"
journalctl -u broadcast -n 420 --no-pager | grep -Ei 'bait/reconcile|advancedBaitRows|advanced_bait|dense_bait|live_hycom|bait_score|polygon_total|hycom' | tail -160 || true

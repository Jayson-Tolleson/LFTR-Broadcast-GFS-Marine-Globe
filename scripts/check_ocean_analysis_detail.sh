#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
LAYERS="bait,boater,shark-intel"

echo "[ocean-analysis] settings"
grep -R --line-number -Ei 'GFS_BOAT_COUNT|GFS_OCEAN_ANALYSIS|GFS_OCEAN_POINTS|GFS_HYCOM_.*STRIDE|GFS_ADVANCED_BAIT' .env .env.example deploy/templates/app.env.template deploy/lib/google.sh 2>/dev/null || true

echo
echo "[ocean-analysis] force live layers"
curl -fsS --max-time 45 "$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world&layers=$LAYERS&force=1&reason=manual_ocean_analysis_detail_force" \
| python3 -m json.tool \
| grep -Ei '"bait"|"boater"|"shark"|"scheduled"|"throttled"|"force_empty_repair"|"error"|"unavailable"' || true

echo
echo "[ocean-analysis] frame contract"
curl -fsS --max-time 140 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=$LAYERS&mode=refresh&refresh=1&provider_jobs=1&reason=manual_ocean_analysis_detail_check" \
| python3 -m json.tool \
| grep -Ei '"source"|"status"|"boat_count"|"count"|"max_boats"|"renderable_count_hint"|"oceanAnalysisPoints"|"ocean_analysis_point_count"|"ocean_analysis_points"|"finite_sst_point_count"|"analysis_step"|"points"|"advancedBaitRows"|"advanced_bait_row_count"|"bait_score"|"polygons"|"current_speed_kt"|"water_temp_f"|"sst"'

echo
echo "[ocean-analysis] recent journal"
journalctl -u broadcast -n 520 --no-pager \
| grep -Ei 'hycom|ocean subset|ocean_analysis|oceanAnalysisPoints|boats/reconcile|bait/reconcile|advancedBaitRows|advanced_bait|sstReadiness|renderable_count_hint|NameError|Permission denied' \
| tail -220 || true

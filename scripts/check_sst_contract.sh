#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
echo "[sst-contract] force bait boater shark"
curl -fsS --max-time 45 "$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world&layers=bait,boater,shark-intel&force=1&reason=manual_sst_contract_force" | python3 -m json.tool | grep -Ei '"bait"|"boater"|"shark"|"scheduled"|"throttled"|"force_empty_repair"'
echo
echo "[sst-contract] scene frame"
curl -fsS --max-time 120 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=bait,boater,shark-intel&mode=refresh&refresh=1&provider_jobs=1&reason=manual_sst_contract_scene" | python3 -m json.tool | grep -Ei '"source"|"status"|"sst"|"oceanPoints"|"ocean_point_count"|"points"|"boats"|"polygons"|"score_points"|"sst_land_mask"|"water_temp_f"|"current_speed_kt"|"sea_mask_contract"'
echo
echo "[journal]"
journalctl -u broadcast -n 300 --no-pager | grep -Ei 'hycom|sst|ocean subset|bait/reconcile|boats/reconcile|shark|oceanPointRows|ocean_point_count|No SST|waiting_for_sst' | tail -160 || true

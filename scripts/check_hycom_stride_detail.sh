#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"

echo "[hycom-stride-detail] settings"
grep -R --line-number -Ei 'GFS_HYCOM_.*STRIDE|GFS_HYCOM_DETAIL_PROFILE|GFS_OCEAN_POINTS' .env .env.example deploy/templates/app.env.template deploy/lib/google.sh 2>/dev/null || true

echo
echo "[hycom-stride-detail] frame ocean contract"
curl -fsS --max-time 160 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=bait,boater,shark-intel&mode=refresh&refresh=1&provider_jobs=1&reason=manual_hycom_stride_detail_check" \
| python3 -m json.tool \
| grep -Ei '"provider_stride"|"stride"|"hycom"|"sst_shape"|"grid_shape"|"oceanAnalysisPoints"|"ocean_analysis_point_count"|"finite_sst_point_count"|"analysis_step"|"advancedBaitRows"|"advanced_bait_row_count"|"boats"|"source"|"status"'

echo
echo "[hycom-stride-detail] recent HYCOM lines"
journalctl -u broadcast -n 520 --no-pager \
| grep -Ei 'hycom.*horizStride|ocean subset fetched|hycom.*timeout|download_failed|ocean_analysis|advancedBaitRows|boats/reconcile|bait/reconcile' \
| tail -220 || true

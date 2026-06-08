#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
echo "[boat-transform] settings"
grep -R --line-number -Ei 'GFS_BOAT_MODEL|GFS_BOAT_WATER|GFS_BOAT_GLYPH|GFS_BOAT_UNDERGLOW|GFS_BOAT_COUNT' .env .env.example deploy/templates/app.env.template deploy/lib/google.sh 2>/dev/null || true
echo
echo "[boat-transform] frame boater contract"
curl -fsS --max-time 120 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=boater&mode=refresh&refresh=1&provider_jobs=1&reason=manual_boat_transform_check" \
| python3 -m json.tool \
| grep -Ei '"boater"|"boats"|"source"|"status"|"oceanAnalysisPoints"|"ocean_point_count"|"ocean_analysis_point_count"|"current_speed_kt"|"water_temp_f"|"heading"|"dirDeg"'
echo
echo "[boat-transform] recent browser debug"
journalctl -u broadcast -n 420 --no-pager \
| grep -Ei 'boats/reconcile|modelTransform|water_hugging|boat-transform|oceanPointCount|sstReadiness|boater' \
| tail -180 || true

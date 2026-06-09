#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
LAYERS="bait,boater,shark-intel"

echo "[ocean-renderers] import sanity"
python3 - <<'PY'
import py_compile, pathlib, sys
bad=[]
for p in pathlib.Path('.').rglob('*.py'):
    if 'venv/' in str(p):
        continue
    try:
        py_compile.compile(str(p), doraise=True)
    except Exception as e:
        bad.append((str(p), str(e)))
if bad:
    print("[ocean-renderers] compile failures:")
    for p,e in bad[:20]:
        print(p, e)
    sys.exit(1)
print("[ocean-renderers] python compile ok")
PY

echo
echo "[ocean-renderers] force live ocean cache layers"
curl -fsS --max-time 45 "$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world&layers=$LAYERS&force=1&reason=manual_ocean_points_renderer_force" \
| python3 -m json.tool \
| grep -Ei '"bait"|"boater"|"shark"|"scheduled"|"throttled"|"force_empty_repair"|"error"|"unavailable"' || true

echo
echo "[ocean-renderers] frame contract"
curl -fsS --max-time 120 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=$LAYERS&mode=refresh&refresh=1&provider_jobs=1&reason=manual_ocean_points_renderer_check" \
| python3 -m json.tool \
| grep -Ei '"bait"|"boater"|"source"|"status"|"unavailable"|"error"|"advancedBaitRows"|"advanced_bait_row_count"|"bait_score"|"polygons"|"points"|"oceanPointCount"|"ocean_point_count"|"renderable_count_hint"|"validTime"|"sstReadiness"|"minOceanPoints"|"current_speed_kt"|"water_temp_f"'

echo
echo "[ocean-renderers] recent journal"
journalctl -u broadcast -n 420 --no-pager \
| grep -Ei 'NameError|import os|bait_live_required_unavailable|scene-cache-bait|hycom|ocean subset|boats/reconcile|bait/reconcile|oceanPointCount|sstReadiness|advancedBaitRows|Permission denied' \
| tail -180 || true

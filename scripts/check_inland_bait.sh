#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
Q="bbox=${BBOX}&visible_bbox=${BBOX}&scene_tier=world&source=auto&geometry=vector&lod=auto&cache=1&tile_cache=1&parallel=1&auto_build=0&max_tiles=96&reason=inland_bait_check"
python3 - <<PY
import json, sys, urllib.request
base=${BASE@Q}; q=${Q@Q}
for name,path in [
  ('inland_water', f'/gfs/api/inland-water?{q}'),
  ('inland_temp_bait', f'/gfs/api/inland-water-temp?bbox=${BBOX}&visible_bbox=${BBOX}&reason=inland_bait_check'),
  ('inland_bait_debug', f'/gfs/api/inland-bait?bbox=${BBOX}&visible_bbox=${BBOX}&live=0'),
]:
    url=base+path
    try:
        data=json.load(urllib.request.urlopen(url, timeout=25))
    except Exception as e:
        print(f'{name}: ERROR {e}')
        continue
    bait=data.get('bait') or data.get('inland_bait') or data
    print(f'\n{name}: ok={data.get("ok")} status={data.get("status")} source={data.get("source")}')
    print('  polygons=', len(data.get('polygons') or []), 'lines=', len(data.get('lines') or []), 'temp_points=', len(data.get('temperature_points') or data.get('tempLabels') or []))
    print('  bait_score=', len((bait or {}).get('bait_score') or data.get('bait_score') or []), 'targets=', len((bait or {}).get('targets') or data.get('bait_targets') or []), 'contract=', (bait or {}).get('contract') or data.get('contract'))
PY

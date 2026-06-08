#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
echo "[lftr] Checking ocean SST/current and bait scene for bbox=$BBOX"
echo
echo "== /gfs/api/ocean =="
TMP1=$(mktemp)
curl -fsS "$BASE/gfs/api/ocean?bbox=$BBOX" -o "$TMP1"
python3 - "$TMP1" <<'PYOCEAN'
import json, sys
p=json.load(open(sys.argv[1]))
grid=p.get('grid') or {}
meta=p.get('source_meta') or p.get('meta') or {}
sst=p.get('sst') or grid.get('sst') or []
u=p.get('current_u') or p.get('u') or grid.get('current_u') or []
v=p.get('current_v') or p.get('v') or grid.get('current_v') or []
def shape(a):
    return [len(a), len(a[0]) if a and isinstance(a[0], list) else 0]
print(json.dumps({
  'ok': p.get('ok'),
  'source': p.get('source'),
  'grid_shape': grid.get('grid_shape'),
  'sst_shape': shape(sst),
  'u_shape': shape(u),
  'v_shape': shape(v),
  'sst_source': meta.get('sst_source'),
  'current_source': meta.get('current_source'),
  'live_ncss_ok': meta.get('live_ncss_ok'),
}, indent=2))
PYOCEAN
rm -f "$TMP1"
echo
echo "== /gfs/api/bait-advanced =="
TMP2=$(mktemp)
curl -fsS "$BASE/gfs/api/bait-advanced?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=world" -o "$TMP2"
python3 - "$TMP2" <<'PYBAIT'
import json, sys
p=json.load(open(sys.argv[1]))
b=p.get('bait') or {}
rows=p.get('bait_score') or p.get('oceanPoints') or []
print(json.dumps({
  'ok': p.get('ok'),
  'source': p.get('source'),
  'payload_state': p.get('payload_state'),
  'polygons': len(b.get('polygons') or p.get('polygons') or []),
  'bait_score_rows': len(rows),
  'valid_time': p.get('valid_time'),
  'error': p.get('error'),
  'quality_policy': p.get('quality_policy'),
}, indent=2))
PYBAIT
rm -f "$TMP2"
echo
echo "== recent journal SST/cache warnings =="
journalctl -u broadcast -n 400 --no-pager | grep -Ei "sst_shape|hycom ncss raw|ocean subset fetched|ALLOW_SYNTHETIC_FALLBACK|_centroid|scene-cache-bait|scene-cache-boater|inland_water_temp" | tail -80 || true

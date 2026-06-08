#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
VISIBLE="${3:-$BBOX}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
enc_bbox=$("$PYTHON_BIN" - <<PY
import urllib.parse
print(urllib.parse.quote('${BBOX}', safe=''))
PY
)
enc_visible=$("$PYTHON_BIN" - <<PY
import urllib.parse
print(urllib.parse.quote('${VISIBLE}', safe=''))
PY
)
url_fast="${BASE%/}/gfs/api/scene-cache?bbox=${enc_bbox}&visible_bbox=${enc_visible}&layers=boater,bait&mode=fast&fast=1&refresh=0&reason=boater_bait_check_fast"
url_refresh="${BASE%/}/gfs/api/cache/refresh?bbox=${enc_bbox}&visible_bbox=${enc_visible}&layers=boater,bait&reason=boater_bait_check_refresh"
echo "== boater/bait fast cache =="
curl -fsS "$url_fast" | "$PYTHON_BIN" - <<'PY'
import json, sys
p=json.load(sys.stdin)
layers=p.get('layers') or {}
for name in ('boater','bait'):
    row=layers.get(name) or {}
    bait=row.get('bait') or {}
    src=row.get('source') or bait.get('source')
    meta=row.get('source_meta') or {}
    lm=row.get('sst_landmask') or meta.get('sst_landmask') or (bait.get('meta') or {}).get('sst_landmask') or {}
    print(f"{name}: ok={row.get('ok')} state={row.get('payload_state') or row.get('status')} source={src}")
    if name=='boater':
        print(f"  boats={len(row.get('boats') or [])} points={len(row.get('points') or row.get('ocean_points') or [])} renderable_hint={row.get('renderable_count_hint')} rejections={(row.get('grid') or {}).get('rejection_counts')}")
    else:
        print(f"  polygons={len(bait.get('polygons') or [])} outer={len(bait.get('outer_polygons') or [])} inner={len(bait.get('inner_polygons') or [])} core={len(bait.get('core_polygons') or [])} scores={len(row.get('bait_score') or [])}")
    print(f"  landmask={lm} contract={row.get('landmask_contract') or meta.get('landmask_contract') or (bait.get('meta') or {}).get('landmask_contract')}")
PY

echo "== schedule refresh =="
curl -fsS "$url_refresh" | "$PYTHON_BIN" -m json.tool | sed -n '1,120p'
echo "== journal follow-up =="
echo "journalctl -u broadcast -n 400 --no-pager | grep -Ei 'scene-cache-(boater|bait)|hycom|sst_landmask|landmask|bait_score|boats'"

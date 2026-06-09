#!/usr/bin/env bash
set -euo pipefail
BASE_URL="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
OUT_DIR="${TMPDIR:-/tmp}/lftr_bait_boater_diag"
mkdir -p "$OUT_DIR"

fetch_json() {
  local name="$1"
  local url="$2"
  local out="$OUT_DIR/$name.json"
  local code
  code=$(curl -sS -L --max-time 45 -w '%{http_code}' -o "$out" "$url" || true)
  echo "endpoint=$name http_status=$code url=$url"
  if [[ "$code" != 2* ]]; then
    echo "ERROR endpoint=$name unavailable_or_non_2xx status=$code body=$(head -c 120 "$out" 2>/dev/null || true)" >&2
    return 2
  fi
  python3 - <<PY "$out" "$name"
import json,sys
p=sys.argv[1]; name=sys.argv[2]
s=open(p,encoding='utf-8',errors='replace').read()
if '<html' in s[:300].lower():
    raise SystemExit(f'ERROR endpoint={name} returned HTML')
json.loads(s)
print(f'json_ok endpoint={name}')
PY
}

BAIT_URL="$BASE_URL/gfs/api/bait?bbox=$BBOX&visible_bbox=$BBOX&mode=refresh&refresh=1&reason=manual_bait_boater_diag"
BOATS_URL="$BASE_URL/gfs/api/boats?bbox=$BBOX&visible_bbox=$BBOX&mode=refresh&refresh=1&reason=manual_bait_boater_diag"
FIELD_URL="$BASE_URL/gfs/api/field?field=current&bbox=$BBOX&visible_bbox=$BBOX&reason=manual_bait_boater_diag"
OCEAN_URL="$BASE_URL/gfs/api/ocean?bbox=$BBOX&visible_bbox=$BBOX&reason=manual_bait_boater_diag"

fetch_json bait "$BAIT_URL"
fetch_json boats "$BOATS_URL"
fetch_json field_current "$FIELD_URL" || true
fetch_json ocean "$OCEAN_URL" || true

python3 - <<'PY' "$OUT_DIR"
import json, math, sys
from pathlib import Path
root=Path(sys.argv[1])
def load(n):
    p=root/f'{n}.json'
    return json.loads(p.read_text()) if p.exists() else {}
def finite(v):
    try: f=float(v)
    except Exception: return False
    return math.isfinite(f)
def path_stats(payload):
    bait=payload.get('bait') if isinstance(payload.get('bait'),dict) else {}
    polys=[]
    for src in (payload,bait):
        for k in ('polygons','zones','inner_polygons','outer_polygons','core_polygons'):
            if isinstance(src.get(k),list): polys.extend(src[k])
    invalid=0; pts=0
    for poly in polys:
        path=poly.get('path') if isinstance(poly,dict) else None
        if not isinstance(path,list) or len(path)<3:
            invalid+=1; continue
        for p in path:
            pts+=1
            if not isinstance(p,dict) or not finite(p.get('lat')) or not finite(p.get('lng',p.get('lon'))): invalid+=1
    return len(polys), pts, invalid
bait=load('bait'); boats=load('boats'); ocean=load('ocean'); field=load('field_current')
polys,path_pts,bad_poly=path_stats(bait)
boat_rows=boats.get('boats') if isinstance(boats.get('boats'),list) else []
bad_boats=sum(1 for b in boat_rows if not isinstance(b,dict) or not finite(b.get('lat')) or not finite(b.get('lon',b.get('lng'))))
wave_null=0
for b in boat_rows:
    w=b.get('waves') if isinstance(b,dict) and isinstance(b.get('waves'),dict) else {}
    if not finite(w.get('sigHeightFt', w.get('waveHeightFt'))): wave_null+=1
bdiag=bait.get('diagnostics') if isinstance(bait.get('diagnostics'),dict) else {}
odiag=ocean.get('diagnostics') if isinstance(ocean.get('diagnostics'),dict) else {}
valid_points=int(bait.get('valid_ocean_point_count') or bait.get('water_mask_count') or bdiag.get('valid_ocean_point_count') or bdiag.get('water_mask_count') or len(ocean.get('points') or []) or len(field.get('points') or []) or 0)
print('summary bait=', {'ok': bait.get('ok'), 'incomplete': bait.get('incomplete'), 'stale': (bait.get('cache') or {}).get('mode'), 'source': bait.get('source'), 'valid_points': valid_points, 'water_mask_count': bait.get('water_mask_count') or bdiag.get('water_mask_count'), 'polygon_count': polys, 'path_points': path_pts, 'invalid_coords': bad_poly, 'cache': bait.get('cache')})
print('summary boats=', {'ok': boats.get('ok'), 'incomplete': boats.get('incomplete'), 'stale': (boats.get('cache') or {}).get('mode'), 'source': boats.get('source'), 'boats': len(boat_rows), 'invalid_coords': bad_boats, 'wave_null_count': wave_null, 'diagnostics': boats.get('diagnostics')})
print('summary field_current=', {'ok': field.get('ok'), 'source': field.get('source'), 'points': len(field.get('points') or field.get('current_points') or [])})
print('summary ocean=', {'ok': ocean.get('ok'), 'source': ocean.get('source'), 'points': len(ocean.get('points') or []), 'cache': ocean.get('cache')})
if bad_poly or bad_boats: raise SystemExit('invalid coordinate contract')
if bait.get('ok') is True and valid_points > 0 and polys == 0: raise SystemExit('bait ok true but zero polygons despite valid ocean cells')
ocean_cells=len(ocean.get('points') or []) or len(field.get('points') or field.get('current_points') or [])
if boats.get('ok') is True and ocean_cells > 0 and len(boat_rows) == 0: raise SystemExit('boats ok true but zero boats despite ocean cells')
PY

if command -v journalctl >/dev/null 2>&1; then
  if journalctl -u broadcast.service -n 300 --no-pager 2>/dev/null | grep -Ei 'cannot convert float NaN|NaN-to-int|serialization error|Traceback' >/tmp/lftr_bait_boater_log_errors.txt; then
    cat /tmp/lftr_bait_boater_log_errors.txt >&2
    exit 4
  fi
fi

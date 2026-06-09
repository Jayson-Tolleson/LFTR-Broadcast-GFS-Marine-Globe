#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
VISIBLE="${3:-$BBOX}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
q() { "$PYTHON_BIN" -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
EBBOX=$(q "$BBOX")
EVIS=$(q "$VISIBLE")

echo "[lftr] SST truth check bbox=$BBOX visible=$VISIBLE"
echo
fetch_json() {
  local url="$1"
  local tmp
  tmp=$(mktemp)
  if ! curl -fsS "$url" -o "$tmp"; then
    echo "curl_failed url=$url" >&2
    rm -f "$tmp"
    return 1
  fi
  "$PYTHON_BIN" - "$tmp" <<'PY'
import json, sys, math
p=json.load(open(sys.argv[1]))

def shape(a):
    if isinstance(a, list):
        return [len(a), len(a[0]) if a and isinstance(a[0], list) else 0]
    return None

def finite_count(a, cap=200000):
    n=0; seen=0
    if not isinstance(a, list):
        return None
    for row in a:
        vals = row if isinstance(row, list) else [row]
        for v in vals:
            seen += 1
            try:
                if math.isfinite(float(v)):
                    n += 1
            except Exception:
                pass
            if seen >= cap:
                return n
    return n

if 'cache' in p and isinstance(p.get('cache'), dict):
    layers=(p.get('cache') or {}).get('layers') or p.get('layers') or {}
    out={'schema':p.get('schema'),'bbox':p.get('bbox'),'layers':{}}
    for name,row in layers.items():
        meta=row.get('source_meta') if isinstance(row, dict) else {}
        bait=row.get('bait') if isinstance(row, dict) else {}
        bmeta=(bait or {}).get('meta') if isinstance(bait, dict) else {}
        out['layers'][name]={
            'status': row.get('status') if isinstance(row, dict) else None,
            'source': row.get('source') if isinstance(row, dict) else None,
            'cache_hit': row.get('cache_hit') if isinstance(row, dict) else None,
            'age_sec': row.get('age_sec') if isinstance(row, dict) else None,
            'sst_source': (meta or {}).get('sst_source') or (bmeta or {}).get('sst_source'),
            'current_source': (meta or {}).get('current_source') or (bmeta or {}).get('current_source'),
            'landmask_contract': row.get('landmask_contract') if isinstance(row, dict) else None or (meta or {}).get('landmask_contract') or (bmeta or {}).get('landmask_contract'),
            'sst_landmask': row.get('sst_landmask') if isinstance(row, dict) else None or (meta or {}).get('sst_landmask') or (bmeta or {}).get('sst_landmask'),
            'bait_score': len(row.get('bait_score') or []) if isinstance(row, dict) else 0,
            'polygons': len((bait or {}).get('polygons') or row.get('polygons') or []) if isinstance(row, dict) else 0,
            'boats': len(row.get('boats') or []) if isinstance(row, dict) else 0,
            'points': len(row.get('points') or row.get('ocean_points') or []) if isinstance(row, dict) else 0,
        }
    print(json.dumps(out, indent=2, sort_keys=True))
else:
    grid=p.get('grid') or {}
    meta=p.get('source_meta') or p.get('meta') or {}
    sst=p.get('sst') or grid.get('sst') or grid.get('sea_surface_temperature') or []
    u=p.get('current_u') or p.get('u') or grid.get('current_u') or grid.get('u') or []
    v=p.get('current_v') or p.get('v') or grid.get('current_v') or grid.get('v') or []
    out={
        'ok':p.get('ok'),
        'source':p.get('source'),
        'payload_state':p.get('payload_state'),
        'sst_shape':shape(sst),
        'sst_finite_sample_count':finite_count(sst),
        'u_shape':shape(u),
        'v_shape':shape(v),
        'sst_source':meta.get('sst_source'),
        'current_source':meta.get('current_source'),
        'landmask_contract':meta.get('landmask_contract') or p.get('landmask_contract'),
        'sst_landmask':meta.get('sst_landmask') or p.get('sst_landmask'),
        'grid_shape':grid.get('grid_shape'),
        'error':p.get('error'),
    }
    print(json.dumps(out, indent=2, sort_keys=True))
PY
  rm -f "$tmp"
}

echo "== direct /gfs/api/ocean =="
fetch_json "${BASE%/}/gfs/api/ocean?bbox=${EBBOX}&visible_bbox=${EVIS}"
echo

echo "== scene-frame bait/boater/shark-intel refresh =="
fetch_json "${BASE%/}/gfs/api/scene-frame?bbox=${EBBOX}&visible_bbox=${EVIS}&layers=bait,boater,shark-intel&mode=refresh&refresh=1&provider_jobs=1&reason=sst_truth_check"
echo

echo "== scene-cache bait/boater/shark-intel fast read =="
fetch_json "${BASE%/}/gfs/api/scene-cache?bbox=${EBBOX}&visible_bbox=${EVIS}&layers=bait,boater,shark-intel&mode=fast&fast=1&refresh=0&reason=sst_truth_check_fast"
echo

echo "== recent SST journal lines =="
journalctl -u broadcast -n 500 --no-pager | grep -Ei 'hycom ncss raw|ocean subset fetched|sst_shape|sst_landmask|landmask_contract|scene-cache-(bait|boater|shark)|ALLOW_SYNTHETIC_FALLBACK|_centroid' | tail -100 || true

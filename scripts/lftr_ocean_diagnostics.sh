#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
BBOX="${2:--118.75,32.85,-117.15,34.35}"
LAYERS="${3:-bait,shark-intel,boater}"

echo "LFTR ocean diagnostics"
echo "BASE_URL=$BASE_URL"
echo "BBOX=$BBOX"
echo "LAYERS=$LAYERS"
echo

echo "1) Config"
curl -sS "$BASE_URL/gfs/api/config" | python3 -m json.tool | head -120 || true
echo

echo "2) Scene frame fast cache read"
curl -sS "$BASE_URL/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=$LAYERS&mode=fast&refresh=0&provider_jobs=1&reason=manual_ocean_diag_fast" \
  | tee /tmp/lftr_ocean_diag_fast.json \
  | python3 -m json.tool | head -220 || true
echo

echo "3) Scene frame refresh"
curl -sS "$BASE_URL/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=$LAYERS&mode=refresh&refresh=1&provider_jobs=1&reason=manual_ocean_diag_refresh" \
  | tee /tmp/lftr_ocean_diag_refresh.json \
  | python3 -m json.tool | head -260 || true
echo

echo "4) Compact layer summary"
python3 - <<'PY'
import json
from pathlib import Path

for path in ["/tmp/lftr_ocean_diag_fast.json", "/tmp/lftr_ocean_diag_refresh.json"]:
    print("\n==", path)
    try:
        data = json.loads(Path(path).read_text())
    except Exception as e:
        print("cannot parse:", e)
        continue

    layers = (((data.get("cache") or {}).get("layers")) or data.get("layers") or {})
    for name, layer in layers.items():
        if not isinstance(layer, dict):
            continue
        print(name, {
            "status": layer.get("status"),
            "source": layer.get("source"),
            "points": layer.get("points"),
            "polygons": layer.get("polygons"),
            "boats": layer.get("boats"),
            "cache_hit": (layer.get("cache") or {}).get("hit") if isinstance(layer.get("cache"), dict) else layer.get("cache_hit"),
            "version": (layer.get("cache") or {}).get("version") if isinstance(layer.get("cache"), dict) else layer.get("version"),
            "bbox": layer.get("bbox"),
            "resolution": layer.get("resolution"),
        })
PY

echo
echo "5) Journal hints"
echo "Run this separately on server:"
echo "journalctl -u broadcast.service -n 400 --no-pager | egrep -i 'ocean|hycom|rtofs|sst|current|bait|boater|shark|wide|tile|provider|timeout|points|unavailable'"

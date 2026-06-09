#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"
TIER="${3:-world}"

echo "[debug-contract] scene-frame $BBOX $TIER"
curl -fsS --max-time 90 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=clouds,bait,boater,shark-intel,inland_water_temp&mode=fast&refresh=0&provider_jobs=1&reason=debug_contract_check" \
| python3 - <<'PY'
import json,sys
p=json.load(sys.stdin)
layers=(p.get("cache") or {}).get("layers") or {}
for name in ("bait","boater","clouds","shark-intel","inland_water_temp"):
    layer=layers.get(name) or {}
    print("\n==", name, "==")
    for k in ("status","source","cache_hit","age_sec"):
        print(k, ":", layer.get(k))
    if name=="bait":
        b=(p.get("bait") or p.get("payloads",{}).get("bait") or {})
        bait=b.get("bait") or b
        print("outer/inner/core:", len(bait.get("outer_polygons") or []), len(bait.get("inner_polygons") or []), len(bait.get("core_polygons") or []))
        print("score rows:", len(b.get("bait_score") or bait.get("bait_score") or []))
        meta=bait.get("meta") or {}
        print("meta:", {x:meta.get(x) for x in ("polygon_total","renderer","depth_policy","valid_cells")})
    if name=="boater":
        print("boats:", len((p.get("boats") or {}).get("boats") or []), "points:", len((p.get("boats") or {}).get("points") or []))
PY

echo
echo "[debug-contract] recent browser-facing journal lines"
journalctl -u broadcast -n 250 --no-pager | grep -Ei 'bait/reconcile|boats/reconcile|clouds/render|current-zones|scene-cache/apply|cloudBodyBudgetSource|advanced-bait-depth' | tail -80 || true

#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--119.2,33.3,-117.6,34.4}"
TIER="${3:-local}"

echo "[hycom-check] bbox=$BBOX tier=$TIER"
echo

URL="$BASE/gfs/api/provider-tiles?bbox=$BBOX&providers=hycom&urls=1&limit=4"
echo "[provider-tiles] $URL"
RESP="$(curl -fsS --max-time 90 "$URL" || true)"
printf '%s\n' "$RESP" | python3 - <<'PY'
import json,sys,math
text=sys.stdin.read()
if not text.strip():
    print("[provider-tiles] EMPTY/FAILED")
    raise SystemExit(0)
try:
    payload=json.loads(text)
except Exception:
    print(text[:4000]); raise
print(json.dumps(payload, indent=2)[:16000])
def walk(x):
    if isinstance(x, dict):
        sm=x.get("source_meta")
        if isinstance(sm, dict):
            q=sm.get("quality_gate") or {}
            print("\n[HYCOM SUMMARY]")
            for k in ("live_ocean_truth_ok","has_sst","has_current","hycom_sst_status","hycom_current_status","blocking_reasons","hycom_variable_contract","selected_attempt","selected_dataset","selected_vars","selected_u_var","selected_v_var","grid_shape","lat_values_len","lon_values_len","hycom_slices","opened_urls"):
                print(f"{k}: {sm.get(k, q.get(k))}")
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
walk(payload)
PY

echo
SC="$BASE/gfs/api/scene-cache?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=$TIER&layers=bait,shark-intel,boater&mode=fast&fast=1&refresh=0&reason=hycom_truth_check"
echo "[scene-cache-fast] $SC"
curl -fsS --max-time 45 "$SC" | python3 -m json.tool | grep -Ei '"status"|"source"|"cache_hit"|"warming"|"bait"|"shark"|"boater"|"sst"|"current"|"hycom"|"polygons"|"blocking_reasons"|"live_ocean_truth_ok"' || true

echo
RF="$BASE/gfs/api/cache/refresh?bbox=$BBOX&visible_bbox=$BBOX&scene_tier=$TIER&layers=bait,shark-intel,boater&reason=hycom_truth_force_refresh"
echo "[refresh] $RF"
curl -fsS --max-time 45 "$RF" | python3 -m json.tool || true

echo
echo "[journal hint]"
echo "journalctl -u broadcast -n 300 --no-pager | grep -Ei 'hycom|sst|current|bait|shark|boater|blocking_reasons|selected_attempt|ncss'"

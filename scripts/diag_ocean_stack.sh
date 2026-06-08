#!/usr/bin/env bash
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
SCENE_TIER="${3:-world}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
UNIT="${UNIT:-broadcast}"
OUT_DIR="${OUT_DIR:-/tmp/lftr_ocean_diag_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

urlencode() {
  "$PYTHON_BIN" - "$1" <<'PY'
import sys, urllib.parse
print(urllib.parse.quote(sys.argv[1], safe=''))
PY
}

BBOX_Q=$(urlencode "$BBOX")
VISIBLE_Q="$BBOX_Q"
BASE="${BASE%/}"
LAYERS="bait,boater,shark-intel"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
fetch(){
  local name="$1" url="$2" timeout_s="${3:-90}"
  local path="$OUT_DIR/$name.json"
  echo "GET $url" >&2
  if curl -fsS --max-time "$timeout_s" "$url" -o "$path"; then
    echo "$path"
  else
    echo "[diag] request failed: $url" >&2
    echo "{}" > "$path"
    echo "$path"
  fi
}

summarize_json(){
  local path="$1" mode="$2"
  "$PYTHON_BIN" - "$path" "$mode" <<'PY'
import json, sys, math
path, mode = sys.argv[1], sys.argv[2]
try:
    p=json.load(open(path))
except Exception as e:
    print({"error": f"json_read_failed: {e}"})
    raise SystemExit(0)

def get(d, *keys, default=None):
    cur=d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur=cur[k]
    return cur

def arr_len(x):
    return len(x) if isinstance(x, list) else 0

def shape(a):
    if isinstance(a, list):
        return [len(a), len(a[0]) if a and isinstance(a[0], list) else 0]
    return [0,0]

def compact(x):
    print(json.dumps(x, indent=2, sort_keys=True))

if mode == "ocean":
    grid = p.get("grid") or {}
    meta = p.get("source_meta") or p.get("meta") or {}
    sst = p.get("sst") or grid.get("sst") or grid.get("sst_f") or []
    u = p.get("current_u") or p.get("u") or grid.get("current_u") or grid.get("u") or []
    v = p.get("current_v") or p.get("v") or grid.get("current_v") or grid.get("v") or []
    pts = p.get("points") or p.get("ocean_points") or p.get("samples") or []
    compact({
        "ok": p.get("ok"), "status": p.get("status"), "source": p.get("source"),
        "grid_shape": grid.get("grid_shape") or p.get("grid_shape"),
        "sst_shape": shape(sst), "current_u_shape": shape(u), "current_v_shape": shape(v),
        "points": arr_len(pts), "valid_time": p.get("valid_time") or meta.get("valid_time"),
        "sst_source": meta.get("sst_source") or meta.get("source_sst"),
        "current_source": meta.get("current_source") or meta.get("source_current"),
        "live_ncss_ok": meta.get("live_ncss_ok"), "error": p.get("error") or meta.get("error"),
    })
elif mode == "bait_advanced":
    bait = p.get("bait") or p
    polygons = bait.get("polygons") or p.get("polygons") or []
    scores = p.get("bait_score") or bait.get("bait_score") or p.get("scores") or []
    pts = p.get("points") or p.get("ocean_points") or p.get("oceanPoints") or []
    compact({
        "ok": p.get("ok"), "status": p.get("status") or p.get("payload_state"), "source": p.get("source") or bait.get("source"),
        "polygons": arr_len(polygons), "bait_score_rows": arr_len(scores), "points": arr_len(pts),
        "valid_time": p.get("valid_time") or bait.get("valid_time"),
        "bbox": p.get("bbox") or bait.get("bbox"), "error": p.get("error") or bait.get("error"),
        "landmask": p.get("sst_landmask") or bait.get("sst_landmask") or (bait.get("meta") or {}).get("sst_landmask"),
    })
elif mode in ("frame", "scene_cache"):
    layers = get(p, "layers", default=None) or get(p, "cache", "layers", default={}) or {}
    out={"source": p.get("source"), "mode": p.get("mode") or get(p,"cache","mode"), "bbox": p.get("bbox"), "layers": {}}
    for name in ("bait", "boater", "shark-intel", "shark_intel"):
        row = layers.get(name) or {}
        b = row.get("bait") or {}
        pts = row.get("points") or row.get("ocean_points") or row.get("oceanPoints") or []
        boats = row.get("boats") or []
        scores = row.get("bait_score") or b.get("bait_score") or []
        polys = row.get("polygons") or b.get("polygons") or []
        if row:
            out["layers"][name]={
                "status": row.get("status") or row.get("payload_state"),
                "source": row.get("source") or b.get("source"),
                "cache_hit": row.get("cache_hit") if "cache_hit" in row else get(row,"cache","hit"),
                "age_sec": row.get("age_sec") if "age_sec" in row else get(row,"cache","age_sec"),
                "version": row.get("version") or get(row,"cache","version"),
                "points": arr_len(pts), "boats": arr_len(boats), "polygons": arr_len(polys), "bait_score_rows": arr_len(scores),
                "oceanPointCount": row.get("oceanPointCount") or row.get("ocean_point_count"),
                "sstReadiness": row.get("sstReadiness") or row.get("sst_readiness"),
                "error": row.get("error") or b.get("error"),
            }
    compact(out)
else:
    compact(p)
PY
}

say "LFTR ocean stack diagnostic"
echo "base=$BASE"
echo "bbox=$BBOX"
echo "scene_tier=$SCENE_TIER"
echo "out=$OUT_DIR"

say "1) Direct SST/current endpoint"
OCEAN_JSON=$(fetch ocean "$BASE/gfs/api/ocean?bbox=$BBOX_Q" 90)
summarize_json "$OCEAN_JSON" ocean

say "2) Direct bait advanced endpoint"
BAIT_JSON=$(fetch bait_advanced "$BASE/gfs/api/bait-advanced?bbox=$BBOX_Q&visible_bbox=$VISIBLE_Q&scene_tier=$SCENE_TIER&mode=refresh&refresh=1&reason=manual_ocean_diag_bait_advanced" 120)
summarize_json "$BAIT_JSON" bait_advanced

say "3) Fast scene cache for bait/boater/shark-intel"
SCENE_CACHE_JSON=$(fetch scene_cache_fast "$BASE/gfs/api/scene-cache?bbox=$BBOX_Q&visible_bbox=$VISIBLE_Q&scene_tier=$SCENE_TIER&layers=$LAYERS&mode=fast&fast=1&refresh=0&reason=manual_ocean_diag_fast_cache" 60)
summarize_json "$SCENE_CACHE_JSON" scene_cache

say "4) Force background cache refresh for ocean layers"
REFRESH_JSON=$(fetch cache_refresh "$BASE/gfs/api/cache/refresh?bbox=$BBOX_Q&visible_bbox=$VISIBLE_Q&scene_tier=$SCENE_TIER&layers=$LAYERS&force=1&reason=manual_ocean_diag_force_refresh" 90)
summarize_json "$REFRESH_JSON" raw

say "5) Refresh scene-frame compile/draw contract"
FRAME_JSON=$(fetch scene_frame_refresh "$BASE/gfs/api/scene-frame?bbox=$BBOX_Q&visible_bbox=$VISIBLE_Q&scene_tier=$SCENE_TIER&layers=$LAYERS&mode=refresh&refresh=1&provider_jobs=1&reason=manual_ocean_diag_scene_frame" 180)
summarize_json "$FRAME_JSON" frame

say "6) Recent journal clues"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u "$UNIT" -n 900 --no-pager > "$OUT_DIR/journal_tail.txt" 2>/dev/null || true
  grep -Ei 'hycom|rtofs|sst|current|bait_advanced|bait-advanced|bait_live_required_unavailable|scene-cache-bait|scene_cache:bait|boater|scene-cache-boater|ocean_live_required_unavailable|waiting_for_sst_points|oceanPointCount|sstReadiness|landmask|provider_empty|NaN|Traceback|ERROR' "$OUT_DIR/journal_tail.txt" | tail -220 || true
else
  echo "journalctl unavailable"
fi

say "Saved raw diagnostic JSON"
ls -1 "$OUT_DIR"

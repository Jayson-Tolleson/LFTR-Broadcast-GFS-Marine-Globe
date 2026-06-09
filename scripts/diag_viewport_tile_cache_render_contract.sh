#!/usr/bin/env bash
set -u -o pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

BASE_URL="${BASE_URL%/}"
REQUEST_AREA="$(python3 - "$BBOX" <<'PY'
import sys
try:
    w,s,e,n=[float(x) for x in sys.argv[1].split(',')[:4]]
    if e < w:
        e += 360.0
    print(max(0.0, e-w)*max(0.0, n-s))
except Exception:
    print(0.0)
PY
)"

ENDPOINTS=(
  "/gfs/api/bait?bbox=${BBOX}"
  "/gfs/api/boats?bbox=${BBOX}"
  "/gfs/api/clouds?bbox=${BBOX}"
  "/gfs/api/field?field=current&bbox=${BBOX}"
  "/gfs/api/weather?bbox=${BBOX}"
)

fail=0
printf 'Viewport tile/cache/render diagnostic\n'
printf 'base=%s bbox=%s request_area_deg2=%s\n\n' "$BASE_URL" "$BBOX" "$REQUEST_AREA"

for idx in "${!ENDPOINTS[@]}"; do
  endpoint="${ENDPOINTS[$idx]}"
  out="$TMP_DIR/${idx}.json"
  body="$TMP_DIR/${idx}.body"
  url="${BASE_URL}${endpoint}"
  status="$(curl -fsS --max-time 20 -w '%{http_code}' -o "$body" "$url" 2>"$TMP_DIR/${idx}.err" || true)"
  if [[ -z "$status" ]]; then status="000"; fi
  if [[ "$status" != 2* ]]; then
    printf 'FAIL endpoint=%s http=%s error=%s\n' "$endpoint" "$status" "$(cat "$TMP_DIR/${idx}.err")"
    fail=1
    continue
  fi
  if ! python3 -m json.tool "$body" > "$out" 2>"$TMP_DIR/${idx}.jsonerr"; then
    printf 'FAIL endpoint=%s http=%s non_json=%s\n' "$endpoint" "$status" "$(head -c 120 "$body")"
    fail=1
    continue
  fi
  python3 - "$endpoint" "$status" "$out" "$REQUEST_AREA" <<'PY' || fail=1
import json, math, sys
endpoint, status, path, request_area = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4] or 0)
data=json.load(open(path))
diag=data.get('diagnostics') if isinstance(data.get('diagnostics'), dict) else {}

def count_valid_objects():
    if 'bait' in endpoint:
        return max(len(data.get('polygons') or []), len(data.get('zones') or []), int(data.get('polygon_count') or 0))
    if 'boats' in endpoint:
        return max(len(data.get('boats') or []), int(data.get('count') or 0))
    if 'field' in endpoint:
        return max(len(data.get('points') or []), len(data.get('current_points') or []), int(data.get('count') or 0))
    if 'clouds' in endpoint:
        return max(len(data.get('items') or []), len(data.get('tiles') or []), len(data.get('cloud_regions') or []), len(data.get('features') or []), int(data.get('count') or 0))
    return max(len(data.get('items') or []), len(data.get('features') or []), int(data.get('count') or 0))

def finite_lat_lon(lat, lon):
    try:
        lat=float(lat); lon=float(lon)
        return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    except Exception:
        return False
invalid=0
for poly in (data.get('polygons') or data.get('zones') or []):
    for pt in (poly.get('path') or []):
        if not finite_lat_lon(pt.get('lat'), pt.get('lng', pt.get('lon'))):
            invalid += 1
for boat in (data.get('boats') or []):
    if not finite_lat_lon(boat.get('lat'), boat.get('lng', boat.get('lon'))):
        invalid += 1
objects=count_valid_objects()
provider_area=float(diag.get('provider_bbox_max_area') or data.get('provider_bbox_max_area') or 0)
tiles_req=int(diag.get('tiles_requested') or data.get('tiles_requested') or 0)
tiles_hit=int(diag.get('tiles_hit') or 0)
tiles_missed=int(diag.get('tiles_missed') or 0)
tiles_fetched=int(diag.get('tiles_fetched') or 0)
provider_fetch=int(diag.get('provider_fetch_count') or 0)
ttl=int(diag.get('ttl_seconds') or data.get('ttl_seconds') or 0)
ok=data.get('ok')
incomplete=bool(data.get('incomplete') or diag.get('incomplete'))
stale=bool(data.get('stale') or diag.get('stale'))
cache_promotable=diag.get('cache_promotable')
print(f"endpoint={endpoint} http={status} ok={ok} incomplete={incomplete} stale={stale} bbox={data.get('bbox') or diag.get('bbox')} tiles_requested={tiles_req} tiles_hit={tiles_hit} tiles_missed={tiles_missed} tiles_fetched={tiles_fetched} provider_fetch_count={provider_fetch} provider_bbox_max_area={provider_area} valid_objects={objects} ttl_seconds={ttl} cache_promotable={cache_promotable} invalid_coords={invalid}")
errors=[]
if invalid:
    errors.append(f'invalid_coordinates={invalid}')
if ok is True and objects <= 0 and not incomplete:
    errors.append('ok_true_zero_objects_without_incomplete')
if provider_area and request_area and provider_area > (request_area + 1.0) * 1.25:
    errors.append(f'provider_bbox_too_large={provider_area}>{request_area}')
if diag.get('accepted_empty_cache_promotion') is True or diag.get('accepted_all_nan_cache_promotion') is True:
    errors.append('empty_or_all_nan_cache_promotion_reported')
if errors:
    print('FAIL '+endpoint+' '+' '.join(errors))
    raise SystemExit(1)
PY
done

if [[ "$fail" -ne 0 ]]; then
  printf '\nFAIL viewport tile/cache/render contract check failed. If this is a local workstation, ensure the Quart server is running before using this live diagnostic.\n'
  exit 1
fi
printf '\nPASS viewport tile/cache/render contract check completed.\n'

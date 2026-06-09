#!/usr/bin/env bash
set -euo pipefail

fail=0
warn=0
printf 'Checking for active wide/global GFS provider/render paths...\n'

check_fail() {
  local label="$1" pattern="$2"; shift 2
  local output
  output="$(rg -n --glob '!*.md' --glob '!*.txt' --glob '!*.zip' --glob '!scripts/check_no_wide_gfs_calls.sh' "$pattern" "$@" || true)"
  if [[ -n "$output" ]]; then
    printf 'FAIL %s\n%s\n' "$label" "$output"
    fail=1
  else
    printf 'PASS %s\n' "$label"
  fi
}

check_warn() {
  local label="$1" pattern="$2"; shift 2
  local output
  output="$(rg -n --glob '!*.md' --glob '!*.txt' --glob '!*.zip' --glob '!scripts/check_no_wide_gfs_calls.sh' "$pattern" "$@" || true)"
  if [[ -n "$output" ]]; then
    printf 'WARN %s\n%s\n' "$label" "$output"
    warn=1
  else
    printf 'PASS %s\n' "$label"
  fi
}

check_fail 'no active whole-world bbox literals in provider/server code' 'bbox\s*=\s*[^\n]*(\[-?180\s*,\s*-?90\s*,\s*180\s*,\s*90|west[^\n]*-180[^\n]*east[^\n]*180[^\n]*south[^\n]*-90[^\n]*north[^\n]*90)' server static/js/gfs
check_fail 'no global lake temperature fetch path in active code' 'global[_ -]?lake[_ -]?temp|lake_temp_global|GFS_GLOBAL_LAKE' server static/js/gfs
check_warn 'review fallback/retry text for wide expansion (comments/docs may appear here)' '(fallback|retry|expand)[^\n]*(global|world|continent|whole|180|90)' server/gfs server/gfs_service_parts static/js/gfs
check_warn 'review remaining hardcoded world constants (allowed only for clamps/docs/metadata)' '(-180\.0|180\.0|-90\.0|90\.0|-180,|180,|-90,|90,)' server/gfs server/gfs_service_parts static/js/gfs
check_warn 'review direct layer clearing paths; renderer should validate replacement first' '(clear|remove|dispose)[A-Za-z0-9_]*(Layer|Objects|Bodies|Polygons|Boats|Markers)|innerHTML\s*=\s*["'\''`]["'\''`]' static/js/gfs

python3 - <<'PY'
from server.gfs.tile_contract import (
    cache_promotable_payload,
    layer_ttl_seconds,
    normalize_bbox,
    split_dateline_bbox,
    split_viewport_tiles,
    viewport_tile_diagnostics,
)
assert normalize_bbox({'west': -126, 'south': 28, 'east': -114, 'north': 40}) == {'west': -126.0, 'south': 28.0, 'east': -114.0, 'north': 40.0}
assert len(split_dateline_bbox({'west': 170, 'south': -5, 'east': -170, 'north': 5})) == 2
assert len(split_viewport_tiles({'west': -126, 'south': 28, 'east': -114, 'north': 40}, grid=2)) == 4
assert layer_ttl_seconds('clouds') >= 600
assert cache_promotable_payload({'ok': True, 'points': []}, 'current') is False
assert cache_promotable_payload({'ok': True, 'points': [{'lat': float('nan'), 'lon': float('nan')}]}, 'current') is False
assert cache_promotable_payload({'ok': True, 'points': [{'lat': 33.5, 'lon': -119.0}]}, 'current') is True
assert viewport_tile_diagnostics(layer='current', bbox={'west': -126, 'south': 28, 'east': -114, 'north': 40}, payload={'ok': True, 'points': []})['incomplete'] is True
print('PASS viewport tile helper policy tests')
PY

if [[ "$fail" -ne 0 ]]; then
  printf 'FAIL active wide/global call guard failed.\n'
  exit 1
fi
if [[ "$warn" -ne 0 ]]; then
  printf 'PASS with warnings: review warnings above; no active fail patterns found.\n'
else
  printf 'PASS no suspicious active wide/global GFS call patterns found.\n'
fi

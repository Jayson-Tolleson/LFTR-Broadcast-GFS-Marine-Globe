#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
# default near Lake Havasu / Colorado corridor
BBOX="${2:--114.78,34.18,-114.46,34.64}"
echo "[inland-bait-style] raw inland bait"
curl -fsS --max-time 90 "$BASE/gfs/api/inland-bait?bbox=$BBOX&live=1" | python3 -m json.tool | grep -Ei '"source"|"renderer"|"style_contract"|"targets"|"bait_score"|"advancedBaitRows"|"advanced_bait_row_count"|"temperature_points"|"count"|"zone_count"'
echo
echo "[inland-bait-style] scene frame / inland-water diagnostics"
curl -fsS --max-time 120 "$BASE/gfs/api/inland-water?bbox=$BBOX&live=1&scene_tier=harbor&tile_cache=1&parallel=1&auto_build=1&max_tiles=24&reason=manual_inland_bait_style_check" | python3 -m json.tool | grep -Ei '"polygons"|"lines"|"temperature_points"|"bait"|"render"|"source"|"geometry_quality"|"diagnostics"'
echo
echo "[journal]"
journalctl -u broadcast -n 420 --no-pager | grep -Ei 'inland-bait|inland-water|marching-square|thermal|bait-thermal|positive extrusion|orange glow|advanced bait' | tail -180 || true

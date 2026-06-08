#!/usr/bin/env bash
set -euo pipefail

# Progressive, first-run Inland Waters runtime tile builder.
# It fetches the current viewport as small real USGS/NHD ArcGIS source squares,
# and after EACH successful source square it appends render tiles + rewrites
# index.json.  The web route can therefore draw partial inland water as soon as
# one useful tile completes; it does not wait for every source square.

BBOX="${1:-${NHDPLUS_VIEW_BBOX:-${NHDPLUS_INITIAL_BBOX:--126,29,-114,39}}}"
CACHE_DAYS="${NHDPLUS_CACHE_DAYS:-31}"
SRC_ROOT="${NHDPLUS_SRC_ROOT:-data_sources/nhd_runtime_cache}"
OUT_ROOT="${NHDPLUS_OUT_ROOT:-static/data/nhdplus_hr/tiles}"
ARCGIS_TILE_DEG="${NHD_ARCGIS_TILE_DEG:-0.25}"
ARCGIS_LAYERS="${NHD_ARCGIS_LAYERS:-12}"
PAGE_SIZE="${NHD_ARCGIS_PAGE_SIZE:-100}"
MAX_RECORDS="${NHD_ARCGIS_MAX_RECORDS_PER_TILE:-1800}"
NHD_MIN_POLYGON_POINTS="${NHD_MIN_POLYGON_POINTS:-3}"
NHD_MIN_LINE_POINTS="${NHD_MIN_LINE_POINTS:-2}"
NHD_MAX_POLYGONS_PER_TILE="${NHD_MAX_POLYGONS_PER_TILE:-5}"
NHD_MAX_LINES_PER_TILE="${NHD_MAX_LINES_PER_TILE:-8}"
NHDPLUS_GEOMETRY_MODE="${NHDPLUS_GEOMETRY_MODE:-vector}"
# Prominence filters: maximum detail geometry, rational render workload.
# These keep the largest waterbodies and longest rivers/streams per tile.
NHD_MIN_AREA_KM2_WORLD="${NHD_MIN_AREA_KM2_WORLD:-0.6475}"
NHD_MIN_AREA_KM2_REGIONAL="${NHD_MIN_AREA_KM2_REGIONAL:-0.25}"
NHD_MIN_AREA_KM2_LOCAL="${NHD_MIN_AREA_KM2_LOCAL:-0.0}"
NHD_MIN_AREA_KM2_HARBOR="${NHD_MIN_AREA_KM2_HARBOR:-0.0}"
NHD_MIN_LINE_KM_WORLD="${NHD_MIN_LINE_KM_WORLD:-8.0}"
NHD_MIN_LINE_KM_REGIONAL="${NHD_MIN_LINE_KM_REGIONAL:-3.0}"
NHD_MIN_LINE_KM_LOCAL="${NHD_MIN_LINE_KM_LOCAL:-0.8}"
NHD_MIN_LINE_KM_HARBOR="${NHD_MIN_LINE_KM_HARBOR:-0.20}"
export NHD_MIN_POLYGON_POINTS NHD_MIN_LINE_POINTS NHD_MAX_POLYGONS_PER_TILE NHD_MAX_LINES_PER_TILE NHDPLUS_GEOMETRY_MODE
NHD_ARCGIS_SOURCE_MIN_AREA_KM2="${NHD_ARCGIS_SOURCE_MIN_AREA_KM2:-0.6475}"
export NHD_MIN_AREA_KM2_WORLD NHD_MIN_AREA_KM2_REGIONAL NHD_MIN_AREA_KM2_LOCAL NHD_MIN_AREA_KM2_HARBOR NHD_ARCGIS_SOURCE_MIN_AREA_KM2
export NHD_MIN_LINE_KM_WORLD NHD_MIN_LINE_KM_REGIONAL NHD_MIN_LINE_KM_LOCAL NHD_MIN_LINE_KM_HARBOR
MAX_SOURCE_TILES="${NHD_ARCGIS_MAX_SOURCE_TILES:-${NHD_VIEW_MAX_SOURCE_TILES:-96}}"
PROGRESSIVE="${NHD_PROGRESSIVE_TILE_BUILD:-1}"
TOTAL_WRITTEN=0

parse_bbox() {
  IFS=',' read -r W S E N <<< "$1"
  python3 - <<PY
w=float('$W'); s=float('$S'); e=float('$E'); n=float('$N')
assert w < e and s < n, 'invalid bbox'
print(f'{w},{s},{e},{n}')
PY
}

bbox_intersects_py() {
  python3 - "$1" "$2" <<'PY'
import sys
aw,as_,ae,an=[float(x) for x in sys.argv[1].split(',')]
bw,bs,be,bn=[float(x) for x in sys.argv[2].split(',')]
print('1' if not (ae < bw or aw > be or an < bs or as_ > bn) else '0')
PY
}

# Viewport bbox is the contract. The builder writes directly into one shared runtime cache root.

plan_source_tiles() {
  python3 - "$1" "$2" "$3" <<'PYCODE'
import math, sys
bbox=sys.argv[1]
step=float(sys.argv[2])
limit=max(1, int(float(sys.argv[3] or '4')))
w,s,e,n=[float(x) for x in bbox.split(',')]
width=max(1e-9, e-w); height=max(1e-9, n-s)
tiles=[]
y=s
while y < n - 1e-9:
    y2=min(n, y+step)
    x=w
    while x < e - 1e-9:
        x2=min(e, x+step)
        tx=(x+x2)/2; ty=(y+y2)/2
        nx=min(0.999999,max(0.0,(tx-w)/width)); ny=min(0.999999,max(0.0,(ty-s)/height))
        bx=int(nx*4); by=int(ny*4)
        area=max(0.0,(x2-x)*(y2-y))
        tiles.append(((bx,by), area, x,y,x2,y2))
        x=x2
    y=y2
buckets={}
for key, area, x,y,x2,y2 in tiles:
    buckets.setdefault(key,[]).append((area,y,x,x2,y2))
for vals in buckets.values():
    vals.sort()
# Round-robin through a screen grid so the first 1-4 source squares cover the viewport,
# not only the bbox center / Central Valley cluster.
order=[]
for by in range(4):
    xs=range(4) if by%2==0 else range(3,-1,-1)
    for bx in xs:
        if (bx,by) in buckets:
            order.append((bx,by))
out=[]
while order and len(out)<limit:
    nxt=[]
    for key in order:
        vals=buckets.get(key) or []
        if vals and len(out)<limit:
            area,y,x,x2,y2=vals.pop(0)
            out.append((x,y,x2,y2))
        if vals:
            nxt.append(key)
    order=nxt
for x,y,x2,y2 in out:
    print(f"{x:.6f},{y:.6f},{x2:.6f},{y2:.6f}")
PYCODE
}

count_index_tiles() {
  python3 - "$1" <<'PY'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])/'index.json'
try:
    data=json.loads(p.read_text())
    print(len(data.get('tiles') or []))
except Exception:
    print(0)
PY
}

BBOX="$(parse_bbox "$BBOX")"
mkdir -p "$SRC_ROOT" "$OUT_ROOT"

echo "LFTR progressive Inland Waters view runtime tile builder"
echo "bbox=$BBOX"
echo "source_tile_deg=$ARCGIS_TILE_DEG cache_days=$CACHE_DAYS layers=$ARCGIS_LAYERS max_source_tiles=$MAX_SOURCE_TILES progressive=$PROGRESSIVE geometry_mode=$NHDPLUS_GEOMETRY_MODE min_poly=$NHD_MIN_POLYGON_POINTS min_line=$NHD_MIN_LINE_POINTS"
echo "source_rest_contract=MapServer/12/query f=geojson where=(FTYPE=390 OR FTYPE=436) plus optional AREASQKM>=NHD_ARCGIS_SOURCE_MIN_AREA_KM2 for world/regional; default thresholds world=0.6475 km2, regional=0.25 km2, local/harbor=0; local/harbor may set source min area to 0 for ponds/small lakes; outFields=OBJECTID,permanent_identifier,gnis_name,areasqkm,Shape_Area,ftype,fcode,resolution,visibilityfilter,elevation returnGeometry=true; returned polygon vertices are converted directly to app polygons"
echo "first-run speed policy=viewport-balanced round-robin source squares; first batch is 1-4 tiles; one successful square writes index.json immediately"
echo "render limits=max_polygons_per_tile=$NHD_MAX_POLYGONS_PER_TILE max_lines_per_tile=$NHD_MAX_LINES_PER_TILE min_area_km2(world/regional/local/harbor)=$NHD_MIN_AREA_KM2_WORLD/$NHD_MIN_AREA_KM2_REGIONAL/$NHD_MIN_AREA_KM2_LOCAL/$NHD_MIN_AREA_KM2_HARBOR min_line_km=$NHD_MIN_LINE_KM_WORLD/$NHD_MIN_LINE_KM_REGIONAL/$NHD_MIN_LINE_KM_LOCAL/$NHD_MIN_LINE_KM_HARBOR"
echo "policy=first usable source square writes/updates index.json immediately; coarse means simplified real USGS world overview, not mock fallback"

src="$SRC_ROOT/view_tiles"
out="$OUT_ROOT"
mkdir -p "$src" "$out"

echo ""
echo "=== viewport affected; progressive fetch/build bbox=$BBOX out=$out ==="

i=0
while IFS= read -r tb; do
  [[ -n "$tb" ]] || continue
  i=$((i+1))
  safe_tb="${tb//,/_}"
  source_file="$src/view_${safe_tb}_arcgis_nhd_large_scale.geojson.gz"
  before="$(count_index_tiles "$out")"
  echo "[progressive viewport $i/$MAX_SOURCE_TILES] fetch source square $tb"
  if python3 scripts/fetch_nhd_arcgis_bbox.py \
    --bbox="$tb" \
    --out="$source_file" \
    --tile-deg "$ARCGIS_TILE_DEG" \
    --layers "$ARCGIS_LAYERS" \
    --cache-days "$CACHE_DAYS" \
    --page-size "$PAGE_SIZE" \
    --max-records-per-layer-tile "$MAX_RECORDS" \
    --max-source-tiles 1; then
      if [[ -s "$source_file" ]]; then
        echo "[progressive viewport $i] append render tiles from $source_file"
        python3 scripts/build_nhdplus_hr_tiles.py \
          --source-geojson "$source_file" \
          --out "$out" \
          --bbox="$tb" \
          --append || true
        after="$(count_index_tiles "$out")"
        echo "[progressive viewport $i] index tiles before=$before after=$after out=$out/index.json"
        if [[ "$after" -gt "$before" ]]; then
          TOTAL_WRITTEN=$((TOTAL_WRITTEN + after - before))
        fi
      fi
  else
    echo "[progressive viewport $i] source square empty or failed; continuing"
  fi
done < <(plan_source_tiles "$BBOX" "$ARCGIS_TILE_DEG" "$MAX_SOURCE_TILES")

echo "[progressive] complete; newly written/updated render tiles estimate=$TOTAL_WRITTEN"
python3 scripts/check_nhdplus_hr_tile_cache.py || true

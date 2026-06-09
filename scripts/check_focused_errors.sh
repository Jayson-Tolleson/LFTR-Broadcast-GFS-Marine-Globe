#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"

echo "[focused-errors] compile sanity"
python3 - <<'PY'
import py_compile, pathlib, sys
bad=[]
for p in pathlib.Path('.').rglob('*.py'):
    if 'venv/' in str(p):
        continue
    try:
        py_compile.compile(str(p), doraise=True)
    except Exception as e:
        bad.append((str(p), str(e)))
if bad:
    for p,e in bad[:50]:
        print(p, e)
    sys.exit(1)
print("python compile ok")
PY

echo
echo "[focused-errors] frame quick check"
curl -fsS --max-time 120 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=bait,boater,inland_water_temp,inland-water&mode=refresh&refresh=1&provider_jobs=1&reason=manual_focused_errors_check" \
| python3 -m json.tool \
| grep -Ei '"source"|"status"|"error"|"unavailable"|"bait"|"advanced_bait_row_count"|"polygons"|"boats"|"ocean_analysis_point_count"|"renderable_count_hint"|"inland"'

echo
echo "[focused-errors] recent error lines"
journalctl -u broadcast -n 520 --no-pager \
| grep -Ei 'Traceback|NameError|os is not defined|render failed|inland-water.js|live bait grid solve failed|hycom.*timed out|download_failed|Permission denied|hypercorn exited' \
| tail -220 || true

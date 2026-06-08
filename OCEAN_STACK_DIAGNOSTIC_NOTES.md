# Ocean stack diagnostic

Run this from the deployed app root or from the unpacked zip:

```bash
chmod +x scripts/diag_ocean_stack.sh
scripts/diag_ocean_stack.sh http://127.0.0.1:8000 '-126,28,-114,40' world
```

Optional env vars:

```bash
UNIT=broadcast OUT_DIR=/tmp/lftr_ocean_diag PYTHON_BIN=python3 scripts/diag_ocean_stack.sh https://lftr.biz '-126,28,-114,40' world
```

It checks:

1. `/gfs/api/ocean` for SST/current grid shape, source, valid time, and point count.
2. `/gfs/api/bait-advanced` for bait polygons, scores, ocean points, landmask, and errors.
3. `/gfs/api/scene-cache` fast read for bait, boater, and shark-intel cache state.
4. `/gfs/api/cache/refresh` with `force=1` to kick the ocean layer warm path.
5. `/gfs/api/scene-frame` refresh compile/draw contract.
6. Recent `journalctl -u broadcast` clues for HYCOM/RTOFS/SST/current/bait/boater failures.

Raw JSON is saved into `/tmp/lftr_ocean_diag_*` by default.

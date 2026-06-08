#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--126,28,-114,40}"
echo "[check] ocean truth"
curl -fsS "$BASE/gfs/api/ocean?bbox=$BBOX&visible_bbox=$BBOX" | python3 -c "import json,sys; p=json.load(sys.stdin); meta=p.get('source_meta') or {}; print('ok=',p.get('ok'),'source=',p.get('source')); print('landmask_contract=',meta.get('landmask_contract')); print('sst_landmask=',meta.get('sst_landmask')); print('boats=',len(p.get('boats') or []),'points=',len(p.get('points') or [])); print('grid=',p.get('grid'))" || true
echo "[check] boater/bait cache errors"
journalctl -u broadcast -n 500 --no-pager | grep -Ei 'ALLOW_SYNTHETIC|_centroid|landmask_contract|sst_shape|scene-cache-(bait|boater|inland_water_temp)' | tail -100 || true

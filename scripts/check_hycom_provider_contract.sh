#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"

echo "[hycom-provider] python import"
python3 - <<'PY'
from server.gfs.providers.hycom import HycomProvider, OceanProvider
p = HycomProvider()
print("provider", getattr(p, "provider_name", None), getattr(p, "provider_contract", None))
print("ocean_alias", OceanProvider)
PY

echo
echo "[hycom-provider] source references"
grep -R --line-number -Ei 'HycomProvider|providers.hycom|first_class_ocean_provider|hycom_provider' server/gfs server/gfs_service_parts | head -80 || true

echo
echo "[hycom-provider] frame contract"
curl -fsS --max-time 140 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=bait,boater,shark-intel&mode=refresh&refresh=1&provider_jobs=1&reason=manual_hycom_provider_contract_check" \
| python3 -m json.tool \
| grep -Ei '"provider"|"hycom_provider"|"oceanAnalysisPoints"|"ocean_analysis_point_count"|"advancedBaitRows"|"advanced_bait_row_count"|"boats"|"source"|"status"|"sst"|"current"'

echo
echo "[hycom-provider] recent journal"
journalctl -u broadcast -n 420 --no-pager \
| grep -Ei 'hycom provider|server.gfs.provider.ocean|ocean subset|hycom|advancedBaitRows|boats/reconcile|bait/reconcile|NameError|Traceback' \
| tail -180 || true

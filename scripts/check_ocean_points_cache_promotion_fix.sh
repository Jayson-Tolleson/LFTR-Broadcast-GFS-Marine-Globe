#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m py_compile \
  server/gfs_service_parts/ocean_bait_frame.py \
  server/gfs_service_parts/lightning_cache_media.py \
  server/gfs_service_parts/atmosphere.py

grep -q "rejected_empty_ocean_provider_not_promoted" server/gfs_service_parts/lightning_cache_media.py
grep -q "do_not_promote_empty_ocean_live" server/gfs_service_parts/ocean_bait_frame.py
grep -q "do_not_promote_empty_ocean_points" server/gfs_service_parts/ocean_bait_frame.py
grep -q "non_numeric_forecast_hour_guarded" server/gfs_service_parts/atmosphere.py

echo "OK ocean points cache promotion + cloud forecast-hour guard patch present"

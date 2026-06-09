#!/usr/bin/env bash
set -euo pipefail

echo "[lightning] checking short-TTL event particle contract"
rg -n "_lightning_event_ttl_seconds|_filter_recent_lightning_payload|event_particle_contract" server/gfs_service_parts/lightning_cache_media.py >/dev/null
rg -n "expires_in_seconds|expired_flashes_removed|stale_cache_no_recent_lightning|cache_hit_no_recent_lightning" server/gfs_service_parts/lightning_cache_media.py >/dev/null
rg -n "scheduleLightningExpiry|fadeRemoveLightningElement|data-gfs-layer=\"lightning\"|must never remove other weather layers" static/js/gfs/lightning-zones.js >/dev/null
rg -n "expires_in_seconds|event_ttl_seconds|data-lightning-ttl-seconds" static/js/gfs/lightning-zones.js >/dev/null
node --check static/js/gfs/lightning-zones.js >/dev/null
python3 -m py_compile server/gfs_service_parts/lightning_cache_media.py >/dev/null
echo "PASS lightning event particles expire, fade, and use scoped layer cleanup"

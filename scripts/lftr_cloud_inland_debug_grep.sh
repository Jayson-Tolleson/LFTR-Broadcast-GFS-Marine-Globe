#!/usr/bin/env bash
set -euo pipefail

UNIT="${1:-broadcast.service}"

journalctl -u "$UNIT" -n 800 --no-pager \
  | egrep -i 'clouds/render|render/noop|clouds/morph|cloud_feature|frontal_shield|inland-water|lake temp|ncss_surface|temp-source|provider-wide-request-skipped|tile-refresh|ocean_live_required_unavailable|bait_live_required_unavailable' \
  || true

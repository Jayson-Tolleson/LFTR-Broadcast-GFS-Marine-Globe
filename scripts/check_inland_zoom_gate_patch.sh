#!/usr/bin/env bash
set -euo pipefail
cd "${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
echo "[zoom-gate] patched markers"
grep -RIn "INLAND_DETAIL_TIERS\|zoom-gated\|inland_water_and_inland_bait_only" static/js/gfs/main.js static/js/gfs/world_subscription_renderer.js || true

echo
echo "[zoom-gate] syntax"
if command -v node >/dev/null 2>&1; then
  node --check static/js/gfs/main.js
  node --check static/js/gfs/world_subscription_renderer.js
else
  echo "node unavailable; skipping JS syntax check"
fi

echo
echo "[zoom-gate] recent inland world builds"
sudo journalctl -u broadcast --since "20 minutes ago" --no-pager 2>/dev/null \
  | egrep -i "inland-water/build.*tier=world|zoom-gated" \
  | tail -40 || true

#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"
APP_USER="${APP_USER:-jayson_tolleson}"
APP_GROUP="${APP_GROUP:-jayson_tolleson}"

if id "$APP_USER" >/dev/null 2>&1; then
  APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER")}"
fi

echo "[cache-fix] app_dir=$APP_DIR app_user=$APP_USER app_group=$APP_GROUP"

if [ ! -d "$APP_DIR" ]; then
  echo "[cache-fix] ERROR: app dir not found: $APP_DIR" >&2
  exit 1
fi

sudo mkdir -p \
  "$APP_DIR/.cache" \
  "$APP_DIR/.cache/gfs_nomads" \
  "$APP_DIR/.cache/gfs_scene" \
  "$APP_DIR/.cache/gfs_tiles" \
  "$APP_DIR/.cache/lightning" \
  "$APP_DIR/data_sources" \
  "$APP_DIR/data_sources/hycom_cache" \
  "$APP_DIR/data_sources/nhd_runtime_cache" \
  "$APP_DIR/data_sources/nhd_runtime_cache/_build_logs" \
  "$APP_DIR/data_sources/nhdplus_hr_state_cache" \
  "$APP_DIR/data_sources/nhdplus_hr_state_cache/_build_logs"

echo "[cache-fix] stopping broadcast while repairing ownership"
sudo systemctl stop broadcast 2>/dev/null || true

echo "[cache-fix] chown/chmod runtime cache/data dirs"
sudo chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/.cache" "$APP_DIR/data_sources"
sudo chmod -R u+rwX,g+rwX "$APP_DIR/.cache" "$APP_DIR/data_sources"

# Remove root-owned pycache/caches that can preserve bad permissions or stale symbols.
echo "[cache-fix] clearing Python pycache and stale split-layer placeholders"
sudo find "$APP_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
sudo -u "$APP_USER" find "$APP_DIR/.cache" -type f \( \
  -name '*scene_cache_empty_bait*' -o \
  -name '*scene_cache_empty_boater*' -o \
  -name '*scene_cache_empty_shark*' -o \
  -name '*sst_fallback_v2*' \
\) -delete 2>/dev/null || true

echo "[cache-fix] write test"
sudo -u "$APP_USER" bash -lc "test -w '$APP_DIR/.cache' && touch '$APP_DIR/.cache/.lftr_write_test' && rm -f '$APP_DIR/.cache/.lftr_write_test' && echo '[cache-fix] cache writable ok'"

echo "[cache-fix] restarting broadcast"
sudo systemctl start broadcast

echo "[cache-fix] recent cache/error lines"
journalctl -u broadcast -n 160 --no-pager | grep -Ei 'cache writable|Permission denied|tile cache|scene_cache_empty|hycom|bait|boater|inland' || true

#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"
cd "$APP_DIR"
echo "[cache-clear] stopping broadcast service if active"
sudo systemctl stop broadcast.service 2>/dev/null || true
echo "[cache-clear] removing runtime caches that can carry stale land-mask payloads"
rm -rf \
  "$APP_DIR/.cache/gfs_nomads" \
  "$APP_DIR/.cache/gfs_provider" \
  "$APP_DIR/.cache/hycom" \
  "$APP_DIR/.cache/ocean" \
  "$APP_DIR/.cache/scene_cache" \
  "$APP_DIR/.cache/lightning" \
  "$APP_DIR/.cache/tiles" \
  "$APP_DIR/.cache/inland_water" \
  "$APP_DIR/data_sources/scene_cache" \
  "$APP_DIR/data_sources/provider_cache" \
  "$APP_DIR/data_sources/ocean_cache" \
  "$APP_DIR/data_sources/hycom_cache" \
  "$APP_DIR/data_sources/nhd_runtime_cache/_route_cache" \
  "$APP_DIR/data_sources/nhd_runtime_cache/_temp_cache" \
  "$APP_DIR/server/gfs_service_parts/__pycache__" \
  "$APP_DIR/server/gfs/__pycache__" \
  "$APP_DIR/server/gfs/providers/__pycache__" \
  "$APP_DIR/server/gfs/derive/__pycache__" \
  "$APP_DIR/server/__pycache__" 2>/dev/null || true
find "$APP_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$APP_DIR/.cache" "$APP_DIR/data_sources"
chown -R "${APP_USER:-jayson_tolleson}:${APP_GROUP:-${APP_USER:-jayson_tolleson}}" "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true
echo "[cache-clear] done"

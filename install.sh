#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ensure_bootstrap_packages() {
  # Makes a freshly downloaded GCP VM ready for this installer path.
  # Note: unzip is still needed before extracting this ZIP; this keeps reruns/self-installs healthy.
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y unzip curl ca-certificates gdal-bin
  fi
}


normalize_installer_permissions() {
  chmod +x "${ROOT_DIR}/broadcast.sh" "${ROOT_DIR}/install.sh" "${ROOT_DIR}/deploy/install.sh" 2>/dev/null || true
  if [[ -d "${ROOT_DIR}/deploy" ]]; then
    find "${ROOT_DIR}/deploy" -type d -exec chmod 755 {} + 2>/dev/null || true
    find "${ROOT_DIR}/deploy" -type f -name "*.sh" -exec chmod 755 {} + 2>/dev/null || true
  fi
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

ensure_bootstrap_packages
normalize_installer_permissions
exec bash "${ROOT_DIR}/deploy/install.sh" "$@"


# LFTR install-time cache/data permissions.
APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"
APP_USER="${APP_USER:-jayson_tolleson}"
APP_GROUP="${APP_GROUP:-jayson_tolleson}"
mkdir -p "$APP_DIR/.cache" "$APP_DIR/data_sources" "$APP_DIR/data_sources/hycom_cache" "$APP_DIR/data_sources/nhd_runtime_cache/_build_logs" "$APP_DIR/data_sources/nhdplus_hr_state_cache/_build_logs" 2>/dev/null || true
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true
chmod -R u+rwX,g+rwX "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true



# LFTR service working-directory stability.
if [ -f "$APP_DIR/deploy/systemd/broadcast.service" ]; then
  sudo cp "$APP_DIR/deploy/systemd/broadcast.service" /etc/systemd/system/broadcast.service
  sudo systemctl daemon-reload
fi


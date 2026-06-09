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

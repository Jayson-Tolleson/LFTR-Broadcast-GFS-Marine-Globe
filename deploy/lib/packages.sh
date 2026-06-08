#!/usr/bin/env bash
set -euo pipefail

install_system_packages() {
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip curl rsync nginx certbot \
    dnsutils netcat-openbsd coturn jq ffmpeg nodejs npm \
    whiptail dialog ncurses-bin unzip gdal-bin
}

#!/usr/bin/env bash
set -euo pipefail

# LFTR one-command installer entrypoint for a freshly unzipped package.
# It installs unzip/bootstrap helpers, creates ~/broadcast for the sudo user,
# copies the full package there, cd's into it, then starts broadcast.sh.

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
if [[ "$REAL_USER" == "root" ]]; then
  REAL_HOME="/root"
else
  REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
fi
REAL_HOME="${REAL_HOME:-/home/$REAL_USER}"
PROJECT_NAME="LFTR-Broadcast-GFS-Marine-Globe"
RUNNER_NAME="$(basename "${BASH_SOURCE[0]}")"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$REAL_HOME/broadcast"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y unzip curl ca-certificates tar
fi

mkdir -p "$DEST_DIR"

# Copy the package into ~/broadcast without requiring rsync.
# Avoid recursively copying ~/broadcast into itself if this script is rerun from there.
if [[ "$SRC_DIR" != "$DEST_DIR" ]]; then
  find "$DEST_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  (cd "$SRC_DIR" && tar --exclude='./.git' --exclude='./__pycache__' -cf - .) | (cd "$DEST_DIR" && tar -xf -)
fi

chown -R "$REAL_USER:$REAL_USER" "$DEST_DIR" 2>/dev/null || true
chmod +x "$DEST_DIR/broadcast.sh" "$DEST_DIR/install.sh" "$DEST_DIR/deploy/install.sh" 2>/dev/null || true

cd "$DEST_DIR"
echo "$PROJECT_NAME staged in $DEST_DIR"
echo "Entrypoint: $RUNNER_NAME"
echo "Starting installer from $(pwd)"
exec bash ./broadcast.sh "$@"

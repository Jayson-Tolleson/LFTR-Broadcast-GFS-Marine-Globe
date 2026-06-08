#!/usr/bin/env bash
set -euo pipefail

install_python_runtime_impl() {
  [[ -f "$APP_DIR/requirements.txt" ]] || die "requirements.txt missing at $APP_DIR"
  python3 -m venv "$APP_DIR/venv"
  "$APP_DIR/venv/bin/pip" install --upgrade pip setuptools wheel
  "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
}

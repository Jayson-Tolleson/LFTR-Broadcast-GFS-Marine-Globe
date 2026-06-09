#!/usr/bin/env bash
set -euo pipefail

read_env_value_from_file() {
  local env_file="$1"
  local key="$2"
  [[ -f "$env_file" ]] || return 1
  awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, "", $0); print; found=1; exit} END{exit !found}' "$env_file"
}

configure_google_maps_api_key() {
  local existing=""
  if [[ -f "$APP_ENV_FILE" ]]; then
    existing="$(read_env_value_from_file "$APP_ENV_FILE" "GOOGLE_MAPS_API_KEY" || true)"
  fi

  if [[ -n "${GOOGLE_MAPS_API_KEY:-}" ]]; then
    return 0
  fi

  if [[ -n "$existing" ]]; then
    GOOGLE_MAPS_API_KEY="$existing"
  fi

  if [[ -t 0 && -t 1 ]]; then
    local input_key=""
    if [[ -n "${GOOGLE_MAPS_API_KEY:-}" ]]; then
      log_info "GOOGLE_MAPS_API_KEY already set in app env file; press enter to keep current value"
      read -r -p "Enter Google Maps JavaScript API key for Maps 3D/maps3d (press enter to keep existing): " input_key || true
    else
      while [[ -z "${GOOGLE_MAPS_API_KEY:-}" ]]; do
        read -r -p "Enter Google Maps JavaScript API key for Maps 3D/maps3d library: " input_key || true
        input_key="${input_key:-}"
        input_key="${input_key#${input_key%%[![:space:]]*}}"
        input_key="${input_key%${input_key##*[![:space:]]}}"
        if [[ -n "$input_key" ]]; then
          GOOGLE_MAPS_API_KEY="$input_key"
          break
        fi
        log_warn "Google Maps API key is required for 3D globe unless already configured in ${APP_ENV_FILE}"
      done
    fi

    input_key="${input_key:-}"
    input_key="${input_key#${input_key%%[![:space:]]*}}"
    input_key="${input_key%${input_key##*[![:space:]]}}"
    if [[ -n "${input_key:-}" ]]; then
      GOOGLE_MAPS_API_KEY="$input_key"
    fi
  fi

  if [[ -z "${GOOGLE_MAPS_API_KEY:-}" ]]; then
    log_warn "GOOGLE_MAPS_API_KEY is not set; /gfs globe maps3d will remain unavailable until configured"
  fi
}

load_deploy_env() {
  local root_dir="$1"
  local env_file_default="${root_dir}/deploy/.env"
  DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-$env_file_default}"

  if [[ -f "$DEPLOY_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$DEPLOY_ENV_FILE"; set +a
  fi

  APP_USER="${APP_USER:-${SUDO_USER:-${USER:-broadcast}}}"
  APP_GROUP="${APP_GROUP:-$APP_USER}"
  APP_DIR="${APP_DIR:-/home/${APP_USER}/broadcast}"
  APP_SERVICE_NAME="${APP_SERVICE_NAME:-broadcast.service}"
  APP_BIND_HOST="${APP_BIND_HOST:-127.0.0.1}"
  APP_BIND_PORT="${APP_BIND_PORT:-8000}"
  APP_WORKERS="${APP_WORKERS:-1}"

  DOMAIN="${DOMAIN:-lftr.biz}"
  DOMAIN_WWW="${DOMAIN_WWW:-www.${DOMAIN}}"
  CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@${DOMAIN}}"
  ENABLE_TLS="${ENABLE_TLS:-true}"
  ENABLE_COTURN="${ENABLE_COTURN:-true}"

  TURN_PORT="${TURN_PORT:-3478}"
  TURNS_PORT="${TURNS_PORT:-5349}"
  TURN_MIN_PORT="${TURN_MIN_PORT:-49160}"
  TURN_MAX_PORT="${TURN_MAX_PORT:-49200}"
  TURN_USER="${TURN_USER:-webrtc}"
  TURN_PASS="${TURN_PASS:-}"
  TURN_EXTERNAL_IP="${TURN_EXTERNAL_IP:-}"
  TURN_INTERNAL_IP="${TURN_INTERNAL_IP:-}"

  INSTALL_SYSTEM_PACKAGES="${INSTALL_SYSTEM_PACKAGES:-true}"
  INSTALL_PY_DEPS="${INSTALL_PY_DEPS:-true}"
  ENABLE_NGINX="${ENABLE_NGINX:-true}"
  ENABLE_GFS_PROXY="${ENABLE_GFS_PROXY:-false}"
  GFS_UPSTREAM_HOST="${GFS_UPSTREAM_HOST:-127.0.0.1}"
  GFS_UPSTREAM_PORT="${GFS_UPSTREAM_PORT:-8081}"

  GOOGLE_MAPS_API_KEY="${GOOGLE_MAPS_API_KEY:-}"

  REPO_ROOT="$root_dir"
  DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-${APP_DIR}/.deploy}"
  APP_ENV_FILE="${APP_ENV_FILE:-${APP_DIR}/.env}"
  NGINX_SITE_NAME="${NGINX_SITE_NAME:-broadcast_stack}"

  [[ -n "$APP_DIR" ]] || die "APP_DIR must not be empty"
  [[ -n "$APP_SERVICE_NAME" ]] || die "APP_SERVICE_NAME must not be empty"
  [[ -n "$DOMAIN" ]] || die "DOMAIN must not be empty"
}

compute_runtime_values() {
  CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
  NGINX_SITE_AVAILABLE="/etc/nginx/sites-available/${NGINX_SITE_NAME}"
  NGINX_SITE_ENABLED="/etc/nginx/sites-enabled/${NGINX_SITE_NAME}"
  SYSTEMD_UNIT_PATH="/etc/systemd/system/${APP_SERVICE_NAME}"

  if [[ -z "$TURN_PASS" ]]; then
    TURN_PASS="strongpassword"
  fi

  if [[ -z "$TURN_EXTERNAL_IP" ]]; then
    TURN_EXTERNAL_IP="$(curl -fsS ifconfig.me 2>/dev/null || true)"
  fi

  if [[ -z "$TURN_INTERNAL_IP" ]]; then
    TURN_INTERNAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
}

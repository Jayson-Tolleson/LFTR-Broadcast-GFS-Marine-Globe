#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- LFTR colorful installer UI -------------------------------------------------
# Pure bash/ANSI so it works on fresh Debian/Ubuntu GCP VMs without dialog/gum.
if [[ -t 1 && "${NO_COLOR:-}" == "" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_MAG=$'\033[35m'; C_CYAN=$'\033[36m'; C_WHITE=$'\033[37m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_MAG=''; C_CYAN=''; C_WHITE=''
fi

say() { printf '%b\n' "$*"; }
ui_line() { say "${C_DIM}────────────────────────────────────────────────────────────${C_RESET}"; }
ui_banner() {
  if command -v figlet >/dev/null 2>&1 && [[ -t 1 && "${NO_COLOR:-}" == "" ]]; then
    if command -v lolcat >/dev/null 2>&1; then
      figlet -w 100 "LFTR Globe" | lolcat || true
    else
      say "${C_MAG}${C_BOLD}"
      figlet -w 100 "LFTR Globe" || true
      say "${C_RESET}"
    fi
  fi
  say "${C_CYAN}${C_BOLD}"
  cat <<'EOF'
╭────────────────────────────────────────────────────────────╮
│           LFTR Marine Intelligence Globe Installer          │
│        Broadcast + Watch + GFS + Nightfall Atmosphere       │
│          Neon TUI mode: whiptail/dialog when available      │
╰────────────────────────────────────────────────────────────╯
EOF
  say "${C_RESET}"
}
ui_step() { say "${C_BLUE}${C_BOLD}▶${C_RESET} ${C_BOLD}$*${C_RESET}"; }
ui_ok() { say "${C_GREEN}${C_BOLD}✓${C_RESET} $*"; }
ui_warn() { say "${C_YELLOW}${C_BOLD}!${C_RESET} $*"; }
# -------------------------------------------------------------------------------


ensure_bootstrap_packages() {
  # Makes a freshly downloaded GCP VM ready for this installer path.
  # Note: unzip is still needed before extracting this ZIP; this keeps reruns/self-installs healthy.
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y unzip curl ca-certificates

    # TUI/graphical terminal installer helpers.
    # whiptail/dialog give us boxed input screens over SSH; ANSI stays as fallback.
    local pkg
    for pkg in whiptail dialog ncurses-bin figlet toilet lolcat; do
      DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >/dev/null 2>&1 || true
    done
  fi
}


normalize_installer_permissions() {
  # Make a freshly-unzipped package runnable without a manual chmod step.
  # Some unzip/copy paths strip executable bits; the top-level installer repairs them.
  chmod +x "${ROOT_DIR}/broadcast.sh" "${ROOT_DIR}/install.sh" "${ROOT_DIR}/deploy/install.sh" 2>/dev/null || true
  if [[ -d "${ROOT_DIR}/deploy" ]]; then
    find "${ROOT_DIR}/deploy" -type d -exec chmod 755 {} + 2>/dev/null || true
    find "${ROOT_DIR}/deploy" -type f -name "*.sh" -exec chmod 755 {} + 2>/dev/null || true
  fi
}


use_tui() {
  [[ -t 0 && -t 1 && "${LFTR_TEXT_INSTALLER:-}" != "1" ]] && command -v whiptail >/dev/null 2>&1
}

prompt_default () {
  local prompt="$1"
  local default="$2"
  local value
  if use_tui; then
    value=$(whiptail --title "LFTR Globe Installer" --inputbox "$prompt" 10 78 "$default" 3>&1 1>&2 2>&3) || value="$default"
    echo "${value:-$default}"
    return
  fi
  read -r -p "$prompt [$default]: " value
  echo "${value:-$default}"
}

installer_pause() {
  if use_tui; then
    whiptail --title "LFTR Globe Installer" --msgbox "$1" 10 78 || true
  else
    ui_ok "$1"
  fi
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

ensure_bootstrap_packages
normalize_installer_permissions

ui_banner
installer_pause "Welcome to the LFTR Marine Intelligence Globe installer. This server will be prepared for nginx, TLS, Python, broadcast, watch, GFS, and nightfall sky mode."
ui_step "Broadcast installer configuration"

DEFAULT_IP="$(curl -s ifconfig.me || echo "127.0.0.1")"
DEFAULT_DOMAIN=""
if compgen -G "/etc/nginx/sites-enabled/*" >/dev/null 2>&1; then
  DEFAULT_DOMAIN="$(grep -h "server_name" /etc/nginx/sites-enabled/* 2>/dev/null | awk '{print $2}' | tr -d ';' | head -n1 || true)"
fi
DEFAULT_DOMAIN="${DEFAULT_DOMAIN:-$DEFAULT_IP}"

DEFAULT_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
DEFAULT_PROJECT="${DEFAULT_PROJECT:-my-gcp-project}"

DEFAULT_MAPS_KEY="PASTE_API_KEY_HERE"

DOMAIN="$(prompt_default "Enter domain name (used for nginx + SSL)" "$DEFAULT_DOMAIN")"
GOOGLE_PROJECT_ID="$(prompt_default "Enter Google Cloud Project ID" "$DEFAULT_PROJECT")"
MAPS_API_KEY="$(prompt_default "Enter Google Maps JS API key" "$DEFAULT_MAPS_KEY")"
EMAIL="$(prompt_default "Enter email for Let's Encrypt certificate" "admin@$DOMAIN")"
GCP_KEY="$(prompt_default "Path to GCP service account key" "/etc/broadcast/gcp-key.json")"
VERTEX_LOCATION="$(prompt_default "Vertex AI location" "global")"
VERTEX_MODEL="$(prompt_default "Vertex AI model" "gemini-2.5-flash")"
AI_PROVIDER="$(prompt_default "AI provider" "vertex")"
# Inland Waters data is never downloaded during install. Runtime uses viewport/zoom on-demand source tiles only.
LFTR_INSTALL_INLAND_DATA=0
INLAND_VIEW_BBOX="${NHDPLUS_VIEW_BBOX:--126,29,-114,39}"
INLAND_CACHE_DAYS="${NHDPLUS_CACHE_DAYS:-31}"

mkdir -p /etc/broadcast
cat > /etc/broadcast/install.env <<CFG
DOMAIN=$DOMAIN
GOOGLE_PROJECT_ID=$GOOGLE_PROJECT_ID
MAPS_API_KEY=$MAPS_API_KEY
GOOGLE_MAPS_API_KEY=$MAPS_API_KEY
GOOGLE_CLOUD_REGION=global
EMAIL=$EMAIL
GCP_KEY=$GCP_KEY
VERTEX_LOCATION=$VERTEX_LOCATION
VERTEX_MODEL=$VERTEX_MODEL
AI_PROVIDER=$AI_PROVIDER
LFTR_INSTALL_INLAND_DATA=$LFTR_INSTALL_INLAND_DATA
NHDPLUS_CACHE_DAYS=$INLAND_CACHE_DAYS
NHDPLUS_VIEW_BBOX=$INLAND_VIEW_BBOX
NHDPLUS_SOURCE_MODE=arcgis
CFG

export DOMAIN
export GOOGLE_PROJECT_ID
export MAPS_API_KEY
export GOOGLE_MAPS_API_KEY="$MAPS_API_KEY"
export GOOGLE_CLOUD_REGION="global"
export EMAIL
export CERTBOT_EMAIL="$EMAIL"
export GCP_KEY
export VERTEX_LOCATION
export VERTEX_MODEL
export AI_PROVIDER
export LFTR_INSTALL_INLAND_DATA
export NHDPLUS_CACHE_DAYS="$INLAND_CACHE_DAYS"
export NHDPLUS_VIEW_BBOX="$INLAND_VIEW_BBOX"
export NHDPLUS_SOURCE_MODE="arcgis"


if [[ "$DOMAIN" == "$DEFAULT_IP" ]]; then
  ui_warn "Domain matches public IP; SSL request will be skipped by installer."
  export SKIP_SSL=1
fi

ui_line
ui_step "Install configuration"
say "${C_CYAN}Domain:${C_RESET} $DOMAIN"
say "${C_CYAN}Project:${C_RESET} $GOOGLE_PROJECT_ID"
say "${C_CYAN}Maps API:${C_RESET} configured"
say "${C_CYAN}Inland Waters data:${C_RESET} install_time=disabled; runtime viewport/zoom on-demand source tiles; retention_days=${NHDPLUS_CACHE_DAYS}"
if [[ -f "$GCP_KEY" ]]; then
  say "${C_CYAN}GCP key:${C_RESET} $GCP_KEY"
else
  say "${C_CYAN}GCP key:${C_RESET} not set; ADC mode"
fi

exec bash "${ROOT_DIR}/deploy/install.sh" "$@"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
ui_phase() {
  local n="$1"; shift
  ui_line
  say "${C_MAG}${C_BOLD}PHASE ${n}${C_RESET} ${C_CYAN}${C_BOLD}— $*${C_RESET}"
  ui_line
}
ui_step() { say "${C_BLUE}${C_BOLD}▶${C_RESET} ${C_BOLD}$*${C_RESET}"; }
ui_ok() { say "${C_GREEN}${C_BOLD}✓${C_RESET} $*"; }
ui_warn() { say "${C_YELLOW}${C_BOLD}!${C_RESET} $*"; }
ui_fail() { say "${C_RED}${C_BOLD}✗${C_RESET} $*" >&2; }
ui_info() { say "${C_DIM}•${C_RESET} $*"; }
# -------------------------------------------------------------------------------


normalize_installer_permissions() {
  # Self-heal installer/deploy permissions so no manual `chmod a+x deploy -R` is needed.
  chmod +x "${ROOT_DIR}/broadcast.sh" "${ROOT_DIR}/install.sh" "${ROOT_DIR}/deploy/install.sh" 2>/dev/null || true
  find "${ROOT_DIR}/deploy" -type d -exec chmod 755 {} + 2>/dev/null || true
  find "${ROOT_DIR}/deploy" -type f -name "*.sh" -exec chmod 755 {} + 2>/dev/null || true
}

DOMAIN="${DOMAIN:-lftr.biz}"
INSTALL_USER="${INSTALL_USER:-${SUDO_USER:-jayson_tolleson}}"
if [[ "$INSTALL_USER" == "root" || -z "$INSTALL_USER" ]]; then
  ROOT_OWNER="$(stat -c %U "$ROOT_DIR" 2>/dev/null || true)"
  if [[ -n "$ROOT_OWNER" && "$ROOT_OWNER" != "root" ]]; then
    INSTALL_USER="$ROOT_OWNER"
  else
    INSTALL_USER="jayson_tolleson"
  fi
fi
APP_DIR="${APP_DIR:-/home/${INSTALL_USER}/broadcast}"
VENV_DIR="${APP_DIR}/venv"
GOOGLE_PROJECT_ID="${GOOGLE_PROJECT_ID:-}"
GCP_KEY="${GCP_KEY:-}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
VERTEX_MODEL="${VERTEX_MODEL:-gemini-2.5-flash}"
AI_PROVIDER="${AI_PROVIDER:-vertex}"
LFTR_INSTALL_INLAND_DATA="${LFTR_INSTALL_INLAND_DATA:-0}"
NHDPLUS_CACHE_DAYS="${NHDPLUS_CACHE_DAYS:-31}"
NHDPLUS_SOURCE_MODE="${NHDPLUS_SOURCE_MODE:-arcgis}"
NHDPLUS_VIEW_BBOX="${NHDPLUS_VIEW_BBOX:-${NHDPLUS_INITIAL_BBOX:--126,29,-114,39}}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@${DOMAIN}}"
GOOGLE_CLOUD_REGION="global"

log() { printf '%b[%s]%b %s\n' "$C_DIM" "$1" "$C_RESET" "$2"; }
fail() { ui_fail "$1"; exit 1; }

validate_static_layout() {
  [[ -d "$APP_DIR" ]] || fail "APP_DIR missing or not a directory: $APP_DIR"

  if [[ -e "$APP_DIR/static" && ! -d "$APP_DIR/static" ]]; then
    ui_fail "static path exists but is not a directory: $APP_DIR/static"
    rm -f "$APP_DIR/static" || true
    fail "invalid static deployment path removed; rerun installer"
  fi

  [[ -d "$APP_DIR/static" ]] || fail "static directory missing: $APP_DIR/static"

  local required_files=(
    "$APP_DIR/static/index.html"
    "$APP_DIR/static/indexgfs.html"
    "$APP_DIR/static/broadcast.html"
    "$APP_DIR/static/watch.html"
  )
  for f in "${required_files[@]}"; do
    [[ -f "$f" ]] || {
      ui_fail "required static file missing: $f"
      ls -la "$APP_DIR/static" || true
      fail "static deployment validation failed"
    }
  done
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
    else
        ui_fail "Cannot detect OS"
        exit 1
    fi

    case "$ID:$VERSION_CODENAME" in
        debian:bookworm|debian:trixie)
            DISTRO="debian"
            ;;
        ubuntu:jammy|ubuntu:noble)
            DISTRO="ubuntu"
            ;;
        *)
            ui_fail "Unsupported OS: $ID $VERSION_CODENAME"
            exit 1
            ;;
    esac

    ui_ok "Detected supported system: $ID $VERSION_CODENAME"
}

phase1_system_prep() {
  ui_phase 1 "SYSTEM PREP"
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Run installer as root: sudo bash broadcast.sh"
  detect_os

  ui_step "Installing system packages, nginx/cert tools, Python build deps"
  apt-get update
  apt-get install -y \
    curl \
    wget \
    git \
    rsync \
    unzip \
    gnupg \
    ca-certificates \
    lsb-release \
    python3 \
    python3-venv \
    python3-pip \
    build-essential \
    gfortran \
    libeccodes-dev \
    libeccodes-tools \
    libnetcdf-dev \
    libhdf5-dev \
    gdal-bin \
    jq
}

phase2_python_runtime() {
  ui_phase 2 "PYTHON RUNTIME"
  id -u "$INSTALL_USER" >/dev/null 2>&1 || useradd -m "$INSTALL_USER"
  mkdir -p "$APP_DIR" /etc/broadcast

  local root_real app_real
  root_real="$(realpath "$ROOT_DIR")"
  app_real="$(realpath "$APP_DIR")"

  if [[ "$root_real" == "$app_real" ]]; then
    ui_ok "Package already in APP_DIR=$APP_DIR; skipping self-rsync"
  else
    ui_step "Syncing package from $ROOT_DIR to $APP_DIR"
    rsync -a --delete --exclude '.git/' --exclude '__pycache__/' --exclude 'mnt/' --exclude '*.pyc' "$ROOT_DIR/" "$APP_DIR/"
  fi

  chown -R "$INSTALL_USER:$INSTALL_USER" "$APP_DIR"
  validate_static_layout

  if [[ -d "$VENV_DIR" && ! -x "$VENV_DIR/bin/python" ]]; then
    ui_warn "Existing virtualenv is incomplete; recreating $VENV_DIR"
    rm -rf "$VENV_DIR"
  fi
  ui_step "Creating/refreshing Python virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  ui_step "Installing Python requirements"
  pip install --upgrade pip setuptools wheel
  pip install -r "$APP_DIR/requirements.txt"
  pip install netCDF4 pydap
  pip install cfgrib eccodes
  "$VENV_DIR/bin/python" - <<'PYVENV'
import importlib.util, sys
missing = [m for m in ("hypercorn", "quart") if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit("missing runtime modules: " + ",".join(missing))
print("runtime modules ok: hypercorn quart")
PYVENV
  find "$APP_DIR" -type d -exec chmod 755 {} \;
  find "$APP_DIR" -type f -exec chmod 644 {} \;
  find "$APP_DIR" -type f \( -name "*.sh" -o -path "*/venv/bin/*" \) -exec chmod 755 {} \;
  chmod o+x "/home/${INSTALL_USER}"
}

phase3_inland_water_data() {
  ui_phase "2B" "HIGH-DEF INLAND WATERS RUNTIME CACHE"
  ui_info "Policy: real source only; no seed/coarse/mock inland-water fallback."
  ui_info "Install-time data download is disabled. Runtime builds real source tiles by viewport/zoom when the Inland Waters pill is active."
  ui_info "Runtime drawing reads one shared local json.gz tile cache only; ArcGIS/NHD fetches are background cache-build jobs."

  mkdir -p "$APP_DIR/static/data/nhdplus_hr/tiles" \
           "$APP_DIR/.cache" \
           "$APP_DIR/data_sources" \
           "$APP_DIR/data_sources/hycom_cache" \
           "$APP_DIR/data_sources/nhd_runtime_cache/_build_logs" \
           "$APP_DIR/data_sources/nhdplus_hr_state_cache/_build_logs"
  chown -R "$INSTALL_USER:$INSTALL_USER" "$APP_DIR/static/data" "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true
  chmod -R u+rwX,g+rwX "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true

  if [[ ! -x "$APP_DIR/scripts/install_nhdplus_hr_view_cache.sh" ]]; then
    chmod +x "$APP_DIR/scripts/install_nhdplus_hr_view_cache.sh" 2>/dev/null || true
  fi
  ui_ok "Prepared Inland Waters cache directories; no data downloaded during install"
}

phase3_firewall() {
  ui_phase 3 "FIREWALL"

  # Ensure ufw exists
  if ! command -v ufw >/dev/null 2>&1; then
    ui_info "Installing ufw firewall"
    apt-get update
    apt-get install -y ufw
  fi

  # Configure firewall
  ui_step "Opening SSH, HTTP, and HTTPS in ufw"
  ufw allow 22 || true
  ufw allow 80 || true
  ufw allow 443 || true
  ufw --force enable || true
}

phase4_google_cloud() {
  ui_phase 4 "GOOGLE CLOUD"

  local metadata_project=""
  local metadata_sa_email=""
  local gcp_detected="no"
  local attached_sa="no"
  local auth_mode="disabled"
  local vertex_enabled="no"
  local apis_attempted="no"

  if curl -fsS --max-time 2 -H 'Metadata-Flavor: Google'     http://metadata.google.internal/computeMetadata/v1/instance/id >/dev/null 2>&1; then
    gcp_detected="yes"
  fi

  if [[ -z "${GOOGLE_PROJECT_ID:-}" ]] && [[ -n "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
    GOOGLE_PROJECT_ID="${GOOGLE_CLOUD_PROJECT}"
  fi

  if [[ -z "$GOOGLE_PROJECT_ID" ]] && command -v gcloud >/dev/null 2>&1; then
    GOOGLE_PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
    GOOGLE_PROJECT_ID="${GOOGLE_PROJECT_ID//\(unset\)/}"
    GOOGLE_PROJECT_ID="$(echo "$GOOGLE_PROJECT_ID" | xargs || true)"
  fi

  if [[ -z "$GOOGLE_PROJECT_ID" ]] && [[ "$gcp_detected" == "yes" ]]; then
    metadata_project="$(curl -fsS --max-time 2 -H 'Metadata-Flavor: Google'       http://metadata.google.internal/computeMetadata/v1/project/project-id 2>/dev/null || true)"
    if [[ -n "$metadata_project" ]]; then
      GOOGLE_PROJECT_ID="$metadata_project"
    fi
  fi

  if [[ "$gcp_detected" == "yes" ]]; then
    metadata_sa_email="$(curl -fsS --max-time 2 -H 'Metadata-Flavor: Google'       http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email 2>/dev/null || true)"
    if [[ -n "$metadata_sa_email" ]]; then
      attached_sa="yes"
    fi
  fi

  local explicit_key=""
  if [[ -n "${GCP_KEY:-}" ]] && [[ -f "$GCP_KEY" ]]; then
    explicit_key="$GCP_KEY"
  elif [[ -f "/etc/broadcast/gcp-key.json" ]]; then
    explicit_key="/etc/broadcast/gcp-key.json"
    GCP_KEY="/etc/broadcast/gcp-key.json"
  fi

  if [[ "$attached_sa" == "yes" ]] && [[ -n "$GOOGLE_PROJECT_ID" ]]; then
    auth_mode="adc"
    vertex_enabled="yes"
    unset GOOGLE_APPLICATION_CREDENTIALS || true
  elif [[ -n "$explicit_key" ]]; then
    auth_mode="json_key"
    vertex_enabled="yes"
    export GOOGLE_APPLICATION_CREDENTIALS="$explicit_key"
    chmod 600 "$explicit_key" || true
  else
    auth_mode="disabled"
    vertex_enabled="no"
    ui_warn "No attached service account ADC and no valid JSON key; AI features disabled"
  fi

  export GOOGLE_CLOUD_REGION="global"
  export GOOGLE_CLOUD_PROJECT="${GOOGLE_PROJECT_ID}"

  mkdir -p /etc/broadcast
  local gcp_key_env=""
  if [[ -n "$explicit_key" ]]; then
    gcp_key_env="$explicit_key"
  fi
  cat > /etc/broadcast/install.env <<EOF
DOMAIN=${DOMAIN}
GOOGLE_PROJECT_ID=${GOOGLE_PROJECT_ID}
GOOGLE_CLOUD_PROJECT=${GOOGLE_PROJECT_ID}
MAPS_API_KEY=${MAPS_API_KEY:-}
GOOGLE_MAPS_API_KEY=${MAPS_API_KEY:-}
GOOGLE_CLOUD_REGION=global
GCP_KEY=${gcp_key_env}
VERTEX_LOCATION=${VERTEX_LOCATION}
VERTEX_MODEL=${VERTEX_MODEL}
AI_PROVIDER=${AI_PROVIDER}
AI_AUTH_MODE=${auth_mode}
VERTEX_ENABLED=${vertex_enabled}
GOOGLE_APPLICATION_CREDENTIALS=${GOOGLE_APPLICATION_CREDENTIALS:-}
LFTR_INSTALL_INLAND_DATA=0
NHDPLUS_CACHE_DAYS=${NHDPLUS_CACHE_DAYS}
NHDPLUS_SOURCE_MODE=${NHDPLUS_SOURCE_MODE}
NHDPLUS_VIEW_BBOX=${NHDPLUS_VIEW_BBOX}
EOF

  if command -v gcloud >/dev/null 2>&1 && [[ -n "$GOOGLE_PROJECT_ID" ]]; then
    apis_attempted="yes"
    if gcloud config set project "$GOOGLE_PROJECT_ID" >/dev/null 2>&1; then
      gcloud services enable         aiplatform.googleapis.com         speech.googleapis.com         texttospeech.googleapis.com >/dev/null 2>&1 || true
    fi
  fi

  ui_info "GCP detected: ${gcp_detected}"
  ui_info "Project ID: ${GOOGLE_PROJECT_ID:-<missing>}"
  ui_info "Attached service account: ${attached_sa}${metadata_sa_email:+ (${metadata_sa_email})}"
  ui_info "AI auth mode: ${auth_mode}"
  ui_info "APIs enable attempted: ${apis_attempted}"
  ui_info "Vertex enabled: ${vertex_enabled}"
}

write_acme_http_nginx() {
  local conf="/etc/nginx/sites-available/broadcast-acme"
  mkdir -p /var/www/certbot/.well-known/acme-challenge
  chown -R www-data:www-data /var/www/certbot || true

  cat > "$conf" <<EOF
# Temporary HTTP-only server used only for Let's Encrypt ACME webroot validation.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type "text/plain";
        try_files \$uri =404;
    }

    location / {
        return 200 "LFTR installer ACME HTTP endpoint is alive.\n";
        add_header Content-Type text/plain;
    }
}
EOF

  rm -f /etc/nginx/sites-enabled/default \
        /etc/nginx/sites-enabled/broadcast \
        /etc/nginx/sites-enabled/broadcast.conf \
        /etc/nginx/sites-enabled/broadcast_stack \
        /etc/nginx/sites-enabled/broadcast-acme || true
  ln -sf "$conf" /etc/nginx/sites-enabled/broadcast-acme
  nginx -t
  systemctl restart nginx
  systemctl enable nginx
}


cert_paths_ready() {
  local cert_dir="/etc/letsencrypt/live/${DOMAIN}"
  [[ -s "${cert_dir}/fullchain.pem" && -s "${cert_dir}/privkey.pem" ]]
}

cert_valid_for_days() {
  local days="${1:-30}"
  local cert="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  local seconds=$((days * 86400))
  [[ -s "$cert" ]] || return 1
  command -v openssl >/dev/null 2>&1 || return 1
  openssl x509 -checkend "$seconds" -noout -in "$cert" >/dev/null 2>&1
}

cert_expiry_text() {
  local cert="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  if [[ -s "$cert" ]] && command -v openssl >/dev/null 2>&1; then
    openssl x509 -enddate -noout -in "$cert" 2>/dev/null | sed 's/^notAfter=//'
  fi
}

ensure_tls_certificate() {
  local renew_days="${CERT_RENEW_DAYS:-30}"

  if cert_paths_ready && cert_valid_for_days "$renew_days"; then
    ui_ok "Existing TLS certificate for ${DOMAIN} is valid for more than ${renew_days} days; skipping Certbot issuance"
    local expiry
    expiry="$(cert_expiry_text || true)"
    [[ -n "$expiry" ]] && ui_info "Certificate expires: $expiry"
    return 0
  fi

  if cert_paths_ready; then
    ui_warn "Existing TLS certificate for ${DOMAIN} is present but near expiry or unreadable; attempting safe renewal"
    certbot renew --cert-name "$DOMAIN" --deploy-hook "systemctl reload nginx || true" || ui_warn "certbot renew did not complete; will attempt webroot issuance"
    if cert_paths_ready && cert_valid_for_days 1; then
      ui_ok "TLS certificate for ${DOMAIN} is present after renewal attempt"
      return 0
    fi
  fi

  warn_if_dns_does_not_match
  write_acme_http_nginx

  ui_step "Requesting Let's Encrypt cert via webroot on port 80"
  certbot certonly     --webroot     -w /var/www/certbot     --preferred-challenges http     --agree-tos     --non-interactive     --email "$CERTBOT_EMAIL"     --keep-until-expiring     --expand     -d "$DOMAIN"

  cert_paths_ready || fail "TLS certificate not found at /etc/letsencrypt/live/$DOMAIN/fullchain.pem"
}

warn_if_dns_does_not_match() {
  local public_ip dns_a dns_aaaa
  public_ip="$(curl -4fsS --max-time 5 https://ifconfig.me 2>/dev/null || curl -4fsS --max-time 5 http://ifconfig.me 2>/dev/null || true)"
  dns_a="$(dig +short A "$DOMAIN" 2>/dev/null | tail -n1 || true)"
  dns_aaaa="$(dig +short AAAA "$DOMAIN" 2>/dev/null | tr '\n' ' ' || true)"

  ui_info "Server public IPv4: ${public_ip:-unknown}"
  ui_info "DNS A for ${DOMAIN}: ${dns_a:-none}"
  if [[ -n "$dns_aaaa" ]]; then
    ui_warn "DNS AAAA for ${DOMAIN}: ${dns_aaaa}"
    ui_warn "If this VM is not serving IPv6, remove the AAAA record before issuing TLS."
  fi
  if [[ -n "$public_ip" && -n "$dns_a" && "$public_ip" != "$dns_a" ]]; then
    ui_warn "DNS A does not match this VM public IP. Let's Encrypt may fail until DNS points here."
  fi
}

phase5_tls() {
  ui_phase 5 "TLS CERTIFICATE"
  ui_step "Installing certbot, nginx, and DNS tools"
  apt-get install -y certbot python3-certbot-nginx nginx dnsutils curl
  mkdir -p /var/www/certbot/.well-known/acme-challenge
  if [[ "${SKIP_SSL:-0}" == "1" ]]; then
    ui_info "SKIP_SSL=1, skipping certificate issuance"
    return 0
  fi
  if [[ -z "${DOMAIN:-}" ]]; then
    ui_warn "DOMAIN empty, skipping certificate issuance"
    return 0
  fi
  if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ui_warn "DOMAIN looks like an IP, skipping certificate issuance"
    return 0
  fi

  ensure_tls_certificate
}

phase6_nginx() {
  ui_phase 6 "NGINX SETUP"
  ui_step "Writing final nginx HTTPS config"
  apt-get install -y nginx
  mkdir -p /var/www/certbot/.well-known/acme-challenge

  cp "$ROOT_DIR/deploy/nginx_template.conf" /etc/nginx/sites-available/broadcast
  sed -i "s/\${DOMAIN}/$DOMAIN/g" /etc/nginx/sites-available/broadcast
  sed -i "s|\${APP_USER}|$INSTALL_USER|g" /etc/nginx/sites-available/broadcast
  sed -i "s|\${APP_ROOT}|$APP_DIR|g" /etc/nginx/sites-available/broadcast

  rm -f /etc/nginx/sites-enabled/default \
        /etc/nginx/sites-enabled/broadcast \
        /etc/nginx/sites-enabled/broadcast.conf \
        /etc/nginx/sites-enabled/broadcast_stack \
        /etc/nginx/sites-enabled/broadcast-acme
  ln -sf /etc/nginx/sites-available/broadcast /etc/nginx/sites-enabled/broadcast

  if ! nginx -t; then
    ui_fail "nginx configuration validation failed"
    exit 1
  fi

  systemctl restart nginx
  systemctl enable nginx
}
phase7_services() {
  ui_phase 7 "SYSTEMD SERVICES"
  ui_step "Verifying Python runtime before systemd restart"
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    ui_warn "Virtualenv python missing before service install; recreating $VENV_DIR"
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
    "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt" netCDF4 pydap cfgrib eccodes
  fi
  "$VENV_DIR/bin/python" -c 'import hypercorn, quart; print("hypercorn runtime ok")'
  ui_step "Installing and starting broadcast systemd service"
  cp "$ROOT_DIR/deploy/systemd/broadcast.service" /etc/systemd/system/broadcast.service
  sed -i "s|\${APP_USER}|$INSTALL_USER|g" /etc/systemd/system/broadcast.service
  sed -i "s|\${APP_GROUP}|$INSTALL_USER|g" /etc/systemd/system/broadcast.service
  sed -i "s|\${APP_DIR}|$APP_DIR|g" /etc/systemd/system/broadcast.service
  sed -i "s|\${CFG_DIR}|/etc/broadcast|g" /etc/systemd/system/broadcast.service


  systemctl daemon-reload
  systemctl reset-failed broadcast.service 2>/dev/null || true
  systemctl enable broadcast
  systemctl restart broadcast
}

wait_local_http_endpoint() {
  local url="$1"
  local label="$2"
  local tries="${3:-90}"
  local sleep_s="${4:-1}"
  local code=""
  for ((i=1; i<=tries; i++)); do
    code="$(curl --noproxy '*' -k -sS -m 2 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" =~ ^(2|3) ]]; then
      ui_ok "$label ready (${code})"
      return 0
    fi
    if (( i == 1 || i % 10 == 0 )); then
      ui_info "waiting for $label at $url attempt ${i}/${tries} code=${code:-connect}"
      systemctl is-active --quiet broadcast.service && ui_info "broadcast.service active while waiting" || true
    fi
    sleep "$sleep_s"
  done
  ui_warn "$label did not become HTTP-ready at $url"
  systemctl status broadcast.service --no-pager -l || true
  journalctl -u broadcast -n 80 --no-pager || true
  return 1
}

phase8_health() {
  ui_phase 8 "HEALTH CHECKS"
  ui_step "Checking backend readiness"

  # The service can be alive while Quart is still importing, warming cache, or
  # rebuilding after a previous failed unit. Give it enough time and show
  # diagnostics before failing.  /health and /api/health are intentionally
  # lightweight; /gfs/api/health is allowed to warn because GFS cache warming can
  # briefly be busy on a cold install.
  systemctl is-active --quiet broadcast.service || {
    systemctl status broadcast.service --no-pager -l || true
    journalctl -u broadcast -n 120 --no-pager || true
    fail "broadcast.service is not active after restart"
  }

  if ! wait_local_http_endpoint "http://127.0.0.1:8000/health" "backend /health" "90" "1"; then
    # Try the API health fallback before declaring install failure.
    wait_local_http_endpoint "http://127.0.0.1:8000/api/health" "backend /api/health" "30" "1"       || fail "broadcast health endpoint check failed"
  fi

  if ! wait_local_http_endpoint "http://127.0.0.1:8000/gfs/api/health" "gfs api health" "20" "1"; then
    ui_warn "gfs api health not ready yet; continuing because core backend is up"
  fi

  if [[ "${SKIP_SSL:-0}" != "1" ]]; then
    if curl --noproxy '*' -kfsS -m 8 "https://$DOMAIN" >/dev/null; then
      ui_ok "public TLS endpoint ready"
    else
      if [[ "${STRICT_PUBLIC_HEALTH:-0}" == "1" ]]; then
        fail "public TLS endpoint check failed"
      fi
      ui_warn "public TLS endpoint check failed; backend install completed, check nginx/DNS/cert separately"
    fi
  fi
  validate_static_layout
  ui_ok "Installer completed successfully"
}

main() {
  normalize_installer_permissions
  phase1_system_prep
  phase2_python_runtime
  phase3_inland_water_data
  phase3_firewall
  phase4_google_cloud
  phase5_tls
  phase6_nginx
  phase7_services
  phase8_health
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_INFO='\033[1;96m'; C_WARN='\033[1;93m'; C_ERR='\033[1;91m'; C_OK='\033[1;92m'
else
  C_RESET=''; C_INFO=''; C_WARN=''; C_ERR=''; C_OK=''
fi

log_info() { printf "%b[INFO]%b %s\n" "$C_INFO" "$C_RESET" "$*"; }
log_warn() { printf "%b[WARN]%b %s\n" "$C_WARN" "$C_RESET" "$*"; }
log_error(){ printf "%b[ERROR]%b %s\n" "$C_ERR" "$C_RESET" "$*" >&2; }
log_ok()   { printf "%b[OK]%b %s\n" "$C_OK" "$C_RESET" "$*"; }

die() { log_error "$*"; exit 1; }

SUDO=""
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  SUDO="sudo"
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

run_required() {
  local title="$1"; shift
  log_info "$title"
  "$@"
  log_ok "$title"
}

run_optional() {
  local title="$1"; shift
  log_info "$title"
  if "$@"; then
    log_ok "$title"
  else
    log_warn "$title (continuing)"
  fi
}

ensure_dir() {
  local d="$1"
  [[ -d "$d" ]] || mkdir -p "$d"
}

render_template() {
  local src="$1" dst="$2"
  shift 2
  local content
  content="$(cat "$src")"
  while (( "$#" )); do
    local key="$1" val="$2"
    shift 2
    content="$(printf '%s' "$content" | sed "s|__${key}__|${val//|/\\|}|g")"
  done
  printf '%s\n' "$content" > "$dst"
}


mask_secret_value() {
  local value="$1"
  local len=${#value}
  if (( len <= 10 )); then
    printf '%s' '***'
    return
  fi
  printf '%s...%s' "${value:0:6}" "${value: -4}"
}

set_env_value() {
  local env_file="$1" key="$2" value="$3"
  local escaped
  escaped=$(printf '%s' "$value" | sed 's/[\&]/\\&/g')
  if [[ -f "$env_file" ]] && grep -qE "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*$|${key}=${escaped}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}

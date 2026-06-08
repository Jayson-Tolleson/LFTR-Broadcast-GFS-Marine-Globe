#!/usr/bin/env bash
set -euo pipefail

run_diagnostics() {
  log_info "Running deployment diagnostics"

  run_optional "app service status" $SUDO systemctl status "$APP_SERVICE_NAME" --no-pager
  run_optional "nginx status" $SUDO systemctl status nginx --no-pager

  if [[ "$ENABLE_COTURN" == "true" ]]; then
    run_optional "coturn status" $SUDO systemctl status coturn --no-pager
  fi

  run_optional "port checks" bash -lc "ss -lntu | grep -E ':(80|443|${APP_BIND_PORT}|${TURN_PORT}|${TURNS_PORT})'"
  run_optional "HTTP /watch check" curl -fsS "http://${APP_BIND_HOST}:${APP_BIND_PORT}/watch"

  if [[ "$ENABLE_NGINX" == "true" ]]; then
    run_optional "HTTP reverse proxy check" curl -fsS "http://127.0.0.1/watch"
    run_optional "HTTP /gfs/api/health check" curl -fsS "http://127.0.0.1/gfs/api/health"
  fi

  if [[ "$ENABLE_TLS" == "true" ]]; then
    if [[ -f "${CERT_DIR}/fullchain.pem" && -f "${CERT_DIR}/privkey.pem" ]]; then
      log_ok "TLS certificates present"
    else
      log_warn "TLS certificates missing at ${CERT_DIR}"
    fi
  fi
}

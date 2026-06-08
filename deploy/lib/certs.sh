#!/usr/bin/env bash
set -euo pipefail

_cert_paths_ready_impl() {
  local cert_dir="/etc/letsencrypt/live/${DOMAIN}"
  [[ -s "${cert_dir}/fullchain.pem" && -s "${cert_dir}/privkey.pem" ]]
}

_cert_valid_for_days_impl() {
  local days="${1:-30}"
  local cert="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  local seconds=$((days * 86400))
  [[ -s "$cert" ]] || return 1
  command -v openssl >/dev/null 2>&1 || return 1
  openssl x509 -checkend "$seconds" -noout -in "$cert" >/dev/null 2>&1
}

_cert_expiry_text_impl() {
  local cert="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  if [[ -s "$cert" ]] && command -v openssl >/dev/null 2>&1; then
    openssl x509 -enddate -noout -in "$cert" 2>/dev/null | sed 's/^notAfter=//'
  fi
}

issue_tls_certificate_impl() {
  local renew_days="${CERT_RENEW_DAYS:-30}"

  if _cert_paths_ready_impl && _cert_valid_for_days_impl "$renew_days"; then
    log_ok "Existing TLS certificate for ${DOMAIN} is valid for more than ${renew_days} days; skipping Certbot issuance"
    local expiry
    expiry="$(_cert_expiry_text_impl || true)"
    [[ -n "$expiry" ]] && log_info "Certificate expires: $expiry"
    return 0
  fi

  mkdir -p /var/www/certbot/.well-known/acme-challenge
  chown -R www-data:www-data /var/www/certbot || true

  local acme_conf="/etc/nginx/sites-available/broadcast-acme"
  cat > "$acme_conf" <<NGINX
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
NGINX

  rm -f /etc/nginx/sites-enabled/default         /etc/nginx/sites-enabled/broadcast         /etc/nginx/sites-enabled/broadcast.conf         /etc/nginx/sites-enabled/broadcast_stack         /etc/nginx/sites-enabled/broadcast-acme || true
  ln -sf "$acme_conf" /etc/nginx/sites-enabled/broadcast-acme
  nginx -t
  systemctl restart nginx

  if _cert_paths_ready_impl; then
    log_warn "Existing TLS certificate for ${DOMAIN} is present but near expiry or unreadable; attempting safe renewal"
    certbot renew --cert-name "$DOMAIN" --deploy-hook "systemctl reload nginx || true" || log_warn "certbot renew failed; attempting webroot issuance"
    if _cert_paths_ready_impl && _cert_valid_for_days_impl 1; then
      log_ok "TLS certificate for ${DOMAIN} is present after renewal attempt"
      return 0
    fi
  fi

  certbot certonly     --webroot     -w /var/www/certbot     --preferred-challenges http     --agree-tos     --email "$CERTBOT_EMAIL"     -d "$DOMAIN"     --keep-until-expiring     --expand     --non-interactive || log_warn "certbot issuance failed (continuing)"

  if _cert_paths_ready_impl; then
    log_ok "TLS certificate present for ${DOMAIN}"
  else
    log_warn "TLS certificate missing for ${DOMAIN}"
  fi

  if [[ "${CERTBOT_DRY_RUN:-0}" == "1" ]]; then
    certbot renew --dry-run || log_warn "certbot dry-run renewal failed"
  fi
}

#!/usr/bin/env bash
set -euo pipefail

configure_coturn_impl() {
  local cert="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  local pkey="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
  cat > /etc/turnserver.conf <<EOF
listening-port=${TURN_PORT}
tls-listening-port=${TURNS_PORT}
realm=${DOMAIN}
use-auth-secret
static-auth-secret=${TURN_PASS}
min-port=${TURN_MIN_PORT}
max-port=${TURN_MAX_PORT}
cert=${cert}
pkey=${pkey}
no-cli
EOF

  sed -i 's/^#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn || true
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat > /etc/letsencrypt/renewal-hooks/deploy/restart-coturn.sh <<'EOF'
#!/usr/bin/env bash
systemctl restart coturn
EOF
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/restart-coturn.sh
  systemctl enable coturn
  systemctl restart coturn
}

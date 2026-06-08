#!/usr/bin/env bash
set -euo pipefail

configure_nginx_impl() {
  local conf="/etc/nginx/sites-available/broadcast.conf"
  systemctl start nginx || true
  sleep 1
  cat > "$conf" <<EOF
map \$http_upgrade \$connection_upgrade {
    default upgrade;
    '' close;
}

upstream broadcast_app {
    server ${APP_BIND_HOST}:${APP_BIND_PORT};
    keepalive 32;
}


server {
    listen 80;
    server_name ${DOMAIN};
    client_max_body_size 512m;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location /static/ {
        alias ${APP_DIR}/static/;
        access_log off;
        expires 1h;
        add_header Cache-Control "public, max-age=3600";
        try_files \$uri =404;
    }

    location /uploads/ {
        alias ${APP_DIR}/uploads/;
        client_max_body_size 512m;
        try_files \$uri =404;
    }

    location ^~ /ws/ {
        proxy_pass http://broadcast_app;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
        proxy_cache off;
    }

    location ^~ /gfs/ws/ {
        proxy_pass http://broadcast_app;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
        proxy_cache off;
    }


    location / {
        proxy_pass http://broadcast_app;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
EOF
  rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/broadcast /etc/nginx/sites-enabled/broadcast.conf /etc/nginx/sites-enabled/broadcast_stack || true
  ln -sf "$conf" /etc/nginx/sites-enabled/broadcast.conf
  nginx -t
  systemctl restart nginx
  systemctl enable nginx
}

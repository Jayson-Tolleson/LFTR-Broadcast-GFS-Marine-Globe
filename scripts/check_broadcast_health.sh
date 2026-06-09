#!/usr/bin/env bash
set -euo pipefail
cd /home/jayson_tolleson/broadcast 2>/dev/null || true
printf '
== broadcast.service ==
'
systemctl status broadcast.service --no-pager -l || true
printf '
== local endpoints ==
'
for url in   http://127.0.0.1:8000/health   http://127.0.0.1:8000/api/health   http://127.0.0.1:8000/gfs/api/health   http://127.0.0.1:8000/gfs; do
  printf '%s -> ' "$url"
  curl --noproxy '*' -k -sS -m 5 -o /tmp/lftr_health_body -w 'HTTP=%{http_code} TIME=%{time_total}
' "$url" || true
  head -c 180 /tmp/lftr_health_body 2>/dev/null || true
  printf '
'
done
printf '
== recent journal ==
'
journalctl -u broadcast -n 160 --no-pager || true

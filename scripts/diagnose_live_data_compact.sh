#!/usr/bin/env bash
set -u
APP_URL="${APP_URL:-http://127.0.0.1:8000}"
BBOX="${BBOX:--119,32.5,-117,34.5}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[diag] app=$APP_URL bbox=$BBOX"
echo

echo "== service =="
(systemctl is-active broadcast 2>/dev/null || true) | sed 's/^/[broadcast] /'
(ps -o pid,ppid,%cpu,%mem,rss,cmd -C python -C bash 2>/dev/null | head -20 || true)
echo

echo "== health =="
for path in /health /api/health /gfs/api/health; do
  printf '%s -> ' "$path"
  curl -fsS --max-time 8 "$APP_URL$path" | "$PYTHON_BIN" -c 'import sys,json; s=sys.stdin.read();
try:
 d=json.loads(s); print({k:d.get(k) for k in ("ok","enabled","service","fish_count") if k in d})
except Exception: print(s[:220])' || echo "FAILED"
done
echo

summary() {
  local name="$1"; shift
  local url="$1"; shift
  echo "== $name =="
  curl -fsS --max-time 25 "$url" | "$PYTHON_BIN" -c '
import sys,json
s=sys.stdin.read()
try:
    d=json.loads(s)
except Exception as e:
    print("NON_JSON", str(e), s[:400]); raise SystemExit(0)
keys = {k:d.get(k) for k in ["ok","status","payload_state","source_state","display_state","mode","source","engine","count"] if k in d}
print(keys)
for arr in ["items","features","precip_columns","points","current_points","ocean_points","boats"]:
    v=d.get(arr)
    if isinstance(v,list): print(arr, len(v))
if isinstance(d.get("summary"), dict): print("summary", d.get("summary"))
if isinstance(d.get("grid"), dict): print("grid", d.get("grid"))
if isinstance(d.get("cache"), dict): print("cache", {k:d["cache"].get(k) for k in ["hit","mode","refresh_scheduled","age_sec","ttl_seconds","stale_seconds"] if k in d["cache"]})
if isinstance(d.get("confidence"), dict): print("confidence", d.get("confidence"))
if isinstance(d.get("debug"), dict): print("debug", d.get("debug"))
if isinstance(d.get("diag"), dict): print("diag", d.get("diag"))
if isinstance(d.get("debug_contract"), dict): print("debug_contract", d.get("debug_contract"))
' || true
  echo
}

summary clouds "$APP_URL/gfs/api/clouds?bbox=$BBOX"
summary bait "$APP_URL/gfs/api/bait?bbox=$BBOX&quality=full"
summary boats "$APP_URL/gfs/api/boats?bbox=$BBOX"
summary current-field "$APP_URL/gfs/api/field?field=current&bbox=$BBOX"
summary ocean-points "$APP_URL/gfs/api/ocean-points?bbox=$BBOX&lod=auto"

echo "== recent focused journal =="
sudo journalctl -u broadcast --since "10 minutes ago" --no-pager 2>/dev/null \
  | egrep -i "error|warning|traceback|exception|cloud_tiles|bbox contract|hycom|ocean subset|bait|boat|sst_shape|rejected" \
  | tail -80 || true

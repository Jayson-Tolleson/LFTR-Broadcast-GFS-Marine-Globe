#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/jayson_tolleson/broadcast}"
VENV_DIR="${VENV_DIR:-$APP_DIR/venv}"
APP_USER="${APP_USER:-jayson_tolleson}"
APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER" 2>/dev/null || echo "$APP_USER")}"

ensure_app_dir() {
  if [[ ! -d "$APP_DIR" ]]; then
    echo "[broadcast-runner] ERROR: app_dir missing app_dir=$APP_DIR" >&2
    exit 1
  fi
  cd "$APP_DIR" || {
    echo "[broadcast-runner] ERROR: unable to cd app_dir=$APP_DIR" >&2
    exit 1
  }
  export PWD="$APP_DIR"
}


ensure_static_dir() {
  local waited=0
  while [[ ! -d "$APP_DIR/static" || ! -f "$APP_DIR/static/indexgfs.html" ]]; do
    if [[ "$waited" -ge 20 ]]; then
      echo "[broadcast-runner] ERROR: static assets missing after wait static=$APP_DIR/static indexgfs=$APP_DIR/static/indexgfs.html" >&2
      exit 2
    fi
    echo "[broadcast-runner] waiting for static assets during deploy/unzip static=$APP_DIR/static waited=${waited}s" >&2
    sleep 1
    waited=$((waited + 1))
  done
}

ensure_runtime_dirs() {
  local dirs=(
    "$APP_DIR/.cache"
    "$APP_DIR/.cache/gfs_nomads"
    "$APP_DIR/.cache/gfs_scene"
    "$APP_DIR/.cache/gfs_tiles"
    "$APP_DIR/.cache/lightning"
    "$APP_DIR/data_sources"
    "$APP_DIR/data_sources/hycom_cache"
    "$APP_DIR/data_sources/nhd_runtime_cache"
    "$APP_DIR/data_sources/nhd_runtime_cache/_build_logs"
    "$APP_DIR/data_sources/nhdplus_hr_state_cache"
    "$APP_DIR/data_sources/nhdplus_hr_state_cache/_build_logs"
  )

  echo "[broadcast-runner] ensuring runtime cache/data directories are writable app_user=$APP_USER app_group=$APP_GROUP"
  for d in "${dirs[@]}"; do
    mkdir -p "$d" 2>/dev/null || true
  done

  chmod -R u+rwX,g+rwX "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true
  if [[ "$(id -u)" == "0" ]]; then
    chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true
    chmod -R u+rwX,g+rwX "$APP_DIR/.cache" "$APP_DIR/data_sources" 2>/dev/null || true
  fi

  if ! test -w "$APP_DIR/.cache"; then
    echo "[broadcast-runner] WARNING: $APP_DIR/.cache is not writable by $(id -un); cache-backed layers may fail"
  else
    echo "[broadcast-runner] cache writable ok path=$APP_DIR/.cache"
  fi
}

export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export PYTHONMALLOC="${PYTHONMALLOC:-malloc}"
export HDF5_USE_FILE_LOCKING="${HDF5_USE_FILE_LOCKING:-FALSE}"
export GFS_TILE_WARM_WORKERS="${GFS_TILE_WARM_WORKERS:-1}"
export GFS_TILE_READ_WORKERS="${GFS_TILE_READ_WORKERS:-1}"
export GFS_ALLOW_SYNTHETIC_FALLBACK="${GFS_ALLOW_SYNTHETIC_FALLBACK:-0}"
export ALLOW_SYNTHETIC_FALLBACK="${ALLOW_SYNTHETIC_FALLBACK:-0}"

shutdown_requested=0
child_pid=""

kill_child_tree() {
  local sig="${1:-TERM}"
  if [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    echo "[broadcast-runner] forwarding SIG$sig to hypercorn pid=$child_pid" >&2
    # Kill the child process group if possible, then the child itself.
    kill "-$child_pid" -s "$sig" 2>/dev/null || true
    kill -s "$sig" "$child_pid" 2>/dev/null || true
  fi
}

_term() {
  shutdown_requested=1
  echo "[broadcast-runner] shutdown requested; app_dir=$APP_DIR cwd=$(pwd 2>/dev/null || echo missing)" >&2
  kill_child_tree TERM
  local waited=0
  while [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null && [[ "$waited" -lt 12 ]]; do
    sleep 1
    waited=$((waited + 1))
  done
  if [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    echo "[broadcast-runner] hypercorn did not stop after ${waited}s; sending SIGKILL" >&2
    kill_child_tree KILL
  fi
  wait "$child_pid" 2>/dev/null || true
  exit 143
}
trap _term TERM INT

ensure_app_dir
ensure_static_dir
ensure_runtime_dirs

echo "[broadcast-runner] durable foreground supervisor starting app_dir=$APP_DIR cwd=$(pwd) shell_pid=$$" >&2
while true; do
  # Important: installers/unzip operations can replace the app folder while the
  # runner survives. Re-enter APP_DIR before every Hypercorn spawn so Python's
  # multiprocessing spawn never calls os.getcwd() on a deleted cwd.
  ensure_app_dir
  echo "[broadcast-runner] starting hypercorn app_dir=$APP_DIR cwd=$(pwd)" >&2

  set +e
  setsid "$VENV_DIR/bin/python" "$APP_DIR/scripts/run_hypercorn_single.py" &
  child_pid=$!
  wait "$child_pid"
  code=$?
  set -e

  child_pid=""
  if [[ "$shutdown_requested" == "1" ]]; then
    echo "[broadcast-runner] hypercorn stopped during requested shutdown code=$code" >&2
    exit "$code"
  fi

  echo "[broadcast-runner] hypercorn exited unexpectedly code=$code; restarting in 3s" >&2
  sleep 3
done

#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"

echo "[cloud-hybrid] settings"
grep -R --line-number -Ei 'GFS_CLOUD_PARTICLE_RENDER_STYLE|GFS_CLOUD_HYBRID_ELLIPSE|GFS_CLOUD_MAX_REGIONS|GFS_MAX_CLOUD_PARTICLES' .env .env.example deploy/templates/app.env.template deploy/lib/google.sh 2>/dev/null || true

echo
echo "[cloud-hybrid] scene frame cloud budget"
curl -fsS --max-time 90 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=clouds,rain&mode=refresh&refresh=1&provider_jobs=1&reason=manual_cloud_hybrid_ellipse_check" \
| python3 -m json.tool \
| grep -Ei '"max_cloud_shells"|"max_cloud_particles"|"cloud"|"render_budget"|"source"|"status"'

echo
echo "[cloud-hybrid] recent cloud render lines"
journalctl -u broadcast -n 360 --no-pager \
| grep -Ei 'clouds/render|maxCloudBodies|maxCloudParticles|cloudParticleMode|particleGovernor|hybrid|ellipse|particles|bodies' \
| tail -160 || true

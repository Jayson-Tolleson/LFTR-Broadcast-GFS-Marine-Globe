#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
BBOX="${2:--136,32,-94,55}"

echo "[cloud-demand] env/defaults"
grep -R --line-number -Ei 'GFS_CLOUD_MAX_REGIONS|GFS_MAX_CLOUD_PARTICLES|GFS_CLOUD_PARTICLE_MODE|GFS_CLOUD_PARTICLE_SIZE' .env .env.example deploy/templates/app.env.template deploy/lib/google.sh 2>/dev/null || true

echo
echo "[cloud-demand] scene frame cloud budget"
curl -fsS --max-time 90 "$BASE/gfs/api/scene-frame?bbox=$BBOX&visible_bbox=$BBOX&layers=clouds,rain&mode=refresh&refresh=1&provider_jobs=1&reason=manual_cloud_particle_demand_check" \
| python3 -m json.tool \
| grep -Ei '"max_cloud_shells"|"max_cloud_particles"|"cloud"|"render_budget"|"source"|"status"'

echo
echo "[cloud-demand] recent browser/server cloud lines"
journalctl -u broadcast -n 300 --no-pager \
| grep -Ei 'clouds/render|maxCloudBodies|maxCloudParticles|cloudParticleMode|particleGovernor|particles|bodies' \
| tail -120 || true

# Cloud TTL morph + particle throttle patch

This patch targets the cloud renderer lifecycle, not the GFS data provider.

## Fixes

- Cloud cache/TTL updates no longer force redraws just because cache metadata changes.
- Cloud layer signature now ignores cache `ttl`, `warmed_at`, and version churn, and keys mostly on actual drawable geometry content.
- New cloud scenes fade in before old cloud scenes fade out, so TTL refreshes look like a morph/crossfade instead of a flash/blank/repaint.
- Old clouds linger and fade out more slowly after a replacement scene is appended.
- Cloud particles are capped separately from cloud shells and are sharply reduced at world/global zoom.
- During a cloud replacement transition, particle cap is temporarily reduced so the GPU is not hit by both old and new particle sets at full density.

## Default particle budget after patch

- World/global: low accent particles only.
- Regional/coastal: medium particles.
- Local/harbor: higher particles, still bounded.
- Set `window.GFS_CLOUD_PARTICLE_MODE = "off" | "eco" | "low" | "balanced" | "high"` before module load to tune.

## Verify

Run:

```bash
bash scripts/check_cloud_ttl_morph_particles_patch.sh
```

Browser console checks:

```js
window.GFS_CLOUD_PARTICLE_MODE = 'eco'
window.__GFS_CLOUD_MIN_RENDER_INTERVAL_MS
```

Watch for:

- `render/noop-same-version` on cache-only TTL updates.
- No blanking on cloud refresh.
- Lower `particles` counts in `[gfs clouds] rendered bodies` logs.

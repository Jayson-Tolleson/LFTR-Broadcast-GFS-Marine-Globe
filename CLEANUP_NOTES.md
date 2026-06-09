# LFTR cleanup notes

This build applies the five cleanup targets:

1. **Giant route/service files reduced by extraction points**
   - Inland Waters now lives behind `server/gfs/inland/` with a compatibility facade at `server/gfs/inland_water.py`.
   - Cache policy moved to `server/gfs/cache_policy.py`.
   - Existing imports still work.

2. **Inland Waters is its own subsystem**
   - New package layout:
     - `server/gfs/inland/payloads.py` — current implementation
     - `server/gfs/inland/cache.py` — runtime tile cache helpers
     - `server/gfs/inland/builders.py` — builder contract constants
     - `server/gfs/inland/geometry.py` — shoreline-only geometry helpers
     - `server/gfs/inland/temp.py` — temperature label contract
     - `server/gfs/inland/bait.py` — inland bait contract

3. **Cache policy collapsed toward provider cache + scene cache**
   - Scene-cache janitor now delegates to a single `cache_policy` helper.
   - Old cache names remain as compatibility shims, but the policy is centralized.

4. **Persistent scene-object registry added**
   - New frontend module: `static/js/gfs/scene_registry.js`.
   - Cloud rendering now uses persistent layer reconciliation instead of relying on full clear/rebuild cycles.

5. **Rendering cleanup for clouds**
   - Cloud renderers now return `__gfsKeepExisting` disposers through the persistent registry.
   - RendererLayer now replaces persistent disposers safely without firing the old one during refresh.
   - Pill-off still clears the layer.

Validation run:

```bash
python3 -m py_compile server/gfs_service.py server/gfs/inland_water.py server/gfs/inland/payloads.py server/gfs/cache_policy.py server/gfs/routes.py
node --check static/js/gfs/scene_registry.js
node --check static/js/gfs/cloud-zones.js
node --check static/js/gfs/layers/renderer_layer.js
```

## Cloud filled-shell follow-up
- Switched clouds from outline-dominant / low-fill carrier material to fill-first shell material.
- Disabled detached CSS drop-shadow by default; 4x edge glow is still represented through stroke/material without drawing a separate silhouette.
- Raised top cap, bottom cap, sidewall, minimum fill, and max coverage-fill defaults so cloud polygons read as real shaded shells.
- Preserved cloud particles, jitter, wobble, and pooled particle updates; particles now act as texture/motion over the filled shell rather than the only visible cloud mass.
- Kept cap surfaces thin-stroked so they do not become duplicate 4x outline paths.

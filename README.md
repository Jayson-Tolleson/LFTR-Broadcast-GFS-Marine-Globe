# LFTR Broadcast / GFS Marine Intelligence Globe

Python 3 Quart + Hypercorn web app with HTML / JS / CSS routes for live broadcasting, viewer watch, and the `/gfs` marine intelligence globe.

## Routes

```text
/           landing / primary app
/broadcast  broadcaster camera and screen route
/watch      viewer route
/gfs        marine intelligence globe
```

## Install / update

This zip is packaged to unzip into `~/broadcast`.

```bash
cd ~
unzip -o LFTR-Broadcast-GFS-patched-cloud-single-3d-installer.zip -d ~
cd ~/broadcast
sudo bash scripts/repair_broadcast_service.sh
```

Full installer:

```bash
cd ~/broadcast
sudo bash deploy/install.sh
```

Health checks:

```bash
systemctl status broadcast --no-pager
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/gfs/api/health
journalctl -u broadcast -n 160 --no-pager
```

## Runtime service

The expected service command is:

```text
/home/jayson_tolleson/broadcast/venv/bin/python -m hypercorn --bind 127.0.0.1:8000 --workers 1 main:app
```

Single-worker Hypercorn is intentional because the app keeps scene/cache state in-process.

## Architecture contract

```text
requests create cache
cache creates frame
frame creates rendering
pills choose visibility/subscription only
```

Pills should not directly own provider downloads. They should subscribe to layers and let scene-cache / renderer code decide what to draw.

Layer groups:

```text
weather padded bbox:
  clouds
  rain
  lightning
  jetstream
  inland_water_temp

ocean visible bbox:
  bait
  shark-intel
  boater

static/read-first geometry:
  locations
  inland-water
```


## BBox contract v2

This build makes bbox meanings explicit so padded atmosphere fetches do not leak into boats, bait, shorelines, or labels.

```text
visible_bbox        exact/current viewport draw box
render_bbox         same as visible_bbox for renderer placement
scene_read_bbox     cache read box; visible unless the request is weather-only
weather_fetch_bbox  padded box for clouds/rain/lightning/jetstream sampling
ocean_fetch_bbox    visible bbox for bait, boater, shark-intel
shoreline_bbox      compact visible bbox with small shoreline pad only
jetstream_bbox      visible bbox where balloons are placed
jetstream_fetch_bbox padded weather box used only for jet wind sampling
```

Debug now emits `bbox/contract`, `bbox_contract`, `visible_bbox`, and bbox role notes on scene-cache and cache-refresh calls.

## Jetstream balloons check/fix

Jet balloons now use the bbox contract directly:

```text
balloon placement:     jetstream_bbox / visible viewport
wind sampling fetch:   jetstream_fetch_bbox / padded weather fetch
DOM marker flag:       data-jetstream-balloon="true"
invalid positions:     skipped instead of creating bad gmp-marker-3d positions
animation loop:        dead/replaced balloons are compacted so removed markers do not stay in the item list
```

## Current visual patch

This build includes the style patch requested after the risky cleanup:

```text
inland-water shorelines: bright teal core + soft teal glow, shoreline-only, no lake fill
cloud shells: full filled shell default, top/bottom shell surfaces default-on
cloud shell edges: 4x thicker/glowier shell edges
cloud fill policy: fill remains authoritative so clouds do not become border-only outlines
README policy: this is the only markdown/readme file in the package
```

Important cloud knobs are available in the browser before boot if needed:

```js
window.GFS_CLOUD_SHELL_SURFACES_ENABLED = true;
window.GFS_CLOUD_SHELL_BOTTOM_SURFACE_ENABLED = true;
window.GFS_CLOUD_SHELL_EDGE_WIDTH_MULTIPLIER = 4;
window.GFS_CLOUD_SHELL_EDGE_OPACITY_MULTIPLIER = 4;
window.GFS_CLOUD_SHELL_OPACITY = 1.0;
```

## Inland water behavior

Inland water is cache-first and builder-backed:

```text
/gfs/api/inland-water              read-only geometry cache
/gfs/api/inland-water/build-cache  explicit builder
```

Healthy debug sequence:

```text
inland-water/cache-read
inland-water/build-cache-start
inland-water/build-cache-response
inland-water/cache-read-end polygons > 0 or lines > 0
inland-water/render polygons > 0 or lines > 0
inland-water/shoreline-complete mode=shoreline_only_teal_glow
```

Manual build-cache test:

```bash
curl -s "http://127.0.0.1:8000/gfs/api/inland-water/build-cache?bbox=-127,28,-113,40&visible_bbox=-127,28,-113,40&scene_tier=world&geometry=coarse&lod=auto&parallel=1&max_tiles=96&reason=manual_build" | python3 -m json.tool
```

Manual read test:

```bash
curl -s "http://127.0.0.1:8000/gfs/api/inland-water?bbox=-127,28,-113,40&visible_bbox=-127,28,-113,40&scene_tier=world&source=auto&geometry=coarse&lod=auto&cache=1&tile_cache=1&parallel=1&auto_build=0&max_tiles=96&reason=manual_read" | python3 -m json.tool
```

## Cloud rendering behavior

Clouds are intended to render as:

```text
one filled extruded shell
+ full shell cap surfaces
+ integrated 4x shell edge glow
+ subordinate animated particles inside the shell
```

The fill-first material is reapplied by a browser watchdog so a Maps 3D style refresh should not leave only borders behind.

Expected debug / DOM markers:

```text
data-cloud-solid-shell="true"
data-cloud-full-shell-default="top_and_bottom_caps_on"
data-cloud-edge-glow-4x="true"
renderPath: unified_filled_shell_with_subordinate_texture_particles
```

## Risky cleanup summary

This package intentionally removed old/parallel code paths from the earlier build:

```text
removed pycache / pyc files
removed old FastAPI sea sidecar and sidecar service/nginx pieces
removed unused tile-runtime scaffolding that was not the live authority
removed unused service wrapper packages and experimental AI queue pieces
removed /gfs/api/sea compatibility shell entirely
kept live Quart routes, GFSService, inland builder, broadcast/watch, and active /gfs render modules
```

The live authority is now the Quart app plus `GFSService`, scene-cache routes, active inland-water builder/cache, and active frontend renderers.

## Useful commands

```bash
# service repair
sudo bash scripts/repair_broadcast_service.sh

# service logs
journalctl -u broadcast -n 200 --no-pager

# quick health
sudo bash scripts/check_broadcast_health.sh

# syntax smoke checks from ~/broadcast
python3 -m compileall -q .
find static/js -name '*.js' -print0 | xargs -0 -n1 node --check
```

## Notes

The package is cleaned for the current LFTR `/broadcast`, `/watch`, and `/gfs` direction. If an older removed sidecar/tool is needed again, restore it deliberately instead of allowing it to quietly compete with the live scene-cache/render path.

### Cloud visual mode: edge/particle no-cover

This build keeps clouds darker/cooler and more affected-looking while restoring both the top and bottom shell cap polygons by default. Cloud mass is still carried by billowy particle texture plus a cold side tint, but the shell now renders with top and bottom caps on and 4x edge glow. Debug markers include `data-cloud-no-cover-polygon="false"`, `data-cloud-full-shell-default="top_and_bottom_caps_on"`, and `data-cloud-edge-glow-4x="true"`. Operators can still disable a cap explicitly by setting `window.GFS_CLOUD_SHELL_SURFACES_ENABLED = false` or `window.GFS_CLOUD_SHELL_BOTTOM_SURFACE_ENABLED = false` before the module loads.



## Cloud polygon root fix

This build fixes the root Maps 3D polygon contract instead of adding more cloud geometry. `gmp-polygon-3d` supports `extruded` as a boolean ground-connection only; there is no supported finite `extrudedHeight` attribute. The shared polygon helper now treats boolean attributes correctly, removes `extruded="false"` instead of leaving it present, and stores requested extrusion height only as debug metadata. Cloud polygons explicitly set `extrudeToGround: false`, so high-altitude clouds no longer create hidden/heavy ground curtains. Top and bottom cloud caps remain on, 4x edge glow remains on, and particle defaults are reduced for lighter rendering.

Cloud debug markers include `data-cloud-full-shell-default="top_and_bottom_caps_on"`, `data-cloud-native-ground-extrude="false"`, and `data-cloud-edge-glow-4x="true"`.

## Cleanup build notes

This package keeps one README and moves the detailed refactor notes to `CLEANUP_NOTES.md`.

Current architectural direction:

```text
providers -> provider cache -> scene cache -> persistent renderer objects
```

Pills should only choose visibility/subscription. They should not own provider fetches.

Inland Waters is now isolated behind `server/gfs/inland/` while the old `server/gfs/inland_water.py` import remains as a compatibility facade.

Clouds now use the persistent scene registry in `static/js/gfs/scene_registry.js` so refreshes reconcile live objects instead of blanking the whole cloud layer first.


## Universal 24x24 provider tile contract

The GFS/provider path now uses one congruent viewport splitter:

```text
visible bbox -> 24 x 24 = 576 universal tiles
576 tiles x 6 providers = 3,456 possible provider-tile jobs
```

The six provider families are:

```text
ncss_gfs
rtofs
hycom
inland_geometry
usgs_waterflow
shoreline
```

Every provider receives the same `tile_id` and the same tile bbox for a given viewport.  This is intentionally less clever than provider-specific tiling: it gives us one bbox truth, one cache key shape, one debug story, and no hidden legacy/global tile math in the normal provider contract.

Useful debug calls:

```bash
curl -s "http://127.0.0.1:8000/gfs/api/tile-plan?bbox=-119,33.5,-118,34.5" | python3 -m json.tool
curl -s "http://127.0.0.1:8000/gfs/api/provider-tiles?bbox=-119,33.5,-118,34.5" | python3 -m json.tool
curl -s "http://127.0.0.1:8000/gfs/api/provider-tiles?bbox=-119,33.5,-118,34.5&providers=ncss_gfs,hycom&urls=1&limit=4" | python3 -m json.tool
```

## GFS service split cleanup

The former monolithic `server/gfs_service.py` has been reduced to a small coordinator class plus behavior-preserving mixins under `server/gfs_service_parts/`:

- `core.py` — shared app/service helpers, fish/environment intelligence, health/config payloads.
- `atmosphere.py` — GFS decode, cloud/rain/lightning/wind derivation, weather scene composition.
- `tiles_scene.py` — tile bounds, scene tile cache, inland-water tile merging, tile intel.
- `ocean_bait_frame.py` — provider diagnostics, ocean/current/boats/bait, frame cache and warming.
- `lightning_cache_media.py` — GOES GLM lightning cache, always-on warming, media/report helpers, scene-cache facade.

This is intentionally not a new legacy layer. It is a readability split around the existing runtime behavior so the next cleanup can delete or consolidate individual subsystems without editing a 9k-line file.

## Cleanup Notes

### 2026-06-08 reduction/deletion pass 2

- Removed unused browser helpers from `static/js/gfs/main.js`.
- Removed unused cloud helper paths from `static/js/gfs/cloud-zones.js`.
- Removed the legacy `/gfs/api/cache/warm` compatibility route; browser warming now nudges `/gfs/api/cache/refresh`.
- Removed orphan service methods from the split GFS service parts.
- Re-ran Python compile and JavaScript syntax validation.

## 2026-06-07 boot safety + earth-first priority

- Fixed three ES-module syntax regressions that could stop `/gfs` from booting:
  - `static/js/gfs/api.js` restored `jget(...)`.
  - `static/js/gfs/boats.js` restored `fetchBoatsPayload(...)`.
  - `static/js/gfs/inland-water.js` restored inland bait/temp helper function headers.
- Added an earth-first boot policy: the photorealistic globe gets first paint/update priority, while scene-cache/inland/background overlay warming is deferred slightly.
- Canonical viewport metadata now marks `quality: high` and `earth_priority: high_res_first` so debug payloads show the intended base-earth priority.
- Validation now checks JS files as ES modules, not just loose scripts, so missing function headers are caught.


## SST truth check

This build maps `shark-intel` to HYCOM warm jobs as well as bait/boater, and includes:

```bash
./scripts/check_sst_truth.sh http://127.0.0.1:8000 -126,28,-114,40
```

Expected: direct `/gfs/api/ocean` reports non-empty `sst_shape`, `u_shape`, and `v_shape`; scene-cache reports bait/boater live from HYCOM-backed sources; shark-intel warms from SST-masked math.

## Stability hotfix: cache warmers + inland bait/temp + polar cloud seam

This build forces the compatibility symbols that older split-cache warmers still import:

- `ALLOW_SYNTHETIC_FALLBACK=False` in `server.gfs_service`
- `_centroid`, `_temperature_point`, `_enrich_inland_feature` in `server.gfs.inland_water`
- no packaged `__pycache__` files
- cloud polar seam fallback so valid cloud payloads are not fully rejected at `82.5..90` edge tiles
- systemd repair now uses `scripts/run_broadcast_service.sh`, `PYTHONDONTWRITEBYTECODE=1`, and `Restart=on-failure` to avoid clean-exit restart loops

After install:

```bash
cd ~/broadcast
sudo bash scripts/repair_broadcast_service.sh
sudo systemctl restart broadcast
journalctl -u broadcast -n 300 --no-pager | grep -Ei "ALLOW_SYNTHETIC|_centroid|split warm failed|hycom|sst_shape|clouds|restart counter"
```

Good signs:

- no `ALLOW_SYNTHETIC_FALLBACK is not defined`
- no `cannot import name '_centroid'`
- HYCOM still reports `sst_shape`, `u_shape`, `v_shape`
- cloud polar seam warnings are reduced


### HYCOM variable contract

Ocean truth is HYCOM-only. The primary ESPC-D-V02 surface request uses `sst`, `sss`, `ssu`, and `ssv`.
`ssu` is the eastward surface sea-water velocity and `ssv` is the northward surface sea-water velocity. `water_u` / `water_v` are only used by the secondary depth-0 current-only diagnostic attempt and cannot satisfy SST-gated bait/shark truth by themselves.


### Ocean depth contract

HYCOM remains the water/SST/current truth gate. Depth is restored as a companion field on HYCOM-proven ocean points: `bottom_depth_m`, `bottom_depth_ft`, `preferred_bait_depth_m/ft`, and `bait_depth_band_m/ft`. This estimate does not prove water and does not bypass HYCOM SST/current; it only enriches cells that already passed the HYCOM gate.


### Congruent bait/depth intelligence contract

Ocean bait uses HYCOM SST/current as the ocean truth gate and attaches `depth_intel` as a HYCOM-gated bathymetry companion. Inland bait does not use HYCOM; it uses USGS/NHD waterbody geometry plus inland flow/current estimates and live/derived temperature. Both expose the same HUD-facing fields: `bottom_depth_ft`, `preferred_bait_depth_ft`, `bait_depth_ft`, `bait_depth_band_ft`, and `depth_intel.source`, so the intel pane can read depth consistently without pretending inland depth came from HYCOM.


## Runtime cache permission repair

If journalctl shows `[Errno 13] Permission denied: '/home/jayson_tolleson/broadcast/.cache'`, run:

```bash
cd ~/broadcast
sudo bash scripts/fix_runtime_cache_permissions.sh
```

The service runner also self-heals `.cache` and `data_sources` ownership/writability on every service start so GFS scene cache, lightning, HYCOM, inland-water logs, bait, and boater contracts can build.


## Cloud particle demand governor

Cloud bodies stay at the 500-body budget, but particles default to a balanced governor to reduce GPU/DOM demand.

Defaults:
- `GFS_CLOUD_MAX_REGIONS=500`
- `GFS_MAX_CLOUD_PARTICLES_DESKTOP=5200`
- `GFS_MAX_CLOUD_PARTICLES_MOBILE=2600`
- `GFS_CLOUD_PARTICLE_SIZE_MULTIPLIER=0.25`
- `GFS_CLOUD_PARTICLE_MODE=balanced`

Runtime modes:
- `balanced` keeps high body count with safer particles.
- `safe` or `low` reduces particle pressure further.
- `eco` is the emergency low-demand mode.
- `high` or `party` restores high-density particles for testing.


## Hybrid cloud ellipse clusters

Cloud particles now default to a hybrid approach: each cloud body keeps its shell, then gets about 10–20 persistent 3D marker ellipses. Each ellipse marker contains many tiny SVG micro-specks, so it reads as 100–1000 particles of mass without creating 100–1000 independent map objects per body.

Key settings:
- `GFS_CLOUD_PARTICLE_RENDER_STYLE=hybrid_ellipse_clusters`
- `GFS_CLOUD_HYBRID_ELLIPSE_MIN_PER_BODY=10`
- `GFS_CLOUD_HYBRID_ELLIPSE_MAX_PER_BODY=20`
- `GFS_CLOUD_HYBRID_ELLIPSE_MICRO_MIN=9`
- `GFS_CLOUD_HYBRID_ELLIPSE_MICRO_MAX=22`

Ellipse size/count is pulled from cloud coverage, shell thickness, family, and footprint. The existing advection/wobble path moves the clusters with wind plus local jitter.


## Ocean points to renderers fix

This build fixes a backend `os` import gap in env-driven ocean/bait code and relaxes the boat frontend gate. HYCOM often returns live SST/current samples while `validTime` is absent in the payload alias; boats now render when finite HYCOM SST/current samples and enough ocean points are present.

Check with:

```bash
cd ~/broadcast
bash scripts/check_ocean_points_to_renderers.sh
```


## Ocean analysis detail and 10-boat default

This build starts the boater layer at 10 boats and separates the ocean data field from the visible boat count.

Targets:
- `GFS_BOAT_COUNT_MAX=10`
- `GFS_VIEWPORT_BOAT_COUNT=10`
- `GFS_OCEAN_ANALYSIS_MODE=coastal_refine`
- `GFS_OCEAN_POINTS_DATA_MAX=5000`
- `GFS_OCEAN_POINTS_RENDER_MAX=600`
- `GFS_ADVANCED_BAIT_DETAIL_MULTIPLIER=4`
- `GFS_ADVANCED_BAIT_GRID_CAP=420`

The intended chain is:

```text
HYCOM finite SST/current cells
  -> oceanAnalysisPoints for boats/shark/HUD/current squares
  -> advancedBaitRows for dense bait probability + marching-square contours
```

Check with:

```bash
cd ~/broadcast
bash scripts/check_ocean_analysis_detail.sh
```


## Boat GLB transform cleanup

The boat GLB now defaults to a water-hugging, stable-scale transform:

- `GFS_BOAT_MODEL_SCALE=1.0`
- `GFS_BOAT_MODEL_SCALE_MAX=1.0`
- `GFS_BOAT_MODEL_SCALE_GROWTH=0.0`
- `GFS_BOAT_WATER_ALTITUDE_M=0.25`
- `GFS_BOAT_GLYPH_ALTITUDE_M=7.5`
- `GFS_BOAT_UNDERGLOW_ALTITUDE_M=1.2`
- `GFS_BOAT_MODEL_YAW_OFFSET_DEG=0`

Only `GFS_BOAT_MODEL_YAW_OFFSET_DEG` should need tuning if the GLB export's bow axis is off. Try 90, 180, or 270 if the bow points sideways/backward.


## Focused error stability patch

This build focuses on the boot errors exposed by debug/journal:

- compile-proofs Python files that use `os.getenv`
- guards inland-water temp/bait enrichment so shoreline render continues if enrichment fails
- avoids HYCOM stride 2/3 on larger coastal boxes by default because those requests timed out
- adds `scripts/check_focused_errors.sh`

Run:

```bash
cd ~/broadcast
bash scripts/check_focused_errors.sh
```


## HYCOM as first-class ocean provider

HYCOM now has an explicit provider facade:

```python
from server.gfs.providers.hycom import HycomProvider
```

The old `RtofsProvider` name remains as a compatibility alias, but the contract is now:

```text
HycomProvider
  -> oceanAnalysisPoints for boats/shark/HUD/current squares
  -> advancedBaitRows for dense bait contours
```

Check with:

```bash
cd ~/broadcast
bash scripts/check_hycom_provider_contract.sh
```


## HYCOM stride detail profile

This build lowers HYCOM stride one tier now that provider load is better distributed.

Defaults:
- `GFS_HYCOM_WORLD_STRIDE=9`
- `GFS_HYCOM_REGIONAL_STRIDE=6`
- `GFS_HYCOM_COASTAL_STRIDE=4`
- `GFS_HYCOM_HARBOR_STRIDE=3`
- `GFS_HYCOM_TIMEOUT_SAFE_STRIDE=4`
- `GFS_HYCOM_MAX_PROVIDER_STRIDE=9`
- `GFS_OCEAN_POINTS_DATA_MAX=6500`
- `GFS_OCEAN_POINTS_RENDER_MAX=800`

This is more detailed than the prior safe profile, while still avoiding stride 1/2 on large viewport boxes until true HYCOM tiling lands.


## HYCOM aggressive stride profile

This build applies the requested aggressive HYCOM stride policy. NCSS `horizStride`
must be an integer, so `.25` is normalized to native/no stride (`1`, omitted from
the NCSS query).

Applied defaults:
- `GFS_HYCOM_WORLD_STRIDE=2`
- `GFS_HYCOM_REGIONAL_STRIDE=1`
- `GFS_HYCOM_COASTAL_STRIDE=2`
- `GFS_HYCOM_HARBOR_STRIDE=1`
- `GFS_HYCOM_TIMEOUT_SAFE_STRIDE=1`
- `GFS_HYCOM_MAX_PROVIDER_STRIDE=2`
- `GFS_OCEAN_POINTS_DATA_MAX=12000`
- `GFS_OCEAN_POINTS_RENDER_MAX=1200`

This is intentionally high-demand and may timeout on large viewport boxes until
HYCOM tiling is implemented.


## HYCOM provider circular import hotfix

If `/health` never comes up and journalctl shows:

```text
ImportError: cannot import name 'HycomProvider' from partially initialized module 'server.gfs.providers.hycom'
```

run:

```bash
cd ~/broadcast
sudo bash scripts/fix_hycom_provider_import.sh
```

The fixed provider facade imports the legacy implementation from
`server.gfs.providers.rtofs`, not from itself.


## Globe boot safety

`static/js/gfs/main.js` now lazy-loads high-risk overlay modules (`bait`, `boats`,
`inland-water`, `shark-intel`, `current-zones`) after the base module starts.
A broken layer module should no longer prevent the globe from booting.

Check with:

```bash
cd ~/broadcast
bash scripts/check_globe_boot_frontend.sh
```


## Runner working-directory stability

The service runner now re-enters `/home/jayson_tolleson/broadcast` before every
Hypercorn restart. This prevents Python multiprocessing from failing with:

```text
FileNotFoundError: [Errno 2] No such file or directory
dir=os.getcwd()
```

The systemd service/template also includes:

```ini
WorkingDirectory=/home/jayson_tolleson/broadcast
Environment=APP_DIR=/home/jayson_tolleson/broadcast
KillMode=control-group
TimeoutStopSec=18
```

For an already-installed server, run:

```bash
cd ~/broadcast
sudo bash scripts/fix_runner_workdir.sh
bash scripts/check_runner_workdir.sh
```

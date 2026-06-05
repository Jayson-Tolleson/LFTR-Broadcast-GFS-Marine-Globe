# LFTR Marine Intelligence Globe

Quart/Hypercorn web app for the LFTR live broadcast stack and marine intelligence globe.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Main landing / globe entry. |
| `/gfs` | Google Maps 3D marine intelligence globe. |
| `/broadcast` | Broadcaster camera / screen / audio route. |
| `/watch` | Viewer route for live stream playback. |
| `/gfs/api/*` | Weather, ocean, bait, boats, clouds, rain, lightning, inland-water, cache, and debug APIs. |

## What this build focuses on

This package includes the recent runtime fixes for:

- Fast lightweight `/gfs/api/frame` controller shell.
- Shared GFS decoded snapshot reuse to reduce repeated `cfgrib` opens.
- FastAPI sea-of-intelligence sidecar for derived clouds/rain/bait/boats/jetstream contour payloads.
- Progressive inland-water first-run build.
- Land-first inland-water source-square planning.
- Inland-water prominence filtering so only the larger / more important water bodies are highlighted.
- Cache-first visual staging so `/gfs` can open before every heavy layer is complete.

## Install

```bash
unzip LFTR-Broadcast-GFS-Marine-Globe-*.zip
cd broadcast
sudo bash broadcast.sh
```

The installer configures Python, Hypercorn, nginx, systemd, TLS, firewall rules, Google/Vertex configuration, and the app service.

After install:

```bash
sudo systemctl status broadcast --no-pager
journalctl -u broadcast -n 200 --no-pager
curl -s http://127.0.0.1:8000/gfs/api/config | python3 -m json.tool
```

## Update / reinstall

From the app directory:

```bash
sudo bash broadcast.sh
sudo systemctl restart broadcast
```

If nginx was changed:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Important environment variables

The installer writes these into the app environment. Defaults are tuned for a small GCP VM, around 2 vCPU / 16 GB RAM.

```bash
# Maps / domain
DOMAIN=lftr.biz
GOOGLE_MAPS_API_KEY=...

# GFS frame and decoded-source reuse
GFS_FRAME_LIGHTWEIGHT_ONLY=true
GFS_FRAME_HEAVY_WARM=false
GFS_DECODE_SNAPSHOT_TTL_SECONDS=900

# GFS/cache profile
GFS_TILE_WARM_WORKERS=3
GFS_TILE_WARM_BUILD_LIMIT=32
GFS_ALWAYS_ON_CACHE=true
GFS_ALWAYS_ON_INTERVAL_SEC=180
GFS_ALWAYS_ON_MAX_TILES=96

# FastAPI sea engine
LFTR_FASTAPI_ENABLED=true
LFTR_FASTAPI_BIND_HOST=127.0.0.1
LFTR_FASTAPI_BIND_PORT=8010
LFTR_SEA_GRID_SIZE=48
LFTR_SEA_CRISP_MODE=true

# Inland water first-run and prominence
LFTR_INLAND_WATER_PROMINENCE_FRACTION=0.25
LFTR_INLAND_WATER_MIN_PROMINENT_FEATURES=8
LFTR_INLAND_WATER_MIN_POLYGON_POINTS=12
LFTR_INLAND_WATER_MIN_LINE_POINTS=8
NHDPLUS_STATE_CACHE_DAYS=180
NHD_ARCGIS_TILE_DEG=0.5
NHD_ARCGIS_PAGE_SIZE=100
```

A fuller example is provided in `.env.example`.

## `/gfs/api/frame` behavior

`/gfs/api/frame` is intentionally lightweight in this build. It should answer quickly with a controller/status shell instead of blocking on the full weather scene.

Expected debug payload:

```json
{
  "status": "frame_controller_shell",
  "source": "fast_frame_waiting_for_tile_cache",
  "cache": {
    "mode": "frame_lightweight_controller",
    "heavy_composite_disabled": true,
    "heavy_refresh_deferred": true
  }
}
```

Real render payloads are staged through the layer APIs:

```text
/gfs/api/sea
/gfs/api/clouds
/gfs/api/boats
/gfs/api/bait-advanced
/gfs/api/lightning
/gfs/api/inland-water
/gfs/api/tiles
/gfs/api/cache-warm
```

## Inland water first-run behavior

Inland water is strict real-source only. The app does **not** draw seed/coarse/mock water.

On a fresh install, the first `/gfs` load may show:

```json
{
  "status": "building",
  "source": "real_usgs_nhd_arcgis_view_tiles_building",
  "selected_tiles": 0,
  "polygons": 0
}
```

That is normal while real USGS/NHD source squares are building. The progressive policy is:

```text
no local high-def tiles yet
→ schedule real source build for the visible land/state bbox
→ fetch one source square at a time
→ write .json.gz render tiles and index.json as each square completes
→ draw partial water as soon as one completed tile is readable
→ continue filling the cache over time
```

It should **not** wait for every selected tile to complete. One completed `.json.gz` tile plus `index.json` is enough for a partial draw.

## Inland water verification

Check route status:

```bash
curl -s "http://127.0.0.1:8000/gfs/api/inland-water?bbox=-127,28,-113,40&visible_bbox=-126,29,-114,39&scene_tier=world&source=auto&geometry=vector&lod=auto&cache=1&tile_cache=1&parallel=1&auto_build=1&max_tiles=16&reason=verify" | python3 -m json.tool
```

Check builder status:

```bash
curl -s "http://127.0.0.1:8000/gfs/api/inland-water/status?bbox=-127,28,-113,40" | python3 -m json.tool
```

Find completed tile indexes:

```bash
find /home/jayson_tolleson/broadcast -path "*nhdplus_hr*tiles/index.json" -print
```

Find completed tile files:

```bash
find /home/jayson_tolleson/broadcast -path "*nhdplus_hr*" -name "*.json.gz" | head -50
```

Watch inland-water build logs:

```bash
tail -f /home/jayson_tolleson/broadcast/data_sources/nhdplus_hr_state_cache/_build_logs/*.log
```

Expected good route response:

```json
{
  "status": "ok",
  "source": "nhdplus_hr_json_gz_or_geojson_high_def",
  "cache": {
    "selected_tiles": 1
  },
  "polygons": 10,
  "lines": 40,
  "prominence_filter": {
    "enabled": true,
    "fraction": 0.25
  }
}
```

## Common debugging commands

```bash
# service logs
journalctl -u broadcast -n 420 --no-pager

# live logs
journalctl -u broadcast -f

# backend health
curl -s http://127.0.0.1:8000/gfs/api/config | python3 -m json.tool

# frame should be fast/lightweight
curl -s "http://127.0.0.1:8000/gfs/api/frame?bbox=-134,22,-106,46&visible_bbox=-126,29,-114,39&scene_tier=world&quality=full" | python3 -m json.tool

# cloud payload
curl -s "http://127.0.0.1:8000/gfs/api/clouds?bbox=-134,22,-106,46&visible_bbox=-126,29,-114,39&scene_tier=world&quality=full" | python3 -m json.tool

# sea engine
curl -s "http://127.0.0.1:8000/gfs/api/sea?bbox=-134,22,-106,46&grid_size=256&layers=day_night,clouds,rain,bait,boats,jetstream" | python3 -m json.tool
```

## Troubleshooting

### Inland water says `building` but polygons stay `0`

Check whether the builder is writing tiles:

```bash
find /home/jayson_tolleson/broadcast -path "*nhdplus_hr*" -name "*.json.gz" | head -50
find /home/jayson_tolleson/broadcast -path "*nhdplus_hr*tiles/index.json" -print
```

Then inspect the builder log:

```bash
tail -120 /home/jayson_tolleson/broadcast/data_sources/nhdplus_hr_state_cache/_build_logs/*.log
```

Likely causes:

- The requested viewport is ocean-heavy and needs a land-first clipped state bbox.
- USGS/ArcGIS requests are slow or returning no features for the first source square.
- Tiles are written somewhere the route is not scanning.
- `index.json` is not being updated after `.json.gz` files are created.

### `/gfs/api/frame` is slow or aborting

Make sure lightweight frame mode is enabled:

```bash
grep -E "GFS_FRAME_LIGHTWEIGHT_ONLY|GFS_FRAME_HEAVY_WARM" /etc/systemd/system/broadcast.service /home/jayson_tolleson/broadcast/.env 2>/dev/null
```

Expected:

```bash
GFS_FRAME_LIGHTWEIGHT_ONLY=true
GFS_FRAME_HEAVY_WARM=false
```

### Journalctl repeatedly shows `cfgrib group opened`

That means GFS decode reuse is not being applied or the TTL is too short. Check:

```bash
grep GFS_DECODE_SNAPSHOT_TTL_SECONDS /home/jayson_tolleson/broadcast/.env 2>/dev/null
```

Expected:

```bash
GFS_DECODE_SNAPSHOT_TTL_SECONDS=900
```

## Docs

Additional implementation notes are in `docs/`:

```text
docs/gfs-frame-fast-snapshot.md
docs/inland-water-auto-build.md
docs/inland-water-partial-cache-pop.md
docs/inland-water-runtime-cache.md
docs/state-sidecar-cache.md
docs/state-sidecar-source-completion.md
docs/pill-control-route-cleanup.md
```

## Data policy

- Real sources only.
- No fake/mock/coarse inland-water fallback.
- Cache-first rendering is allowed.
- Stale-while-revalidate is allowed when the source is real.
- Empty source data should be reported honestly instead of fabricated.


## Recommended unzip / install path

```

The zip itself contains a top-level `broadcast/` folder, so unzipping with `-d ~` creates or refreshes `~/broadcast` without needing a manual file move.

Hover intel note: the fixed title-bar hover pane has been removed. Drawn polygons/lines/markers now use cursor-following hover tips, including payload rows such as temperature, bait score, source, path points, and layer-specific metrics when those values are present in the payload.

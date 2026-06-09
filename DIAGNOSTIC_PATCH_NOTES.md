# LFTR diagnostic live-data patch

This package was patched from the June 8 runner/workdir build using the server printouts from install testing.

## What the printouts showed

- The service was running and health routes returned JSON 200.
- GFS/NOMADS ingestion was active and the canonical grid reached 721x1440.
- HYCOM ocean data was active for the SoCal bbox, with live SST/current arrays.
- Cloud live tiles were generated but then all rejected by the backend bbox contract for a wide boot/world bbox.
- `/gfs/api/field?field=current` returned a Quart HTML 404 even though current/ocean data existed through newer endpoints.
- Bait and boats could initially return cache-warming shells while background live providers were still populating.

## Patch summary

1. Hardened cloud tile center extraction in `server/gfs_service_parts/core.py`.
   - Accepts bounds center, bbox midpoint, center/centroid dicts, GeoJSON-style pairs, direct lat/lon, region footprints, and enriched band footprints.
   - Adds diagnostic ranges for valid tile centers versus kept tile centers.

2. Prevented the wide boot/world cloud bbox from deleting all valid live cloud tiles.
   - If a wide or polar-edge bbox rejects every otherwise-valid tile, the server preserves the live payload instead of making clouds empty.
   - Local/normal viewport clipping remains strict.

3. Added `/gfs/api/field` compatibility route in `server/gfs/routes.py`.
   - `/gfs/api/field?field=current&bbox=...` now returns structured JSON from the ocean-points/current provider instead of HTML 404.

4. Added `scripts/diagnose_live_data_compact.sh`.
   - Low-output diagnostic script for service status, compact endpoint summaries, and focused journal warnings/errors.

## Install / update

```bash
cd ~
unzip -o LFTR-Broadcast-GFS-diagnostic-live-data-fix.zip -d ~
cd ~/broadcast
sudo bash broadcast.sh
sudo systemctl restart broadcast
```

## Compact validation

```bash
cd ~/broadcast
bash scripts/diagnose_live_data_compact.sh
```

Expected improvements:

- `/gfs/api/field?field=current` returns JSON, not HTML 404.
- Cloud diagnostics should no longer show `cloud_tiles_raw > 0` with `cloud_tiles_kept: 0` for the wide boot/world bbox case.
- HYCOM/ocean current endpoints should expose points once warm.
- Bait/boats may still report `warming` on first request, but should be debuggable without flooding the terminal.

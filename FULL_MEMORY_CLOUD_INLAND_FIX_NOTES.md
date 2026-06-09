# Full Memory / Clouds / Inland Temperature Fix Notes

Applied for the next LFTR GFS zip.

## Cloud render + memory

- Cloud particle markers are now capped at **500 desktop / 320 mobile** by default.
- Cloud body/shell default cap reduced to 240; cloud texture now comes from lightweight SVG sprite clusters instead of thousands of marker particles.
- Cloud sprites continue to advect and wobble every animation tick.
- Cloud shell path morphing is slowed to 12 seconds by default to avoid Google Maps 3D polygon path flash.
- Same stable cloud scene now reuses the existing visual scene for 10 minutes by default; the current clouds keep advecting instead of being fully redrawn on each cache heartbeat.
- The RendererLayer cloud minimum redraw interval is now 10 minutes unless the operator overrides `window.__GFS_CLOUD_MIN_RENDER_INTERVAL_MS`.

## Debug/log pressure

- Repetitive large debug events are throttled and slimmed:
  - `scene-cache/apply`
  - `scene-progress/payload`
  - same-version render noops
- Debug still shows layer status/source/version/counts, but it no longer dumps huge repeated cache objects every heartbeat.

## Inland water temperature

- Inland water temperature sampling now checks already-decoded GFS/NCSS surface groups first.
- Normal temp label reads are **cache-only** and do not wake a new NCSS/GFS fetch.
- The sampled variable candidates remain surface-first: `t0m`, `TMP:surface`, `TMP_surface`, `skt`, `SKT`, then safe fallbacks.
- Output metadata marks successful cached reads as `cached_ncss_surface_t0m_only_no_fetch`.

## Ocean-backed empty cache protection

- Preserved previous hotfix behavior: bait/boater/shark-intel provider-empty rows are not promoted over usable last-good cache.
- HYCOM remains a tile/provider source; large bbox bait uses cache-only/no one-shot HYCOM fetch policy.


# Zoom-gate patch: inland water / inland bait

Purpose: reduce browser graphics load and stop global/world views from being covered by inland lake temperature labels and inland bait overlays.

## Policy

- Global/world view: do **not** fetch, build, or render inland-water geometry, inland-water temperature labels, or inland bait/targets.
- Regional/coastal/local/harbor detail: inland-water geometry and inland bait/temp overlays may fetch/render if the Inland Waters pill is enabled.
- Ocean bait, boats, clouds, rain, lightning, jetstream, and fish markers are not disabled by this patch.

## Frontend changes

- `static/js/gfs/main.js`
  - Adds an inland zoom gate using the existing `sceneTierForViewport()` buckets.
  - Allows inland detail only for `harbor`, `local`, `coastal`, and `regional` tiers.
  - Skips `refreshDeferredInlandWater()`, inland build-cache requests, and mandatory boot bootstrap on world views.
  - Clears existing inland-water visuals when the camera zooms back out to world.
  - Keeps boot earth-first/lightweight: inland detail waits until regional/coastal/harbor zoom.

- `static/js/gfs/world_subscription_renderer.js`
  - Filters `inland-water` and `inland_water_temp` out of scene-cache layer requests unless the viewport tier is regional/coastal/local/harbor.
  - Skips inland layer subscription requests at world zoom and clears inland visuals instead.

## Test in browser console

At global/world zoom:

```js
window.__gfsInlandZoomGate
```

Expected: `allowed: false`, `tier: "world"`.

Zoom into a harbor / lake / regional area and trigger a steady refresh. Expected: `allowed: true` with tier `regional`, `coastal`, or `harbor`.

## Low-output server check

```bash
cd ~/broadcast
sudo journalctl -u broadcast --since "5 minutes ago" --no-pager \
  | egrep -i "inland-water/build|inland.*zoom-gated|cache/refresh|scene_tier" \
  | tail -80
```

At global view, you should no longer see new `inland-water/build ... tier=world` jobs caused by page boot.

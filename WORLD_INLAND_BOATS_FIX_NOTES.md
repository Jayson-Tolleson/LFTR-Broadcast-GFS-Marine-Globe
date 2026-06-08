# LFTR GFS World Inland Gate + Boater Import Fix

This package is based on the previous zoom-gated inland-bait build and adds a harder diagnostic fix from the latest browser/server printouts.

## Fixed

1. **World inland-water requests still firing**
   - The browser now filters `inland-water` and `inland_water_temp` out of scene-cache and scene-frame layer lists whenever the viewport tier is `world`.
   - Server routes now also enforce the same gate, so even if a stale browser tab requests inland layers at world zoom, `/gfs/api/scene-frame`, `/gfs/api/scene-cache`, `/gfs/api/cache/refresh`, `/gfs/api/inland-water`, `/gfs/api/inland-water-temp`, and `/gfs/api/inland-water/build-cache` will not build/fetch world inland geometry/temp/bait.
   - Lake outlines and inland-bait priming remain enabled only at harbor/local/coastal/regional zoom.

2. **Boater renderer import failure**
   - `static/js/gfs/boats.js` had two `function normalizeDeg(...)` declarations.
   - ES modules do not allow duplicate function declarations in the same scope, so lazy import failed with: `SyntaxError: redeclaration of function normalizeDeg`.
   - The duplicate was removed; there is now one shared `normalizeDeg(value, fallback = 0)` function.

## Verify after install

```bash
cd ~/broadcast
bash scripts/check_world_inland_boats_patch.sh
sudo systemctl restart broadcast
```

Browser/global zoom expectations:

- No `/gfs/api/inland-water?...scene_tier=world` fetches should appear except as `status: zoom_gated` if an old tab asks anyway.
- Scene-frame layers at world should not contain `inland-water` or `inland_water_temp`.
- No `boats SyntaxError: redeclaration of function normalizeDeg` should appear.

Helpful live check:

```bash
sudo journalctl -u broadcast --since "5 minutes ago" --no-pager \
  | egrep -i "inland-water/build|scene_tier=world|redeclaration of function normalizeDeg|world_zoom_gate|error|warning" \
  | tail -80
```

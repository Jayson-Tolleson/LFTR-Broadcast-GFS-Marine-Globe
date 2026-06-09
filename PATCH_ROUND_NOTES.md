# LFTR GFS Overview + Cloud + Provider Round

This package applies the post-journalctl fixes discussed during install/debug.

## Key behavior

1. **Runner/static safety**
   - `scripts/run_broadcast_service.sh` waits for `static/indexgfs.html` before launching Hypercorn.
   - Hypercorn is started through `scripts/run_hypercorn_single.py` to reduce worker/spawn churn and avoid repeated clean exits resetting visuals.

2. **Inland water overview is allowed at world zoom**
   - World/overview zoom can build/read/render lake outlines.
   - World/overview inland geometry is downgraded to `geometry=coarse&lod=overview`.
   - World/overview temp companion samples only a few largest closed lake candidates for labels.
   - Inland bait/targets/contours remain gated until `regional`, `coastal`, `local`, or `harbor` tiers.

3. **Clouds morph instead of flashing**
   - Cloud redraw signatures ignore TTL/cache-only churn.
   - New cloud scenes crossfade in while previous cloud shells linger/fade out.
   - Cloud particles are tier-capped and temporarily reduced during replacement.

4. **NOMADS resilience**
   - Failed live NOMADS fetches fall back to last-known-good GRIB/cached real payload when available.
   - Unavailable NOMADS returns a non-fatal warming shell instead of clearing render layers.

5. **HYCOM provider protection**
   - HYCOM is treated as a tile/provider, not a giant one-shot query system.
   - Wide bboxes return cache-only/large-bbox shell instead of blocking on huge NCSS requests.
   - Smaller coastal/local tiles still fetch live HYCOM SST/current.

## Verify after install

```bash
cd ~/broadcast
bash scripts/check_overview_cloud_provider_patch.sh
sudo systemctl restart broadcast
sudo journalctl -u broadcast --since "5 minutes ago" --no-pager \
  | egrep -i "hypercorn exited|inland-water/build|world_overview|overview_only|cloud|hycom|nomads|error|warning" \
  | tail -160
```

Expected:

- No static missing errors.
- No `boats SyntaxError: redeclaration of function normalizeDeg`.
- World inland-water may build/read, but it should be `coarse/overview` and bait should be `zoom_gated`.
- Cloud TTL churn should not hard-flash the layer.
- Large HYCOM requests should report cache-only policy instead of long NCSS timeouts.

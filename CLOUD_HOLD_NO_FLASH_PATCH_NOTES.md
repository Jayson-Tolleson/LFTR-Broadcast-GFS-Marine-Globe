# Cloud hold / no-flash patch

Purpose: clouds are a persistent visual layer. New TTL/cache frames must not destructively replace existing cloud shells.

Changes:
- Cloud render signature ignores valid_time/source_time/resolved_time/cache version churn.
- Cloud signature uses coarse geometry fingerprint only: bbox, body counts, layer grid dimensions, and quantized first/middle/last feature anchors.
- Cloud renderer marks new draws with `__gfsKeepExisting=true` so RendererLayer does not call the previous disposer on every cloud refresh.
- New cloud shells may overlap old shells; this is intentional and avoids visible blank/fade cycles.
- All cloud advection timers are registered and stopped together when cloud layer is explicitly cleared or pill is turned off.
- Default cloud minimum render interval increased to 180 seconds unless overridden by `window.__GFS_CLOUD_MIN_RENDER_INTERVAL_MS`.

Expected behavior:
- No cloud flash every second from TTL/cache refresh.
- Existing shells remain on the map.
- New shells may fade in over existing shells when real geometry changes.
- Clouds clear only when the Clouds pill is turned off or `window.clearGfsCloudLayer()` is called.

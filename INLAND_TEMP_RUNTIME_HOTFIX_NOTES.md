# Inland Temp Runtime Hotfix

Patched frontend runtime errors seen in the 15:49 debug log:

- Defines `tempLabelBudget(range)` so inland enrichment setup no longer fails before temp/bait child overlays.
- Defines `tempLabelStride(range)` for far/medium/local label thinning.
- Defines `isRealTemperaturePoint(pt)` so cached NCSS/GFS surface temp points are accepted and bogus/synthetic points are rejected.
- Keeps shoreline geometry rendering even when temp points are sparse.
- Samples `inland-water/shoreline-complete` debug events instead of emitting one line per lake at world zoom.
- Preserves the cloud hold guard already visible in logs (`render/hold-min-interval`, 600000 ms) so clouds advect/morph rather than fully redraw every cache heartbeat.

Expected next log improvements:

- No `tempLabelBudget is not defined`.
- No `isRealTemperaturePoint is not defined`.
- `inland-water/enrichment-setup-failed` should disappear unless there is a new, real downstream issue.
- World zoom still renders largest-lake-per-tile outlines; temp/bait enrichment stays lake-bound and gated.

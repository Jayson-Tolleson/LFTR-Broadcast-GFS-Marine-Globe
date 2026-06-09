# Ocean points + cloud parser hotfix

This build targets the journalctl issue where bait, boater, and shark-intel stayed empty even though scene-cache was healthy.

## Fixed

- Empty HYCOM/ocean responses with `points=0` are no longer promoted as latest-good scene-cache rows for:
  - `bait`
  - `boater`
  - `shark-intel`
- `ocean_live_required_unavailable`, `bait_live_required_unavailable`, `provider_empty`, `waiting_for_sst_points`, and `large_bbox_cache_only_no_live_ncss` are treated as warming/placeholders unless they contain renderable points, polygons, contours, or score rows.
- Background warmers now reject empty ocean-backed payloads instead of overwriting good cache with zero-point results.
- Ocean-points and ocean-live heavy fetches only cache provider results when usable points/content exists.
- Bait advanced provider failures are returned as diagnostics but not stored as fresh live bait cache.
- Cloud/NOMADS metadata now guards non-numeric `forecast_hour` values, preventing derived IDs such as `gfs-19-06:494709:frontal_shield` from crashing cloud feature derivation.

## Expected log differences

You should now see diagnostic cache policy markers like:

```txt
write_policy: rejected_empty_ocean_provider_not_promoted
write_policy: do_not_promote_empty_ocean_live
write_policy: do_not_promote_empty_ocean_points
write_policy: do_not_promote_empty_bait_grid
```

These are good when HYCOM returns no points: they mean the empty provider result was not allowed to become latest-good render cache.

## HYCOM behavior

HYCOM remains a provider behind cache/tile policy. Large world-tier bboxes do not trigger one giant blocking NCSS request. Wide views keep cache/tile diagnostics until smaller/allowed tiles produce real points.

# HYCOM Provider Contract

HYCOM is the first-class ocean provider.

## Provider

`server.gfs.providers.hycom.HycomProvider`

The historical implementation lived behind `server.gfs.providers.rtofs.RtofsProvider`.
That name remains as a compatibility alias only.

## Source variables

HYCOM provider fetches:

- `sst` — sea surface temperature
- `sss` — sea surface salinity
- `ssu` — eastward surface current
- `ssv` — northward surface current

## Consumers

HYCOM output is consumed by:

```text
HycomProvider subset grid
  -> oceanAnalysisPoints
      -> boats
      -> shark-intel
      -> HUD
      -> current squares

HycomProvider subset grid
  -> advancedBaitRows
      -> dense bait score field
      -> marching-square contours
      -> depth extrusion
```

Boats and bait should not fetch HYCOM independently. They consume the ocean
provider contract.

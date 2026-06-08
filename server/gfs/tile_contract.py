from __future__ import annotations

import hashlib
import math
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

DEFAULT_VIEWPORT_GRID = int(os.getenv("GFS_VIEWPORT_TILE_GRID", "24") or "24")
PROVIDERS = ("ncss_gfs", "rtofs", "hycom", "inland_geometry", "lake_environment", "usgs_waterflow", "shoreline")

NCSS_GFS_BASE = os.getenv(
    "GFS_NCSS_BASE_URL",
    "https://tds.scigw.unidata.ucar.edu/thredds/ncss/grid/grib/NCEP/GFS/Global_0p25deg/TwoD",
)
NCSS_GFS_VARS = [
    "Temperature_height_above_ground",
    "Relative_humidity_height_above_ground",
    "Dewpoint_temperature_height_above_ground",
    "Pressure_reduced_to_MSL_msl",
    "Total_cloud_cover_entire_atmosphere",
    "Low_cloud_cover_low_cloud",
    "Medium_cloud_cover_middle_cloud",
    "High_cloud_cover_high_cloud",
    "Precipitation_rate_surface",
    "u-component_of_wind_height_above_ground",
    "v-component_of_wind_height_above_ground",
]
RTOFS_ERDDAP_BASE = os.getenv("GFS_RTOFS_ERDDAP_URL", "https://coastwatch.noaa.gov/erddap/griddap/noaacwBLENDEDsstDaily.nc")
HYCOM_NCSS_BASE = os.getenv("GFS_HYCOM_NCSS_URL", "https://ncss.hycom.org/thredds/ncss/GLBy0.08/expt_93.0")
NHD_ARCGIS_BASE = os.getenv("GFS_NHD_ARCGIS_URL", "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/6/query")
USGS_SITE_BASE = "https://waterservices.usgs.gov/nwis/site/"
USGS_IV_BASE = "https://waterservices.usgs.gov/nwis/iv/"
LAKE_ENV_NCSS_BASE = os.getenv("GFS_LAKE_ENV_NCSS_URL", NCSS_GFS_BASE)
LAKE_ENV_NCSS_VARS = ["Temperature_height_above_ground", "u-component_of_wind_height_above_ground", "v-component_of_wind_height_above_ground", "Relative_humidity_height_above_ground", "Pressure_reduced_to_MSL_msl"]


def normalize_bbox(bbox: dict[str, Any] | list[float] | tuple[float, ...] | None) -> dict[str, float]:
    if isinstance(bbox, dict):
        west = float(bbox.get("west", bbox.get("minLon", bbox.get("left", -130.0))))
        south = float(bbox.get("south", bbox.get("minLat", bbox.get("bottom", 20.0))))
        east = float(bbox.get("east", bbox.get("maxLon", bbox.get("right", -60.0))))
        north = float(bbox.get("north", bbox.get("maxLat", bbox.get("top", 55.0))))
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        west, south, east, north = [float(x) for x in bbox[:4]]
    else:
        west, south, east, north = -130.0, 20.0, -60.0, 55.0
    west = max(-180.0, min(180.0, west))
    east = max(-180.0, min(180.0, east))
    south = max(-89.9, min(89.9, south))
    north = max(-89.9, min(89.9, north))
    if north < south:
        south, north = north, south
    # Viewport tile math is intentionally simple/congruent.  If a caller crosses
    # the dateline, split that viewport before using this contract.
    if east < west:
        west, east = east, west
    if math.isclose(east, west):
        east = min(180.0, west + 0.01)
    if math.isclose(north, south):
        north = min(89.9, south + 0.01)
    return {"west": west, "south": south, "east": east, "north": north}


def bbox_fragment(bbox: dict[str, float]) -> str:
    return "{west:.5f}_{south:.5f}_{east:.5f}_{north:.5f}".format(**bbox).replace("-", "m").replace(".", "p")


def viewport_key(bbox: dict[str, float], grid: int) -> str:
    raw = "{grid}:{west:.5f},{south:.5f},{east:.5f},{north:.5f}".format(grid=grid, **bbox)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ViewportTile:
    tile_id: str
    row: int
    col: int
    bbox: dict[str, float]
    center: dict[str, float]


def split_viewport_tiles(bbox: dict[str, Any] | list[float] | tuple[float, ...] | None, grid: int | None = None) -> list[ViewportTile]:
    b = normalize_bbox(bbox)
    n = max(1, int(grid or DEFAULT_VIEWPORT_GRID))
    dx = (b["east"] - b["west"]) / float(n)
    dy = (b["north"] - b["south"]) / float(n)
    vkey = viewport_key(b, n)
    tiles: list[ViewportTile] = []
    for row in range(n):
        for col in range(n):
            west = b["west"] + col * dx
            east = b["west"] + (col + 1) * dx
            south = b["south"] + row * dy
            north = b["south"] + (row + 1) * dy
            tb = {"west": west, "south": south, "east": east, "north": north}
            tiles.append(ViewportTile(
                tile_id=f"vp{vkey}_r{row:02d}_c{col:02d}",
                row=row,
                col=col,
                bbox=tb,
                center={"lat": (south + north) * 0.5, "lon": (west + east) * 0.5},
            ))
    return tiles


def _url_with_query(base: str, pairs: list[tuple[str, Any]]) -> str:
    return f"{base}?{urllib.parse.urlencode([(k, str(v)) for k, v in pairs])}"


def provider_url(provider: str, bbox: dict[str, float]) -> str:
    p = provider.replace("-", "_").strip().lower()
    b = normalize_bbox(bbox)
    if p == "ncss_gfs":
        pairs: list[tuple[str, Any]] = []
        for var in NCSS_GFS_VARS:
            pairs.append(("var", var))
        pairs += [("north", b["north"]), ("south", b["south"]), ("west", b["west"]), ("east", b["east"]), ("time", "present"), ("accept", "netcdf4"), ("addLatLon", "true")]
        return _url_with_query(NCSS_GFS_BASE, pairs)
    if p == "rtofs":
        # Adapter owns the exact dataset/variable mapping.  This default is a
        # concrete ERDDAP-style SST subset URL for the congruent tile bbox.
        query = "analysed_sst[(last)][({south}):1:({north})][({west}):1:({east})]".format(**b)
        return f"{RTOFS_ERDDAP_BASE}?{urllib.parse.quote(query, safe='[]():,.-_')}"
    if p == "hycom":
        pairs = [("var", "water_u"), ("var", "water_v"), ("var", "water_temp"), ("var", "surf_el"), ("north", b["north"]), ("south", b["south"]), ("west", b["west"]), ("east", b["east"]), ("horizStride", 1), ("time", "present"), ("accept", "netcdf4")]
        return _url_with_query(HYCOM_NCSS_BASE, pairs)
    if p == "inland_geometry":
        geom = f"{b['west']},{b['south']},{b['east']},{b['north']}"
        pairs = [("f", "geojson"), ("where", "1=1"), ("geometry", geom), ("geometryType", "esriGeometryEnvelope"), ("inSR", 4326), ("spatialRel", "esriSpatialRelIntersects"), ("outFields", "GNIS_NAME,FTYPE,FCODE,AREASQKM"), ("returnGeometry", "true"), ("outSR", 4326)]
        return _url_with_query(NHD_ARCGIS_BASE, pairs)
    if p == "lake_environment":
        # Lake-bound NCSS surface environment.  This is intentionally separate
        # from viewport atmosphere so a lake's own bounds can drive temperature
        # and bait marching-square work without requesting the whole viewport.
        pairs: list[tuple[str, Any]] = []
        for var in LAKE_ENV_NCSS_VARS:
            pairs.append(("var", var))
        pairs += [("north", b["north"]), ("south", b["south"]), ("west", b["west"]), ("east", b["east"]), ("time", "present"), ("accept", "netcdf4"), ("addLatLon", "true")]
        return _url_with_query(LAKE_ENV_NCSS_BASE, pairs)
    if p == "usgs_waterflow":
        bb = f"{b['west']},{b['south']},{b['east']},{b['north']}"
        return _url_with_query(USGS_IV_BASE, [("format", "json"), ("bBox", bb), ("parameterCd", "00060,00065,00010"), ("siteStatus", "active")])
    if p == "shoreline":
        bb = f"{b['west']},{b['south']},{b['east']},{b['north']}"
        return f"/gfs/api/provider/shoreline/tile?bbox={urllib.parse.quote(bb)}&source=local_then_osm_then_naturalearth&format=geojson"
    raise ValueError(f"unknown provider: {provider}")


def provider_tile_plan(bbox: dict[str, Any] | list[float] | tuple[float, ...] | None, providers: list[str] | None = None, grid: int | None = None) -> dict[str, Any]:
    b = normalize_bbox(bbox)
    n = max(1, int(grid or DEFAULT_VIEWPORT_GRID))
    wanted = [p.replace("-", "_").strip().lower() for p in (providers or list(PROVIDERS)) if str(p).strip()]
    wanted = [p for p in wanted if p in PROVIDERS]
    tiles = split_viewport_tiles(b, n)
    return {
        "ok": True,
        "schema": "lftr_provider_tile_contract_v1",
        "contract": "one_viewport_bbox_split_into_24x24_congruent_tiles_for_all_providers; lake_environment may also use per-lake bounds",
        "grid": {"rows": n, "cols": n, "count": n * n},
        "viewport_bbox": b,
        "viewport_key": viewport_key(b, n),
        "providers": wanted,
        "provider_count": len(wanted),
        "possible_provider_tile_jobs": len(wanted) * len(tiles),
        "tiles": [
            {"tile_id": t.tile_id, "row": t.row, "col": t.col, "bbox": t.bbox, "center": t.center}
            for t in tiles
        ],
    }


def provider_jobs(bbox: dict[str, Any] | list[float] | tuple[float, ...] | None, providers: list[str] | None = None, grid: int | None = None, limit: int | None = None) -> dict[str, Any]:
    plan = provider_tile_plan(bbox, providers=providers, grid=grid)
    jobs: list[dict[str, Any]] = []
    for tile in plan["tiles"]:
        for provider in plan["providers"]:
            jobs.append({
                "job_id": f"{provider}:{tile['tile_id']}",
                "provider": provider,
                "tile_id": tile["tile_id"],
                "row": tile["row"],
                "col": tile["col"],
                "bbox": tile["bbox"],
                "cache_key": f"providers/{provider}/{tile['tile_id']}_{bbox_fragment(tile['bbox'])}",
                "url": provider_url(provider, tile["bbox"]),
            })
    if limit is not None:
        jobs = jobs[:max(0, int(limit))]
    out = dict(plan)
    out["jobs"] = jobs
    out["job_count"] = len(jobs)
    return out

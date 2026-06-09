from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.gfs.models import BBox
from server.gfs.serializers import iso_utc


@dataclass(frozen=True)
class Viewport:
    west: float
    south: float
    east: float
    north: float


@dataclass(frozen=True)
class ErddapSlice:
    lon_start: float
    lon_stop: float
    lat_start: float
    lat_stop: float


def _iso_time_or_last(valid_time: datetime | None) -> str:
    if not valid_time:
        return "last"
    return str(iso_utc(valid_time))


def _selector(value: str) -> str:
    value = str(value).strip()
    if value.startswith('(') and value.endswith(')'):
        inner = value[1:-1]
        return f'[({inner}):1:({inner})]'
    return f'[({value}):1:({value})]'


def normalize_lon(lon: float, convention: str) -> float:
    if convention == "0360":
        while lon < 0.0:
            lon += 360.0
        while lon >= 360.0:
            lon -= 360.0
        return lon
    while lon < -180.0:
        lon += 360.0
    while lon >= 180.0:
        lon -= 360.0
    return lon


def split_antimeridian(viewport: Viewport) -> list[ErddapSlice]:
    # ERDDAP lon slices cannot cross the antimeridian in one interval.
    if viewport.west <= viewport.east:
        return [ErddapSlice(viewport.west, viewport.east, viewport.south, viewport.north)]
    return [
        ErddapSlice(viewport.west, 180.0, viewport.south, viewport.north),
        ErddapSlice(-180.0, viewport.east, viewport.south, viewport.north),
    ]


def build_ncss_subset_request(viewport: Viewport, vars: list[str], stride: int, valid_time: datetime | None, base_url: str, *, accept: str = "netCDF4") -> str:
    _ = valid_time
    stride_value = max(1, int(stride or 1))
    query: list[tuple[str, str]] = [
        ("north", str(viewport.north)),
        ("south", str(viewport.south)),
        ("west", str(viewport.west)),
        ("east", str(viewport.east)),
        ("time", "present"),
        ("horizStride", str(stride_value)),
        ("accept", accept),
        ("addLatLon", "true"),
    ]
    for v in vars:
        query.append(("var", v))
    return f"{base_url}?{urllib.parse.urlencode(query)}"


def build_erddap_subset_request(
    viewport: Viewport,
    dataset_csv_url: str,
    vars: list[str],
    stride: int,
    valid_time: datetime | None,
    *,
    lon_convention: str = "pm180",
    lat_descending: bool = False,
    extra_dimensions: list[str] | None = None,
) -> list[str]:
    time_selector = _iso_time_or_last(valid_time)
    stride_val = max(1, int(stride or 1))
    south = max(-89.9999, min(89.9999, float(viewport.south)))
    north = max(-89.9999, min(89.9999, float(viewport.north)))
    lat_start = max(-89.9999, min(89.9999, north if lat_descending else south))
    lat_stop = max(-89.9999, min(89.9999, south if lat_descending else north))
    if lon_convention == "0360":
        lon_view = Viewport(
            west=normalize_lon(viewport.west, "0360"),
            south=south,
            east=normalize_lon(viewport.east, "0360"),
            north=north,
        )
    else:
        lon_view = Viewport(
            west=normalize_lon(viewport.west, "pm180"),
            south=south,
            east=normalize_lon(viewport.east, "pm180"),
            north=north,
        )
    dim_prefix = ''.join([_selector(d) for d in ([time_selector] + list(extra_dimensions or []))])
    urls: list[str] = []
    for s in split_antimeridian(lon_view):
        lon_start = float(s.lon_start)
        lon_stop = float(s.lon_stop)
        if lon_convention != "0360" and lon_start > lon_stop:
            lon_start, lon_stop = lon_stop, lon_start
        constraints = []
        for var in vars:
            constraints.append(f"{var}{dim_prefix}[({lat_start}):{stride_val}:({lat_stop})][({lon_start}):{stride_val}:({lon_stop})]")
        safe_constraint = urllib.parse.quote(','.join(constraints), safe='[]():,.-_')
        urls.append(f"{dataset_csv_url}?{safe_constraint}")
    return urls


def build_station_enrichment_request(viewport: Viewport, valid_time: datetime | None) -> dict[str, Any]:
    # Station APIs are not bbox-grid sources: use viewport center for nearest-station enrichment only.
    center_lat = (viewport.south + viewport.north) * 0.5
    center_lon = (viewport.west + viewport.east) * 0.5
    return {
        "center_lat": center_lat,
        "center_lon": center_lon,
        "valid_time": _iso_time_or_last(valid_time),
    }


def viewport_from_bbox(bbox: BBox) -> Viewport:
    return Viewport(west=bbox.west, south=bbox.south, east=bbox.east, north=bbox.north)

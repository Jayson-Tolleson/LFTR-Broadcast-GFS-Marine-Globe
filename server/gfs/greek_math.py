from __future__ import annotations

import math


def deg_to_rad(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    return math.radians(lat_deg), math.radians(lon_deg)


def rad_to_deg(lat_rad: float, lon_rad: float) -> tuple[float, float]:
    return math.degrees(lat_rad), math.degrees(lon_rad)


def wrapped_longitude(lon_rad: float) -> float:
    return ((lon_rad + math.pi) % (2.0 * math.pi)) - math.pi


def bearing_from_uv(u: float, v: float) -> float:
    return math.atan2(u, v)


def cell_offsets(cell_size_deg: float) -> tuple[float, float]:
    rad = math.radians(cell_size_deg)
    return rad, rad

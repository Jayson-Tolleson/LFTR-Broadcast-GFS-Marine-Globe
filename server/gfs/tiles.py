from __future__ import annotations

import math


def tile_bounds(z: int, x: int, y: int) -> dict:
    n = 2 ** z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0

    def lat(t: float) -> float:
        v = math.pi * (1 - 2 * t / n)
        return math.degrees(math.atan(math.sinh(v)))

    lat_n = lat(y)
    lat_s = lat(y + 1)
    return {"west": lon_w, "south": lat_s, "east": lon_e, "north": lat_n}


def tile_payload(z: int, x: int, y: int) -> dict:
    b = tile_bounds(z, x, y)
    return {
        "bounds": b,
        "fish": [{"lat": (b["north"] + b["south"]) / 2, "lon": (b["east"] + b["west"]) / 2, "confidence": 0.81}],
        "bait": [{"lat": b["south"], "lon": b["west"], "score": 0.66}],
        "storms": [{"lat": b["north"], "lon": b["east"], "severity": "moderate"}],
        "cameras": [],
    }

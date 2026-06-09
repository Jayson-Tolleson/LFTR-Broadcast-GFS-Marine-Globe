from __future__ import annotations

import math
from typing import Any


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


# Light-weight geometry fallback for CSV fishing markers.
# This intentionally avoids heavy GIS deps/shapefiles during app import so /gfs can boot.
# Text in intelligence.py can still override this result for lakes, piers, surf, etc.
SOCAL_COASTLINE_POINTS: tuple[tuple[float, float], ...] = (
    (32.58, -117.13),  # San Diego / Imperial Beach
    (32.72, -117.25),
    (33.00, -117.28),
    (33.38, -117.60),
    (33.60, -117.88),  # Newport / Balboa
    (33.66, -118.00),  # Huntington
    (33.73, -118.18),  # Long Beach harbor
    (33.90, -118.43),
    (34.00, -118.50),  # Santa Monica
    (34.28, -119.30),
    (34.42, -119.70),
    (34.95, -120.65),
)

# Inland lakes/reservoirs present in the project CSV can be identified by rough boxes even
# when a row has weak/blank text. These are conservative and only affect the fallback label.
FRESHWATER_BOXES: tuple[tuple[str, float, float, float, float], ...] = (
    ("Lake Kaweah / Slick Rock", 36.25, 36.55, -119.05, -118.75),
    ("Lake Success / Tule River", 35.95, 36.18, -119.05, -118.78),
    ("Millerton / Finegold", 36.90, 37.15, -119.85, -119.45),
)

HARBOR_BOXES: tuple[tuple[str, float, float, float, float], ...] = (
    ("Newport / Balboa Harbor", 33.55, 33.65, -117.96, -117.82),
    ("Huntington / Seal Beach", 33.62, 33.78, -118.12, -117.94),
    ("Long Beach Harbor", 33.68, 33.82, -118.30, -118.05),
    ("San Diego Bay", 32.60, 32.78, -117.25, -117.05),
)


def _in_box(lat: float, lon: float, box: tuple[str, float, float, float, float]) -> bool:
    _name, south, north, west, east = box
    return south <= lat <= north and west <= lon <= east


def _distance_deg(lat: float, lon: float, point: tuple[float, float]) -> float:
    plat, plon = point
    # Scale longitude by latitude to make the rough degree distance less distorted.
    x = (lon - plon) * math.cos(math.radians((lat + plat) / 2.0))
    y = lat - plat
    return math.hypot(x, y)


def classify_waterbody_geometry(loc: dict[str, Any]) -> dict[str, Any]:
    """Classify a fish marker using a safe geometry-only fallback.

    The richer text/species rules live in ``server.gfs.intelligence`` and may override
    this result. This function must stay dependency-free because it is imported during
    app startup; a missing optional GIS package should never take the whole service down.
    """
    lat = _to_float(loc.get("lat") or loc.get("latitude"))
    lon = _to_float(loc.get("lon") or loc.get("lng") or loc.get("longitude"))

    for box in FRESHWATER_BOXES:
        if _in_box(lat, lon, box):
            return {
                "habitat_key": "freshwater",
                "waterbody": "Inland freshwater",
                "method": "geometry_box",
                "classification_reason": f"Location falls inside the {box[0]} freshwater fallback box.",
                "matched_zone": box[0],
                "coast_distance_deg": None,
            }

    for box in HARBOR_BOXES:
        if _in_box(lat, lon, box):
            return {
                "habitat_key": "pier_bay",
                "waterbody": "Pier / bay / harbor",
                "method": "geometry_box",
                "classification_reason": f"Location falls inside the {box[0]} harbor/bay fallback box.",
                "matched_zone": box[0],
                "coast_distance_deg": None,
            }

    nearest = min((_distance_deg(lat, lon, p) for p in SOCAL_COASTLINE_POINTS), default=999.0)
    if nearest <= 0.045:
        key = "surf"
        label = "Surf / shoreline"
        reason = "Location is very close to the Southern California coastline fallback line."
    elif nearest <= 0.22:
        key = "coastal_general"
        label = "Coastal saltwater"
        reason = "Location is near the Southern California coast but outside a harbor/surf box."
    else:
        key = "freshwater" if lat > 35.0 and -121.0 < lon < -117.0 else "coastal_general"
        label = "Inland freshwater" if key == "freshwater" else "Coastal saltwater"
        reason = "Location is far from the coastline fallback line; using inland/coastal heuristic."

    return {
        "habitat_key": key,
        "waterbody": label,
        "method": "geometry_fallback",
        "classification_reason": reason,
        "matched_zone": None,
        "coast_distance_deg": round(nearest, 5) if math.isfinite(nearest) else None,
    }

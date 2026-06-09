"""Compatibility facade for the Inland Waters subsystem.

The implementation lives under ``server.gfs.inland``.  Older scene-cache
warmers still import a few private helper names from this facade, so they must
be explicitly re-exported here.  Star imports skip underscore-prefixed names,
which caused the live inland temp/bait warmer to fail with:
``cannot import name '_centroid'``.
"""
from __future__ import annotations

import math
from typing import Any

from server.gfs.inland.payloads import *  # noqa: F401,F403

try:  # preferred real helpers
    from server.gfs.inland.payloads import (  # type: ignore
        _centroid as _payload_centroid,
        _temperature_point as _payload_temperature_point,
        _enrich_inland_feature as _payload_enrich_inland_feature,
    )
except Exception:  # pragma: no cover - keep facade import-safe during partial installs
    _payload_centroid = None
    _payload_temperature_point = None
    _payload_enrich_inland_feature = None


def _centroid(path: list[dict[str, Any]] | list[Any]) -> tuple[float, float] | None:
    """Return (lat, lon) for a path, with a safe fallback implementation."""
    if callable(_payload_centroid):
        try:
            return _payload_centroid(path)  # type: ignore[misc]
        except Exception:
            pass
    lats: list[float] = []
    lons: list[float] = []
    for pt in path or []:
        if isinstance(pt, dict):
            lat = pt.get('lat', pt.get('latitude'))
            lon = pt.get('lon', pt.get('lng', pt.get('longitude')))
        elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
            # Accept [lon, lat] or [lat, lon]; prefer web/map path dicts elsewhere.
            a, b = pt[0], pt[1]
            lat, lon = (b, a) if abs(float(a or 0)) > 90 else (a, b)
        else:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except Exception:
            continue
        if math.isfinite(lat_f) and math.isfinite(lon_f):
            lats.append(lat_f)
            lons.append(lon_f)
    if not lats or not lons:
        return None
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _temperature_point(*args: Any, **kwargs: Any) -> Any:
    if callable(_payload_temperature_point):
        return _payload_temperature_point(*args, **kwargs)  # type: ignore[misc]
    return None


def _enrich_inland_feature(feature: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    if callable(_payload_enrich_inland_feature):
        return _payload_enrich_inland_feature(feature, *args, **kwargs)  # type: ignore[misc]
    return feature


# Explicit private-name exports for old warmers that import from this facade.
try:
    __all__  # type: ignore[name-defined]
except Exception:
    __all__ = [name for name in globals() if not name.startswith("__")]
for _name in ("_centroid", "_temperature_point", "_enrich_inland_feature"):
    if _name not in __all__:
        __all__.append(_name)


# Compatibility exports for split-cache inland temp/bait warmers.
def _centroid(path):
    try:
        pts = [(float(p.get("lat")), float(p.get("lng", p.get("lon")))) for p in (path or []) if isinstance(p, dict) and p.get("lat") is not None and (p.get("lng") is not None or p.get("lon") is not None)]
        if not pts:
            return None
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    except Exception:
        return None

def _temperature_point(*args, **kwargs):
    try:
        from server.gfs.inland.payloads import _temperature_point as fn  # type: ignore
        return fn(*args, **kwargs)
    except Exception:
        return None

def _enrich_inland_feature(feature, *args, **kwargs):
    try:
        from server.gfs.inland.payloads import _enrich_inland_feature as fn  # type: ignore
        return fn(feature, *args, **kwargs)
    except Exception:
        return feature

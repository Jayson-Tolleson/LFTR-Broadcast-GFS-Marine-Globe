"""Geometry helpers reserved for inland water polygons/shorelines."""
from __future__ import annotations


def strip_fill_keep_shoreline(feature: dict) -> dict:
    out = dict(feature or {})
    out["fillOpacity"] = 0
    out["fill"] = "rgba(0,0,0,0)"
    out["shoreline_only"] = True
    return out

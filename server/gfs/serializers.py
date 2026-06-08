from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _iso(value: datetime | None) -> str | None:
    return iso_utc(value)


def serialize_weather(
    *,
    valid_time: datetime | None,
    bbox: list[float],
    stride: int,
    fields: dict[str, Any],
    stale: bool,
    source_time: str | None = None,
    resolved_time: str | None = None,
    polygon_field_v1: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sample = next(iter(fields.values()), [])
    ny = len(sample) if isinstance(sample, list) else 0
    nx = len(sample[0]) if ny and isinstance(sample[0], list) else 0
    return {
        "valid_time": _iso(valid_time),
        "bbox": bbox,
        "source_resolution_deg": 0.25,
        "grid": {"nx": nx, "ny": ny, "dx": 0.25, "dy": 0.25},
        "fields": fields,
        "stale": stale,
        "source_time": source_time,
        "resolved_time": resolved_time,
        "polygon_field_v1": polygon_field_v1 or None,
    }


def serialize_clouds(
    *,
    valid_time: datetime | None,
    bbox: list[float],
    layers: list[dict[str, Any]],
    convective: dict[str, Any],
    stale: bool,
    source_time: str | None = None,
    resolved_time: str | None = None,
    polygon_field_v1: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "valid_time": _iso(valid_time),
        "bbox": bbox,
        "cloud_layers": layers,
        "sigma_cloud_layers": layers,
        "convective": convective,
        "stale": stale,
        "source_time": source_time,
        "resolved_time": resolved_time,
        "polygon_field_v1": polygon_field_v1 or None,
    }


def serialize_bait(
    *,
    valid_time: datetime | None,
    bbox: list[float],
    bait_score: Any,
    front_lines: list[Any] | None = None,
    convergence_polygons: list[Any] | None = None,
    boil_probability_polygons: list[Any] | None = None,
    confidence: dict[str, float] | None = None,
    stale: bool,
    source_time: str | None = None,
    resolved_time: str | None = None,
    polygon_field_v1: dict[str, Any] | None = None,
    bait_base_field_v1: dict[str, Any] | None = None,
    bait_advanced_field_v1: dict[str, Any] | None = None,
    bait: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "valid_time": _iso(valid_time),
        "bbox": bbox,
        "bait_score": bait_score,
        "front_lines": front_lines or [],
        "convergence_polygons": convergence_polygons or [],
        "boil_probability_polygons": boil_probability_polygons or [],
        "confidence": confidence or {},
        "stale": stale,
        "source_time": source_time,
        "resolved_time": resolved_time,
        "polygon_field_v1": polygon_field_v1 or None,
        "bait_base_field_v1": bait_base_field_v1 or None,
        "bait_advanced_field_v1": bait_advanced_field_v1 or None,
        "bait": bait or {"status": "incomplete", "source": "suppressed_incomplete", "polygons": [], "outer_polygons": [], "inner_polygons": [], "core_polygons": []},
    }

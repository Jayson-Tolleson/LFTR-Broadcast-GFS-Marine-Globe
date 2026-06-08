from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CanonicalViewport:
    west: float
    south: float
    east: float
    north: float
    stride: int = 1
    quality: str = "full"

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
            "stride": self.stride,
            "quality": self.quality,
        }

    def as_bbox(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def snap_value(value: float, step: float) -> float:
    return round(value / step) * step


def _normalize_quality(value: Any) -> str:
    txt = str(value or "").strip().lower()
    if txt in {"coarse", "low", "fast"}:
        return "coarse"
    return "full"


def canonicalize_viewport(raw: dict[str, Any] | None) -> CanonicalViewport:
    raw = raw or {}
    west = _safe_float(raw.get("west"), -180.0)
    south = _safe_float(raw.get("south"), -80.0)
    east = _safe_float(raw.get("east"), 180.0)
    north = _safe_float(raw.get("north"), 80.0)
    quality = _normalize_quality(raw.get("quality"))

    if east <= west:
        east = west + 0.5
    if north <= south:
        north = south + 0.5

    span = max(east - west, north - south)
    stride = _safe_int(raw.get("stride"), 1)
    if stride < 1:
        stride = 1
    stride = max(1, min(4, stride))
    step = 0.25 * stride

    west = max(-179.9, snap_value(west, step))
    south = max(-89.9, snap_value(south, step))
    east = min(179.9, snap_value(east, step))
    north = min(89.9, snap_value(north, step))
    return CanonicalViewport(west=west, south=south, east=east, north=north, stride=stride, quality=quality)


def parse_viewport_args(args: Any) -> CanonicalViewport:
    raw: dict[str, Any] = {
        "west": args.get("west") if hasattr(args, "get") else None,
        "south": args.get("south") if hasattr(args, "get") else None,
        "east": args.get("east") if hasattr(args, "get") else None,
        "north": args.get("north") if hasattr(args, "get") else None,
        "quality": args.get("quality") if hasattr(args, "get") else None,
        "stride": args.get("stride") if hasattr(args, "get") else None,
    }
    raw_bbox = args.get("bbox") if hasattr(args, "get") else None
    if isinstance(raw_bbox, str) and raw_bbox.strip():
        try:
            west, south, east, north = [float(part.strip()) for part in raw_bbox.split(",")]
            raw.update({"west": west, "south": south, "east": east, "north": north})
        except Exception:
            pass
    return canonicalize_viewport(raw)

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Lean first-pass atmospheric variable sets for GFS TwoD live NCSS fetches.
WEATHER_VARIABLES = (
    "wind_u",
    "wind_v",
    "air_temp",
    "rel_humidity",
    "dewpoint",
    "pressure_msl",
)

CLOUD_VARIABLES = (
    "cloud_total",
    "cloud_low",
    "cloud_mid",
    "cloud_high",
)

BAIT_ATMOSPHERIC_VARIABLES = (
    "wind_u",
    "wind_v",
    "air_temp",
    "rel_humidity",
    "dewpoint",
    "pressure_msl",
    "precip_rate",
    "cloud_total",
)



ALL_ATMOSPHERIC_VARIABLES = tuple(dict.fromkeys(
    WEATHER_VARIABLES
    + CLOUD_VARIABLES
    + BAIT_ATMOSPHERIC_VARIABLES
))

@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


@dataclass(frozen=True)
class RequestIntent:
    bbox: BBox
    bboxes: tuple[BBox, ...]
    pad: float
    quality: str
    stride: int
    valid_time: datetime | None
    debug: bool = False


@dataclass
class EngineResult:
    payload: dict[str, Any]
    stale: bool = False

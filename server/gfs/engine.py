from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class IntentBBox:
    west: float
    south: float
    east: float
    north: float

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]

    def as_dict(self) -> dict[str, float]:
        return {"west": self.west, "south": self.south, "east": self.east, "north": self.north}


@dataclass(frozen=True)
class GfsIntent:
    bbox: IntentBBox
    quality: str = "auto"


class GfsEngine:
    """Compatibility wrapper around the server.gfs_service.GFSService.

    Several older endpoints, notably /api/gfs/scene, expect an engine object with
    parse_intent() and weather_payload().  The newer app mostly calls GFSService
    methods directly, so these compatibility methods keep existing callers from
    crashing while preserving the modular service implementation.
    """

    def __init__(self, config: Any | None = None, static_dir: Path | str | None = None) -> None:
        self.config = config
        self.static_dir = Path(static_dir) if static_dir is not None else Path(__file__).resolve().parents[2] / "static"
        self._service = None

    @property
    def service(self):
        if self._service is None:
            from server.gfs_service import GFSService
            self._service = GFSService(str(self.static_dir))
        return self._service

    @staticmethod
    def _first(args: Mapping[str, Any], *names: str, default: Any = None) -> Any:
        for name in names:
            try:
                value = args.get(name)  # Quart MultiDict supports get()
            except Exception:
                value = None
            if value not in (None, ""):
                return value
        return default

    @classmethod
    def _float(cls, args: Mapping[str, Any], *names: str, default: float) -> float:
        value = cls._first(args, *names, default=default)
        try:
            return float(value)
        except Exception:
            return float(default)

    def parse_intent(self, args: Mapping[str, Any] | None = None) -> GfsIntent:
        args = args or {}
        bbox_raw = self._first(args, "bbox", "bounds", default=None)
        west, south, east, north = -130.0, 20.0, -60.0, 55.0
        if bbox_raw:
            try:
                parts = [float(x.strip()) for x in str(bbox_raw).split(",")[:4]]
                if len(parts) == 4:
                    west, south, east, north = parts
            except Exception:
                pass
        else:
            west = self._float(args, "west", "left", "leftlon", default=west)
            south = self._float(args, "south", "bottom", "bottomlat", default=south)
            east = self._float(args, "east", "right", "rightlon", default=east)
            north = self._float(args, "north", "top", "toplat", default=north)
        west = max(-180.0, min(180.0, west))
        east = max(-180.0, min(180.0, east))
        south = max(-89.9, min(89.9, south))
        north = max(-89.9, min(89.9, north))
        if north < south:
            south, north = north, south
        quality = str(self._first(args, "quality", "lod", default="auto") or "auto")
        return GfsIntent(bbox=IntentBBox(west=west, south=south, east=east, north=north), quality=quality)

    async def weather_payload(self, intent: GfsIntent | Mapping[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(intent, GfsIntent):
            bbox = intent.bbox.as_dict()
        elif isinstance(intent, Mapping):
            bbox = self.parse_intent(intent).bbox.as_dict()
        else:
            bbox = self.parse_intent({}).bbox.as_dict()
        return self.service.generate_weather_payload(bbox)

    def __getattr__(self, name: str):
        return getattr(self.service, name)

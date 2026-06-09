from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(slots=True)
class GfsConfig:
    debug_enabled: bool = False
    ws_base: str = "/ws/gfs"
    api_base: str = "/gfs/api"
    cache_ttl_seconds: int = 1800


def load_gfs_config(debug_enabled: bool = False) -> GfsConfig:
    return GfsConfig(
        debug_enabled=bool(debug_enabled),
        ws_base=os.getenv("GFS_WS_BASE", "/ws/gfs"),
        api_base=os.getenv("GFS_API_BASE", "/gfs/api"),
        cache_ttl_seconds=int(os.getenv("GFS_CACHE_TTL_SECONDS", "1800") or 1800),
    )

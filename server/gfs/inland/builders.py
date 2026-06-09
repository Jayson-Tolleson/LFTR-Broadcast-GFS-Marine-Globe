"""Inland water builder policy.

The build subprocess still lives in the route layer for now; this module keeps
the normalized contract in one place for future extraction.
"""
from __future__ import annotations

MAX_RUNTIME_BUILD_AGE_SECONDS = 1800
GLOBAL_BUILD_GUARD_SECONDS = 90
DEFAULT_BUILD_BBOX = {"west": -126.0, "south": 29.0, "east": -114.0, "north": 39.0}


def build_mode_contract() -> dict[str, str]:
    return {
        "read": "cache_read_only_never_launch_builder",
        "build-cache": "explicit_progressive_real_usgs_nhd_tile_build",
        "render": "draw_ready_tiles_incrementally_without_waiting_for_all_tiles",
    }

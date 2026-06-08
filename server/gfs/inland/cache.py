"""Inland runtime tile cache helpers.

This module is intentionally small and side-effect free. It documents the cache
contract used by the route/service layer: reads never launch builders; explicit
build-cache calls write durable tiles; renderers consume whatever tile is ready.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


def runtime_tile_roots(static_dir: Path) -> list[Path]:
    base = Path(static_dir).resolve().parent
    return [
        base / "data_sources" / "nhdplus_hr_state_cache",
        base / ".cache" / "inland_water",
    ]


def existing_runtime_roots(static_dir: Path) -> list[Path]:
    return [p for p in runtime_tile_roots(static_dir) if p.exists()]


def iter_tile_files(static_dir: Path) -> Iterable[Path]:
    for root in existing_runtime_roots(static_dir):
        yield from root.rglob("*.json.gz")

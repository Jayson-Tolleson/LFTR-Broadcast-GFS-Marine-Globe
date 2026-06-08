from __future__ import annotations

from typing import Any

from server.gfs.tile_contract import DEFAULT_VIEWPORT_GRID, provider_jobs

LAYER_PROVIDER_MAP: dict[str, tuple[str, ...]] = {
    "clouds": ("ncss_gfs",),
    "rain": ("ncss_gfs",),
    "lightning": ("ncss_gfs",),
    "jetstream": ("ncss_gfs",),
    "boater": ("rtofs", "hycom"),
    "bait": ("rtofs", "hycom"),
    "shark-intel": ("rtofs", "hycom"),
    "inland-water": ("inland_geometry", "shoreline"),
    "inland_water_temp": ("lake_environment", "usgs_waterflow"),
}


def normalize_scene_layers(layers: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return pill layers in the same compact names the browser renders."""
    aliases = {
        "boats": "boater",
        "boater_awareness": "boater",
        "shark_intel": "shark-intel",
        "sharkintel": "shark-intel",
        "inland_water": "inland-water",
        "inland_waterways": "inland-water",
    }
    out: list[str] = []
    for raw in layers or []:
        layer = aliases.get(str(raw).strip().lower(), str(raw).strip().lower())
        if layer and layer not in out:
            out.append(layer)
    return out


def providers_for_layers(layers: list[str] | tuple[str, ...] | None) -> list[str]:
    """Map selected pills to the minimum provider set for a 24x24 parallel plan."""
    providers: list[str] = []
    for layer in normalize_scene_layers(layers):
        for provider in LAYER_PROVIDER_MAP.get(layer, ()):  # unknown/debug layers stay cache-only
            if provider not in providers:
                providers.append(provider)
    return providers


def scene_frame_payload(
    svc: Any,
    bbox: dict[str, float] | None,
    visible_bbox: dict[str, float] | None,
    layers: list[str] | tuple[str, ...] | None,
    *,
    mode: str = "read",
    refresh: bool = False,
    include_provider_jobs: bool = True,
    job_limit: int | None = None,
) -> dict[str, Any]:
    """Single /gfs frame contract: get/cache, compile, optional warm, draw metadata.

    The browser can render directly from ``frame.layers`` and use
    ``frame.provider_tiles.jobs`` to fire provider downloads in parallel.  The
    default grid is 24x24 per provider, matching the desired request base while
    avoiding multiple browser endpoint families per pill click.
    """
    selected_layers = normalize_scene_layers(layers)
    mode_l = str(mode or "read").lower()
    fast = mode_l in {"fast", "first_paint", "cache", "cache_only"}
    frame = svc.scene_cache_fast_payload(bbox, visible_bbox, selected_layers, mode=mode_l) if fast else svc.scene_cache_payload(bbox, visible_bbox, selected_layers, refresh=False, mode=mode_l)

    providers = providers_for_layers(selected_layers)
    frame["schema"] = "lftr_gfs_scene_frame_v1"
    frame["source"] = "single_scene_frame_cache_read_compile_draw_contract"
    frame["selected_layers"] = selected_layers
    frame["pipeline"] = {
        "get_data": "/gfs/api/scene-frame",
        "save_data": "scene cache + provider tile cache",
        "compile_data": "server-side layer frame",
        "draw_data": "browser layer engine consumes frame.layers",
        "update_map": "pill-selected layers only",
        "request_policy": "one frame request plus optional 24x24 parallel provider jobs per selected provider",
    }
    frame["provider_tiles"] = provider_jobs(bbox, providers=providers, grid=DEFAULT_VIEWPORT_GRID, limit=job_limit) if include_provider_jobs and providers else {
        "ok": True,
        "schema": "lftr_provider_tile_contract_v1",
        "grid": {"rows": DEFAULT_VIEWPORT_GRID, "cols": DEFAULT_VIEWPORT_GRID, "count": DEFAULT_VIEWPORT_GRID * DEFAULT_VIEWPORT_GRID},
        "providers": providers,
        "jobs": [],
        "job_count": 0,
    }
    if refresh:
        frame["refresh"] = svc.scene_cache_refresh_payload(bbox, visible_bbox, selected_layers, reason=f"scene_frame_{mode_l}")
    return frame

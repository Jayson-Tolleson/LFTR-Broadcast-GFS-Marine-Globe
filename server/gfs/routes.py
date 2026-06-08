from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from quart import Blueprint, current_app, jsonify, request, websocket

from server.gfs_service import GFSService
from server.gfs.sky import sky_payload
from server.gfs.inland_water import inland_water_payload, inland_conditions_payload, inland_bait_payload
from server.gfs.pipeline import scene_frame_payload

log = logging.getLogger("server.gfs.routes")
_INLAND_BUILD_JOBS: dict[str, dict[str, Any]] = {}




def _pid_is_running(pid: Any) -> bool:
    try:
        pid_i = int(pid)
        if pid_i <= 0:
            return False
        os.kill(pid_i, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _refresh_inland_build_jobs() -> None:
    now = time.time()
    for key, job in list(_INLAND_BUILD_JOBS.items()):
        if not isinstance(job, dict):
            continue
        if job.get("running") and not _pid_is_running(job.get("pid")):
            job["running"] = False
            job["status"] = "completed"
            job["completed_at"] = now
        # Keep recent history for diagnostics, but do not let stale job records
        # block a new viewport build forever.
        age = now - float(job.get("started_at") or now)
        if not job.get("running") and age > 3600:
            _INLAND_BUILD_JOBS.pop(key, None)

def _parse_bbox() -> dict[str, float] | None:
    raw = (request.args.get("bbox") or "").strip()
    if not raw:
        return None
    try:
        west, south, east, north = [float(x.strip()) for x in raw.split(",")[:4]]
        return {"west": west, "south": south, "east": east, "north": north}
    except Exception:
        return None


def _parse_visible_bbox() -> dict[str, float] | None:
    raw = (request.args.get("visible_bbox") or request.args.get("visibleBbox") or "").strip()
    if raw:
        try:
            west, south, east, north = [float(x.strip()) for x in raw.split(",")[:4]]
            return {"west": west, "south": south, "east": east, "north": north}
        except Exception:
            pass
    vp = (request.args.get("viewport") or "").strip()
    if vp:
        try:
            import json
            data = json.loads(vp)
            vb = data.get("visibleBbox") or data.get("visible_bbox")
            if isinstance(vb, dict):
                return {"west": float(vb["west"]), "south": float(vb["south"]), "east": float(vb["east"]), "north": float(vb["north"])}
            return {"west": float(data["west"]), "south": float(data["south"]), "east": float(data["east"]), "north": float(data["north"])}
        except Exception:
            return None
    return None



def _parse_layers(default: str = "") -> list[str]:
    raw = request.args.get("layers") or request.args.get("pills") or default or ""
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _parse_providers() -> list[str]:
    raw = request.args.get("providers") or request.args.get("provider") or ""
    return [x.strip().replace("-", "_") for x in str(raw).split(",") if x.strip()]

def _bbox_to_query(bbox: dict[str, float] | None) -> str:
    b = bbox or {"west": -126.0, "south": 29.0, "east": -114.0, "north": 39.0}
    return f"{float(b['west']):.4f},{float(b['south']):.4f},{float(b['east']):.4f},{float(b['north']):.4f}"


def _normalize_inland_build_bbox(bbox: dict[str, float] | None, scene_tier: str | None = None, geometry: str | None = None) -> tuple[dict[str, float], str | None]:
    """Keep runtime NHD builds viewport-scoped, US-water focused, and finite.

    Bad camera/tilt bboxes occasionally expand to near-global bounds. Launching a
    real ArcGIS/NHD build for those boxes spawns many subprocesses and starves the
    cache pop path. This helper clamps builds to a sane western-US/nearshore box
    and lets the route return a JSON diagnostic instead of starting runaway work.
    """
    b = bbox or {"west": -126.0, "south": 29.0, "east": -114.0, "north": 39.0}
    try:
        west = max(-179.9, min(179.9, float(b.get("west", -126.0))))
        south = max(-89.9, min(89.9, float(b.get("south", 29.0))))
        east = max(-179.9, min(179.9, float(b.get("east", -114.0))))
        north = max(-89.9, min(89.9, float(b.get("north", 39.0))))
    except Exception:
        west, south, east, north = -126.0, 29.0, -114.0, 39.0
    if east <= west:
        cx = (east + west) / 2.0
        west, east = cx - 6.0, cx + 6.0
    if north <= south:
        cy = (north + south) / 2.0
        south, north = cy - 5.0, cy + 5.0
    width = east - west
    height = north - south
    tier = str(scene_tier or "world").lower()
    # Keep this helper pure: it only normalizes/clamps the build bbox.
    # Geometry/env tuning belongs in _launch_inland_view_build(), where the
    # subprocess environment actually exists.  A prior revision referenced
    # undefined `geometry`/`env` names here, which prevented missing-tile
    # auto-builds from launching.
    max_w = 14.0 if tier in {"harbor", "local", "coastal", "regional"} else 24.0
    max_h = 12.0 if tier in {"harbor", "local", "coastal", "regional"} else 20.0
    note = None
    if width > max_w or height > max_h:
        cx = (west + east) / 2.0
        cy = (south + north) / 2.0
        west = cx - min(width, max_w) / 2.0
        east = cx + min(width, max_w) / 2.0
        south = cy - min(height, max_h) / 2.0
        north = cy + min(height, max_h) / 2.0
        note = f"bbox_clamped_for_runtime_build_from_{width:.1f}x{height:.1f}_deg"
    # USGS NHD runtime source route is only useful for CONUS/nearby US waters here.
    west = max(-130.0, west); east = min(-100.0, east)
    south = max(22.0, south); north = min(50.0, north)
    if east <= west or north <= south:
        west, south, east, north = -126.0, 29.0, -114.0, 39.0
        note = (note + ";" if note else "") + "bbox_reset_to_west_coast_default"
    return {"west": west, "south": south, "east": east, "north": north}, note


def _scene_tier_from_bbox_or_arg(bbox: dict[str, float] | None, scene_tier: str | None = None) -> str:
    tier = str(scene_tier or "").strip().lower()
    if tier:
        return tier
    b = bbox or {}
    try:
        west = float(b.get("west")); east = float(b.get("east")); south = float(b.get("south")); north = float(b.get("north"))
        width = abs(east - west); height = abs(north - south); span = max(width, height); area = max(0.0, width * height)
        if span <= 1.6 and area <= 2.6:
            return "harbor"
        if span <= 4.0 and area <= 14.0:
            return "coastal"
        if span <= 12.0 and area <= 90.0:
            return "regional"
    except Exception:
        pass
    return "world"


def _inland_detail_allowed_for_tier(tier: str | None) -> bool:
    return str(tier or "").strip().lower() in {"harbor", "local", "coastal", "regional"}


def _inland_overview_allowed_for_tier(tier: str | None) -> bool:
    # World zoom still primes cheap shoreline outlines and one lake-temp label per tile.
    # Only bait/thermal contour rendering waits for regional/local/harbor zoom.
    return str(tier or "").strip().lower() in {"world", "overview", "regional", "coastal", "local", "harbor"}


def _filter_world_inland_layers(layers: list[str] | tuple[str, ...] | None, bbox: dict[str, float] | None, scene_tier: str | None = None) -> tuple[list[str], dict[str, Any] | None]:
    wanted = list(layers or [])
    tier = _scene_tier_from_bbox_or_arg(bbox, scene_tier)
    # Keep inland-water and inland_water_temp in the scene at world zoom. The renderer/server
    # mark them as overview-only so bait contours do not draw until zoomed in.
    return wanted, {
        "inland_zoom_gate": {
            "allowed": True,
            "tier": tier,
            "overview_allowed": _inland_overview_allowed_for_tier(tier),
            "bait_allowed": _inland_detail_allowed_for_tier(tier),
            "policy": "world primes largest-lake outline/temp per tile; inland bait renders only on regional/coastal/local/harbor zoom",
        }
    }


def _inland_world_gate_shell(endpoint: str, bbox: dict[str, float] | None, scene_tier: str | None = None) -> dict[str, Any]:
    tier = _scene_tier_from_bbox_or_arg(bbox, scene_tier)
    return {
        "ok": True,
        "status": "overview_only",
        "payload_state": "world_overview_only",
        "source": "inland_world_overview_shell",
        "endpoint": endpoint,
        "bbox": bbox,
        "scene_tier": tier,
        "polygons": [],
        "lines": [],
        "features": [],
        "items": [],
        "temperature_points": [],
        "tempLabels": [],
        "bait": {"polygons": [], "bait_score": [], "targets": []},
        "count": 0,
        "policy": "world zoom may draw/prime largest lake outlines and one closed-lake temp per tile; bait contours are gated until zoomed in",
    }


def _inland_build_key(bbox: dict[str, float] | None, tier: str | None, geometry: str | None = None) -> str:
    b = bbox or {}
    return ":".join([
        f"{float(b.get('west', -126.0)):.2f}",
        f"{float(b.get('south', 29.0)):.2f}",
        f"{float(b.get('east', -114.0)):.2f}",
        f"{float(b.get('north', 39.0)):.2f}",
        str(tier or 'world'),
        str(geometry or 'vector'),
    ])


def _launch_inland_view_build(static_dir: Path, bbox: dict[str, float] | None, scene_tier: str | None = None, geometry: str | None = None) -> dict[str, Any]:
    """Start a viewport-scoped real NHD runtime tile build.

    This is intentionally not called by the installer by default and is never a
    fake/coarse fallback. On page load/viewport change it fetches real NHD
    ArcGIS source squares only for the current view/zoom, writes long-lived
    source cache files, then builds local json.gz runtime tiles. The Inland
    Waters read route never launches this automatically; callers must use
    /gfs/api/inland-water/build-cache when they intentionally want a build.
    """
    bbox, clamp_note = _normalize_inland_build_bbox(bbox, scene_tier, geometry)
    bbox_q = _bbox_to_query(bbox)
    key = _inland_build_key(bbox, scene_tier, geometry)
    _refresh_inland_build_jobs()
    now = time.time()
    # Global guard: do not spawn a gang of NHD subprocesses for the same moving viewport.
    for old_key, old_job in list(_INLAND_BUILD_JOBS.items()):
        if old_job.get("running") and now - float(old_job.get("started_at") or 0) < 90:
            return {**old_job, "deduped": True, "dedupe_reason": "recent_inland_build_global_guard", "requested_bbox": bbox_q, "clamp_note": clamp_note}
    existing = _INLAND_BUILD_JOBS.get(key)
    if existing and existing.get("running") and now - float(existing.get("started_at") or 0) < 1800:
        return {**existing, "deduped": True, "clamp_note": clamp_note}
    app_root = static_dir.parent
    script = app_root / "scripts" / "install_nhdplus_hr_view_cache.sh"
    if not script.exists():
        return {"status": "error", "running": False, "error": "install_nhdplus_hr_view_cache.sh_missing", "script": str(script)}
    log_dir = app_root / "data_sources" / "nhd_runtime_cache" / "_build_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ("inland_build_" + key.replace(":", "_").replace(",", "_") + ".log")
    env = os.environ.copy()
    env.setdefault("NHDPLUS_CACHE_DAYS", "31")
    env.setdefault("NHD_INLAND_LAKES_ONLY", "1")
    env["NHDPLUS_OUT_ROOT"] = "static/data/nhdplus_hr/tiles"
    env["NHDPLUS_SRC_ROOT"] = "data_sources/nhd_runtime_cache"
    # Smaller source squares on close zoom keep individual ArcGIS requests fast.
    tier = str(scene_tier or "world").lower()
    geometry_mode = str(geometry or "vector").lower()
    if geometry_mode in {"coarse", "overview", "world_coarse"}:
        env["NHDPLUS_GEOMETRY_MODE"] = "coarse"
        env.setdefault("NHD_MIN_AREA_KM2_WORLD", "0.6475")
        env.setdefault("NHD_MIN_LINE_KM_WORLD", "3.0")
        env.setdefault("NHD_MAX_POLYGONS_PER_TILE_WORLD", "32")
        env.setdefault("NHD_MAX_LINES_PER_TILE_WORLD", "0")
    else:
        env.setdefault("NHDPLUS_GEOMETRY_MODE", "vector")
    if tier in {"harbor", "local", "coastal", "high"}:
        env.setdefault("NHD_MIN_AREA_KM2_LOCAL", "0.0")
        env.setdefault("NHD_MIN_AREA_KM2_HARBOR", "0.0")
        env.setdefault("NHD_ARCGIS_SOURCE_MIN_AREA_KM2", "0.0")
        env.setdefault("NHD_ARCGIS_TILE_DEG", "0.25")
        env.setdefault("NHD_ARCGIS_PAGE_SIZE", "40")
        env.setdefault("NHD_ARCGIS_MAX_SOURCE_TILES", "96")
    elif tier in {"regional", "medium"}:
        env.setdefault("NHD_MIN_AREA_KM2_REGIONAL", "0.25")
        env.setdefault("NHD_ARCGIS_SOURCE_MIN_AREA_KM2", "0.25")
        env.setdefault("NHD_ARCGIS_TILE_DEG", "0.5")
        env.setdefault("NHD_ARCGIS_PAGE_SIZE", "50")
        env.setdefault("NHD_ARCGIS_MAX_SOURCE_TILES", "96")
    else:
        env.setdefault("NHD_MIN_AREA_KM2_WORLD", "0.6475")
        env.setdefault("NHD_ARCGIS_SOURCE_MIN_AREA_KM2", "0.6475")
        env.setdefault("NHD_ARCGIS_TILE_DEG", "1.0")
        env.setdefault("NHD_ARCGIS_PAGE_SIZE", "60")
        env.setdefault("NHD_ARCGIS_MAX_SOURCE_TILES", "96")
    env.setdefault("NHD_ARCGIS_MAX_RECORDS_PER_TILE", "600")
    # Current requested Inland Waters behavior: lakes only. Streams/flowlines can
    # be re-enabled later with NHD_INLAND_LAKES_ONLY=0 and NHD_ARCGIS_LAYERS=12,9,6.
    env.setdefault("NHD_ARCGIS_LAYERS", "12")
    env.setdefault("NHD_ARCGIS_TIMEOUT_SECONDS", "30")
    env.setdefault("NHD_ARCGIS_RETRIES", "1")
    env["NHDPLUS_VIEW_BBOX"] = bbox_q
    cmd = ["bash", str(script), bbox_q]
    with open(log_path, "ab") as fh:
        fh.write((f"\\n=== inland build start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} bbox={bbox_q} tier={tier} ===\\n").encode())
        proc = subprocess.Popen(cmd, cwd=str(app_root), stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    job = {"status": "started", "running": True, "pid": proc.pid, "bbox": bbox_q, "scene_tier": tier, "geometry": geometry_mode, "key": key, "started_at": now, "log": str(log_path), "source": "real_usgs_nhd_arcgis_runtime_tiles_builder", "clamp_note": clamp_note}
    _INLAND_BUILD_JOBS[key] = job
    log.info("[inland-water/build] started pid=%s bbox=%s tier=%s log=%s layers=%s lakes_only=%s timeout=%ss", proc.pid, bbox_q, tier, log_path, env.get("NHD_ARCGIS_LAYERS"), env.get("NHD_INLAND_LAKES_ONLY"), env.get("NHD_ARCGIS_TIMEOUT_SECONDS"))
    return job


def _svc(static_dir: Path) -> GFSService:
    svc = current_app.extensions.get("gfs_service")
    if svc is None:
        svc = GFSService(str(static_dir))
        current_app.extensions["gfs_service"] = svc
    return svc


async def _call_sync(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


def _json_clean(value: Any, depth: int = 0) -> Any:
    """Return a JSON-safe object so endpoint errors do not become nginx 502s.

    This intentionally accepts numpy scalars/arrays, NaN/Inf, Path objects, sets,
    and other accidental provider objects that can leak out of NCSS/netCDF paths.
    """
    if depth > 32:
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # numpy scalar/array compatibility without importing numpy.
    if hasattr(value, "item"):
        try:
            return _json_clean(value.item(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "tolist"):
        try:
            return _json_clean(value.tolist(), depth + 1)
        except Exception:
            pass
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            try:
                key = str(k)
            except Exception:
                key = repr(k)
            out[key] = _json_clean(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_json_clean(v, depth + 1) for v in value]
    try:
        return str(value)
    except Exception:
        return repr(value)


def _json(payload: Any, status: int = 200):
    return jsonify(_json_clean(payload)), status


PUBLIC_GFS_ROUTE_CONTRACT = [
    {"path": "/gfs", "role": "page", "policy": "serves the globe UI"},
    {"path": "/gfs/api/config", "role": "boot", "policy": "configuration only"},
    {"path": "/gfs/api/status", "role": "boot/debug", "policy": "cheap status only"},
    {"path": "/gfs/api/sky", "role": "sky", "policy": "day/night atmosphere only"},
    {"path": "/gfs/api/locations", "role": "static layer", "policy": "loaded once, rendered locally"},
    {"path": "/gfs/api/fishai", "role": "title-bar command/intel", "policy": "rule-based onboard direction parser; actions only, no renderer-owned provider fetch"},
    {"path": "/gfs/api/scene-cache", "role": "main cache read", "policy": "cache creates frame; no direct layer repair from browser"},
    {"path": "/gfs/api/cache/refresh", "role": "background warmer", "policy": "schedules proven layer providers behind shared scene cache"},
    {"path": "/gfs/api/inland-water", "role": "inland geometry cache read", "policy": "read-only runtime json.gz tiles"},
    {"path": "/gfs/api/inland-water/build-cache", "role": "inland build queue", "policy": "explicit/deduped NHD/ArcGIS runtime-tile builder"},
    {"path": "/gfs/api/inland-water-temp", "role": "inland live companion", "policy": "temperature labels only; never clears shoreline geometry"},
    {"path": "/ws/gfs", "role": "updates", "policy": "nonblocking cache/tile events"},
]

DEBUG_GFS_ROUTE_CONTRACT = [
    {"path": "/gfs/api/debug/provider/clouds", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=clouds,rain", "policy": "canonical debug/manual provider alias"},
    {"path": "/gfs/api/debug/provider/bait", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=bait", "policy": "canonical debug/manual provider alias"},
    {"path": "/gfs/api/debug/provider/shark-intel", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=shark-intel", "policy": "canonical debug/manual provider alias for Shark Intel"},
    {"path": "/gfs/api/debug/provider/boater", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=boater", "policy": "canonical debug/manual provider alias"},
    {"path": "/gfs/api/debug/provider/lightning", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=lightning", "policy": "canonical debug/manual provider alias"},
    {"path": "/gfs/api/debug/inland/status", "family": "inland_debug", "replacement": "/gfs/api/inland-water", "policy": "canonical debug/manual inland alias"},
    {"path": "/gfs/api/debug/inland/diagnostics", "family": "inland_debug", "replacement": "/gfs/api/inland-water", "policy": "canonical debug/manual inland alias"},
    {"path": "/gfs/api/tiles", "family": "tile_debug", "replacement": "/gfs/api/scene-cache", "policy": "debug/manual only"},
    {"path": "/gfs/api/tile-plan", "family": "tile_debug", "replacement": "/gfs/api/scene-cache", "policy": "debug/manual only"},
    {"path": "/gfs/api/cache/status", "family": "cache_debug", "replacement": "/gfs/api/debug/routes", "policy": "debug/manual only"},
    {"path": "/gfs/api/clouds", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=clouds,rain", "policy": "debug/manual only"},
    {"path": "/gfs/api/bait-advanced", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=bait", "policy": "debug/manual only"},
    {"path": "/gfs/api/shark-intel", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=shark-intel", "policy": "debug/manual only"},
    {"path": "/gfs/api/boats", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=boater", "policy": "debug/manual only"},
    {"path": "/gfs/api/ocean-points", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=boater,bait", "policy": "debug/manual only"},
    {"path": "/gfs/api/lightning", "family": "provider_debug", "replacement": "/gfs/api/cache/refresh?layers=lightning", "policy": "debug/manual only"},
    {"path": "/gfs/api/provider-debug", "family": "provider_debug", "replacement": "/gfs/api/debug/routes", "policy": "cheap by default; ?deep=1 is manual only"},
    {"path": "/gfs/api/payload-debug", "family": "provider_debug", "replacement": "/gfs/api/debug/routes", "policy": "cheap by default; ?deep=1 is manual only"},
    {"path": "/gfs/api/source-check", "family": "provider_debug", "replacement": "/gfs/api/debug/routes", "policy": "debug/manual only"},
    {"path": "/gfs/api/inland-water/status", "family": "inland_debug", "replacement": "/gfs/api/inland-water", "policy": "debug/manual only"},
    {"path": "/gfs/api/inland-water/diagnostics", "family": "inland_debug", "replacement": "/gfs/api/inland-water", "policy": "debug/manual only"},
    {"path": "/gfs/api/inland-conditions", "family": "inland_debug", "replacement": "/gfs/api/inland-water-temp", "policy": "debug/manual only"},
    {"path": "/gfs/api/inland-bait", "family": "inland_debug", "replacement": "/gfs/api/scene-cache?layers=inland_water_temp", "policy": "debug/manual only"},
]

def _debug_contract_for_path(path: str) -> dict[str, Any]:
    for item in DEBUG_GFS_ROUTE_CONTRACT:
        if item.get("path") == path:
            return dict(item)
    return {"path": path, "family": "debug", "replacement": "/gfs/api/scene-cache", "policy": "debug/manual only"}

def _annotate_debug_payload(payload: Any, endpoint: str | None = None, family: str | None = None, replacement: str | None = None) -> Any:
    contract = _debug_contract_for_path(endpoint or request.path)
    contract.update({k: v for k, v in {"family": family, "replacement": replacement}.items() if v})
    meta = {
        "route_class": "debug_only",
        "debug_family": contract.get("family"),
        "replacement": contract.get("replacement"),
        "policy": contract.get("policy") or "debug/manual only",
        "main_recommendation": "requests_create_cache_cache_creates_frame_frame_creates_rendering_pills_only_choose_visibility_and_subscription",
    }
    if isinstance(payload, dict):
        out = dict(payload)
        out.setdefault("route_class", meta["route_class"])
        out.setdefault("debug_contract", meta)
        return out
    return {"ok": True, "payload": payload, "debug_contract": meta, "route_class": "debug_only"}

def _debug_json(payload: Any, endpoint: str | None = None, family: str | None = None, replacement: str | None = None, status: int = 200):
    return _json(_annotate_debug_payload(payload, endpoint=endpoint, family=family, replacement=replacement), status)


def _endpoint_shell(endpoint: str, exc: Exception, bbox: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "endpoint": endpoint,
        "source": f"{endpoint.strip('/').replace('/', '_')}_error_caught",
        "payload_state": "provider_failed",
        "bbox": bbox,
        "error": str(exc),
        "note": "Endpoint caught exception and returned JSON status 200 so the browser does not see GET non-ok/502.",
    }



_FISHAI_LAYER_WORDS = {
    "cloud": "clouds", "clouds": "clouds",
    "rain": "rain", "precip": "rain", "precipitation": "rain",
    "lightning": "lightning", "glm": "lightning",
    "jet": "jetstream", "jetstream": "jetstream", "balloons": "jetstream",
    "bait": "bait", "baitfish": "bait",
    "shark": "shark-intel", "sharks": "shark-intel", "shark-intel": "shark-intel",
    "boat": "boater", "boats": "boater", "boater": "boater",
    "inland": "inland-water", "lake": "inland-water", "water": "inland-water",
    "locations": "locations", "beacons": "locations",
}

_FISHAI_SPECIES_HINTS = {
    "leopard": {"label": "leopard shark", "layers": ["shark-intel", "bait"], "rig": "Carolina/fish-finder rig, 2/0–5/0 circle hook, fresh squid/mackerel, legal-slot 36–42 in only."},
    "tiger": {"label": "tiger shark watch", "layers": ["shark-intel"], "rig": "Warm-water anomaly watch only in SoCal; verify legality and treat as observation/handle-care intel."},
    "sand": {"label": "sand shark / sandy nearshore", "layers": ["shark-intel", "bait"], "rig": "Sandy nearshore/dock edge: smaller circle hook, squid/anchovy/mackerel strip; avoid undersize nursery zones."},
    "shark": {"label": "legal-slot shark", "layers": ["shark-intel", "bait"], "rig": "Fish-finder/Carolina rig, circle hook, abrasion leader, measure total length before keeping."},
    "halibut": {"label": "halibut", "layers": ["bait", "boater"], "rig": "Sliding sinker or dropper loop with live bait/swimbait; target sand/structure edges and bait contours."},
    "bass": {"label": "bass", "layers": ["bait"], "rig": "Swimbait, spoon, or bait rig along structure/current/bait edges."},
    "mackerel": {"label": "mackerel", "layers": ["bait"], "rig": "Sabiki or small metal/jig near active bait boils and pier current seams."},
    "corbina": {"label": "corbina", "layers": ["bait"], "rig": "Light Carolina rig with sand crabs/mussels in the skinny-water trough."},
    "trout": {"label": "trout", "layers": ["inland-water"], "rig": "Small spoons, mini-jigs, PowerBait or nightcrawler near temp/depth labels."},
    "catfish": {"label": "catfish", "layers": ["inland-water"], "rig": "Slip sinker with cut bait/nightcrawler near evening depth breaks."},
}


def _fishai_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9\-']*", (text or "").lower()))


def _fishai_loc_id(loc: dict[str, Any]) -> str:
    return str(loc.get("id") or loc.get("location_id") or loc.get("location_key") or loc.get("csv_id") or "")


def _fishai_loc_name(loc: dict[str, Any]) -> str:
    return str(loc.get("name") or loc.get("title") or _fishai_loc_id(loc) or "Fishing location")


def _fishai_geo_nm(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    try:
        a1, o1, a2, o2 = float(lat1), float(lon1), float(lat2), float(lon2)
        mean = math.radians((a1 + a2) / 2.0)
        return math.hypot((a2 - a1) * 60.0, (o2 - o1) * 60.0 * math.cos(mean))
    except Exception:
        return float("nan")


def _fishai_best_location(locations: list[dict[str, Any]], query: str, selected: dict[str, Any] | None = None) -> dict[str, Any] | None:
    q = (query or "").lower().strip()
    words = _fishai_words(q)
    selected_lat = selected.get("lat") if isinstance(selected, dict) else None
    selected_lon = selected.get("lon") if isinstance(selected, dict) else None
    best: tuple[float, dict[str, Any]] | None = None
    for loc in locations:
        name = _fishai_loc_name(loc).lower()
        report_text = " ".join(str(x) for x in (loc.get("all_reports") or loc.get("reports") or [])).lower()
        hay = f"{name} {report_text}"
        loc_words = _fishai_words(hay)
        score = 0.0
        if q and q in name:
            score += 90
        if q and q in hay:
            score += 35
        overlap = len(words & loc_words)
        score += overlap * 12
        for token in words:
            if len(token) >= 4 and token in hay:
                score += 6
        lat = loc.get("lat")
        lon = loc.get("lon")
        if selected_lat is not None and selected_lon is not None:
            d = _fishai_geo_nm(selected_lat, selected_lon, lat, lon)
            if math.isfinite(d):
                score += max(0, 10 - min(10, d / 12))
        if best is None or score > best[0]:
            best = (score, loc)
    return best[1] if best and best[0] > 0 else None


def _fishai_score_locations(locations: list[dict[str, Any]], prompt: str, limit: int = 5) -> list[dict[str, Any]]:
    words = _fishai_words(prompt)
    marine_words = {"shore", "surf", "pier", "dock", "harbor", "ocean", "coast", "bay", "beach", "jetty"}
    species_words = set(_FISHAI_SPECIES_HINTS)
    out = []
    for loc in locations:
        name = _fishai_loc_name(loc)
        reports = loc.get("all_reports") or loc.get("reports") or []
        hay = f"{name} {' '.join(str(x) for x in reports)}".lower()
        score = 42.0
        score += min(28, len(words & _fishai_words(hay)) * 7)
        score += min(18, sum(1 for w in marine_words if w in hay and (not words or w in words)) * 6)
        score += min(24, sum(1 for w in species_words if w in words and w in hay) * 8)
        prob = loc.get("probability") or loc.get("confidence")
        try:
            score += max(0, min(12, float(prob) * 12))
        except Exception:
            pass
        if "shark" in words and re.search(r"shark|surf|pier|dock|harbor|bay|beach|coast|ocean", hay):
            score += 14
        if "halibut" in words and re.search(r"halibut|sand|surf|bay|harbor", hay):
            score += 12
        if "mackerel" in words and re.search(r"mackerel|pier|bait", hay):
            score += 12
        lat = loc.get("lat")
        lon = loc.get("lon")
        out.append({
            "id": _fishai_loc_id(loc),
            "name": name,
            "lat": lat,
            "lon": lon,
            "score": round(max(0, min(100, score)), 1),
            "reason": "matched reports/name/species keywords with available beacon intel",
        })
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out[:limit]


def _fishai_extract_destination(prompt: str) -> str:
    text = (prompt or "").strip()
    m = re.search(r"(?:go\s+to|goto|open|show|view|location)\s+(.+)$", text, flags=re.I)
    if not m:
        return ""
    tail = m.group(1)
    tail = re.split(r"\b(?:tilt|heading|range|zoom|angle|and|with|species|rig|target)\b", tail, maxsplit=1, flags=re.I)[0]
    return tail.strip(" .,;:!?")[:80]


def _fishai_camera_actions(prompt: str) -> list[dict[str, Any]]:
    text = prompt or ""
    camera: dict[str, Any] = {}
    m = re.search(r"(?:tilt|angle)\s*(\d{1,3})", text, flags=re.I)
    if m:
        camera["tilt"] = max(0, min(89, int(m.group(1))))
    m = re.search(r"(?:heading|bearing)\s*(\d{1,3})", text, flags=re.I)
    if m:
        camera["heading"] = int(m.group(1)) % 360
    m = re.search(r"(?:range|zoom)\s*(\d+(?:\.\d+)?)\s*(km|mi|m|meters?|miles?)?", text, flags=re.I)
    if m:
        value = float(m.group(1))
        unit = (m.group(2) or "m").lower()
        if unit.startswith("km"):
            value *= 1000.0
        elif unit.startswith("mi"):
            value *= 1609.344
        camera["range"] = max(500.0, min(20000000.0, value))
    return [{"type": "set_camera", "camera": camera}] if camera else []


def _fishai_layer_actions(prompt: str, species_layers: list[str]) -> list[dict[str, Any]]:
    text = (prompt or "").lower()
    actions: list[dict[str, Any]] = []
    for raw, layer in _FISHAI_LAYER_WORDS.items():
        if not re.search(rf"\b{re.escape(raw)}\b", text):
            continue
        if re.search(rf"\b(?:off|hide|disable|clear)\b[^.]*\b{re.escape(raw)}\b|\b{re.escape(raw)}\b[^.]*\b(?:off|hide|disable|clear)\b", text):
            actions.append({"type": "set_layer", "layer": layer, "enabled": False})
        elif re.search(rf"\b(?:on|show|enable|draw|render)\b[^.]*\b{re.escape(raw)}\b|\b{re.escape(raw)}\b[^.]*\b(?:on|show|enable|draw|render)\b", text):
            actions.append({"type": "set_layer", "layer": layer, "enabled": True})
    for layer in species_layers:
        actions.append({"type": "set_layer", "layer": layer, "enabled": True})
    # Deduplicate preserving last state per layer.
    merged: dict[str, dict[str, Any]] = {}
    for action in actions:
        merged[str(action.get("layer"))] = action
    return list(merged.values())


def _fishai_species_context(prompt: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    words = _fishai_words(prompt)
    contexts = []
    layers: list[str] = []
    prefs: list[str] = []
    for key, meta in _FISHAI_SPECIES_HINTS.items():
        if key in words:
            contexts.append({"key": key, **meta})
            layers.extend(meta.get("layers") or [])
            prefs.append(meta["label"])
    # Special target-size phrasing for the Shark Intel pill.
    if "36" in words or "42" in words or "slot" in words:
        layers.append("shark-intel")
        contexts.append({"key": "slot36_42", "label": "36–42 in legal-slot shark", "rig": "Measure total length; keep only legal fish and release undersize nursery fish."})
    return contexts, sorted(set(layers)), prefs


def _fishai_payload(prompt: str, svc: GFSService, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    prompt = (prompt or "").strip()
    fish_payload = svc.fish_payload()
    locations = fish_payload.get("locations") or fish_payload.get("items") or []
    if not isinstance(locations, list):
        locations = []
    selected = selected if isinstance(selected, dict) else None
    species_ctx, species_layers, species_prefs = _fishai_species_context(prompt)
    top_locations = _fishai_score_locations(locations, prompt, 5)
    actions: list[dict[str, Any]] = []
    reply: list[str] = []

    destination = _fishai_extract_destination(prompt)
    dest_loc = _fishai_best_location(locations, destination, selected) if destination else None
    if dest_loc:
        lat = dest_loc.get("lat")
        lon = dest_loc.get("lon")
        actions.append({"type": "fly_to", "lat": lat, "lon": lon, "altitude": 120, "range": 260000, "tilt": 58, "heading": 210, "location_id": _fishai_loc_id(dest_loc), "location_name": _fishai_loc_name(dest_loc)})
        actions.append({"type": "open_location", "location_id": _fishai_loc_id(dest_loc), "location_name": _fishai_loc_name(dest_loc)})
        reply.append(f"Going to {_fishai_loc_name(dest_loc)} and opening its intel pane.")
    elif destination:
        reply.append(f"I could not match '{destination}' to a saved beacon; showing best scored locations instead.")

    actions.extend(_fishai_camera_actions(prompt))
    actions.extend(_fishai_layer_actions(prompt, species_layers))
    if species_layers:
        actions.append({"type": "refresh_layers", "layers": species_layers})
    if species_prefs:
        actions.append({"type": "set_preference", "key": "target_species", "value": ", ".join(species_prefs)})

    if species_ctx:
        rigs = [str(x.get("rig")) for x in species_ctx if x.get("rig")]
        reply.append("Target: " + ", ".join(x.get("label", x.get("key", "species")) for x in species_ctx[:3]))
        if rigs:
            reply.append("Rig: " + rigs[0])
    else:
        reply.append("Search mode: ranking saved fish beacons by prompt words, reports, water access hints, and active overlay goals.")

    if re.search(r"\b(best|high\s*score|target|catch|where|fish)\b", prompt, flags=re.I) and top_locations:
        reply.append("Top high-score pane candidates: " + "; ".join(f"{x['name']} {round(x['score'])}%" for x in top_locations[:3]))

    if re.search(r"\b(shark|leopard|tiger|sand)\b", prompt, flags=re.I):
        reply.append("Shark Intel will prefer SST-valid ocean/coast cells, 36–42 in leopard prime-slot contours, and tiger/sand shark watch notes in the pane.")

    if not actions and top_locations:
        first = top_locations[0]
        # For pure search questions, do not force navigation; return top locations.
        if re.search(r"\b(go|goto|open|show|view|move|fly)\b", prompt, flags=re.I):
            actions.append({"type": "fly_to", "lat": first.get("lat"), "lon": first.get("lon"), "altitude": 120, "range": 320000, "tilt": 55, "heading": 210, "location_id": first.get("id"), "location_name": first.get("name")})

    return {
        "ok": True,
        "schema": "lftr_fishai_command_v1",
        "source": "onboard_rule_based_fishai",
        "prompt": prompt,
        "headline": "FISHAI command/search ready",
        "reply_lines": reply[:8],
        "actions": actions,
        "top_locations": top_locations,
        "species_context": species_ctx,
        "supported_examples": [
            "go to Newport pier tilt 60 heading 240",
            "best leopard shark 36-42 shore rig",
            "show bait and shark intel",
            "clouds off rain on",
            "target halibut high score",
        ],
    }

def create_gfs_blueprint(static_dir: Path) -> Blueprint:
    bp = Blueprint("gfs", __name__)

    @bp.get("/gfs")
    async def gfs_page():
        from quart import Response, send_file
        page = static_dir / "indexgfs.html"
        if page.exists():
            return await send_file(str(page))
        # Keep /gfs from crashing if an interrupted install briefly leaves the
        # static file missing.  The installer still validates this file, but the
        # route should report a useful page instead of a 500 traceback.
        fallback = static_dir / "index.html"
        if fallback.exists():
            log.error("/gfs static/indexgfs.html missing; serving index.html fallback. Re-run installer to restore GFS page.")
            return await send_file(str(fallback))
        log.error("/gfs static/indexgfs.html missing and no fallback exists static_dir=%s", static_dir)
        return Response("LFTR /gfs page missing static/indexgfs.html. Re-run the .run installer or restore broadcast/static/indexgfs.html.", status=503, content_type="text/plain")

    @bp.get("/gfs/api/health")
    async def health():
        return jsonify(await _call_sync(_svc(static_dir).health_payload))

    @bp.get("/gfs/api/config")
    async def config():
        payload = await _call_sync(_svc(static_dir).config_payload)
        # Frontend currently opens /ws/gfs, so advertise that exact endpoint.
        if isinstance(payload, dict):
            payload["ws_base"] = "/ws/gfs"
        return jsonify(payload)

    @bp.get("/gfs/api/status")
    async def status():
        return jsonify(await _call_sync(_svc(static_dir).status_payload))

    @bp.get("/gfs/api/debug")
    @bp.get("/gfs/api/debug/routes")
    async def debug_routes():
        return _json({
            "ok": True,
            "schema": "lftr_gfs_debug_route_contract_v1",
            "source": "route_contract",
            "public_routes": PUBLIC_GFS_ROUTE_CONTRACT,
            "debug_only_routes": DEBUG_GFS_ROUTE_CONTRACT,
            "main_recommendation": "requests_create_cache_cache_creates_frame_frame_creates_rendering_pills_only_choose_visibility_and_subscription",
            "render_cycle": [
                "viewport_settled",
                "read_scene_cache_fast",
                "render_latest_versions",
                "nudge_cache_refresh_background",
                "swap_changed_layer_versions_only",
            ],
            "pill_cycle": [
                "pill_off_hides_and_clears_visuals_no_network",
                "pill_on_subscribes_layer_reads_cache_then_nudges_refresh",
            ],
            "world_subscription_renderer": {
                "normal_browser_routes": ["/gfs/api/scene-cache", "/gfs/api/cache/refresh", "/gfs/api/inland-water", "/gfs/api/inland-water/build-cache", "/gfs/api/inland-water-temp", "/gfs/api/locations", "/gfs/api/sky", "/gfs/api/fishai"],
                "polygon_contract": "stable id + geometry hash/version; update/morph changed polygons; fade stale polygons; never rebuild unchanged geometry",
                "shark_intel": "Shark Intel is a normal scene-cache layer: layers=shark-intel; provider/debug aliases are manual only; renderer reuses the bait marching-squares spine for boil contours.",
                "particle_candidates": ["rain", "clouds", "jetstream", "lightning"],
            },
        })

    @bp.get("/gfs/api/sky")
    async def sky():
        try:
            lat = float(request.args.get("lat", ""))
            lon = float(request.args.get("lon", ""))
        except Exception:
            return _json({
                "ok": False,
                "schema": "lftr_gfs_sky_v1",
                "error": "invalid_lat_lon",
                "note": "Provide numeric lat and lon query params."
            }, 400)
        if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 540:
            return _json({
                "ok": False,
                "schema": "lftr_gfs_sky_v1",
                "error": "invalid_lat_lon",
                "note": "lat must be -90..90 and lon should be a finite longitude."
            }, 400)
        payload = sky_payload(lat, lon)
        log.debug("[gfs/sky] updated mode=%s sun=%.2f lat=%.3f lon=%.3f", payload.get("mode"), payload.get("sun_elevation_deg"), payload.get("lat"), payload.get("lon"))
        return _json(payload)



    @bp.get("/gfs/api/scene-frame")
    async def scene_frame():
        bbox = _parse_bbox()
        layers = _parse_layers()
        layers, gate_meta = _filter_world_inland_layers(layers, bbox, request.args.get("scene_tier"))
        mode = str(request.args.get("mode") or "read").lower()
        refresh = str(request.args.get("refresh") or "0").lower() in {"1", "true", "yes", "on"}
        include_jobs = str(request.args.get("provider_jobs", "1")).lower() not in {"0", "false", "no", "off"}
        try:
            limit_raw = request.args.get("job_limit") or request.args.get("limit") or ""
            job_limit = int(limit_raw) if str(limit_raw).strip() else None
        except Exception:
            job_limit = None
        try:
            payload = await _call_sync(scene_frame_payload, _svc(static_dir), bbox, _parse_visible_bbox(), layers, mode=mode, refresh=refresh, include_provider_jobs=include_jobs, job_limit=job_limit)
            if gate_meta and isinstance(payload, dict):
                payload.setdefault("meta", {}).update(gate_meta)
            return _json(payload)
        except Exception as exc:
            log.exception("/gfs/api/scene-frame failed")
            shell = _endpoint_shell("/gfs/api/scene-frame", exc, bbox)
            shell.update({"schema": "lftr_gfs_scene_frame_v1_error", "layers": {}, "provider_tiles": {"jobs": [], "job_count": 0}, "refresh_interval_ms": 120000})
            return _json(shell)

    @bp.get("/gfs/api/scene-cache")
    async def scene_cache():
        bbox = _parse_bbox()
        layers = _parse_layers()
        layers, gate_meta = _filter_world_inland_layers(layers, bbox, request.args.get("scene_tier"))
        mode = str(request.args.get("mode") or "read").lower()
        refresh = str(request.args.get("refresh", "0" if mode in {"read", "first_paint", "fast", "cache"} else "1")).lower() not in {"0", "false", "no", "off"}
        fast = str(request.args.get("fast") or request.args.get("cache_only") or "0").lower() in {"1", "true", "yes", "on"}
        try:
            if fast or mode in {"fast", "first_paint", "cache_only"}:
                # Strict boot/read path: no live provider, no background build, no
                # threadpool wait.  This endpoint must return quickly even while
                # old warmers/providers are busy.
                payload = _svc(static_dir).scene_cache_fast_payload(bbox, _parse_visible_bbox(), layers, mode=mode)
                if gate_meta and isinstance(payload, dict):
                    payload.setdefault("meta", {}).update(gate_meta)
                return _json(payload)
            payload = await _call_sync(_svc(static_dir).scene_cache_payload, bbox, _parse_visible_bbox(), layers, refresh=refresh, mode=mode)
            if gate_meta and isinstance(payload, dict):
                payload.setdefault("meta", {}).update(gate_meta)
            return _json(payload)
        except Exception as exc:
            log.exception("/gfs/api/scene-cache failed")
            shell = _endpoint_shell("/gfs/api/scene-cache", exc, bbox)
            shell.update({"schema": "lftr_scene_cache_subscription_v1_error", "layers": {}, "cache": {"layers": {}}, "refresh_interval_ms": 120000})
            return _json(shell)

    @bp.get("/gfs/api/cache/refresh")
    @bp.post("/gfs/api/cache/refresh")
    async def scene_cache_refresh():
        bbox = _parse_bbox()
        layers = _parse_layers()
        layers, gate_meta = _filter_world_inland_layers(layers, bbox, request.args.get("scene_tier"))
        reason = request.args.get("reason") or "browser_2min_subscription_refresh"
        if str(request.args.get("force") or "").lower() in {"1", "true", "yes", "on"}:
            reason = f"{reason}_force=1"
        try:
            payload = await _call_sync(_svc(static_dir).scene_cache_refresh_payload, bbox, _parse_visible_bbox(), layers, reason)
            if gate_meta and isinstance(payload, dict):
                payload.setdefault("meta", {}).update(gate_meta)
            return _json(payload)
        except Exception as exc:
            log.exception("/gfs/api/cache/refresh failed")
            shell = _endpoint_shell("/gfs/api/cache/refresh", exc, bbox)
            shell.update({"schema": "lftr_scene_cache_refresh_v1_error", "jobs": {}, "refresh_interval_ms": 120000})
            return _json(shell)

    @bp.get("/gfs/api/cache/janitor")
    @bp.post("/gfs/api/cache/janitor")
    async def scene_cache_janitor():
        reason = request.args.get("reason") or "manual"
        try:
            return _json(await _call_sync(_svc(static_dir).scene_cache_janitor_payload, reason))
        except Exception as exc:
            log.exception("/gfs/api/cache/janitor failed")
            shell = _endpoint_shell("/gfs/api/cache/janitor", exc, _parse_bbox())
            shell.update({"schema": "lftr_scene_cache_janitor_v1_error", "removed_count": 0})
            return _json(shell)

    @bp.get("/gfs/api/inland-water-temp")
    @bp.get("/gfs/api/inland-water/temperature")
    async def inland_water_temperature():
        bbox = _parse_bbox()
        scene_tier = request.args.get("scene_tier") or request.args.get("tier")
        try:
            payload = await _call_sync(_svc(static_dir).inland_water_temp_payload, bbox, _parse_visible_bbox())
            if isinstance(payload, dict):
                tier = _scene_tier_from_bbox_or_arg(_parse_visible_bbox() or bbox, scene_tier)
                payload["scene_tier"] = tier
                payload["inland_bait_render_allowed"] = _inland_detail_allowed_for_tier(tier)
                payload["overview_only"] = not _inland_detail_allowed_for_tier(tier)
            return _json(payload)
        except Exception as exc:
            log.exception("/gfs/api/inland-water-temp failed")
            shell = _endpoint_shell("/gfs/api/inland-water-temp", exc, bbox)
            shell.update({"temperature_points": [], "tempLabels": [], "count": 0, "status": "error"})
            return _json(shell)

    @bp.get("/gfs/api/weather")
    async def weather():
        return jsonify(await _call_sync(_svc(static_dir).generate_weather_payload, _parse_bbox()))

    @bp.get("/gfs/api/debug/provider/clouds")
    @bp.get("/gfs/api/clouds")
    async def clouds():
        bbox = _parse_bbox()
        try:
            payload = await _call_sync(_svc(static_dir).cloud_tiles_payload, bbox, _parse_visible_bbox())
            return _debug_json(payload, endpoint="/gfs/api/clouds", family="provider_debug", replacement="/gfs/api/cache/refresh?layers=clouds,rain")
        except Exception as exc:
            log.exception("/gfs/api/clouds failed")
            shell = _endpoint_shell("/gfs/api/clouds", exc, bbox)
            shell.update({"items": [], "tiles": [], "cloud_regions": [], "precip_columns": [], "features": []})
            return _json(shell)

    @bp.get("/gfs/api/provider-tiles")
    @bp.get("/gfs/api/providers/tiles")
    async def provider_tiles():
        bbox = _parse_bbox()
        include_urls = str(request.args.get("urls") or request.args.get("include_urls") or "0").lower() in {"1", "true", "yes", "on"}
        try:
            limit_raw = request.args.get("limit") or ""
            limit = int(limit_raw) if str(limit_raw).strip() else None
        except Exception:
            limit = None
        return _json(_svc(static_dir).provider_tile_contract_payload(bbox, _parse_providers() or None, include_urls=include_urls, limit=limit))

    @bp.get("/gfs/api/tile-plan")
    async def tile_plan():
        try:
            max_tiles = int(request.args.get("max_tiles") or request.args.get("limit") or 576)
        except Exception:
            max_tiles = 576
        return _debug_json(await _call_sync(_svc(static_dir).tile_plan_payload, _parse_bbox(), max_tiles), endpoint="/gfs/api/tile-plan", family="tile_debug", replacement="/gfs/api/scene-cache")

    @bp.get("/gfs/api/tiles")
    @bp.get("/gfs/api/tile-intel")
    async def tiles_intel():
        try:
            max_tiles = int(request.args.get("max_tiles") or request.args.get("limit") or 576)
        except Exception:
            max_tiles = 576
        try:
            return _debug_json(await _call_sync(_svc(static_dir).tiles_intel_payload, _parse_bbox(), max_tiles, _parse_visible_bbox()), endpoint="/gfs/api/tiles", family="tile_debug", replacement="/gfs/api/scene-cache")
        except Exception as exc:
            log.exception("/gfs/api/tiles failed")
            return _json({
                "ok": False,
                "schema": "lftr_scene_tiles_point_cache_v1_error_caught",
                "source": "tiles_endpoint_error_caught",
                "payload_state": "read_failed",
                "error": str(exc),
                "tiles": [],
                "count": 0,
                "note": "tiles endpoint caught exception and returned JSON status 200 instead of nginx/Quart 500",
            })

    @bp.get("/gfs/api/cache/status")
    async def cache_status():
        return _debug_json(await _call_sync(_svc(static_dir).cache_status_payload), endpoint="/gfs/api/cache/status", family="cache_debug", replacement="/gfs/api/debug/routes")

    @bp.get("/gfs/api/tile-intel/<tile_id>")
    async def one_tile_intel(tile_id: str):
        return jsonify(await _call_sync(_svc(static_dir).tile_intel_payload, tile_id, 300, _parse_visible_bbox()))

    @bp.get("/gfs/api/ocean")
    async def ocean():
        bbox = _parse_bbox()
        try:
            return _debug_json(await _call_sync(_svc(static_dir).ocean_payload, bbox, _parse_visible_bbox()), endpoint="/gfs/api/ocean", family="provider_debug", replacement="/gfs/api/scene-cache")
        except Exception as exc:
            log.exception("/gfs/api/ocean failed")
            shell = _endpoint_shell("/gfs/api/ocean", exc, bbox)
            shell.update({"points": [], "boats": [], "items": []})
            return _json(shell)

    @bp.get("/gfs/api/debug/provider/ocean-points")
    @bp.get("/gfs/api/ocean-points")
    async def ocean_points():
        lod = (request.args.get("lod") or "auto").strip() or "auto"
        bbox = _parse_bbox()
        try:
            return _debug_json(await _call_sync(_svc(static_dir).ocean_points_payload, bbox, lod, _parse_visible_bbox()), endpoint="/gfs/api/ocean-points", family="provider_debug", replacement="/gfs/api/cache/refresh?layers=boater,bait")
        except Exception as exc:
            log.exception("/gfs/api/ocean-points failed")
            shell = _endpoint_shell("/gfs/api/ocean-points", exc, bbox)
            shell.update({"points": [], "items": [], "grid": {"real_grid": False, "reason": "error_caught"}})
            return _json(shell)

    @bp.get("/gfs/api/field")
    async def field_compat():
        """Compatibility endpoint for older frontend/debug calls.

        Recent builds expose ocean vectors through /gfs/api/ocean-points and
        /gfs/api/boats.  Some install/debug scripts still probe
        /gfs/api/field?field=current; returning a structured live/cache payload
        here avoids a misleading HTML 404 while preserving the newer contract.
        """
        bbox = _parse_bbox()
        field_name = str(request.args.get("field") or "current").strip().lower()
        lod = (request.args.get("lod") or "auto").strip() or "auto"
        try:
            if field_name in {"current", "currents", "ocean", "ocean_current", "hycom"}:
                payload = await _call_sync(_svc(static_dir).ocean_points_payload, bbox, lod, _parse_visible_bbox())
                points = payload.get("points") or payload.get("current_points") or payload.get("ocean_points") or []
                out = dict(payload) if isinstance(payload, dict) else {"ok": False, "points": []}
                out.update({
                    "ok": bool(out.get("ok", True)),
                    "endpoint": "/gfs/api/field",
                    "field": field_name,
                    "compat_route": True,
                    "source_endpoint": "/gfs/api/ocean-points",
                    "points": points,
                    "current_points": points,
                    "ocean_points": points,
                    "count": len(points),
                    "route_class": "compat_provider_debug",
                    "replacement": "/gfs/api/ocean-points?bbox=" + _bbox_to_query(bbox),
                })
                return _debug_json(out, endpoint="/gfs/api/field", family="compat_provider_debug", replacement="/gfs/api/ocean-points")
            return _json({
                "ok": False,
                "endpoint": "/gfs/api/field",
                "field": field_name,
                "error": "unsupported_field",
                "supported_fields": ["current", "currents", "ocean", "hycom"],
                "route_class": "compat_provider_debug",
            })
        except Exception as exc:
            log.exception("/gfs/api/field failed")
            shell = _endpoint_shell("/gfs/api/field", exc, bbox)
            shell.update({"field": field_name, "points": [], "current_points": [], "count": 0, "compat_route": True})
            return _json(shell)

    @bp.get("/gfs/api/debug/provider/boater")
    @bp.get("/gfs/api/boats")
    async def boats():
        bbox = _parse_bbox()
        try:
            return _debug_json(await _call_sync(_svc(static_dir).boats_payload, bbox, _parse_visible_bbox()), endpoint="/gfs/api/boats", family="provider_debug", replacement="/gfs/api/cache/refresh?layers=boater")
        except Exception as exc:
            log.exception("/gfs/api/boats failed")
            shell = _endpoint_shell("/gfs/api/boats", exc, bbox)
            shell.update({"boats": [], "items": [], "count": 0})
            return _json(shell)

    @bp.get("/gfs/api/debug/provider/lightning")
    @bp.get("/gfs/api/lightning")
    async def lightning():
        try:
            minutes = int(request.args.get("minutes") or request.args.get("window_minutes") or 20)
        except Exception:
            minutes = 20
        bbox = _parse_bbox()
        try:
            return _debug_json(await _call_sync(_svc(static_dir).lightning_payload, bbox, _parse_visible_bbox(), minutes), endpoint="/gfs/api/lightning", family="provider_debug", replacement="/gfs/api/cache/refresh?layers=lightning")
        except Exception as exc:
            log.exception("/gfs/api/lightning failed")
            shell = _endpoint_shell("/gfs/api/lightning", exc, bbox)
            shell.update({"schema": "lftr_lightning_v1", "flashes": [], "items": [], "regions": [], "fallback_used": False})
            return _json(shell)

    @bp.get("/gfs/api/scene")
    async def scene():
        return jsonify(await _call_sync(_svc(static_dir).get_scene_payload, _parse_bbox()))

    @bp.get("/gfs/api/hazards")
    async def hazards():
        return jsonify(await _call_sync(_svc(static_dir).hazards_payload))

    @bp.get("/gfs/api/diagnostics")
    async def diagnostics():
        return jsonify(await _call_sync(_svc(static_dir).diagnostics_payload))

    @bp.get("/gfs/api/fish")
    async def fish():
        return jsonify(await _call_sync(_svc(static_dir).fish_payload))

    @bp.get("/gfs/api/sources")
    @bp.get("/gfs/api/source-diagnostics")
    async def sources():
        return jsonify(await _call_sync(_svc(static_dir).source_diagnostics_payload, _parse_bbox()))

    @bp.get("/gfs/api/locations")
    async def locations():
        payload = await _call_sync(_svc(static_dir).fish_payload)
        # Compatibility: some UI callers expect `locations`, while
        # the service payload returns `items`. Keep both keys during cleanup.
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list) and not isinstance(payload.get("locations"), list):
                payload["locations"] = items
        return jsonify(payload)

    @bp.post("/gfs/api/fishai")
    async def fishai():
        body = await request.get_json(force=True, silent=True) or {}
        prompt = str(body.get("prompt") or body.get("q") or body.get("query") or "")
        selected = body.get("selected_location") if isinstance(body, dict) else None
        if not prompt.strip():
            return _json({
                "ok": False,
                "schema": "lftr_fishai_command_v1",
                "error": "prompt_required",
                "headline": "FISHAI needs a direction or search phrase",
                "reply_lines": ["Try: go to Newport pier, best leopard shark 36-42, show bait, tilt 60 heading 240."],
                "actions": [],
            }, 400)
        try:
            return _json(await _call_sync(_fishai_payload, prompt, _svc(static_dir), selected))
        except Exception as exc:
            log.exception("/gfs/api/fishai failed")
            return _json({
                "ok": False,
                "schema": "lftr_fishai_command_v1_error",
                "source": "fishai_error_caught",
                "headline": "FISHAI command parser caught an error",
                "reply_lines": [str(exc)],
                "actions": [],
            })

    @bp.get("/gfs/api/source-check")
    async def source_check():
        svc = _svc(static_dir)
        return _debug_json(await _call_sync(svc.validate_bbox_real_fields, _parse_bbox()), endpoint="/gfs/api/source-check", family="provider_debug", replacement="/gfs/api/debug/routes")

    @bp.get("/gfs/api/payload-debug")
    @bp.get("/gfs/api/provider-debug")
    async def payload_debug():
        svc = _svc(static_dir)
        bbox = _parse_bbox()
        # Do not let provider-debug default to the whole globe.  HYCOM's NCSS
        # request grid uses 0..360 longitude and full-world bboxes are slow and
        # easy to turn into invalid west==east constraints.  Use the same SoCal
        # startup bbox unless a bbox query param is supplied.
        if bbox is None:
            bbox = {"west": -126.0, "south": 29.0, "east": -114.0, "north": 39.0}
        try:
            deep = str(request.args.get("deep") or "0").lower() in {"1", "true", "yes"}
            if deep:
                payload = await _call_sync(svc.payload_debug_payload, bbox)
            else:
                payload = {
                    "ok": True,
                    "endpoint": request.path,
                    "mode": "cheap_debug_status_no_provider_fetch",
                    "bbox": bbox,
                    "cache_warm": await _call_sync(svc.cache_warm_status_payload),
                    "cache_status": await _call_sync(svc.cache_status_payload),
                    "note": "Use ?deep=1 for heavy provider checks. Default is cheap so debug cannot stall page load.",
                }
        except Exception as exc:
            log.exception("/gfs/api/provider-debug failed")
            payload = {
                "ok": False,
                "endpoint": request.path,
                "error": str(exc),
                "bbox": bbox,
                "note": "provider-debug caught an exception and returned JSON status 200 instead of HTTP 500",
            }
        return _debug_json(payload, endpoint=request.path, family="provider_debug", replacement="/gfs/api/debug/routes")

    @bp.get("/gfs/api/shark-intel")
    @bp.get("/gfs/api/debug/provider/shark-intel")
    async def shark_intel_debug():
        bbox = _parse_bbox()
        try:
            payload = await _call_sync(_svc(static_dir)._scene_cache_build_layer, "shark-intel", bbox, _parse_visible_bbox())
            return _debug_json(payload, endpoint=request.path, family="provider_debug", replacement="/gfs/api/cache/refresh?layers=shark-intel")
        except Exception as exc:
            log.exception("/gfs/api/shark-intel failed")
            return _json(_endpoint_shell(request.path, exc, bbox))

    @bp.get("/gfs/api/bait")
    @bp.get("/gfs/api/bait-advanced")
    @bp.get("/gfs/api/bait/advanced")
    async def bait():
        bbox = _parse_bbox()
        try:
            return _debug_json(await _call_sync(_svc(static_dir).bait_advanced_payload, bbox, _parse_visible_bbox()), endpoint="/gfs/api/bait-advanced", family="provider_debug", replacement="/gfs/api/cache/refresh?layers=bait")
        except Exception as exc:
            log.exception("/gfs/api/bait-advanced failed")
            shell = _endpoint_shell("/gfs/api/bait-advanced", exc, bbox)
            shell.update({"bait": {"polygons": [], "bait_score": []}, "bait_score": [], "polygons": []})
            return _json(shell)




    @bp.get("/gfs/api/inland-water")
    async def inland_water():
        bbox = _parse_bbox()
        try:
            try:
                max_tiles = int(request.args.get("max_tiles") or request.args.get("limit") or 96)
            except Exception:
                max_tiles = 96
            # Cache-first/tile-aligned mode is now the default for the visible pill.
            # The direct bbox helper remains available with tile_cache=0 for debugging.
            source = request.args.get("source") or "auto"
            geometry = request.args.get("geometry") or "vector"
            lod = request.args.get("lod") or "auto"
            scene_tier = request.args.get("scene_tier") or request.args.get("tier") or None
            tier = _scene_tier_from_bbox_or_arg(_parse_visible_bbox() or bbox, scene_tier)
            if not _inland_detail_allowed_for_tier(tier):
                geometry = "coarse"
                lod = "overview"
                scene_tier = tier
            use_tile_cache = str(request.args.get("tile_cache", request.args.get("cache", "1"))).lower() not in {"0", "false", "no", "off"}
            if use_tile_cache:
                payload = await _call_sync(_svc(static_dir).inland_water_tiles_payload, bbox, max_tiles, _parse_visible_bbox(), source=source, geometry=geometry, lod=lod, scene_tier=scene_tier)
            else:
                payload = await _call_sync(inland_water_payload, static_dir, bbox, source=source, geometry=geometry, lod=lod, scene_tier=scene_tier)

            # Clean contract:
            #   mode=read  -> cache/read only, never starts a builder
            #   /build-cache -> explicit progressive runtime tile build
            # This prevents stale hidden-builder behavior where a read request
            # silently returned source=...building and selected_tiles=0.
            try:
                read_mode = str(request.args.get("mode") or "read").lower() in {"read", "cache", "cache_read"}
                payload_count = int((payload.get("count") or 0) if isinstance(payload, dict) else 0)
                if isinstance(payload, dict):
                    payload = dict(payload)
                    tier = _scene_tier_from_bbox_or_arg(_parse_visible_bbox() or bbox, scene_tier)
                    payload["scene_tier"] = tier
                    payload["overview_only"] = not _inland_detail_allowed_for_tier(tier)
                    payload["inland_bait_render_allowed"] = _inland_detail_allowed_for_tier(tier)
                    payload["world_policy"] = "largest_lake_per_tile_outline_overview" if not _inland_detail_allowed_for_tier(tier) else "full_inland_detail"
                    payload["read_status"] = "cache_hit" if payload_count > 0 else "cache_miss"
                    payload["build_status"] = "not_requested"
                    payload["contract"] = "lftr_inland_water_runtime_tiles_v2"
                    payload["mode"] = "read_cache_only" if read_mode else "read_cache_only"
                    if payload_count <= 0:
                        payload["status"] = payload.get("status") or "cache_miss"
                        payload["message"] = payload.get("message") or "No matching Inland Waters runtime tile is ready yet. Use /gfs/api/inland-water/build-cache to queue real USGS/NHD viewport tiles."
                    payload.pop("build", None)
            except Exception as mode_exc:
                log.info("/gfs/api/inland-water read contract annotation skipped: %s", mode_exc)

            return _json(payload)
        except Exception as exc:
            log.exception("/gfs/api/inland-water failed")
            shell = _endpoint_shell("/gfs/api/inland-water", exc, bbox)
            shell.update({"polygons": [], "lines": [], "count": 0, "status": "error"})
            return _json(shell)


    @bp.get("/gfs/api/debug/inland/status")
    @bp.get("/gfs/api/inland-water/status")
    async def inland_water_status():
        bbox = _parse_bbox()
        try:
            tier = request.args.get("scene_tier") or request.args.get("tier") or None
            key = _inland_build_key(bbox, tier)
            _refresh_inland_build_jobs()
            job = _INLAND_BUILD_JOBS.get(key)
            payload = {
                "ok": True,
                "status": "ok",
                "contract": "lftr_inland_water_runtime_cache_status_v1",
                "policy": "read-only shared runtime tile cache; build-cache is the only builder entrypoint",
                "bbox": bbox,
                "build": ({**job, "deduped": True, "progressive": True} if job else None),
                "partial_draw_policy": "active LOD only; up to 24 selected tiles; one completed runtime json.gz tile plus index.json is enough to draw partial inland water",
            }
            return _debug_json(payload, endpoint="/gfs/api/inland-water/status", family="inland_debug", replacement="/gfs/api/inland-water")
        except Exception as exc:
            log.exception("/gfs/api/inland-water/status failed")
            shell = _endpoint_shell("/gfs/api/inland-water/status", exc, bbox)
            shell.update({"status": "error"})
            return _json(shell)




    @bp.post("/gfs/api/inland-water/build-cache")
    @bp.get("/gfs/api/inland-water/build-cache")
    async def inland_water_build_cache():
        bbox = _parse_visible_bbox() or _parse_bbox()
        scene_tier = request.args.get("scene_tier") or request.args.get("tier") or None
        try:
            # This only launches/dedupes a subprocess and should return
            # immediately.  Calling it directly prevents the request from being
            # delayed behind slow sea/cloud/cache executor work.
            geometry = request.args.get("geometry") or "vector"
            tier = _scene_tier_from_bbox_or_arg(bbox, scene_tier)
            if not _inland_detail_allowed_for_tier(tier):
                geometry = "coarse"
                scene_tier = tier
            job = _launch_inland_view_build(static_dir, bbox, scene_tier, geometry)
            return _json({
                "status": job.get("status") or "started",
                "running": bool(job.get("running")),
                "deduped": bool(job.get("deduped")),
                "pid": job.get("pid"),
                "bbox": job.get("bbox") or _bbox_to_query(bbox),
                "scene_tier": job.get("scene_tier") or scene_tier or "world",
                "log": job.get("log"),
                "source": job.get("source") or "real_usgs_nhd_arcgis_runtime_tiles_builder",
                "geometry": job.get("geometry") or geometry,
                "overview_only": not _inland_detail_allowed_for_tier(tier),
                "inland_bait_render_allowed": _inland_detail_allowed_for_tier(tier),
                "policy": "world/regional read route reads shared runtime cache; build-cache queues real USGS/NHD tiles. World uses coarse largest-lake outline overview + one temp label per tile; inland bait waits for regional/coastal/local/harbor.",
                "guard": job.get("dedupe_reason") or ("deduped" if job.get("deduped") else "started"),
            })
        except Exception as exc:
            log.exception("/gfs/api/inland-water/build-cache failed")
            shell = _endpoint_shell("/gfs/api/inland-water/build-cache", exc, bbox)
            shell.update({"status": "error", "running": False})
            return _json(shell)


    @bp.get("/gfs/api/debug/inland/diagnostics")
    @bp.get("/gfs/api/inland-water/diagnostics")
    async def inland_water_diagnostics():
        bbox = _parse_bbox()
        try:
            source = request.args.get("source") or "auto"
            geometry = request.args.get("geometry") or "vector"
            lod = request.args.get("lod") or "auto"
            scene_tier = request.args.get("scene_tier") or request.args.get("tier") or None
            payload = await _call_sync(inland_water_payload, static_dir, bbox, source=source, geometry=geometry, lod=lod, scene_tier=scene_tier)
            return _debug_json({
                "ok": True,
                "status": "ok",
                "bbox": payload.get("bbox"),
                "source": payload.get("source"),
                "source_path": payload.get("source_path"),
                "query": payload.get("query"),
                "geometry_quality": payload.get("geometry_quality"),
                "diagnostics": payload.get("diagnostics"),
                "polygon_count": len(payload.get("polygons") or []),
                "line_count": len(payload.get("lines") or []),
                "temperature_point_count": len(payload.get("temperature_points") or []),
                "contract": "lftr_inland_water_diagnostics_v2_honest_shoreline",
            }, endpoint="/gfs/api/inland-water/diagnostics", family="inland_debug", replacement="/gfs/api/inland-water")
        except Exception as exc:
            log.exception("/gfs/api/inland-water/diagnostics failed")
            shell = _endpoint_shell("/gfs/api/inland-water/diagnostics", exc, bbox)
            shell.update({"status": "error", "diagnostics": {}})
            return _json(shell)

    @bp.get("/gfs/api/inland-conditions")
    async def inland_conditions():
        bbox = _parse_bbox()
        try:
            if request.args.get("lat") is not None and request.args.get("lon") is not None:
                lat = float(request.args.get("lat"))
                lon = float(request.args.get("lon"))
            else:
                b = bbox or {"west": -118.6, "south": 33.4, "east": -117.4, "north": 34.2}
                lat = (float(b["south"]) + float(b["north"])) / 2.0
                lon = (float(b["west"]) + float(b["east"])) / 2.0
            live = str(request.args.get("live") or request.args.get("ncss") or "").lower() in {"1", "true", "yes", "y"}
            return _debug_json(await _call_sync(inland_conditions_payload, static_dir, _svc(static_dir), bbox, lat, lon, live), endpoint="/gfs/api/inland-conditions", family="inland_debug", replacement="/gfs/api/inland-water-temp")
        except Exception as exc:
            log.exception("/gfs/api/inland-conditions failed")
            shell = _endpoint_shell("/gfs/api/inland-conditions", exc, bbox)
            shell.update({"inland_water": False, "water_temp_est_f": None, "temperature_points": [], "status": "error"})
            return _json(shell)

    @bp.get("/gfs/api/inland-bait")
    async def inland_bait():
        bbox = _parse_bbox()
        try:
            lat_raw = request.args.get("lat")
            lon_raw = request.args.get("lon")
            lat = float(lat_raw) if lat_raw is not None else None
            lon = float(lon_raw) if lon_raw is not None else None
            live = str(request.args.get("live") or request.args.get("ncss") or "").lower() in {"1", "true", "yes", "y"}
            return _debug_json(await _call_sync(inland_bait_payload, static_dir, _svc(static_dir), bbox, lat, lon, live), endpoint="/gfs/api/inland-bait", family="inland_debug", replacement="/gfs/api/scene-cache?layers=inland_water_temp")
        except Exception as exc:
            log.exception("/gfs/api/inland-bait failed")
            shell = _endpoint_shell("/gfs/api/inland-bait", exc, bbox)
            shell.update({"targets": [], "temperature_points": [], "count": 0, "status": "error"})
            return _json(shell)

    @bp.get("/gfs/api/location/<location_id>")
    async def location_detail(location_id: str):
        return jsonify(await _call_sync(_svc(static_dir).location_payload, location_id))

    @bp.get("/gfs/api/intelligence/node/<node_id>")
    @bp.get("/gfs/api/location/<node_id>/intelligence")
    async def node_intelligence(node_id: str):
        svc = _svc(static_dir)
        payload = await _call_sync(svc.node_intelligence_payload, node_id)
        return jsonify(payload)

    @bp.get("/gfs/api/location/<location_id>/live-intel")
    async def location_live_intel(location_id: str):
        return jsonify(await _call_sync(_svc(static_dir).location_live_intel_payload, location_id))

    @bp.get("/gfs/api/location/<location_id>/environment")
    @bp.get("/gfs/api/location/<location_id>/weather")
    async def location_environment(location_id: str):
        return jsonify(await _call_sync(_svc(static_dir).location_environment_payload, location_id))

    @bp.get("/gfs/api/location/<location_id>/media")
    @bp.get("/gfs/api/location/<location_id>/videos")
    @bp.get("/gfs/api/location/<location_id>/live")
    async def location_media(location_id: str):
        return jsonify(await _call_sync(_svc(static_dir).location_media, location_id))

    @bp.post("/gfs/api/location/<location_id>/report")
    @bp.post("/gfs/api/location/<location_id>/reports")
    async def location_report(location_id: str):
        body = await request.get_json(force=True, silent=True) or {}
        text = str(body.get("report_text") or body.get("text") or "")
        return jsonify(await _call_sync(_svc(static_dir).upsert_report, location_id, text))

    @bp.post("/gfs/api/location/<location_id>/live")
    async def location_live_update(location_id: str):
        body = await request.get_json(force=True, silent=True) or {}
        active = bool(body.get("active"))
        stream_url = str(body.get("stream_url") or body.get("url") or "")
        return jsonify(await _call_sync(_svc(static_dir).upsert_live, location_id, active, stream_url))

    @bp.post("/gfs/api/location/<location_id>/upload")
    async def location_upload(location_id: str):
        files = await request.files
        f = files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "file required"}), 400
        raw = f.read()
        if asyncio.iscoroutine(raw):
            raw = await raw
        return jsonify(await _call_sync(_svc(static_dir).save_upload_video, location_id, f.filename or "upload.mp4", raw))

    @bp.get("/gfs/api/tile/<layer>/<int:z>/<int:x>/<int:y>.json")
    async def tile_json(layer: str, z: int, x: int, y: int):
        return jsonify(await _call_sync(_svc(static_dir).tile_layer_payload, layer, z, x, y))

    @bp.websocket("/ws/gfs")
    async def ws_gfs():
        ws = websocket._get_current_object()
        try:
            await ws.send_json({"type": "connected", "service": "gfs", "mode": "nonblocking_cache_first_ws"})
        except Exception:
            return
        while True:
            try:
                raw = await websocket.receive()
            except Exception:
                break
            try:
                import json as _json
                msg = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                try:
                    await ws.send_json({"type": "ignored", "reason": "bad_json"})
                except Exception:
                    break
                continue
            kind = (msg or {}).get("type") or "ping"
            if kind == "ping":
                try:
                    await ws.send_json({"type": "pong", "ts": int(asyncio.get_event_loop().time() * 1000)})
                except Exception:
                    break
            elif kind == "viewport" and isinstance((msg or {}).get("bbox"), dict):
                try:
                    bbox = (msg or {}).get("bbox")
                    reason = str((msg or {}).get("reason") or "steady")
                    svc = _svc(static_dir)
                    ack = await _call_sync(svc.note_viewport_priority, bbox, reason)
                    # Websocket viewport notes must stay cheap.  Do not launch large
                    # live tile warm jobs from the websocket path; the HTTP cache refresh
                    # scheduler handles that and dedupes already-running jobs.
                    await ws.send_json({"type": "viewport_ack", "cache_warm_started": False, **ack})
                except Exception as exc:
                    log.exception("gfs websocket viewport priority failed")
                    try:
                        await ws.send_json({"type": "error", "message": str(exc)})
                    except Exception:
                        break
            elif kind in {"snapshot", "get_snapshot", "subscribe"}:
                try:
                    payload = await _call_sync(_svc(static_dir).ws_snapshot)
                    if asyncio.iscoroutine(payload):
                        payload = await payload
                    await ws.send_json(payload)
                except Exception as exc:
                    log.exception("gfs websocket snapshot failed")
                    try:
                        await ws.send_json({"type": "error", "message": str(exc)})
                    except Exception:
                        break
            else:
                try:
                    await ws.send_json({"type": "ignored", "reason": "unknown_type", "kind": kind})
                except Exception:
                    break

    @bp.websocket("/gfs/ws")
    @bp.websocket("/gfs/ws/")
    async def ws_gfs_alias():
        await ws_gfs()

    return bp

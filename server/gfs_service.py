from __future__ import annotations

import sitecustomize  # noqa: F401 - LFTR runtime compatibility guards

ALLOW_SYNTHETIC_FALLBACK = False
import base64
import gc
import csv
import hashlib
import json
import math
import os
import random
import re
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from typing import Any, Dict, List, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover - fallback mode
    np = None

try:
    import xarray as xr
except Exception:  # pragma: no cover - fallback mode
    xr = None

try:
    from diskcache import Cache as DiskCache
except Exception:  # pragma: no cover - fallback mode
    DiskCache = None

try:
    import pygrib
except Exception:  # pragma: no cover - optional fallback decoder
    pygrib = None

from werkzeug.utils import secure_filename

from server.gfs_state import GFSState
from server.gfs.intelligence import build_location_profile
from server.gfs.shark_intel import shark_intel_payload
from server.gfs.models import BBox
from server.gfs.cache_policy import janitor_scene_rows
from server.gfs.tile_contract import DEFAULT_VIEWPORT_GRID, provider_jobs, provider_tile_plan, split_viewport_tiles
from server.gfs.providers.hycom import HycomProvider
try:
    from server.gfs.providers.coastwatch import CoastwatchProvider
except Exception:  # optional live chlorophyll provider
    CoastwatchProvider = None
try:
    from server.gfs.derive.bait import derive_bait_payload
except Exception:  # optional bait grid solver
    derive_bait_payload = None

try:
    from server.gfs.inland_water import inland_water_payload as build_inland_water_payload
except Exception:  # optional inland water layer
    build_inland_water_payload = None


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = STATIC_DIR / "data"
FISH_CSV = DATA_DIR / "fishloclist.csv"


_TRANSPARENT_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/w8AAgMBgB5o2a4AAAAASUVORK5CYII="
)
_ALLOWED_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

DEFAULT_HTTP_TIMEOUT = float(os.getenv("GFS_HTTP_TIMEOUT_SECONDS", "6") or "6")
ENV_CACHE_TTL_SECONDS = 900
NWS_CACHE_TTL_SECONDS = 1800
TIDE_CACHE_TTL_SECONDS = 1200
SST_CACHE_TTL_SECONDS = 10_800
NWS_API_BASE = "https://api.weather.gov"
NOAA_TIDES_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
NOAA_COOPS_MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
DEFAULT_UA = "LFTR-GFS/1.0 (+https://lftr.biz)"
DEFAULT_WORLD_ENV_MARKER = {"lat": 34.2, "lon": -120.0}

NOMADS_FILTER_BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
DEFAULT_GFS_TIMEOUT_SECONDS = int(float(os.getenv("GFS_TIMEOUT_SECONDS", "22") or "22"))
DEFAULT_GFS_RETRIES = 3
DEFAULT_GFS_CACHE_DIR = BASE_DIR / ".cache" / "gfs_nomads"
DEFAULT_SCENE_TILE_CACHE_DIR = BASE_DIR / ".cache" / "scene_tiles"
DEFAULT_CFGRIB_INDEX_DIR = BASE_DIR / ".cache" / "cfgrib"
DEFAULT_FRAME_CACHE_DIR = BASE_DIR / ".cache" / "frames"
DEFAULT_SPLIT_CACHE_DIR = BASE_DIR / ".cache" / "split_payloads"
DEFAULT_OCEAN_CACHE_DIR = BASE_DIR / ".cache" / "ocean"
SCENE_CACHE_REFRESH_INTERVAL_MS = int(os.getenv("GFS_SCENE_CACHE_REFRESH_MS", "120000") or "120000")
SCENE_CACHE_MAX_MEMORY_ROWS = int(os.getenv("GFS_SCENE_CACHE_MAX_MEMORY_ROWS", "96") or "96")
INLAND_VIEW_TILE_LIMIT = int(os.getenv("GFS_INLAND_VIEW_TILE_LIMIT", "96") or "96")
SCENE_CACHE_MAX_AGE_SECONDS = int(os.getenv("GFS_SCENE_CACHE_MAX_AGE_SECONDS", "86400") or "86400")
SCENE_CACHE_STALE_SECONDS = int(os.getenv("GFS_SCENE_CACHE_STALE_SECONDS", "600") or "600")
SCENE_CACHE_FIRST_PAINT_MAX_AGE_SECONDS = int(os.getenv("GFS_SCENE_CACHE_FIRST_PAINT_MAX_AGE_SECONDS", "86400") or "86400")
SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS = int(os.getenv("GFS_SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS", "240") or "240")
STATIC_SCENE_CACHE_LAYERS = {"locations", "inland-water"}
LIVE_SCENE_CACHE_DEFAULT_LAYERS = ["clouds", "rain", "lightning", "boater", "bait", "shark-intel"]
BOOT_SCENE_CACHE_DEFAULT_LAYERS = ["locations", "clouds", "rain", "lightning", "boater", "bait", "shark-intel"]
DEFAULT_GFS_CACHE_TTL_SECONDS = int(os.getenv("GFS_CACHE_TTL_SECONDS", str(60 * 40)) or str(60 * 40))
DEFAULT_GFS_CYCLE_AVAILABILITY_DELAY_MINUTES = 290
INGEST_FALLBACK_CYCLE_DEPTH = 4
INGEST_PREFERRED_FORECAST_HOUR = 0
INGEST_CACHE_MIN_BYTES = 2000
INGEST_MIN_INTERVAL_SECONDS = 600
WEATHER_REFRESH_TTL_SECONDS = int(os.getenv("GFS_WEATHER_REFRESH_TTL_SECONDS", "600") or "600")
# Clouds/rain are live-weather display layers.  Do not treat an old GFS
# payload as "fresh" for clouds; use retained last-known-good only as a
# first-paint bridge while a fresh GFS attempt is scheduled.
CLOUD_LIVE_DEDUPE_SECONDS = int(os.getenv("GFS_CLOUD_LIVE_DEDUPE_SECONDS", "300") or "300")
CLOUD_RETAINED_MAX_AGE_SECONDS = int(os.getenv("GFS_CLOUD_RETAINED_MAX_AGE_SECONDS", "86400") or "86400")
GFS_CLOUDS_FORCE_LIVE_FETCH = os.getenv("GFS_CLOUDS_FORCE_LIVE_FETCH", "false").strip().lower() in {"1", "true", "yes", "on"}
SCENE_REFRESH_TTL_SECONDS = int(os.getenv("GFS_FRAME_CACHE_TTL_SECONDS", "120") or "120")
SCENE_DOWNSAMPLE_STRIDE = 3
SCALAR_DOWNSAMPLE_STRIDE = 6
PREFER_LIVE_REAL_DATA = os.getenv("PREFER_LIVE_REAL_DATA", "true").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_STALE_CACHE_BLEND = os.getenv("ALLOW_STALE_CACHE_BLEND", "true").strip().lower() in {"1", "true", "yes", "on"}
REQUIRE_MATCHING_GRID_FOR_CACHE = os.getenv("REQUIRE_MATCHING_GRID_FOR_CACHE", "true").strip().lower() in {"1", "true", "yes", "on"}
ENABLE_LIVE_OCEAN_NCSS = os.getenv("GFS_ENABLE_LIVE_OCEAN_NCSS", "true").strip().lower() in {"1", "true", "yes", "on"}
# Live-data quality policy: default is strict. The app may return cache-warming
# shells or explicit provider_failed/provider_empty payloads, but must not
# silently draw marker/proxy/mock ocean, bait, boat, cloud, or rain data as if
# it came from NCSS/ERDDAP. Set GFS_ALLOW_PROXY_FALLBACK=true only for demos.
GFS_ALLOW_PROXY_FALLBACK = os.getenv("GFS_ALLOW_PROXY_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
GFS_STRICT_LIVE_PAYLOADS = os.getenv("GFS_STRICT_LIVE_PAYLOADS", "true").strip().lower() in {"1", "true", "yes", "on"}
OCEAN_NCSS_TARGET_CELLS = int(os.getenv("GFS_OCEAN_NCSS_TARGET_CELLS", "18"))
OCEAN_NCSS_MAX_BOATS = int(os.getenv("GFS_OCEAN_NCSS_MAX_BOATS", "18"))
OCEAN_NCSS_RENDER_BOATS = int(os.getenv("GFS_OCEAN_NCSS_RENDER_BOATS", "18"))
OCEAN_POINTS_MAX = int(os.getenv("GFS_OCEAN_POINTS_MAX", "420"))
BAIT_ADVANCED_CACHE_TTL_SECONDS = int(os.getenv("GFS_BAIT_ADVANCED_CACHE_TTL_SECONDS", "1200") or "1200")
BAIT_ADVANCED_STALE_SECONDS = int(os.getenv("GFS_BAIT_ADVANCED_STALE_SECONDS", "3600") or "3600")
BAIT_ADVANCED_REFRESH_MIN_GAP_SECONDS = int(os.getenv("GFS_BAIT_ADVANCED_REFRESH_MIN_GAP_SECONDS", "180") or "180")
BAIT_ADVANCED_TILE_DEG_WORLD = float(os.getenv("GFS_BAIT_ADVANCED_TILE_DEG_WORLD", "4.0") or "4.0")
BAIT_ADVANCED_TILE_DEG_REGIONAL = float(os.getenv("GFS_BAIT_ADVANCED_TILE_DEG_REGIONAL", "2.0") or "2.0")
BAIT_ADVANCED_TILE_DEG_LOCAL = float(os.getenv("GFS_BAIT_ADVANCED_TILE_DEG_LOCAL", "1.0") or "1.0")
BAIT_ADVANCED_USE_CACHED_GFS_WEATHER = os.getenv("GFS_BAIT_ADVANCED_USE_CACHED_GFS_WEATHER", "true").strip().lower() in {"1", "true", "yes", "on"}
BAIT_ADVANCED_ALLOW_LIVE_GFS_WEATHER = os.getenv("GFS_BAIT_ADVANCED_ALLOW_LIVE_GFS_WEATHER", "false").strip().lower() in {"1", "true", "yes", "on"}


SURFACE_VARIABLES = ["PRATE", "APCP", "TCDC", "CAPE", "CIN", "PRMSL", "TMP", "RH", "GUST", "UGRD", "VGRD"]
AGL_VARIABLES = ["TMP", "RH", "UGRD", "VGRD", "TCDC"]
ISOBARIC_VARIABLES = ["RH", "TMP", "HGT", "UGRD", "VGRD"]
DEFAULT_REQUIRED_VARIABLES = sorted(set(SURFACE_VARIABLES + AGL_VARIABLES + ISOBARIC_VARIABLES))
DEFAULT_REQUIRED_LEVELS = ["surface", "2_m_above_ground", "10_m_above_ground", "1000_mb", "925_mb", "850_mb", "700_mb", "500_mb", "300_mb"]

PRECIP_BUCKETS_MM_HR = [0.15, 0.7, 2.5, 8.0, 18.0, 40.0]
SPATIAL_GRID_DEG = 8.0
MAX_TILE_DIAGNOSTICS = 600

log = logging.getLogger("server.gfs")


def safe_float(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def floor_to_cycle(dt_utc: datetime) -> int:
    """Return GFS cycle hour bucket (00/06/12/18)."""
    h = dt_utc.hour
    return (h // 6) * 6


def candidate_cycles(dt_utc: datetime) -> list[tuple[str, int]]:
    """Return cycle candidates from newest to older with availability delay."""
    delayed = dt_utc - timedelta(minutes=DEFAULT_GFS_CYCLE_AVAILABILITY_DELAY_MINUTES)
    cur = delayed.replace(minute=0, second=0, microsecond=0)
    cur = cur.replace(hour=floor_to_cycle(cur))
    out: list[tuple[str, int]] = []
    for i in range(0, 4):
        cdt = cur - timedelta(hours=6 * i)
        out.append((cdt.strftime("%Y%m%d"), cdt.hour))
    return out


def nearest_forecast_hour(valid_dt_utc: datetime, cycle_dt_utc: datetime) -> int:
    """Return nearest whole forecast hour from cycle to valid time."""
    return int(round((valid_dt_utc - cycle_dt_utc).total_seconds() / 3600.0))


def clamp_forecast_hour(fhr: int, min_hour: int = 0, max_hour: int = 384) -> int:
    """Clamp forecast hour to legal GFS range."""
    return max(min_hour, min(max_hour, int(fhr)))


class FetchResult:
    def __init__(self, ok: bool, path: Path | None = None, cycle: str = "", forecast_hour: int = 0, valid_time: str = "", error: str = "", url: str = "") -> None:
        self.ok = ok
        self.path = path
        self.cycle = cycle
        self.forecast_hour = forecast_hour
        self.valid_time = valid_time
        self.error = error
        self.url = url


class GFSNomadsClient:
    """NOAA NOMADS GFS 0.25 subset client with on-disk cache."""

    def __init__(self, cache_dir: Path, timeout_seconds: int = DEFAULT_GFS_TIMEOUT_SECONDS, retries: int = DEFAULT_GFS_RETRIES):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": DEFAULT_UA})

    def build_file_name(self, cycle_hour: int, forecast_hour: int) -> str:
        return f"gfs.t{int(cycle_hour):02d}z.pgrb2.0p25.f{int(forecast_hour):03d}"

    def build_dir(self, date_str: str, cycle_hour: int) -> str:
        return f"/gfs.{date_str}/{int(cycle_hour):02d}/atmos"

    def normalize_bbox_for_nomads(self, bbox: dict[str, float]) -> dict[str, float]:
        """Return a bounded NOMADS filter box instead of silently fetching the globe.

        Earlier development used the whole GFS domain for safety, but that makes
        /gfs/api/clouds and /api/gfs/scene slow/fragile.  Use the requested
        scene/fetch bbox, padded just enough for cloud/rain advection and tilt.
        """
        try:
            west = float((bbox or {}).get("west", -180.0))
            east = float((bbox or {}).get("east", 180.0))
            south = float((bbox or {}).get("south", -80.0))
            north = float((bbox or {}).get("north", 80.0))
        except Exception:
            return {"leftlon": -180.0, "rightlon": 180.0, "toplat": 80.0, "bottomlat": -80.0}
        if east < west:
            east += 360.0
        span_lon = max(0.25, min(80.0, east - west))
        span_lat = max(0.25, min(50.0, north - south))
        pad_lon = min(5.0, max(0.5, span_lon * 0.10))
        pad_lat = min(4.0, max(0.5, span_lat * 0.10))
        left = max(-180.0, west - pad_lon)
        right = min(180.0, east + pad_lon)
        bottom = max(-80.0, south - pad_lat)
        top = min(80.0, north + pad_lat)
        if right <= left:
            left, right = -180.0, 180.0
        if top <= bottom:
            bottom, top = -80.0, 80.0
        return {"leftlon": round(left, 4), "rightlon": round(right, 4), "toplat": round(top, 4), "bottomlat": round(bottom, 4)}

    def build_filter_url(self, date_str: str, cycle_hour: int, forecast_hour: int, bbox: dict[str, float], variables: list[str], levels: list[str]) -> str:
        from urllib.parse import urlencode

        file_name = self.build_file_name(cycle_hour, forecast_hour)
        dir_name = self.build_dir(date_str, cycle_hour)
        query: dict[str, Any] = {"file": file_name, "dir": dir_name}
        b = self.normalize_bbox_for_nomads(bbox)
        query.update(b)
        for var in variables:
            query[f"var_{var}"] = "on"
        for lvl in levels:
            query[f"lev_{lvl}"] = "on"
        return f"{NOMADS_FILTER_BASE}?{urlencode(query)}"

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.grib2"

    def fetch_subset(self, url: str, out_path: Path) -> Path:
        tmp = out_path.with_suffix(".tmp")
        for attempt in range(self.retries):
            try:
                resp = self.http.get(url, timeout=self.timeout_seconds)
                resp.raise_for_status()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                body = resp.content
                if b"<html" in body[:200].lower() or ("text/html" in ctype):
                    raise RuntimeError("nomads returned html page")
                if len(body) < 2000:
                    raise RuntimeError("nomads subset too small")
                tmp.write_bytes(body)
                tmp.replace(out_path)
                return out_path
            except Exception:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                if attempt >= self.retries - 1:
                    raise
                time.sleep(0.4 * (2 ** attempt))
        return out_path

    def fetch_latest_available_subset(self, target_dt_utc: datetime, bbox: dict[str, float], variables: list[str], levels: list[str]) -> FetchResult:
        for date_str, cycle_hour in candidate_cycles(target_dt_utc):
            cycle_dt = datetime.strptime(f"{date_str}{cycle_hour:02d}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
            fhr = clamp_forecast_hour(nearest_forecast_hour(target_dt_utc, cycle_dt))
            url = self.build_filter_url(date_str, cycle_hour, fhr, bbox, variables, levels)
            cache_key = f"{date_str}:{cycle_hour}:{fhr}:{json.dumps(self.normalize_bbox_for_nomads(bbox), sort_keys=True)}:{','.join(sorted(variables))}:{','.join(sorted(levels))}"
            path = self._cache_path(cache_key)
            if path.exists() and (time.time() - path.stat().st_mtime) < DEFAULT_GFS_CACHE_TTL_SECONDS:
                return FetchResult(True, path=path, cycle=f"{date_str}{cycle_hour:02d}", forecast_hour=fhr, valid_time=(cycle_dt + timedelta(hours=fhr)).isoformat(), url=url)
            try:
                self.fetch_subset(url, path)
                return FetchResult(True, path=path, cycle=f"{date_str}{cycle_hour:02d}", forecast_hour=fhr, valid_time=(cycle_dt + timedelta(hours=fhr)).isoformat(), url=url)
            except Exception as exc:
                continue
        # If NOMADS temporarily has no matching subset, keep the visual system alive
        # with the newest usable GRIB2 file in our local cache.  The payload will be
        # labeled stale by the ingest path, but it prevents cloud/rain layers from
        # clearing or forcing repeated failed downloads.
        try:
            candidates = [p for p in self.cache_dir.glob("*.grib2") if p.is_file() and p.stat().st_size >= INGEST_CACHE_MIN_BYTES]
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                path = candidates[0]
                age = int(time.time() - path.stat().st_mtime)
                return FetchResult(True, path=path, cycle="stale_cache", forecast_hour=0, valid_time=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(), url=f"local-cache://{path.name}?age_sec={age}")
        except Exception:
            pass
        return FetchResult(False, error="no available nomads subset")


CLOUD_REGIMES = {
    "marine_stratocumulus": {
        "deck_bias": 0.92,
        "tower_bias": 0.10,
        "wispy_bias": 0.08,
        "base_altitude_m": 700.0,
        "depth_m": 1400.0,
        "lateral_scale_km": 160.0,
        "underside_darkness": 0.22,
        "fringe_softness": 0.82,
    },
    "cumulus_field": {
        "deck_bias": 0.38,
        "tower_bias": 0.42,
        "wispy_bias": 0.12,
        "base_altitude_m": 1100.0,
        "depth_m": 2200.0,
        "lateral_scale_km": 95.0,
        "underside_darkness": 0.28,
        "fringe_softness": 0.58,
    },
    "frontal_shield": {
        "deck_bias": 0.78,
        "tower_bias": 0.24,
        "wispy_bias": 0.26,
        "base_altitude_m": 1200.0,
        "depth_m": 4200.0,
        "lateral_scale_km": 210.0,
        "underside_darkness": 0.34,
        "fringe_softness": 0.70,
    },
    "deep_convection": {
        "deck_bias": 0.18,
        "tower_bias": 0.96,
        "wispy_bias": 0.10,
        "base_altitude_m": 900.0,
        "depth_m": 9200.0,
        "lateral_scale_km": 120.0,
        "underside_darkness": 0.58,
        "fringe_softness": 0.36,
    },
    "cirrus_sheet": {
        "deck_bias": 0.20,
        "tower_bias": 0.06,
        "wispy_bias": 0.94,
        "base_altitude_m": 7600.0,
        "depth_m": 2400.0,
        "lateral_scale_km": 240.0,
        "underside_darkness": 0.14,
        "fringe_softness": 0.90,
    },
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def stable_hash_u32(text: str) -> int:
    h = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:8]
    return int(h, 16)


def stable_unit_float(key: str) -> float:
    return (stable_hash_u32(key) % 1_000_000) / 1_000_000.0


def stable_range(key: str, lo: float, hi: float) -> float:
    return lo + (hi - lo) * stable_unit_float(key)


def close_ring(points: list[dict]) -> list[dict]:
    if not points:
        return []
    out = [dict(p) for p in points]
    if out[0].get("lat") != out[-1].get("lat") or out[0].get("lng") != out[-1].get("lng"):
        out.append({"lat": out[0]["lat"], "lng": out[0]["lng"]})
    return out


def ring_centroid(points: list[dict]) -> tuple[float, float]:
    if not points:
        return 0.0, 0.0
    core = points[:-1] if len(points) > 2 and points[0] == points[-1] else points
    lat = sum(float(p["lat"]) for p in core) / max(1, len(core))
    lon = sum(float(p["lng"]) for p in core) / max(1, len(core))
    return lat, lon


def scale_ring(points: list[dict], scale_lat: float, scale_lon: float, center_lat: float, center_lon: float) -> list[dict]:
    out = []
    for p in points:
        out.append(
            {
                "lat": center_lat + (float(p["lat"]) - center_lat) * scale_lat,
                "lng": center_lon + (float(p["lng"]) - center_lon) * scale_lon,
            }
        )
    return close_ring(out)


def offset_ring(points: list[dict], dlat: float, dlon: float) -> list[dict]:
    out = [{"lat": float(p["lat"]) + dlat, "lng": float(p["lng"]) + dlon} for p in points]
    return close_ring(out)


def jitter_ring(points: list[dict], key_base: str, lat_jitter: float, lon_jitter: float) -> list[dict]:
    out = []
    for i, p in enumerate(points):
        out.append(
            {
                "lat": float(p["lat"]) + stable_range(f"{key_base}:jlat:{i}", -lat_jitter, lat_jitter),
                "lng": float(p["lng"]) + stable_range(f"{key_base}:jlon:{i}", -lon_jitter, lon_jitter),
            }
        )
    return close_ring(out)


def km_to_lat_deg(km: float) -> float:
    return km / 110.574


def km_to_lon_deg(km: float, lat_deg: float) -> float:
    return km / max(10.0, 111.320 * math.cos(math.radians(lat_deg)))


def build_irregular_ellipse_ring(center_lat: float, center_lon: float, radius_lat_deg: float, radius_lon_deg: float, points_count: int, key_base: str, irregularity: float = 0.18) -> list[dict]:
    pts: list[dict] = []
    count = max(8, points_count)
    for i in range(count):
        t = (2 * math.pi * i) / count
        wobble = 1 + stable_range(f"{key_base}:w:{i}", -irregularity, irregularity)
        lat = center_lat + math.sin(t) * radius_lat_deg * wobble
        lng = center_lon + math.cos(t) * radius_lon_deg * wobble
        pts.append({"lat": round(lat, 6), "lng": round(lng, 6)})
    return close_ring(pts)


def build_hole_rings_for_cloud(center_lat: float, center_lon: float, outer_ring: list[dict], hole_count: int, key_base: str) -> list[list[dict]]:
    holes: list[list[dict]] = []
    c_lat, c_lon = ring_centroid(outer_ring)
    for i in range(max(0, hole_count)):
        s_lat = stable_range(f"{key_base}:hs:{i}:lat", 0.18, 0.42)
        s_lon = stable_range(f"{key_base}:hs:{i}:lon", 0.18, 0.42)
        ring = scale_ring(outer_ring, s_lat, s_lon, c_lat, c_lon)
        ring = offset_ring(ring, stable_range(f"{key_base}:ho:{i}:lat", -0.05, 0.05), stable_range(f"{key_base}:ho:{i}:lon", -0.05, 0.05))
        holes.append(ring)
    return holes


def build_subcell_layout(regime: str, density: float, organization: float, key_base: str) -> list[dict]:
    base = 3 + int(density * 4)
    if regime == "deep_convection":
        base += 3
    elif regime in {"marine_stratocumulus", "frontal_shield"}:
        base += 2
    count = max(3, min(10, base))
    out: list[dict] = []
    for i in range(count):
        role = "fringe"
        if i == 0:
            role = "core"
        if regime == "deep_convection" and i in {0, 1}:
            role = "tower" if i == 1 else "core"
        elif regime == "cirrus_sheet":
            role = "wispy"
        elif regime == "marine_stratocumulus" and i > count - 3:
            role = "deck"
        out.append(
            {
                "id": f"sc-{i}",
                "dx": round(stable_range(f"{key_base}:dx:{i}", -0.38, 0.38), 4),
                "dy": round(stable_range(f"{key_base}:dy:{i}", -0.38, 0.38), 4),
                "weight": round(_clamp(stable_range(f"{key_base}:w:{i}", 0.45, 1.0) * (0.6 + organization * 0.4), 0.0, 1.0), 4),
                "role": role,
            }
        )
    return out


def rgba_string(r: int, g: int, b: int, a: float) -> str:
    return f"rgba({int(_clamp(r,0,255))},{int(_clamp(g,0,255))},{int(_clamp(b,0,255))},{_clamp(a,0,1):.3f})"


def build_band_footprints(tile_id: str, regime: str, band: str, lat: float, lon: float, lateral_scale_km: float, organization: float, fringe_softness: float, wind_shear: float, anvil_dir_deg: float, anvil_spread_km: float, density: float, coverage: float, key_base: str) -> dict:
    lat_deg = km_to_lat_deg(max(8.0, lateral_scale_km * (0.42 + coverage * 0.8)))
    lon_deg = km_to_lon_deg(max(10.0, lateral_scale_km * (0.52 + coverage * 0.9)), lat)
    points_count = int(_clamp(12 + density * 10 + (1 - organization) * 4, 10, 24))

    footprints = []
    holes = []
    base_ring = build_irregular_ellipse_ring(lat, lon, lat_deg, lon_deg, points_count, f"{key_base}:base", irregularity=0.12 + fringe_softness * 0.18)
    base_ring = jitter_ring(base_ring, f"{key_base}:basejit", lat_deg * 0.08, lon_deg * 0.08)
    footprints.append({"role": "deck" if regime in {"marine_stratocumulus", "frontal_shield"} else "core", "points": base_ring})

    if regime == "deep_convection":
        core = scale_ring(base_ring, 0.45 + organization * 0.2, 0.45 + organization * 0.2, lat, lon)
        footprints.append({"role": "core", "points": core})
        tower = scale_ring(base_ring, 0.28 + organization * 0.16, 0.28 + organization * 0.16, lat, lon)
        footprints.append({"role": "tower", "points": tower})
    elif regime == "cirrus_sheet":
        smear = scale_ring(base_ring, 0.8, 1.35 + wind_shear * 0.5, lat, lon)
        smear = offset_ring(smear, math.sin(math.radians(anvil_dir_deg)) * 0.04, math.cos(math.radians(anvil_dir_deg)) * 0.06)
        footprints.append({"role": "wispy", "points": smear})
    else:
        fringe = scale_ring(base_ring, 1.12 + fringe_softness * 0.18, 1.12 + fringe_softness * 0.18, lat, lon)
        footprints.append({"role": "fringe", "points": fringe})

    if regime in {"marine_stratocumulus", "frontal_shield"}:
        hc = int(_clamp(1 + fringe_softness * 2, 0, 3))
        hole_rings = build_hole_rings_for_cloud(lat, lon, base_ring, hc, f"{key_base}:holes")
        for i, hr in enumerate(hole_rings):
            holes.append({"role": "deck_hole", "points": hr, "id": f"h-{i}"})

    if regime == "deep_convection" and anvil_spread_km > 15:
        anvil_lat = km_to_lat_deg(anvil_spread_km * 0.28)
        anvil_lon = km_to_lon_deg(anvil_spread_km * 0.54, lat)
        anvil = build_irregular_ellipse_ring(lat, lon, anvil_lat, anvil_lon, max(14, points_count), f"{key_base}:anvil", irregularity=0.24)
        footprints.append({"role": "anvil", "points": anvil})

    return {"footprints": footprints, "holes": holes}


def build_band_shells(regime: str, band: str, base_altitude_m: float, top_altitude_m: float, density: float, coverage: float, organization: float, key_base: str) -> list[dict]:
    _ = key_base
    depth = max(100.0, top_altitude_m - base_altitude_m)
    alpha = _clamp(0.12 + density * 0.26 + coverage * 0.18, 0.08, 0.62)
    z = 10 if band == "low" else 20 if band == "mid" else 30
    shells: list[dict] = []
    shells.append(
        {
            "role": "deck" if regime in {"marine_stratocumulus", "frontal_shield"} else "core",
            "footprint_ref": 0,
            "hole_refs": [0] if regime in {"marine_stratocumulus", "frontal_shield"} else [],
            "base_m": round(base_altitude_m, 1),
            "top_m": round(base_altitude_m + depth * (0.5 + organization * 0.35), 1),
            "fill": rgba_string(232, 238, 245, alpha),
            "stroke": rgba_string(245, 248, 252, alpha * 0.45),
            "stroke_width": 0.6,
            "extruded": regime != "cirrus_sheet",
            "z_index": z,
        }
    )
    if regime == "deep_convection":
        shells.append({"role": "tower", "footprint_ref": 2 if band != "high" else 1, "hole_refs": [], "base_m": round(base_altitude_m + depth * 0.25, 1), "top_m": round(top_altitude_m, 1), "fill": rgba_string(245, 247, 250, _clamp(alpha + 0.08, 0, 0.75)), "stroke": rgba_string(252, 253, 255, 0.22), "stroke_width": 0.7, "extruded": True, "z_index": z + 2})
        if band == "high":
            shells.append({"role": "anvil", "footprint_ref": min(3, 2), "hole_refs": [], "base_m": round(base_altitude_m + depth * 0.62, 1), "top_m": round(top_altitude_m, 1), "fill": rgba_string(238, 244, 252, _clamp(alpha * 0.8, 0.1, 0.5)), "stroke": rgba_string(246, 250, 255, 0.18), "stroke_width": 0.5, "extruded": True, "z_index": z + 3})
    elif regime == "cirrus_sheet":
        shells.append({"role": "wispy", "footprint_ref": 1, "hole_refs": [], "base_m": round(base_altitude_m + depth * 0.55, 1), "top_m": round(top_altitude_m, 1), "fill": rgba_string(225, 236, 250, _clamp(alpha * 0.65, 0.08, 0.38)), "stroke": rgba_string(239, 246, 255, 0.12), "stroke_width": 0.4, "extruded": False, "z_index": z + 1})
    else:
        shells.append({"role": "fringe", "footprint_ref": 1, "hole_refs": [], "base_m": round(base_altitude_m + depth * 0.18, 1), "top_m": round(base_altitude_m + depth * 0.72, 1), "fill": rgba_string(236, 242, 248, _clamp(alpha * 0.78, 0.08, 0.48)), "stroke": rgba_string(246, 250, 255, 0.14), "stroke_width": 0.5, "extruded": True, "z_index": z + 1})
    return shells


def classify_cloud_regime(tile: dict) -> str:
    low = float(tile.get("low_density") or 0)
    mid = float(tile.get("mid_density") or 0)
    high = float(tile.get("high_density") or 0)
    precip = float(tile.get("precipitation_factor") or 0)
    convection = float(tile.get("convection_factor") or 0)
    lat = float(((tile.get("bounds") or {}).get("lat_center") or 0))
    if convection > 0.68 and precip > 0.48:
        return "deep_convection"
    if high > 0.62 and low < 0.35 and precip < 0.38:
        return "cirrus_sheet"
    if low > 0.64 and mid < 0.5 and abs(lat) <= 44:
        return "marine_stratocumulus"
    if (low + mid + high) / 3 > 0.5 and mid > 0.46:
        return "frontal_shield"
    return "cumulus_field"


def compute_cloud_appearance(tile: dict, regime: str) -> dict:
    low = float(tile.get("low_density") or 0)
    mid = float(tile.get("mid_density") or 0)
    high = float(tile.get("high_density") or 0)
    precip = float(tile.get("precipitation_factor") or 0)
    convection = float(tile.get("convection_factor") or 0)
    wind = tile.get("wind") or {}
    u_low = float(((wind.get("low") or {}).get("u") or 0))
    v_low = float(((wind.get("low") or {}).get("v") or 0))
    u_high = float(((wind.get("high") or {}).get("u") or 0))
    v_high = float(((wind.get("high") or {}).get("v") or 0))
    wind_shear = _clamp(math.hypot(u_high - u_low, v_high - v_low) / 35.0, 0.0, 1.0)
    coverage = _clamp(low * 0.4 + mid * 0.35 + high * 0.25 + precip * 0.12, 0.0, 1.0)
    cfg = CLOUD_REGIMES.get(regime, CLOUD_REGIMES["cumulus_field"])
    anvil_dir = (math.degrees(math.atan2((u_low + u_high) / 2.0, (v_low + v_high) / 2.0)) + 360.0) % 360.0
    anvil_spread = 0.0
    if regime == "deep_convection":
        anvil_spread = _lerp(35.0, 140.0, _clamp(convection * 0.75 + wind_shear * 0.25, 0.0, 1.0))
    elif regime in {"frontal_shield", "cirrus_sheet"}:
        anvil_spread = _lerp(18.0, 95.0, _clamp(high * 0.7 + wind_shear * 0.3, 0.0, 1.0))
    organization = _clamp(0.24 + coverage * 0.38 + precip * 0.16 + convection * 0.22, 0.0, 1.0)
    return {
        "opacity": round(_clamp(0.16 + coverage * 0.62 + convection * 0.1, 0.0, 1.0), 4),
        "underside_darkness": round(_clamp(cfg["underside_darkness"] + precip * 0.16 + convection * 0.14, 0.0, 1.0), 4),
        "fringe_softness": round(_clamp(cfg["fringe_softness"] - convection * 0.1 + high * 0.08, 0.0, 1.0), 4),
        "organization": round(organization, 4),
        "wind_shear": round(wind_shear, 4),
        "tower_bias": round(_clamp(cfg["tower_bias"] + convection * 0.2, 0.0, 1.0), 4),
        "deck_bias": round(_clamp(cfg["deck_bias"] + low * 0.08 - convection * 0.1, 0.0, 1.0), 4),
        "wispy_bias": round(_clamp(cfg["wispy_bias"] + high * 0.1, 0.0, 1.0), 4),
        "anvil_dir_deg": round(anvil_dir, 2),
        "anvil_spread_km": round(anvil_spread, 1),
    }


def compute_cloud_importance(tile: dict, regime: str) -> float:
    low = float(tile.get("low_density") or 0)
    mid = float(tile.get("mid_density") or 0)
    high = float(tile.get("high_density") or 0)
    precip = float(tile.get("precipitation_factor") or 0)
    convection = float(tile.get("convection_factor") or 0)
    depth = float(tile.get("vertical_depth_m") or 2800)
    org = float(tile.get("organization") or 0.45)
    regime_boost = 0.08 if regime == "deep_convection" else 0.04 if regime in {"frontal_shield", "cirrus_sheet"} else 0.0
    score = low * 0.2 + mid * 0.24 + high * 0.18 + precip * 0.18 + convection * 0.18 + _clamp(depth / 12000.0, 0.0, 1.0) * 0.08 + org * 0.08 + regime_boost
    return round(_clamp(score, 0.0, 1.0), 4)


def enrich_cloud_band_geometry(tile: dict, band: str, regime: str, appearance: dict) -> dict:
    density = float(((tile.get("bands") or {}).get(band, {}).get("density") or tile.get(f"{band}_density") or 0))
    old_alt = float(tile.get(f"altitude_{band}") or (9000 if band == "high" else 4200 if band == "mid" else 1200))
    base_m = float(((tile.get("bands") or {}).get(band, {}).get("base_altitude_m") or old_alt))
    top_m = float(((tile.get("bands") or {}).get(band, {}).get("top_altitude_m") or (base_m + (1800 if band != "high" else 2200))))
    thickness = max(120.0, top_m - base_m)
    cov = float(((tile.get("bands") or {}).get(band, {}).get("coverage") or _clamp(density * 0.9 + float(tile.get("precipitation_factor") or 0) * 0.18, 0, 1)))
    lat = float(((tile.get("bounds") or {}).get("lat_center") or 0.0))
    lon = float(((tile.get("bounds") or {}).get("lon_center") or 0.0))
    lateral = float(((tile.get("bands") or {}).get(band, {}).get("lateral_scale_km") or _lerp(65.0, 190.0, _clamp(cov + density * 0.4, 0, 1))))
    key_base = f"{tile.get('tile_id','tile')}:{tile.get('seed','s')}:{regime}:{band}"
    footprints = build_band_footprints(
        tile_id=str(tile.get("tile_id") or "tile"),
        regime=regime,
        band=band,
        lat=lat,
        lon=lon,
        lateral_scale_km=lateral,
        organization=float(appearance.get("organization") or 0.5),
        fringe_softness=float(appearance.get("fringe_softness") or 0.6),
        wind_shear=float(appearance.get("wind_shear") or 0.0),
        anvil_dir_deg=float(appearance.get("anvil_dir_deg") or 0.0),
        anvil_spread_km=float(appearance.get("anvil_spread_km") or 0.0),
        density=density,
        coverage=cov,
        key_base=key_base,
    )
    shells = build_band_shells(
        regime=regime,
        band=band,
        base_altitude_m=base_m,
        top_altitude_m=top_m,
        density=density,
        coverage=cov,
        organization=float(appearance.get("organization") or 0.5),
        key_base=key_base,
    )
    wind = (((tile.get("wind") or {}).get(band)) or {})
    return {
        "density": round(density, 4),
        "coverage": round(cov, 4),
        "base_altitude_m": round(base_m, 1),
        "top_altitude_m": round(top_m, 1),
        "thickness_m": round(thickness, 1),
        "lateral_scale_km": round(lateral, 1),
        "wind": {"u": round(float(wind.get("u") or 0.0), 3), "v": round(float(wind.get("v") or 0.0), 3)},
        "footprints": footprints["footprints"],
        "holes": footprints["holes"],
        "shells": shells,
    }


def enrich_cloud_tile_geometry(tile: dict) -> dict:
    out = dict(tile)
    regime = classify_cloud_regime(out)
    appearance = compute_cloud_appearance(out, regime)
    seed_text = f"{out.get('tile_id','tile')}:{int(float(out.get('updated_at') or 0)//3600000)}:{regime}"
    out["seed"] = seed_text
    out["regime"] = regime
    out.update(appearance)
    out["importance"] = compute_cloud_importance(out, regime)
    bands = {}
    for b in ("low", "mid", "high"):
        bands[b] = enrich_cloud_band_geometry(out, b, regime, appearance)
    out["bands"] = bands
    out["subcells"] = build_subcell_layout(regime, max(float(out.get("low_density") or 0), float(out.get("mid_density") or 0), float(out.get("high_density") or 0)), float(appearance.get("organization") or 0.5), f"{seed_text}:sub")

    out["base_altitude_m"] = round(min(bands["low"]["base_altitude_m"], bands["mid"]["base_altitude_m"], bands["high"]["base_altitude_m"]), 1)
    out["top_altitude_m"] = round(max(bands["low"]["top_altitude_m"], bands["mid"]["top_altitude_m"], bands["high"]["top_altitude_m"]), 1)
    out["vertical_depth_m"] = round(max(0.0, out["top_altitude_m"] - out["base_altitude_m"]), 1)

    coverage = (float(bands["low"].get("coverage") or 0.0) * 0.42 + float(bands["mid"].get("coverage") or 0.0) * 0.36 + float(bands["high"].get("coverage") or 0.0) * 0.22)
    density = (float(bands["low"].get("density") or 0.0) * 0.40 + float(bands["mid"].get("density") or 0.0) * 0.36 + float(bands["high"].get("density") or 0.0) * 0.24)
    precip_factor = float(out.get("precipitation_factor") or 0.0)
    conv_factor = float(out.get("convection_factor") or 0.0)
    mid_wind = (out.get("wind") or {}).get("mid") or {}

    out["coverage"] = round(_clamp(coverage, 0.0, 1.0), 4)
    out["density"] = round(_clamp(density, 0.0, 1.0), 4)
    out["precip_rate"] = round(max(0.0, precip_factor * 45.0), 3)
    out["storm_energy"] = round(_clamp(conv_factor * 0.68 + precip_factor * 0.32, 0.0, 1.0), 4)
    out["wind_u"] = round(float(mid_wind.get("u") or 0.0), 3)
    out["wind_v"] = round(float(mid_wind.get("v") or 0.0), 3)
    out["importance"] = round(_clamp(float(out.get("importance") or 0.0), 0.0, 1.0), 4)

    # Explicitly mark these as estimated visualization fields.
    out["estimated_cloud_base_m"] = out["base_altitude_m"]
    out["estimated_cloud_top_m"] = out["top_altitude_m"]
    out["estimated_cloud_thickness_m"] = out["vertical_depth_m"]
    out["estimated_density"] = out["density"]

    return out


from server.gfs_service_parts import (
    AtmosphereMixin,
    CoreMixin,
    LightningCacheMediaMixin,
    OceanBaitFrameMixin,
    TilesSceneMixin,
)


class GFSService(
    LightningCacheMediaMixin,
    OceanBaitFrameMixin,
    TilesSceneMixin,
    AtmosphereMixin,
    CoreMixin,
):
    def __init__(self, static_dir: str) -> None:
        self.static_dir = Path(static_dir).resolve()
        self.data_dir = self.static_dir / "data"
        self.fishvid_dir = self.static_dir / "fishvid"
        self.store_path = self.data_dir / "gfs_location_store.json"
        self.state = GFSState()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fishvid_dir.mkdir(parents=True, exist_ok=True)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": DEFAULT_UA, "Accept": "application/json"})
        self.env_cache: Dict[str, Dict[str, Any]] = {}
        self.station_cache: Dict[str, Dict[str, Any]] = {}
        self.point_forecast_cache: Dict[str, Dict[str, Any]] = {}

        def _writable_dir(path: Path, fallback_name: str) -> Path:
            p = Path(path)
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".write_probe"
                probe.write_text("ok")
                probe.unlink(missing_ok=True)
                return p
            except Exception as exc:
                fallback = Path(os.getenv("LFTR_RUNTIME_CACHE_FALLBACK", "/tmp/lftr_broadcast_cache")) / fallback_name
                fallback.mkdir(parents=True, exist_ok=True)
                log.warning("[gfs/cache] path not writable; using fallback path=%s fallback=%s err=%s", p, fallback, exc)
                return fallback

        runtime_gfs_cache_dir = _writable_dir(DEFAULT_GFS_CACHE_DIR, "gfs_nomads")
        self.gfs_client = GFSNomadsClient(runtime_gfs_cache_dir)
        self.disk_cache = DiskCache(str(_writable_dir(DEFAULT_GFS_CACHE_DIR / "payloads", "payloads"))) if DiskCache else None
        self.scene_tile_cache_dir = _writable_dir(DEFAULT_SCENE_TILE_CACHE_DIR, "scene_tiles")
        self.cfgrib_index_dir = _writable_dir(DEFAULT_CFGRIB_INDEX_DIR, "cfgrib")
        self.frame_cache_dir = _writable_dir(DEFAULT_FRAME_CACHE_DIR, "frames")
        self.split_cache_dir = _writable_dir(DEFAULT_SPLIT_CACHE_DIR, "split_payloads")
        self.ocean_cache_dir = _writable_dir(DEFAULT_OCEAN_CACHE_DIR, "ocean")
        for _cache_dir in (runtime_gfs_cache_dir, runtime_gfs_cache_dir / "payloads", self.scene_tile_cache_dir, self.cfgrib_index_dir, self.frame_cache_dir, self.split_cache_dir, self.ocean_cache_dir):
            try:
                Path(_cache_dir).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                log.warning("[gfs/cache] cache directory ensure failed path=%s err=%s", _cache_dir, exc)
        self._scene_tile_cache_lock = threading.Lock()
        self._ingest_lock = threading.Lock()
        self._scene_refresh_lock = threading.Lock()
        self._weather_refresh_lock = threading.Lock()
        self._decode_cache: Dict[str, Dict[str, Any]] = {}
        # Process-local decoded GRIB snapshot cache.  The old path re-opened
        # cfgrib/xarray groups for every clouds/frame/hazard request even when
        # the NOMADS cycle/path had not changed.  Keeping one open decoded
        # snapshot per worker lets scene-cache and sibling routes reuse the
        # same GFS cycle instead of stampeding cfgrib.
        self._gfs_snapshot_lock = threading.RLock()
        self._gfs_snapshot: Dict[str, Any] = {"path": None, "groups": None, "backend": "none", "ts": 0, "cycle": None, "forecast_hour": None}
        self._weather_payload_cache: Dict[str, Any] = {"ts": 0, "payload": None}
        self.ocean_provider = HycomProvider()
        self.bio_provider = CoastwatchProvider() if CoastwatchProvider else None
        self._recent_viewports: list[dict[str, float]] = []
        self._always_on_cache_started = False
        self._always_on_cache_thread = None
        self._always_on_cache_state: dict[str, Any] = {
            "enabled": os.getenv("GFS_ALWAYS_ON_CACHE", "true").strip().lower() in {"1", "true", "yes", "on"},
            "running": False,
            "started_at": None,
            "last_tick": None,
            "last_scheduled": None,
            "last_error": None,
            "interval_sec": int(os.getenv("GFS_ALWAYS_ON_INTERVAL_SEC", "600") or "600"),
            "fresh_target_sec": int(os.getenv("GFS_CACHE_FRESH_TARGET_SEC", "240") or "240"),
            "pad_factor": float(os.getenv("GFS_VIEWPORT_PAD_FACTOR", "1.25") or "1.25"),
        }

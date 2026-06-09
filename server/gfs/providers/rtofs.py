from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import xarray as xr

from server.gfs.models import BBox
from server.gfs.providers.adapters import build_erddap_subset_request, build_station_enrichment_request, split_antimeridian, viewport_from_bbox
from server.gfs.providers.erddap_csv import normalize_erddap_text_url, parse_erddap_grid
from server.gfs.serializers import iso_utc
from server.gfs.ocean_landmask import ocean_mask_from_grids, mask_grid, erode_ocean_mask


log = logging.getLogger("server.gfs.provider.ocean")

NOAA_TIDES_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
HYCOM_NCSS_GRID = os.getenv(
    "GFS_HYCOM_NCSS_GRID",
    # Best all-fields time series. This has been the most useful NCSS entry point
    # for the globe because it can provide surface current + SST + salinity in one
    # bbox subset instead of splitting across the older ice/u/v datasets.
    "https://ncss.hycom.org/thredds/ncss/grid/FMRC_ESPC-D-V02_all/FMRC_ESPC-D-V02_all_best.ncd",
)
HYCOM_UV3Z_NCSS_GRID = os.getenv(
    "GFS_HYCOM_UV3Z_NCSS_GRID",
    "https://ncss.hycom.org/thredds/ncss/grid/FMRC_ESPC-D-V02_uv3z/FMRC_ESPC-D-V02_uv3z_best.ncd",
)

# Default ocean policy: HYCOM NCSS or explicit empty/error. Station/constant
# current grids are useful diagnostics, but they must not be silently rendered as
# live NCSS payloads.
GFS_ALLOW_OCEAN_AUX_FALLBACK = os.getenv("GFS_ALLOW_OCEAN_AUX_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
GFS_REQUIRE_HYCOM_CURRENT_FOR_LIVE = os.getenv("GFS_REQUIRE_HYCOM_CURRENT_FOR_LIVE", "true").strip().lower() in {"1", "true", "yes", "on"}
GFS_REQUIRE_HYCOM_SST_FOR_LIVE = os.getenv("GFS_REQUIRE_HYCOM_SST_FOR_LIVE", "true").strip().lower() in {"1", "true", "yes", "on"}
HYCOM_CACHE_TTL_SECONDS = int(os.getenv("GFS_HYCOM_CACHE_TTL_SECONDS", "1800") or "1800")
HYCOM_CACHE_SCHEMA_VERSION = os.getenv("GFS_HYCOM_CACHE_SCHEMA_VERSION", "hycom_strict_depth_v1")
HYCOM_INFLIGHT_WAIT_SECONDS = float(os.getenv("GFS_HYCOM_INFLIGHT_WAIT_SECONDS", "1.2") or "1.2")
HYCOM_TILE_DEG_WORLD = float(os.getenv("GFS_HYCOM_TILE_DEG_WORLD", "4.0") or "4.0")
HYCOM_TILE_DEG_REGIONAL = float(os.getenv("GFS_HYCOM_TILE_DEG_REGIONAL", "2.0") or "2.0")
HYCOM_TILE_DEG_LOCAL = float(os.getenv("GFS_HYCOM_TILE_DEG_LOCAL", "1.0") or "1.0")
HYCOM_ENABLE_FALLBACK_ATTEMPTS = os.getenv("GFS_HYCOM_ENABLE_FALLBACK_ATTEMPTS", "true").strip().lower() in {"1", "true", "yes", "on"}
HYCOM_MAX_BBOX_AREA_DEG2 = float(os.getenv("GFS_HYCOM_MAX_BBOX_AREA_DEG2", "900") or "900")

ERDDAP_EKMAN_CSV = os.getenv("GFS_ERDDAP_EKMAN_CSV", "https://coastwatch.noaa.gov/erddap/griddap/noaacwBLENDEDNRTcurrentsDaily")
OISST_ERDDAP_CSV = normalize_erddap_text_url(os.getenv(
    "GFS_OISST_ERDDAP_CSV",
    "https://coastwatch.pfeg.noaa.gov/erddap/griddap/ncdcOisst21Agg_LonPM180.csv",
))
# HYCOM-only truth policy. Do not backfill SST from OISST or any other source:
# bait, boats, shark intel, and hover water temperature should wait when HYCOM
# SST/current is missing so we can diagnose the real upstream failure.
GFS_ALLOW_OISST_SST_FALLBACK = False

COOPS_STATIONS = (
    ("9414290", 37.806, -122.465),
    ("8720218", 21.306, -157.867),
    ("8454000", 41.355, -71.968),
    ("8771013", 29.673, -93.836),
    ("9461380", 58.301, -134.419),
)

HYCOM_DATASET_META = {
    "dataset": HYCOM_NCSS_GRID,
    "lon_convention": "0360",
    "lat_descending": False,
    "extra_dimensions": [],
    # Prefer native surface variables from HYCOM all_best NCSS.
    # They avoid vertical-coordinate ambiguity and are stable for bait/current overlays.
    "request_vars": ["sst", "ssu", "ssv", "sss", "surf_el"],
}

SST_DATASET_META = {
    "dataset": HYCOM_NCSS_GRID,
    "var_name": "sst",
    "lon_convention": "0360",
    "lat_descending": False,
    "extra_dimensions": [],
    "request_vars": ["sst", "ssu", "ssv", "sss", "surf_el"],
}

CURRENT_DATASET_META = {
    "dataset": HYCOM_NCSS_GRID,
    # ESPC-D-V02 2-D surface-current variables.  Keep this metadata congruent
    # with the primary NCSS request/parser below: ssu = eastward surface sea-water
    # velocity, ssv = northward surface sea-water velocity.  The uv3z water_u /
    # water_v dataset is only a current-only diagnostic/secondary attempt.
    "u_name": "ssu",
    "v_name": "ssv",
    "speed_name": "surface_current_speed",
    "lon_convention": "0360",
    "lat_descending": False,
    "extra_dimensions": [],
    "request_vars": ["sst", "sss", "ssu", "ssv"],
    "fallback_attempt_vars": ["water_u", "water_v"],
}


def _safe(value: Any, default: float = float("nan")) -> float:
    try:
        v = float(value)
        if math.isnan(v):
            return default
        return v
    except Exception:
        return default


def _lon360(lon: float) -> float:
    while lon < 0.0:
        lon += 360.0
    while lon >= 360.0:
        lon -= 360.0
    return lon


def _hycom_lon_slices(west: float, east: float) -> tuple[list[tuple[float, float]], dict[str, Any]]:
    """Convert app/globe longitudes (-180..180-ish) into HYCOM 0..360 slices.

    HYCOM NCSS uses 0..360 longitude.  The app and Google globe use -180..180.
    Padding/cascade requests may also briefly exceed those ranges.  This helper
    keeps that conversion in one place so provider-debug, ocean-points, boats,
    bait, and cache warming all request the same valid HYCOM bounds.

    Rules:
    - Normal California bbox -126..-114 -> 234..246, one slice.
    - Dateline bbox 170..-170 -> 170..190, one slice in 0..360.
    - Wrapped HYCOM seam 350..20 -> split into 350..359.920044 and 0..20.
    - Full/near-full globe -> one safe 0..359.920044 slice, never west==east.
    """
    raw_west = float(west)
    raw_east = float(east)
    east_unwrapped = raw_east
    while east_unwrapped <= raw_west:
        east_unwrapped += 360.0
    span = east_unwrapped - raw_west

    diag: dict[str, Any] = {
        "input_west": raw_west,
        "input_east": raw_east,
        "east_unwrapped": east_unwrapped,
        "span_deg": span,
        "lon_convention": "app_pm180_to_hycom_0360",
    }

    if (not math.isfinite(span)) or span <= 0.0 or span >= 359.0:
        slices = [(0.0, 359.920044)]
        diag.update({"mode": "full_or_invalid_span_clamped", "slices": [{"west": 0.0, "east": 359.920044}]})
        return slices, diag

    start = _lon360(raw_west)
    stop = start + span
    if stop <= 359.920044:
        slices = [(start, stop)]
    else:
        slices = [(start, 359.920044), (0.0, stop - 360.0)]

    # Drop accidental zero/negative slices, then fall back to a diagnostic full slice
    # instead of producing an invalid NCSS URL that can 500 provider-debug.
    cleaned = [(max(0.0, float(a)), min(359.920044, float(b))) for a, b in slices if float(b) - float(a) > 0.0001]
    if not cleaned:
        cleaned = [(0.0, 359.920044)]
        diag["mode"] = "empty_slice_recovered_to_full"
    else:
        diag["mode"] = "single_slice" if len(cleaned) == 1 else "split_at_hycom_zero_meridian"
    diag["slices"] = [{"west": round(a, 6), "east": round(b, 6)} for a, b in cleaned]
    return cleaned, diag


def _ncss_accept(value: str) -> str:
    value = str(value or '').strip().lower()
    # HYCOM's TDS accepts lower-case netcdf4 most consistently. Some versions
    # reject netCDF4 with a 400 even though other THREDDS servers accept it.
    return 'netcdf4' if value in {'netcdf4', 'netcdf4-classic', 'netcdf4classic'} else 'netcdf'


def _build_ncss_query(*, vars: list[str], west: float, south: float, east: float, north: float, stride: int, time_value: str = 'present', vert_coord: float | None = None) -> str:
    """Build a HYCOM NCSS request.

    Small/harbor/native stride=1 requests native resolution by omitting horizStride.
    Larger regional viewports include a modest horizStride to avoid 25s NCSS
    stalls, then BroadcastGFS performs its own LOD/downsampling after decode.
    """
    params: list[tuple[str, str]] = []
    for var_name in vars:
        params.append(('var', var_name))
    params.extend([
        ('north', f'{north:.6f}'),
        ('south', f'{south:.6f}'),
        ('west', f'{west:.6f}'),
        ('east', f'{east:.6f}'),
    ])
    stride_value = max(1, int(stride or 1))
    if stride_value > 1:
        params.append(('horizStride', str(stride_value)))
    params.extend([
        ('addLatLon', 'true'),
        ('accept', _ncss_accept('netcdf4')),
    ])
    if time_value:
        params.append(('time', time_value))
    if vert_coord is not None:
        params.append(('vertCoord', f'{float(vert_coord):.6f}'.rstrip('0').rstrip('.')))
    return urllib.parse.urlencode(params)


def _selector_expr(value: str) -> str:
    value = str(value).strip()
    if value == "last":
        value = "(last)"
    elif not (value.startswith("(") and value.endswith(")")):
        value = f"({value})"
    return f"[{value}:1:{value}]"


def _range_expr(start: float, stop: float, stride: int) -> str:
    return f"[({start}):{max(1, int(stride or 1))}:({stop})]"


class RtofsProvider:
    """Ocean forcing provider.

    Option A production pass:
    - HYCOM NCSS request for surface SST / currents / salinity in a single request
    - NOAA CO-OPS station fallback for currents if HYCOM currents are unavailable
    """

    _subset_cache: dict[str, tuple[float, dict[str, Any], datetime | None]] = {}
    _subset_inflight: dict[str, threading.Event] = {}
    _subset_lock = threading.Lock()

    def __init__(self) -> None:
        self._last_error: str | None = None
        self._last_fetch_at: datetime | None = None

    @staticmethod
    def _probable_ocean_overlap_bbox(bbox: BBox) -> bool:
        west, east, south, north = float(bbox.west), float(bbox.east), float(bbox.south), float(bbox.north)
        # Coarse west-coast ocean gate: if the bbox is clearly inland desert /
        # interior southwest, HYCOM is not appropriate.
        if east > -111.0 and west > -125.0 and south > 24.0 and north < 50.0:
            return False
        # If the box does not reach the Pacific/Gulf/Atlantic side at all, skip.
        if west > -112.0 and east < -66.0 and south > 24.0 and north < 50.0:
            return False
        return True

    @staticmethod
    def _hycom_tile_deg_for_bbox(bbox: BBox) -> float:
        width = abs(float(bbox.east) - float(bbox.west))
        height = abs(float(bbox.north) - float(bbox.south))
        span = max(width, height)
        if span <= 5.0:
            return HYCOM_TILE_DEG_LOCAL
        if span <= 14.0:
            return HYCOM_TILE_DEG_REGIONAL
        return HYCOM_TILE_DEG_WORLD

    @staticmethod
    def _quantize_bbox_for_hycom(bbox: BBox) -> BBox:
        width = abs(float(bbox.east) - float(bbox.west))
        height = abs(float(bbox.north) - float(bbox.south))
        area = max(0.01, width * height)
        # Prevent huge accidental requests. Keep the center but shrink to a
        # bounded ocean window; scene cache can compose future tile products.
        if area > HYCOM_MAX_BBOX_AREA_DEG2:
            cx = (float(bbox.west) + float(bbox.east)) * 0.5
            cy = (float(bbox.south) + float(bbox.north)) * 0.5
            half_w = min(width * 0.5, 16.0)
            half_h = min(height * 0.5, 12.0)
            bbox = BBox(west=max(-179.9, cx - half_w), south=max(-89.9, cy - half_h), east=min(179.9, cx + half_w), north=min(89.9, cy + half_h))
        step = RtofsProvider._hycom_tile_deg_for_bbox(bbox)
        import math as _math
        west = _math.floor(float(bbox.west) / step) * step
        south = _math.floor(float(bbox.south) / step) * step
        east = _math.ceil(float(bbox.east) / step) * step
        north = _math.ceil(float(bbox.north) / step) * step
        return BBox(west=max(-179.9, west), south=max(-89.9, south), east=min(179.9, east), north=min(89.9, north))

    @staticmethod
    def _cache_key_for_subset(bbox: BBox, stride: int, valid_time: datetime | None) -> str:
        q = RtofsProvider._quantize_bbox_for_hycom(bbox)
        return "hycom:%s:%s:%s:%s:s%s:t%s:%s" % (
            round(q.west, 4), round(q.south, 4), round(q.east, 4), round(q.north, 4),
            max(1, int(stride or 1)),
            valid_time.isoformat() if valid_time else "present",
            HYCOM_CACHE_SCHEMA_VERSION,
        )

    @staticmethod
    def _subset_disk_cache_dir() -> Path:
        root = Path(os.getenv("GFS_HYCOM_DISK_CACHE_DIR", "/tmp/lftr_broadcast_cache/hycom"))
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            root = Path(tempfile.gettempdir()) / "lftr_broadcast_cache" / "hycom"
            root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _subset_disk_cache_path(key: str) -> Path:
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return RtofsProvider._subset_disk_cache_dir() / f"{digest}.json"

    @classmethod
    def _subset_disk_cache_get(cls, key: str) -> tuple[dict[str, Any], datetime | None] | None:
        path = cls._subset_disk_cache_path(key)
        try:
            if not path.exists() or (time.time() - path.stat().st_mtime) > HYCOM_CACHE_TTL_SECONDS:
                return None
            data = json.loads(path.read_text())
            payload = data.get("payload")
            vt_raw = data.get("valid_time")
            vt = datetime.fromisoformat(vt_raw) if vt_raw else None
            if isinstance(payload, dict):
                return payload, vt
        except Exception:
            return None
        return None

    @classmethod
    def _subset_disk_cache_set(cls, key: str, payload: dict[str, Any], valid_time: datetime | None) -> None:
        try:
            path = cls._subset_disk_cache_path(key)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "valid_time": valid_time.isoformat() if valid_time else None, "payload": payload}, separators=(",", ":"), default=str))
            tmp.replace(path)
        except Exception:
            pass

    @classmethod
    def _subset_cache_get(cls, key: str) -> tuple[dict[str, Any], datetime | None] | None:
        with cls._subset_lock:
            row = cls._subset_cache.get(key)
            if not row:
                return None
            ts, payload, vt = row
            if time.time() - ts > HYCOM_CACHE_TTL_SECONDS:
                try:
                    cls._subset_cache.pop(key, None)
                except Exception:
                    pass
                return None
            return payload, vt

    @classmethod
    def _subset_cache_set(cls, key: str, payload: dict[str, Any], valid_time: datetime | None) -> None:
        with cls._subset_lock:
            cls._subset_cache[key] = (time.time(), payload, valid_time)
            # Keep memory bounded.
            if len(cls._subset_cache) > 48:
                oldest = sorted(cls._subset_cache.items(), key=lambda kv: kv[1][0])[:12]
                for k, _ in oldest:
                    cls._subset_cache.pop(k, None)

    @staticmethod
    def _nearest_station(lat: float, lon: float) -> str:
        def dist2(item: tuple[str, float, float]) -> float:
            _, slat, slon = item
            return (lat - slat) ** 2 + (lon - slon) ** 2

        return min(COOPS_STATIONS, key=dist2)[0]

    @staticmethod
    def _http_json(url: str, timeout_s: float = 6.5) -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LFTR-GFS/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as res:
                return json.loads(res.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    @staticmethod
    def _http_text(url: str, timeout_s: float = 12.0) -> tuple[str | None, dict[str, Any]]:
        diag: dict[str, Any] = {"url": url, "timeout_s": timeout_s, "download_method": "urllib_text"}
        started = datetime.utcnow()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LFTR-GFS/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as res:
                raw = res.read()
                text = raw.decode("utf-8", errors="replace")
                diag.update({
                    "ok": True,
                    "status": getattr(res, "status", None),
                    "content_type": res.headers.get("content-type"),
                    "bytes": len(raw),
                    "latency_ms": int((datetime.utcnow() - started).total_seconds() * 1000),
                })
                return text, diag
        except Exception as exc:
            diag.update({
                "ok": False,
                "status": "download_failed",
                "error": str(exc),
                "latency_ms": int((datetime.utcnow() - started).total_seconds() * 1000),
            })
            return None, diag

    @staticmethod
    def _http_bytes(url: str, timeout_s: float | None = None) -> tuple[bytes | None, dict[str, Any]]:
        if timeout_s is None:
            try:
                timeout_s = float(os.getenv("GFS_HYCOM_HTTP_TIMEOUT_S", "18"))
            except Exception:
                timeout_s = 18.0
        diag: dict[str, Any] = {"url": url, "timeout_s": timeout_s, "download_method": "urllib"}
        started = datetime.utcnow()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LFTR-GFS/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as res:
                payload = res.read()
                diag.update({
                    "ok": True,
                    "status": getattr(res, "status", None),
                    "content_type": res.headers.get("content-type"),
                    "bytes": len(payload),
                    "latency_ms": int((datetime.utcnow() - started).total_seconds() * 1000),
                })
                return payload, diag
        except Exception as exc:
            diag.update({
                "ok": False,
                "error": str(exc),
                "latency_ms": int((datetime.utcnow() - started).total_seconds() * 1000),
            })
            return None, diag

    @staticmethod
    def _looks_like_bad_netcdf_payload(path: str) -> tuple[bool, str | None]:
        try:
            with open(path, "rb") as f:
                payload = f.read(1024)
        except Exception as exc:
            return True, f"read_failed:{exc}"
        head = payload[:256].lstrip()
        if not payload:
            return True, "empty_payload"
        if head.startswith(b"<") or b"Error {" in head[:128] or b"Malformed or unexpected Constraint" in payload:
            return True, payload[:512].decode("utf-8", errors="replace")
        return False, None

    @staticmethod
    def _curl_download_netcdf(url: str) -> tuple[str | None, dict[str, Any]]:
        """Download HYCOM via curl without sudo, matching the manual SSH test.

        The app must not run sudo. This path only uses curl's robust HTTPS
        handling, explicit connect/total timeouts, a one-time retry, and then
        validates that the output is actually NetCDF before xarray sees it.
        """
        curl = shutil.which("curl")
        connect_timeout = str(os.getenv("GFS_HYCOM_CURL_CONNECT_TIMEOUT_S", "10"))
        max_time = str(os.getenv("GFS_HYCOM_CURL_MAX_TIME_S", "60"))
        diag: dict[str, Any] = {
            "url": url,
            "download_method": "curl",
            "sudo": False,
            "connect_timeout_s": connect_timeout,
            "max_time_s": max_time,
        }
        if not curl:
            diag.update({"ok": False, "status": "curl_not_found"})
            return None, diag
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".nc")
        tmp_path = tmp.name
        tmp.close()
        cmd = [
            curl, "-L", "--fail", "--silent", "--show-error",
            "--connect-timeout", connect_timeout,
            "--max-time", max_time,
            "--retry", os.getenv("GFS_HYCOM_CURL_RETRY", "1"),
            "--retry-delay", os.getenv("GFS_HYCOM_CURL_RETRY_DELAY_S", "1"),
            "-o", tmp_path,
            "-w", "HTTP=%{http_code} SIZE=%{size_download} TIME=%{time_total}",
            url,
        ]
        started = datetime.utcnow()
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=float(max_time) + 5.0, check=False)
            latency_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
            size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            diag.update({
                "ok": proc.returncode == 0 and size > 0,
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-300:],
                "stderr": (proc.stderr or "")[-500:],
                "bytes": size,
                "latency_ms": latency_ms,
            })
            if proc.returncode != 0 or size <= 0:
                diag.setdefault("status", "download_failed")
                try: os.unlink(tmp_path)
                except Exception: pass
                return None, diag
            bad, preview = RtofsProvider._looks_like_bad_netcdf_payload(tmp_path)
            if bad:
                diag.update({"ok": False, "status": "constraint_rejected_or_html", "preview": preview})
                try: os.unlink(tmp_path)
                except Exception: pass
                return None, diag
            diag.update({"status": "downloaded", "tmp_suffix": ".nc"})
            return tmp_path, diag
        except Exception as exc:
            diag.update({"ok": False, "status": "curl_exception", "error": str(exc), "latency_ms": int((datetime.utcnow() - started).total_seconds() * 1000)})
            try: os.unlink(tmp_path)
            except Exception: pass
            return None, diag

    @staticmethod
    def _download_netcdf(url: str) -> tuple[str | None, dict[str, Any]]:
        method = str(os.getenv("GFS_HYCOM_DOWNLOAD_METHOD", "curl_first")).strip().lower()
        if method in {"curl", "curl_first", "auto"}:
            tmp_path, diag = RtofsProvider._curl_download_netcdf(url)
            if tmp_path:
                return tmp_path, diag
            if method == "curl":
                return None, diag
            curl_diag = diag
        else:
            curl_diag = None

        payload, diag = RtofsProvider._http_bytes(url)
        if curl_diag:
            diag["curl_first_failure"] = curl_diag
        if not payload:
            diag.setdefault("status", "download_failed")
            return None, diag
        with tempfile.NamedTemporaryFile(delete=False, suffix=".nc") as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        bad, preview = RtofsProvider._looks_like_bad_netcdf_payload(tmp_path)
        if bad:
            diag.update({"ok": False, "status": "constraint_rejected_or_html", "preview": preview})
            try: os.unlink(tmp_path)
            except Exception: pass
            return None, diag
        diag.update({"tmp_suffix": ".nc", "status": "downloaded"})
        return tmp_path, diag

    @staticmethod
    def _open_dataset(tmp_path: str) -> tuple[xr.Dataset | None, str | None]:
        engines = ["netcdf4", "h5netcdf", "scipy", None]
        last_exc: Exception | None = None
        for engine in engines:
            try:
                ds = xr.open_dataset(tmp_path, engine=engine) if engine else xr.open_dataset(tmp_path)
                return ds, engine or "default"
            except Exception as exc:
                last_exc = exc
        if last_exc:
            raise last_exc
        return None, None

    @staticmethod
    def _find_array(ds: xr.Dataset, *names: str):
        for name in names:
            if name in ds.variables:
                return ds[name]
            if name in ds.coords:
                return ds.coords[name]
        return None

    @staticmethod
    def _extract_coord_1d(ds: xr.Dataset, *names: str) -> list[float]:
        arr = RtofsProvider._find_array(ds, *names)
        if arr is None:
            return []
        try:
            arr = arr.squeeze(drop=True)
            values = arr.values
        except Exception:
            return []
        try:
            if getattr(arr, 'ndim', 0) == 1:
                seq = values.tolist()
            elif getattr(arr, 'ndim', 0) == 2:
                # NCSS addLatLon may return 2-D lat/lon arrays.  Latitude is constant
                # along each row and longitude is constant down each column, so choose
                # the stable axis from shape/name.
                lname = str(getattr(arr, 'name', '') or '').lower()
                if 'lat' in lname:
                    seq = values[:, 0].tolist()
                else:
                    seq = values[0, :].tolist()
            else:
                return []
        except Exception:
            return []
        out: list[float] = []
        for value in seq:
            f = _safe(value, float('nan'))
            if math.isfinite(f):
                out.append(f)
        return out

    @staticmethod
    def _extract_2d(ds: xr.Dataset, var_name: str) -> list[list[float]]:
        arr = RtofsProvider._find_array(ds, var_name)
        if arr is None:
            return []
        arr = arr.squeeze(drop=True)
        if getattr(arr, "ndim", 0) > 2:
            while getattr(arr, "ndim", 0) > 2:
                arr = arr.isel({arr.dims[0]: 0}, drop=True)
        if getattr(arr, "ndim", 0) != 2:
            return []
        values = arr.values
        ny = int(values.shape[0]) if len(values.shape) > 0 else 0
        nx = int(values.shape[1]) if len(values.shape) > 1 else 0
        out: list[list[float]] = []
        for i in range(ny):
            row: list[float] = []
            for j in range(nx):
                row.append(_safe(values[i][j], float("nan")))
            out.append(row)
        return out

    @staticmethod
    def _build_hycom_attempts(*, west: float, south: float, east: float, north: float, stride: int, valid_time: datetime | None) -> list[dict[str, Any]]:
        lat_min = max(-80.0, min(south, north))
        lat_max = min(90.0, max(south, north))
        stride_val = max(1, int(stride or 1))
        time_value = iso_utc(valid_time) if valid_time else 'present'

        lon_ranges, lon_diag = _hycom_lon_slices(west, east)

        # Order matters. The logs show the successful HYCOM payload path is
        # the compact all_best surface subset with exactly sst/sss/ssu/ssv.
        # Request that first.  surf_el is useful but has caused 25s stalls on
        # nearby bboxes, so it is now an optional second attempt rather than
        # the primary payload contract.
        attempt_specs: list[dict[str, Any]] = [
            {
                "name": "all_best_surface_no_ssh",
                "dataset": HYCOM_NCSS_GRID,
                "vars": ["sst", "sss", "ssu", "ssv"],
                "u_var": "ssu",
                "v_var": "ssv",
                "sst_var": "sst",
                "salinity_var": "sss",
                "vert_coord": None,
            },
            {
                "name": "all_best_surface_with_ssh",
                "dataset": HYCOM_NCSS_GRID,
                "vars": ["sst", "sss", "ssu", "ssv", "surf_el"],
                "u_var": "ssu",
                "v_var": "ssv",
                "sst_var": "sst",
                "salinity_var": "sss",
                "vert_coord": None,
            },
            {
                "name": "uv3z_depth0_currents",
                "dataset": HYCOM_UV3Z_NCSS_GRID,
                "vars": ["water_u", "water_v"],
                "u_var": "water_u",
                "v_var": "water_v",
                "sst_var": None,
                "salinity_var": None,
                "vert_coord": 0.0,
            },
        ]
        if not HYCOM_ENABLE_FALLBACK_ATTEMPTS:
            attempt_specs = attempt_specs[:1]

        attempts: list[dict[str, Any]] = []
        for spec in attempt_specs:
            urls: list[str] = []
            for lon_min, lon_max in lon_ranges:
                query = _build_ncss_query(
                    vars=spec["vars"],
                    west=lon_min,
                    south=lat_min,
                    east=lon_max,
                    north=lat_max,
                    stride=stride_val,
                    time_value=time_value,
                    vert_coord=spec.get("vert_coord"),
                )
                urls.append(f"{spec['dataset']}?{query}")
            attempts.append({**spec, "urls": urls, "lon_ranges": lon_ranges, "hycom_lon_diagnostics": lon_diag, "lat_range": [lat_min, lat_max], "stride": stride_val, "ncss_horiz_stride": ("omitted_native" if stride_val <= 1 else stride_val), "time_value": time_value})
        return attempts

    @staticmethod
    def _merge_antimeridian_parts(parts: list[list[list[float]]]) -> list[list[float]]:
        grids = [g for g in parts if g]
        if not grids:
            return []
        if len(grids) == 1:
            return grids[0]
        min_rows = min(len(g) for g in grids)
        merged: list[list[float]] = []
        for i in range(min_rows):
            row: list[float] = []
            for g in grids:
                row.extend(g[i])
            merged.append(row)
        return merged

    def _fetch_hycom_bundle(self, *, west: float, south: float, east: float, north: float, stride: int, valid_time: datetime | None):
        attempts = self._build_hycom_attempts(west=west, south=south, east=east, north=north, stride=stride, valid_time=valid_time)
        selected_attempt: str | None = None
        selected_dataset: str | None = None
        selected_vars: list[str] = []
        selected_u_var: str | None = None
        selected_v_var: str | None = None
        sst_parts: list[list[list[float]]] = []
        u_parts: list[list[list[float]]] = []
        v_parts: list[list[list[float]]] = []
        sal_parts: list[list[list[float]]] = []
        lat_parts: list[list[float]] = []
        lon_parts: list[list[float]] = []
        opened_urls: list[str] = []
        previews: list[str] = []
        diagnostics: list[dict[str, Any]] = []

        for attempt in attempts:
            attempt_sst_parts: list[list[list[float]]] = []
            attempt_u_parts: list[list[list[float]]] = []
            attempt_v_parts: list[list[list[float]]] = []
            attempt_sal_parts: list[list[list[float]]] = []
            attempt_lat_parts: list[list[float]] = []
            attempt_lon_parts: list[list[float]] = []
            attempt_opened_urls: list[str] = []
            attempt_has_current = False
            attempt_has_sst = False
            for url in attempt["urls"]:
                tmp_path, http_diag = self._download_netcdf(url)
                opened_urls.append(url)
                attempt_opened_urls.append(url)
                if not tmp_path:
                    previews.append(str(http_diag.get("status") or "download_failed_or_constraint_rejected"))
                    diagnostics.append({**http_diag, "attempt": attempt["name"], "dataset": attempt["dataset"], "vars_requested": attempt["vars"]})
                    continue
                ds = None
                try:
                    ds, engine = self._open_dataset(tmp_path)
                    sst_grid = self._extract_2d(ds, attempt.get("sst_var") or "__missing_sst__") if attempt.get("sst_var") else []
                    u_grid = self._extract_2d(ds, attempt.get("u_var") or "__missing_u__")
                    v_grid = self._extract_2d(ds, attempt.get("v_var") or "__missing_v__")
                    sal_grid = self._extract_2d(ds, attempt.get("salinity_var") or "__missing_salinity__") if attempt.get("salinity_var") else []
                    lat_arr = self._find_array(ds, "lat", "latitude", "y")
                    lon_arr = self._find_array(ds, "lon", "longitude", "x")
                    lat_values = self._extract_coord_1d(ds, "lat", "latitude", "y")
                    lon_values = self._extract_coord_1d(ds, "lon", "longitude", "x")
                    diag = {
                        **http_diag,
                        "url": url,
                        "attempt": attempt["name"],
                        "dataset": attempt["dataset"],
                        "vars_requested": attempt["vars"],
                        "u_var": attempt.get("u_var"),
                        "v_var": attempt.get("v_var"),
                        "engine": engine,
                        "status": "opened",
                        "dims": {k: int(v) for k, v in ds.sizes.items()},
                        "vars": list(ds.variables)[:24],
                        "coords": list(ds.coords)[:16],
                        "lat_shape": list(lat_arr.shape) if lat_arr is not None and hasattr(lat_arr, "shape") else [],
                        "lon_shape": list(lon_arr.shape) if lon_arr is not None and hasattr(lon_arr, "shape") else [],
                        "sst_shape": [len(sst_grid), len(sst_grid[0]) if sst_grid else 0],
                        "u_shape": [len(u_grid), len(u_grid[0]) if u_grid else 0],
                        "v_shape": [len(v_grid), len(v_grid[0]) if v_grid else 0],
                        "salinity_shape": [len(sal_grid), len(sal_grid[0]) if sal_grid else 0],
                        "lat_values_len": len(lat_values),
                        "lon_values_len": len(lon_values),
                        "lat_sample": lat_values[:3] + lat_values[-3:] if len(lat_values) > 6 else lat_values,
                        "lon_sample": lon_values[:3] + lon_values[-3:] if len(lon_values) > 6 else lon_values,
                    }
                    diagnostics.append(diag)
                    attempt_sst_parts.append(sst_grid)
                    attempt_u_parts.append(u_grid)
                    attempt_v_parts.append(v_grid)
                    attempt_sal_parts.append(sal_grid)
                    if lat_values:
                        attempt_lat_parts.append(lat_values)
                    if lon_values:
                        attempt_lon_parts.append(lon_values)
                    attempt_has_current = attempt_has_current or bool(u_grid and v_grid)
                    attempt_has_sst = attempt_has_sst or bool(sst_grid)
                    previews.append(json.dumps(diag, separators=(",", ":"))[:800])
                    log.info("hycom ncss raw dataset attempt=%s url=%s engine=%s dims=%s vars=%s coords=%s lat_shape=%s lon_shape=%s sst_shape=%s u_shape=%s v_shape=%s salinity_shape=%s", attempt["name"], url, engine, diag["dims"], diag["vars"], diag["coords"], diag["lat_shape"], diag["lon_shape"], diag["sst_shape"], diag["u_shape"], diag["v_shape"], diag["salinity_shape"])
                    ds.close()
                    ds = None
                except Exception as exc:
                    previews.append(f"open_failed:{attempt['name']}:{exc}")
                    diagnostics.append({**http_diag, "url": url, "attempt": attempt["name"], "dataset": attempt["dataset"], "vars_requested": attempt["vars"], "status": "open_failed", "error": str(exc)})
                finally:
                    if ds is not None:
                        try:
                            ds.close()
                        except Exception:
                            pass
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            if attempt_has_current or attempt_has_sst:
                selected_attempt = attempt["name"]
                selected_dataset = attempt["dataset"]
                selected_vars = list(attempt["vars"])
                selected_u_var = attempt.get("u_var")
                selected_v_var = attempt.get("v_var")
                sst_parts = attempt_sst_parts
                u_parts = attempt_u_parts
                v_parts = attempt_v_parts
                sal_parts = attempt_sal_parts
                lat_parts = attempt_lat_parts
                lon_parts = attempt_lon_parts
                opened_urls = attempt_opened_urls
                break

        return (
            self._merge_antimeridian_parts(sst_parts),
            self._merge_antimeridian_parts(u_parts),
            self._merge_antimeridian_parts(v_parts),
            self._merge_antimeridian_parts(sal_parts),
            opened_urls,
            previews,
            diagnostics,
            {
                "selected_attempt": selected_attempt,
                "selected_dataset": selected_dataset,
                "selected_vars": selected_vars,
                "selected_u_var": selected_u_var,
                "selected_v_var": selected_v_var,
                "attempt_order": [a["name"] for a in attempts],
                "hycom_lon_diagnostics": (attempts[0].get("hycom_lon_diagnostics") if attempts else {}),
                "hycom_slices": [
                    {"west": round(float(a), 6), "east": round(float(b), 6)}
                    for a, b in ((attempts[0].get("lon_ranges") if attempts else []) or [])
                ],
                "lat_values": lat_parts[0] if lat_parts else [],
                "lon_values": [x for part in lon_parts for x in part] if lon_parts else [],
            },
        )

    def _fetch_oisst_sst_grid(self, *, viewport, stride: int, valid_time: datetime | None) -> tuple[list[list[float]], dict[str, Any]]:
        if not GFS_ALLOW_OISST_SST_FALLBACK:
            return [], {"enabled": False, "source": "oisst_erddap_disabled"}
        # Keep fallback requests bounded and quick.  OISST is for observed SST
        # recovery, not a global full-resolution backfill.
        width = abs(float(viewport.east) - float(viewport.west))
        height = abs(float(viewport.north) - float(viewport.south))
        area = max(0.01, width * height)
        fallback_stride = max(1, int(stride or 1))
        if area > 240.0:
            fallback_stride = max(fallback_stride, 3)
        if area > 600.0:
            fallback_stride = max(fallback_stride, 6)
        urls = build_erddap_subset_request(
            viewport,
            OISST_ERDDAP_CSV,
            ["sst"],
            fallback_stride,
            valid_time,
            lon_convention="pm180",
            lat_descending=False,
        )
        parts: list[list[list[float]]] = []
        diagnostics: list[dict[str, Any]] = []
        previews: list[str] = []
        for url in urls:
            text, http_diag = self._http_text(url, timeout_s=float(os.getenv("GFS_OISST_HTTP_TIMEOUT_S", "14") or "14"))
            grid, parse_diag = parse_erddap_grid(text, preferred_value_columns=("sst", "analysed_sst", "sea_surface_temperature"))
            parse_summary = {
                "row_count": parse_diag.row_count,
                "accepted_rows": parse_diag.accepted_rows,
                "lat_count": parse_diag.lat_count,
                "lon_count": parse_diag.lon_count,
                "parser_rejected_rows": parse_diag.parser_rejected_rows,
                "preview_lines": parse_diag.preview_lines[:3],
                "grid_shape": [len(grid), len(grid[0]) if grid else 0],
            }
            diagnostics.append({**http_diag, "source": "oisst_erddap", "parse": parse_summary})
            previews.append(json.dumps(parse_summary, separators=(",", ":"))[:500])
            if grid:
                parts.append(grid)
        grid = self._merge_antimeridian_parts(parts)
        meta = {
            "enabled": True,
            "source": "oisst_erddap",
            "dataset_url": OISST_ERDDAP_CSV,
            "urls": urls,
            "url_count": len(urls),
            "stride": fallback_stride,
            "grid_shape": [len(grid), len(grid[0]) if grid else 0],
            "diagnostics": diagnostics,
            "debug_previews": previews[:3],
        }
        return grid, meta

    def _fetch_station_currents(self, *, center_lat: float, center_lon: float) -> tuple[float | None, float | None]:
        station_id = self._nearest_station(center_lat, center_lon)
        url = (
            f"{NOAA_TIDES_API}?product=currents_predictions&application=lftr&station={station_id}"
            "&time_zone=gmt&units=english&interval=MAX_SLACK&format=json"
        )
        payload = self._http_json(url)
        arr = (payload or {}).get("current_predictions") or (payload or {}).get("cp") or []
        if not arr:
            return None, None
        p0 = arr[0]
        try:
            speed = float(p0.get("Velocity_Major") or p0.get("v") or p0.get("speed"))
            direction = float(p0.get("Direction_Bin") or p0.get("d") or p0.get("direction"))
        except Exception:
            return None, None
        rad = math.radians(direction)
        return speed * math.sin(rad), speed * math.cos(rad)

    @staticmethod
    def _constant_grid(ny: int, nx: int, value: float | None) -> list[list[float]]:
        if value is None or ny < 1 or nx < 1:
            return []
        return [[round(value, 4) for _ in range(nx)] for __ in range(ny)]

    def _ekman_fallback_from_station(self, ny: int, nx: int, center_lat: float, center_lon: float) -> tuple[list[list[float]], list[list[float]], str]:
        u, v = self._fetch_station_currents(center_lat=center_lat, center_lon=center_lon)
        return self._constant_grid(ny, nx, u), self._constant_grid(ny, nx, v), "noaa_coops_aux"

    def _fetch_subset_sync(self, *, bbox: BBox, stride: int, valid_time: datetime | None) -> tuple[dict[str, Any], datetime | None]:
        if not self._probable_ocean_overlap_bbox(bbox):
            payload = {
                "sst": [],
                "current_u": [],
                "current_v": [],
                "current_speed": [],
                "salinity": [],
                "source_meta": {
                    "ocean_source": "hycom_ncss",
                    "current_source": "hycom_skipped_no_ocean_overlap",
                    "sst_source": "hycom_skipped_no_ocean_overlap",
                    "real_subset": False,
                    "skip_reason": "bbox_has_no_probable_ocean_overlap",
                    "bbox": bbox.as_list(),
                },
            }
            log.info("hycom skipped no probable ocean overlap bbox=%s", bbox.as_list())
            return payload, valid_time

        original_bbox = bbox
        bbox = self._quantize_bbox_for_hycom(bbox)
        stride = max(1, int(stride or 1))
        key = self._cache_key_for_subset(bbox, stride, valid_time)
        cached = self._subset_cache_get(key)
        if cached:
            payload, vt = cached
            out = dict(payload)
            out.setdefault("source_meta", {})
            out["source_meta"] = {**out.get("source_meta", {}), "cache": "hycom_memory_hit", "cache_key": key, "requested_bbox": original_bbox.as_list(), "served_bbox": bbox.as_list(), "ttl_seconds": HYCOM_CACHE_TTL_SECONDS}
            log.info("hycom memory cache hit key=%s requested_bbox=%s served_bbox=%s", key, original_bbox.as_list(), bbox.as_list())
            return out, vt
        disk_cached = self._subset_disk_cache_get(key)
        if disk_cached:
            payload, vt = disk_cached
            self._subset_cache_set(key, payload, vt)
            out = dict(payload)
            out.setdefault("source_meta", {})
            out["source_meta"] = {**out.get("source_meta", {}), "cache": "hycom_disk_hit", "cache_key": key, "requested_bbox": original_bbox.as_list(), "served_bbox": bbox.as_list(), "ttl_seconds": HYCOM_CACHE_TTL_SECONDS}
            log.info("hycom disk cache hit key=%s requested_bbox=%s served_bbox=%s", key, original_bbox.as_list(), bbox.as_list())
            return out, vt

        is_owner = False
        with self._subset_lock:
            event = self._subset_inflight.get(key)
            if event is None:
                event = threading.Event()
                self._subset_inflight[key] = event
                is_owner = True
        if not is_owner:
            event.wait(HYCOM_INFLIGHT_WAIT_SECONDS)
            cached = self._subset_cache_get(key)
            if cached:
                payload, vt = cached
                out = dict(payload)
                out.setdefault("source_meta", {})
                out["source_meta"] = {**out.get("source_meta", {}), "cache": "hycom_inflight_joined", "cache_key": key, "requested_bbox": original_bbox.as_list(), "served_bbox": bbox.as_list()}
                log.info("hycom inflight joined key=%s requested_bbox=%s served_bbox=%s", key, original_bbox.as_list(), bbox.as_list())
                return out, vt

        viewport = viewport_from_bbox(bbox)
        slices = split_antimeridian(viewport)
        try:
            sst, current_u, current_v, salinity, subset_urls, previews, diagnostics, attempt_meta = self._fetch_hycom_bundle(
                west=viewport.west,
                south=viewport.south,
                east=viewport.east,
                north=viewport.north,
                stride=stride,
                valid_time=valid_time,
            )
        finally:
            pass

        ny = len(sst) or len(current_u) or len(current_v) or len(salinity)
        nx = 0
        for grid in (sst, current_u, current_v, salinity):
            if grid:
                nx = len(grid[0]) if grid[0] else 0
                break
        station_req = build_station_enrichment_request(viewport, valid_time)
        current_source = "hycom_ncss"
        aux_fallback_used = False
        if not (current_u and current_v and ny and nx):
            if GFS_ALLOW_OCEAN_AUX_FALLBACK:
                current_u, current_v, current_source = self._ekman_fallback_from_station(ny, nx, station_req["center_lat"], station_req["center_lon"])
                aux_fallback_used = bool(current_u and current_v)
            else:
                current_source = "hycom_ncss_missing_no_aux_fallback"
                current_u, current_v = [], []

        sst_fallback_meta: dict[str, Any] = {
            "enabled": False,
            "used": False,
            "policy": "disabled_hycom_only",
            "source": "none",
        }
        if not sst:
            log.warning(
                "hycom SST missing and fallback disabled bbox=%s selected_attempt=%s urls=%s diagnostics_count=%s",
                bbox.as_list(),
                attempt_meta.get("selected_attempt"),
                subset_urls,
                len(diagnostics),
            )

        ocean_mask_raw, landmask_meta = ocean_mask_from_grids(sst=sst, current_u=current_u, current_v=current_v, salinity=salinity)
        try:
            erode_cells = int(os.getenv("GFS_SST_LANDMASK_ERODE_CELLS", "1") or "1")
        except Exception:
            erode_cells = 1
        ocean_mask, erode_meta = erode_ocean_mask(ocean_mask_raw, erode_cells)
        if ocean_mask:
            sst = mask_grid(sst, ocean_mask)
            current_u = mask_grid(current_u, ocean_mask)
            current_v = mask_grid(current_v, ocean_mask)
            salinity = mask_grid(salinity, ocean_mask)
            landmask_meta = {**landmask_meta, "strict_interior_water_gate": erode_meta, "mask_stage": "finite_sst_then_eroded_interior_water"}

        current_speed: list[list[float]] = []
        if current_u and current_v:
            for u_row, v_row in zip(current_u, current_v):
                speed_row: list[float] = []
                for u_val, v_val in zip(u_row, v_row):
                    if math.isfinite(u_val) and math.isfinite(v_val):
                        speed_row.append((u_val * u_val + v_val * v_val) ** 0.5)
                    else:
                        speed_row.append(float("nan"))
                current_speed.append(speed_row)
            current_speed = mask_grid(current_speed, ocean_mask) if ocean_mask else current_speed

        has_sst = bool(sst and ny and nx and any(math.isfinite(float(v)) for row in sst for v in row if isinstance(v, (int, float))))
        has_current = bool(current_u and current_v and ny and nx and any(math.isfinite(float(u)) and math.isfinite(float(v)) for u_row, v_row in zip(current_u, current_v) for u, v in zip(u_row, v_row) if isinstance(u, (int, float)) and isinstance(v, (int, float))))
        live_ncss_ok = ((has_sst or not GFS_REQUIRE_HYCOM_SST_FOR_LIVE) and (has_current or not GFS_REQUIRE_HYCOM_CURRENT_FOR_LIVE) and not aux_fallback_used)
        blocking_reasons: list[str] = []
        if GFS_REQUIRE_HYCOM_SST_FOR_LIVE and not has_sst:
            blocking_reasons.append("hycom_sst_missing")
        if GFS_REQUIRE_HYCOM_CURRENT_FOR_LIVE and not has_current:
            blocking_reasons.append("hycom_current_missing")
        if (attempt_meta.get("selected_attempt") == "uv3z_depth0_currents") and GFS_REQUIRE_HYCOM_SST_FOR_LIVE:
            blocking_reasons.append("selected_attempt_current_only_no_sst")
        if aux_fallback_used:
            blocking_reasons.append("aux_current_fallback_used")
        quality_gate = {
            "live_ncss_ok": bool(live_ncss_ok),
            "live_ocean_truth_ok": bool(live_ncss_ok),
            "has_hycom_sst": bool(has_sst),
            "has_hycom_current": bool(has_current),
            "hycom_sst_status": "ready" if has_sst else "missing",
            "hycom_current_status": "ready" if has_current else "missing",
            "blocking_reasons": blocking_reasons,
            "aux_fallback_used": bool(aux_fallback_used),
            "allow_aux_fallback": bool(GFS_ALLOW_OCEAN_AUX_FALLBACK),
            "sst_fallback_enabled": False,
            "sst_fallback_used": False,
            "require_hycom_sst": bool(GFS_REQUIRE_HYCOM_SST_FOR_LIVE),
            "require_hycom_current": bool(GFS_REQUIRE_HYCOM_CURRENT_FOR_LIVE),
            "rule": "hycom_ncss_sst_and_current_required_no_sst_fallback",
        }

        payload = {
            "sst": sst if has_sst else [],
            "current_u": current_u if has_current else [],
            "current_v": current_v if has_current else [],
            "current_speed": current_speed if has_current else [],
            "salinity": salinity,
            "ocean_mask": ocean_mask,
            "lat_values": attempt_meta.get("lat_values") or [],
            "lon_values": attempt_meta.get("lon_values") or [],
            "source_meta": {
                "ocean_source": "hycom_ncss",
                "current_source": current_source,
                "sst_source": "hycom_ncss",
                "subset_urls": len(subset_urls),
                "lon_convention": "0360",
                "real_subset": bool(live_ncss_ok),
                "quality_gate": quality_gate,
                "has_sst": bool(has_sst),
                "has_current": bool(has_current),
                "hycom_sst_status": quality_gate.get("hycom_sst_status"),
                "hycom_current_status": quality_gate.get("hycom_current_status"),
                "blocking_reasons": quality_gate.get("blocking_reasons"),
                "live_ocean_truth_ok": bool(live_ncss_ok),
                "sst_landmask": landmask_meta,
                "landmask_contract": "finite_sst_eroded_interior_water_gate_for_sst_currents_bait_boats_shark",
                "fallback_used": bool(aux_fallback_used),
                "fallback_sources": ([current_source] if aux_fallback_used else []),
                "sst_fallback": sst_fallback_meta,
                "lat_descending": False,
                "effective_stride": max(1, int(stride or 1)),
                "ncss_horiz_stride": ("omitted_native" if max(1, int(stride or 1)) <= 1 else max(1, int(stride or 1))),
                "extra_dimensions": [],
                "mode": "hycom_espc_d_v02_surface_sst_ssu_ssv_strict_no_sst_fallback",
                "sst_dataset_url": HYCOM_NCSS_GRID,
                "hycom_sst_dataset_url": HYCOM_NCSS_GRID,
                "current_dataset_url": attempt_meta.get("selected_dataset") or HYCOM_NCSS_GRID,
                "hycom_variable_contract": {
                    "sst": "sst",
                    "surface_current_u": "ssu",
                    "surface_current_v": "ssv",
                    "surface_salinity": "sss",
                    "current_only_depth0_u": "water_u",
                    "current_only_depth0_v": "water_v",
                    "primary_attempt": "all_best_surface_no_ssh",
                    "secondary_attempts": ["all_best_surface_with_ssh", "uv3z_depth0_currents"],
                },
                "selected_attempt": attempt_meta.get("selected_attempt"),
                "selected_dataset": attempt_meta.get("selected_dataset"),
                "selected_vars": attempt_meta.get("selected_vars"),
                "selected_u_var": attempt_meta.get("selected_u_var"),
                "selected_v_var": attempt_meta.get("selected_v_var"),
                "attempt_order": attempt_meta.get("attempt_order"),
                "hycom_slices": attempt_meta.get("hycom_slices"),
                "hycom_lon_diagnostics": attempt_meta.get("hycom_lon_diagnostics"),
                "diagnostics": diagnostics,
                "opened_urls": subset_urls,
                "debug_previews": previews[:3],
                "grid_shape": [ny, nx],
                "lat_values_len": len(attempt_meta.get("lat_values") or []),
                "lon_values_len": len(attempt_meta.get("lon_values") or []),
            },
        }
        self._last_fetch_at = datetime.utcnow()
        self._last_error = None
        log.info(
            "ocean subset fetched bbox=%s viewport=%s hycom_slices=%s stride=%s sst_shape=%sx%s real_subset=%s lat_descending=%s",
            bbox.as_list(),
            {"west": viewport.west, "south": viewport.south, "east": viewport.east, "north": viewport.north},
            attempt_meta.get("hycom_slices") or [{"lon_start": s.lon_start, "lon_stop": s.lon_stop} for s in slices],
            max(1, int(stride or 1)),
            ny,
            nx,
            bool(sst),
            False,
        )
        if not sst:
            empty_log = log.info if not GFS_REQUIRE_HYCOM_SST_FOR_LIVE else log.warning
            empty_log(
                "hycom subset empty bbox=%s dataset=%s vars=%s urls=%s rows=%s lat=%s lon=%s parser_rejected=%s http_success_no_data=%s lon_convention=%s lat_descending=%s preview=%s",
                bbox.as_list(),
                attempt_meta.get("selected_dataset") or HYCOM_NCSS_GRID,
                attempt_meta.get("selected_vars") or HYCOM_DATASET_META["request_vars"],
                subset_urls,
                [len(sst)],
                [ny],
                [nx],
                [0],
                True,
                "0360",
                False,
                previews,
            )
            if diagnostics:
                empty_log("hycom subset diagnostics bbox=%s diagnostics=%s", bbox.as_list(), diagnostics)
        try:
            self._subset_cache_set(key, payload, valid_time)
            self._subset_disk_cache_set(key, payload, valid_time)
        finally:
            with self._subset_lock:
                ev = self._subset_inflight.pop(key, None)
                if ev is not None:
                    ev.set()
        return payload, valid_time

    async def fetch_subset(self, *, bbox: BBox, stride: int, valid_time: datetime | None) -> tuple[dict[str, Any], datetime | None]:
        try:
            return await asyncio.to_thread(self._fetch_subset_sync, bbox=bbox, stride=stride, valid_time=valid_time)
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("ocean subset failed bbox=%s local_lod_stride=%s adaptive_ncss_horizStride=%s err=%s", bbox.as_list(), stride, ("omitted_native" if max(1, int(stride or 1)) <= 1 else max(1, int(stride or 1))), exc)
            return {
                "sst": [],
                "current_u": [],
                "current_v": [],
                "current_speed": [],
                "source_meta": {"ocean_source": "hycom_ncss", "current_source": "hycom_ncss_error", "sst_source": "hycom_ncss", "real_subset": False, "live_ocean_truth_ok": False, "blocking_reasons": ["hycom_provider_exception"], "error": str(exc)},
            }, valid_time

    def health(self) -> dict[str, Any]:
        return {
            "provider": "ocean",
            "status": "viewport_subset_only",
            "upstreams": ["hycom_ncss"],
            "aux_fallback_enabled": bool(GFS_ALLOW_OCEAN_AUX_FALLBACK),
            "oisst_sst_fallback_enabled": False,
            "sst_dataset_url": HYCOM_NCSS_GRID,
            "current_dataset_url": HYCOM_NCSS_GRID,
            "last_fetch_at": iso_utc(self._last_fetch_at),
            "last_error": self._last_error,
        }

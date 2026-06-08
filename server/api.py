from __future__ import annotations

import math
import os
import asyncio
from typing import Any

from quart import Blueprint, current_app, jsonify, request

from server.gfs.errors import GfsError

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _json_clean(value: Any, depth: int = 0) -> Any:
    if depth > 30:
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
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
        return {str(k): _json_clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_clean(v, depth + 1) for v in value]
    return str(value)


def _json(payload, status: int = 200):
    return jsonify(_json_clean(payload)), status


@api_bp.get("/health")
async def api_health():
    return jsonify({"ok": True, "service": "broadcast-weather"})


def _field_values(field):
    if isinstance(field, dict):
        return field.get("values") or field.get("data") or field.get("grid") or []
    return field

def _sample_grid(grid, lat: float, lon: float, bbox: list[float]):
    if not isinstance(grid, list) or not grid or not isinstance(grid[0], list):
        return None
    arr = grid[0] if isinstance(grid[0][0], list) else grid
    ny = len(arr)
    nx = len(arr[0]) if ny and isinstance(arr[0], list) else 0
    if ny < 1 or nx < 1:
        return None
    west, south, east, north = [float(v) for v in bbox]
    yi = max(0, min(ny - 1, int(((lat - south) / ((north - south) or 1.0)) * ny)))
    xi = max(0, min(nx - 1, int(((lon - west) / ((east - west) or 1.0)) * nx)))
    try:
        return float(arr[yi][xi])
    except Exception:
        return None


def _jetstream_orb_grid(fields: dict, bbox: list[float], stride: int = 5, altitude_m: float = 3048.0) -> list[dict]:
    u_grid = _field_values(fields.get("wind_u"))
    v_grid = _field_values(fields.get("wind_v"))
    if not isinstance(u_grid, list) or not u_grid or not isinstance(u_grid[0], list):
        return []
    if not isinstance(v_grid, list) or not v_grid or not isinstance(v_grid[0], list):
        return []
    u = u_grid[0] if isinstance(u_grid[0][0], list) else u_grid
    v = v_grid[0] if isinstance(v_grid[0][0], list) else v_grid
    ny = len(u)
    nx = len(u[0]) if ny and isinstance(u[0], list) else 0
    if ny < 1 or nx < 1:
        return []
    west, south, east, north = [float(x) for x in bbox]
    items: list[dict] = []
    for iy in range(0, ny, max(1, int(stride))):
        for ix in range(0, nx, max(1, int(stride))):
            try:
                u_val = float(u[iy][ix])
                v_val = float(v[iy][ix])
            except Exception:
                continue
            speed_mps = (u_val * u_val + v_val * v_val) ** 0.5
            speed_mph = speed_mps * 2.23694
            heading_deg = (math.degrees(math.atan2(u_val, v_val)) + 360.0) % 360.0
            lat = south + ((iy + 0.5) / max(1, ny)) * (north - south)
            lon = west + ((ix + 0.5) / max(1, nx)) * (east - west)
            items.append({
                "lat": lat,
                "lon": lon,
                "mph": round(speed_mph, 2),
                "direction_deg": round(heading_deg, 1),
                "altitude_m": altitude_m,
            })
    return items


def _bbox_span_area(bbox: list[float]) -> tuple[float, float, float]:
    try:
        west, south, east, north = [float(x) for x in bbox[:4]]
        lon_span = abs(east - west) if east >= west else (180.0 - west) + (east + 180.0)
        lat_span = abs(north - south)
        return lon_span, lat_span, lon_span * lat_span
    except Exception:
        return 360.0, 180.0, 64800.0

def _fast_scene_shell(bbox: list[float], reason: str) -> dict:
    return {
        "ok": True,
        "bbox": bbox,
        "valid_time": None,
        "fields": {},
        "jet_orbs": [],
        "source": "scene_cache_first_shell",
        "payload_state": "warming",
        "cache_policy": "no_direct_global_provider_block",
        "jetstream": {
            "ok": False,
            "source": "unavailable_live_required_no_pseudo",
            "mode": "fast_shell_no_jetstream_until_live_vectors",
            "count": 0,
            "source_levels": [],
            "live_gfs_unavailable": True,
            "fallback_used": False,
            "mock": False,
            "proxy": False,
            "reason": reason,
        },
        "hud": {},
    }

@api_bp.get("/gfs/scene")
async def api_gfs_scene():
    try:
        engine = current_app.extensions.get("gfs_engine")
        if engine is None:
            return _json({"ok": False, "error": "gfs_engine_unavailable", "jet_orbs": [], "jetstream": {"ok": False, "source": "engine_unavailable", "count": 0}})

        intent = engine.parse_intent(request.args)
        requested_bbox = intent.bbox.as_list()
        lon_span, lat_span, area = _bbox_span_area(requested_bbox)
        if lon_span > 80.0 or lat_span > 45.0 or area > 2400.0:
            return _json(_fast_scene_shell(requested_bbox, "bbox_too_large_for_user_request_path"))
        try:
            weather = await asyncio.wait_for(engine.weather_payload(intent), timeout=float(os.getenv("GFS_SCENE_ENDPOINT_TIMEOUT_SECONDS", "4.5") or "4.5"))
        except Exception as exc:
            current_app.logger.warning("/api/gfs/scene fast shell after timeout/provider miss bbox=%s err=%s", requested_bbox, exc)
            return _json(_fast_scene_shell(requested_bbox, "provider_timeout_or_unavailable"))
        bbox_raw = weather.get("bbox") or requested_bbox
        if isinstance(bbox_raw, dict):
            bbox = [
                float(bbox_raw.get("west", -180.0)),
                float(bbox_raw.get("south", -90.0)),
                float(bbox_raw.get("east", 180.0)),
                float(bbox_raw.get("north", 90.0)),
            ]
        else:
            bbox = list(bbox_raw or intent.bbox.as_list())
        center_lat = (float(bbox[1]) + float(bbox[3])) * 0.5
        center_lon = (float(bbox[0]) + float(bbox[2])) * 0.5
        fields = weather.get("fields") or {}
        balloon_payload = weather.get("balloons") if isinstance(weather.get("balloons"), dict) else {}
        jet_vectors = list((balloon_payload or {}).get("items") or [])
        # If the dedicated isobaric balloon-vector contract is missing but the
        # live GFS wind grid is present, still return a vetted live-grid balloon
        # field so the Jetstream pill can draw.  The frontend labels this as
        # gfs_uv_grid_derived rather than pretending it is 250/300 hPa.
        if not jet_vectors:
            try:
                jet_vectors = _jetstream_orb_grid(fields, bbox, stride=8, altitude_m=3048.0)
                for _v in jet_vectors:
                    if isinstance(_v, dict):
                        _v.setdefault("source_level", "gfs_uv_grid_derived")
                        _v.setdefault("source", "gfs_uv_grid_derived")
            except Exception:
                jet_vectors = []
        jet_source_levels = sorted({str(v.get("source_level")) for v in jet_vectors if isinstance(v, dict) and v.get("source_level")})

        temp_k = _sample_grid(_field_values(fields.get("temp2m")), center_lat, center_lon, bbox)
        pressure_pa = _sample_grid(_field_values(fields.get("mslp")), center_lat, center_lon, bbox)
        wind_u = _sample_grid(_field_values(fields.get("wind_u")), center_lat, center_lon, bbox)
        wind_v = _sample_grid(_field_values(fields.get("wind_v")), center_lat, center_lon, bbox)
        wind_speed = None
        if wind_u is not None and wind_v is not None:
            wind_speed = (wind_u * wind_u + wind_v * wind_v) ** 0.5

        return _json({
            "ok": True,
            "bbox": bbox,
            "valid_time": weather.get("valid_time"),
            "fields": fields,
            # Jetstream balloons must be backed by real upper-level GFS vectors.
            # Prefer the service's isobaric steering vectors (250/300 hPa) over
            # surface/10m wind grids so the frontend does not question the data.
            "jet_orbs": jet_vectors,
            "jetstream": {
                "ok": bool(jet_vectors),
                "source": ("gfs_isobaric_balloon_vectors" if any((isinstance(v, dict) and str(v.get("source_level", "")).lower() in {"250", "250hpa", "300", "300hpa"}) for v in jet_vectors) else ("gfs_uv_grid_derived" if jet_vectors else "unavailable_live_required")),
                "mode": "live_gfs_isobaric_or_grid_derived_no_frontend_fallback",
                "count": len(jet_vectors),
                "source_levels": jet_source_levels,
                "altitude_note": "prefers upper-level GFS isobaric vectors; may use live GFS uv grid derived vectors; no mock or visual fallback is emitted",
                "fallback_used": False,
                "mock": False,
                "proxy": False,
            },
            "hud": {
                "sample_lat": center_lat,
                "sample_lon": center_lon,
                "temperature_c": (temp_k - 273.15) if temp_k is not None else None,
                "pressure_hpa": (pressure_pa / 100.0) if pressure_pa is not None else None,
                "wind_speed_mps": wind_speed,
            },
        })
    except GfsError as exc:
        return _json(exc.to_json())
    except Exception as exc:
        current_app.logger.exception("/api/gfs/scene failed")
        return _json({
            "ok": False,
            "error": "api_gfs_scene_exception",
            "message": str(exc),
            "bbox": request.args.get("bbox"),
            "jet_orbs": [],
            "jetstream": {
                "ok": False,
                "source": "unavailable_exception",
                "mode": "live_failed_no_mock_no_visual_fallback",
                "count": 0,
                "fallback_used": False,
                "mock": False,
                "proxy": False
            }
        })

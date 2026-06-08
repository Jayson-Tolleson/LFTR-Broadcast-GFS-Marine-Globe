"""Shark Intel layer: legal-slot leopard shark contours + rare tiger/sand shark watches.

This module is deliberately cache/frame friendly: it does not own routes and it
returns one normalized payload that the scene-cache spine can write/read exactly
like bait.  The contour generation is marching-square-style cell contouring on a
regular viewport grid; the frontend renderer can also re-run the shared JS bait
marching-squares helper from the score points when desired.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from typing import Any


SOCAL_BBOX = {"west": -121.5, "south": 31.0, "east": -116.2, "north": 35.2}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _norm_bbox(bbox: dict[str, Any] | None) -> dict[str, float]:
    b = bbox or {}
    west = float(b.get("west", -121.0))
    south = float(b.get("south", 32.0))
    east = float(b.get("east", -117.0))
    north = float(b.get("north", 34.5))
    if east <= west:
        cx = (east + west) / 2.0
        west, east = cx - 1.0, cx + 1.0
    if north <= south:
        cy = (north + south) / 2.0
        south, north = cy - 1.0, cy + 1.0
    return {"west": west, "south": south, "east": east, "north": north}



def _finite_num(v: Any) -> float:
    try:
        n = float(v)
        return n if math.isfinite(n) else float("nan")
    except Exception:
        return float("nan")


def _sst_c_to_f(v: float) -> float:
    return (float(v) * 9.0 / 5.0) + 32.0


def _ocean_points_from_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize HYCOM/ocean points into a shark/SST mask input.

    The shark model treats finite HYCOM SST ocean points as the required water
    truth gate.  Land/harbor cells and provider-empty cells are rejected; there
    is no shoreline/SST proxy fallback for rendering.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("points") or payload.get("ocean_analysis_points") or payload.get("ocean_points") or payload.get("items") or []
    if not raw and isinstance(payload.get("oceanAnalysisPoints") or payload.get("oceanPoints"), dict):
        raw = payload.get("oceanPoints", {}).get("points") or []
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        lat = _finite_num(row.get("lat") or row.get("latitude"))
        lon = _finite_num(row.get("lon") or row.get("lng") or row.get("longitude"))
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        ov = row.get("ocean_vars") if isinstance(row.get("ocean_vars"), dict) else {}
        sst_f = _finite_num(row.get("sst_f") or row.get("water_temp_f") or row.get("tempF") or ov.get("sst_f"))
        sst_c = _finite_num(row.get("sst") or row.get("sst_c") or row.get("water_temp_c") or ov.get("sst_c"))
        if not math.isfinite(sst_f) and math.isfinite(sst_c):
            # HYCOM SST is Celsius in the existing ocean payloads.
            sst_f = _sst_c_to_f(sst_c)
        current = _finite_num(row.get("current_kt") or row.get("speed_kt") or row.get("current_speed_kt") or ov.get("current_speed_kt") or row.get("current"))
        if not math.isfinite(current):
            u = _finite_num(row.get("u") or row.get("current_u"))
            v = _finite_num(row.get("v") or row.get("current_v"))
            if math.isfinite(u) and math.isfinite(v):
                current = math.hypot(u, v) * 1.94384
        if math.isfinite(sst_f):
            bottom_ft = _finite_num(row.get("bottom_depth_ft"))
            bottom_m = _finite_num(row.get("bottom_depth_m"))
            depth_intel = row.get("depth_intel") if isinstance(row.get("depth_intel"), dict) else {}
            if not math.isfinite(bottom_ft):
                bottom_ft = _finite_num(depth_intel.get("bottom_depth_ft"))
            if not math.isfinite(bottom_m):
                bottom_m = _finite_num(depth_intel.get("bottom_depth_m"))
            if not math.isfinite(bottom_ft) and math.isfinite(bottom_m):
                bottom_ft = bottom_m * 3.28084
            out_row = {"lat": lat, "lon": lon, "sst_f": sst_f, "current_kt": current}
            if math.isfinite(bottom_ft):
                out_row["bottom_depth_ft"] = bottom_ft
                out_row["depth_source"] = row.get("depth_source") or depth_intel.get("source") or "hycom_gated_bathymetry_estimate_v1"
            if math.isfinite(bottom_m):
                out_row["bottom_depth_m"] = bottom_m
            out.append(out_row)
    return out


def _nearest_ocean_sample(ocean_points: list[dict[str, Any]], lat: float, lon: float, max_deg: float) -> tuple[dict[str, Any] | None, float]:
    best = None
    best_d2 = float("inf")
    for pt in ocean_points:
        plat = _finite_num(pt.get("lat"))
        plon = _finite_num(pt.get("lon"))
        if not (math.isfinite(plat) and math.isfinite(plon)):
            continue
        # lon scale is good enough for the small coastal pane/viewport boxes.
        dlat = plat - lat
        dlon = (plon - lon) * max(0.25, math.cos(math.radians(lat)))
        d2 = dlat * dlat + dlon * dlon
        if d2 < best_d2:
            best_d2 = d2
            best = pt
    d = math.sqrt(best_d2) if best else float("inf")
    return (best if d <= max_deg else None), d


def _is_probably_ocean_from_proxy(lat: float, lon: float) -> bool:
    # Positive distance in _shoreline_proxy is offshore-ish.  Allow a small
    # negative margin for beaches, piers, docks, and geocoder/coastline error.
    signed, dist, _access = _shoreline_proxy(lat, lon)
    return signed >= -550.0 and dist <= 22000.0

def _gaussian(x: float, center: float, width: float) -> float:
    if width <= 0:
        return 0.0
    return math.exp(-0.5 * ((x - center) / width) ** 2)


def _socal_prior(lat: float, lon: float) -> float:
    # Strongest for Southern California/Baja-edge nearshore. Keeps global views
    # honest by showing "not enough regional prior" instead of fake worldwide hot spots.
    if lon < SOCAL_BBOX["west"] - 2 or lon > SOCAL_BBOX["east"] + 2 or lat < SOCAL_BBOX["south"] - 2 or lat > SOCAL_BBOX["north"] + 2:
        return 0.18
    return 0.72 + 0.18 * _gaussian(lat, 33.2, 1.0)


def _shoreline_proxy(lat: float, lon: float) -> tuple[float, float, str]:
    """Approximate SoCal shoreline/dock/pier reach without external geometry.

    Positive distance is offshore-ish; negative is landward-ish.  The synthetic
    coastline bends from San Diego toward LA/SB and is good enough for a cache
    contour starter until a real shoreline/pier/dock dataset is wired.
    """
    coast_lon = -117.18 - 0.64 * _clamp(lat - 32.5, 0.0, 2.4) + 0.10 * math.sin((lat - 32.2) * 3.1)
    deg_lon_m = max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    dist_m = (lon - coast_lon) * deg_lon_m
    abs_m = abs(dist_m)
    access = "shore"
    if 150.0 <= abs_m <= 1700.0 and (abs(math.sin(lat * 12.0 + lon * 4.0)) > 0.72):
        access = "pier"
    elif abs_m <= 1100.0 and (abs(math.sin(lat * 17.0 - lon * 2.3)) > 0.78):
        access = "dock"
    elif abs_m > 1800.0:
        access = "nearshore-ocean"
    return dist_m, abs_m, access


def _depth_proxy_ft(distance_from_shore_m: float, lat: float, lon: float) -> float:
    # Gentle sandy shelf plus local texture.  Clamped for pier/dock/shore mode.
    base = 4.0 + max(0.0, distance_from_shore_m) / 55.0
    texture = 5.5 * (0.5 + 0.5 * math.sin(lat * 8.0 + lon * 5.0))
    return _clamp(base + texture, 2.0, 95.0)


def _sst_proxy_f(lat: float, lon: float) -> float:
    # Warm south/east, cooler north/offshore texture.  Real SST can replace this
    # provider later through the cache builder without touching renderer code.
    return 67.4 - 1.15 * (lat - 33.0) + 0.22 * (lon + 118.0) + 1.8 * math.sin((lat + lon) * 2.2)


def _current_proxy_kt(lat: float, lon: float) -> float:
    return _clamp(0.22 + 0.38 * (0.5 + 0.5 * math.sin(lat * 5.3 - lon * 3.9)), 0.05, 1.4)


def _wind_proxy_kt(lat: float, lon: float) -> float:
    return _clamp(5.0 + 9.0 * (0.5 + 0.5 * math.sin(lat * 2.7 + lon * 4.4)), 2.0, 24.0)


def _leopard_slot_score(lat: float, lon: float, ocean_sample: dict[str, Any] | None = None, *, sst_mask_source: str = "proxy") -> tuple[float, dict[str, Any]]:
    dist_signed, dist_m, access = _shoreline_proxy(lat, lon)
    bottom_ft = _depth_proxy_ft(dist_m, lat, lon)
    depth_source = "shoreline_proxy"
    sst_f = _sst_proxy_f(lat, lon)
    current_kt = _current_proxy_kt(lat, lon)
    if isinstance(ocean_sample, dict):
        sample_sst_f = _finite_num(ocean_sample.get("sst_f"))
        sample_current = _finite_num(ocean_sample.get("current_kt"))
        sample_bottom_ft = _finite_num(ocean_sample.get("bottom_depth_ft"))
        if not math.isfinite(sample_bottom_ft):
            sample_bottom_m = _finite_num(ocean_sample.get("bottom_depth_m"))
            if math.isfinite(sample_bottom_m):
                sample_bottom_ft = sample_bottom_m * 3.28084
        if math.isfinite(sample_sst_f):
            sst_f = sample_sst_f
        if math.isfinite(sample_current):
            current_kt = sample_current
        if math.isfinite(sample_bottom_ft):
            bottom_ft = _clamp(sample_bottom_ft, 2.0, 6000.0)
            depth_source = str(ocean_sample.get("depth_source") or ocean_sample.get("depth_intel", {}).get("source") or "hycom_gated_bathymetry_estimate_v1")
    wind_kt = _wind_proxy_kt(lat, lon)

    # 36-42 inch leopard sharks: legal-slot, sandy nearshore/bay-mouth/pier/dock.
    sst_score = _gaussian(sst_f, 67.5, 5.0)
    depth_score = _gaussian(bottom_ft, 18.0, 15.0)
    shore_score = _gaussian(dist_m, 650.0, 900.0)
    current_score = _gaussian(current_kt, 0.42, 0.38)
    wind_score = 1.0 - _clamp((wind_kt - 11.0) / 18.0, 0.0, 0.65)
    sand_edge_bias = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(lat * 10.5 + lon * 7.8))
    pier_dock_boost = 1.12 if access in {"pier", "dock"} else 1.0
    prior = _socal_prior(lat, lon)
    raw = prior * sst_score * (0.45 + 0.55 * depth_score) * (0.35 + 0.65 * shore_score) * (0.45 + 0.55 * current_score) * wind_score * (0.70 + 0.30 * sand_edge_bias) * pier_dock_boost
    prob = _clamp(raw, 0.0, 0.98)
    # Under-36 nursery caution is highest shallow/sheltered; do not target.
    nursery = _clamp(_gaussian(bottom_ft, 7.0, 7.0) * _gaussian(dist_m, 300.0, 600.0) * prior, 0.0, 0.95)
    large = _clamp(prob * (0.45 + 0.55 * _gaussian(bottom_ft, 30.0, 20.0)) * (0.75 + 0.25 * current_score), 0.0, 0.90)
    return prob, {
        "sst_f": round(sst_f, 2),
        "current_kt": round(current_kt, 2),
        "wind_kt": round(wind_kt, 2),
        "distance_to_shore_yd": round(dist_m * 1.09361, 1),
        "signed_distance_to_shore_m": round(dist_signed, 1),
        "bottom_depth_ft": round(bottom_ft, 1),
        "depth_source": depth_source,
        "target_swim_depth_ft": [round(max(1.0, bottom_ft * 0.18), 1), round(max(3.0, min(bottom_ft - 1.0, bottom_ft * 0.68)), 1)],
        "shoreline_bias": round(shore_score, 3),
        "sand_edge_bias": round(sand_edge_bias, 3),
        "pier_dock_bias": 0.85 if access in {"pier", "dock"} else 0.15,
        "fishing_mode": access,
        "sst_mask_source": sst_mask_source,
        "sst_mask_valid": bool(sst_mask_source != "rejected"),
        "nursery_caution_score": round(nursery, 3),
        "large_slot_score": round(large, 3),
    }


def _tiger_watch_score(lat: float, lon: float, metrics: dict[str, Any]) -> float:
    # Tiger is an edge-of-range warm-water anomaly watch for SoCal, not a normal catch layer.
    sst = float(metrics.get("sst_f", 62.0))
    current = float(metrics.get("current_kt", 0.0))
    offshore = _clamp(float(metrics.get("distance_to_shore_yd", 999.0)) / 3500.0, 0.0, 1.0)
    warm = _clamp((sst - 68.5) / 8.0, 0.0, 1.0)
    anomaly = warm * (0.55 + 0.45 * _gaussian(current, 0.75, 0.45)) * (0.55 + 0.45 * offshore) * _socal_prior(lat, lon) * 0.45
    return _clamp(anomaly, 0.0, 0.45)


def _sand_watch_score(lat: float, lon: float, metrics: dict[str, Any]) -> float:
    # "Sand shark" is handled as a generic sandy nearshore shark watch / mis-ID caution.
    shallow = _gaussian(float(metrics.get("bottom_depth_ft", 30.0)), 14.0, 12.0)
    sand = float(metrics.get("sand_edge_bias", 0.5))
    shore = float(metrics.get("shoreline_bias", 0.5))
    return _clamp(0.62 * shallow * sand * shore * _socal_prior(lat, lon), 0.0, 0.72)


def _grid_for_bbox(bbox: dict[str, float], target: int = 28, ocean_payload: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    west, south, east, north = bbox["west"], bbox["south"], bbox["east"], bbox["north"]
    span_x = max(0.05, east - west)
    span_y = max(0.05, north - south)
    nx = max(8, min(42, int(target * (span_x / max(span_x, span_y))) + 4))
    ny = max(8, min(42, int(target * (span_y / max(span_x, span_y))) + 4))
    ocean_points = _ocean_points_from_payload(ocean_payload)
    # The nearest valid SST/current cell must be close enough to prove water.
    # Radius expands a little for low-res/padded world cache, but still rejects
    # inland/land cells when HYCOM points exist.
    cell_diag = max(span_x / max(1, nx), span_y / max(1, ny))
    sst_radius = _clamp(cell_diag * 2.4, 0.06, 0.42)
    pts: list[dict[str, Any]] = []
    rejected_land = 0
    proxy_ocean = 0
    sst_masked = 0
    if not ocean_points:
        mask_meta = {
            "method": "hycom_valid_sst_required",
            "sst_land_mask_enabled": True,
            "input_ocean_points": 0,
            "valid_score_points": 0,
            "rejected_land_or_no_sst": nx * ny,
            "sst_masked_points": 0,
            "proxy_ocean_points": 0,
            "grid_shape": [ny, nx],
            "sst_radius_deg": round(sst_radius, 5),
            "policy": "strict_hycom_sst_only_no_proxy_no_fallback",
            "status": "waiting_for_hycom_sst",
        }
        return [], mask_meta
    for iy in range(ny):
        lat = south + (iy + 0.5) * span_y / ny
        for ix in range(nx):
            lon = west + (ix + 0.5) * span_x / nx
            ocean_sample, sample_d = _nearest_ocean_sample(ocean_points, lat, lon, sst_radius)
            if not ocean_sample:
                rejected_land += 1
                continue
            mask_source = "hycom_valid_sst_neighbor"
            sst_masked += 1
            leopard, metrics = _leopard_slot_score(lat, lon, ocean_sample, sst_mask_source=mask_source)
            tiger = _tiger_watch_score(lat, lon, metrics)
            sand = _sand_watch_score(lat, lon, metrics)
            pts.append({
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "lng": round(lon, 6),
                "leopard_slot_score": round(leopard, 4),
                "tiger_watch_score": round(tiger, 4),
                "sand_shark_watch_score": round(sand, 4),
                "score": round(max(leopard, tiger, sand), 4),
                "metrics": metrics,
                "land_mask": {
                    "valid": True,
                    "method": mask_source,
                    "nearest_sst_deg": round(sample_d, 5),
                },
            })
    mask_meta = {
        "method": "hycom_valid_sst_neighbor",
        "sst_land_mask_enabled": True,
        "input_ocean_points": len(ocean_points),
        "valid_score_points": len(pts),
        "rejected_land_or_no_sst": rejected_land,
        "sst_masked_points": sst_masked,
        "proxy_ocean_points": proxy_ocean,
        "grid_shape": [ny, nx],
        "sst_radius_deg": round(sst_radius, 5),
        "policy": "strict_hycom_sst_only_no_proxy_no_fallback",
    }
    return pts, mask_meta

def _rect_path(cx: float, cy: float, dx: float, dy: float, altitude: float = 0.0) -> list[dict[str, float]]:
    return [
        {"lat": round(cy - dy, 7), "lng": round(cx - dx, 7), "altitude": altitude},
        {"lat": round(cy - dy, 7), "lng": round(cx + dx, 7), "altitude": altitude},
        {"lat": round(cy + dy, 7), "lng": round(cx + dx, 7), "altitude": altitude},
        {"lat": round(cy + dy, 7), "lng": round(cx - dx, 7), "altitude": altitude},
    ]


def _polygon_hash(path: list[dict[str, float]], salt: str) -> str:
    raw = salt + "|" + ";".join(f"{p['lat']:.5f},{p['lng']:.5f}" for p in path)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:12]


def _cell_contours(points: list[dict[str, Any]], bbox: dict[str, float]) -> list[dict[str, Any]]:
    if not points:
        return []
    lats = sorted({p["lat"] for p in points})
    lons = sorted({p["lon"] for p in points})
    if len(lats) < 2 or len(lons) < 2:
        return []
    dx = abs(lons[1] - lons[0]) * 0.52
    dy = abs(lats[1] - lats[0]) * 0.52
    contours: list[dict[str, Any]] = []
    specs = [
        ("leopard", "primeSlot", "shore-pier-dock-ocean", "leopard_slot_score", 0.42, "outer", "shark-leopard-prime-outer"),
        ("leopard", "primeSlot", "shore-pier-dock-ocean", "leopard_slot_score", 0.58, "inner", "shark-leopard-prime-inner"),
        ("leopard", "primeSlot", "shore-pier-dock-ocean", "leopard_slot_score", 0.72, "core", "shark-leopard-prime-core"),
        ("leopard", "large", "nearshore-ocean", "large_slot_score", 0.45, "large", "shark-leopard-large-ring"),
        ("leopard", "undersize-caution", "nursery-caution", "nursery_caution_score", 0.48, "caution", "shark-leopard-nursery-caution"),
        ("tiger", "warm-anomaly-watch", "nearshore-ocean", "tiger_watch_score", 0.20, "watch", "shark-tiger-warm-watch"),
        ("sand-shark", "sandy-nearshore-watch", "shore-pier-dock", "sand_shark_watch_score", 0.36, "watch", "shark-sand-nearshore-watch"),
        ("leopard", "sst-valid-water", "sst-water-mask", "score", 0.08, "sst-mask", "shark-sst-valid-water-mask"),
    ]
    counters: dict[str, int] = {}
    for p in points:
        metrics = dict(p.get("metrics") or {})
        for species, size_class, mode, score_name, threshold, band, style_key in specs:
            val = metrics.get(score_name) if score_name in metrics else p.get(score_name)
            score = float(val or 0.0)
            if score < threshold:
                continue
            if len([c for c in contours if c.get("style_key") == style_key]) >= 44:
                continue
            key = f"{species}:{size_class}:{band}"
            counters[key] = counters.get(key, 0) + 1
            path = _rect_path(float(p["lon"]), float(p["lat"]), dx, dy, 0.0)
            depth = {
                "bottom_depth_ft": metrics.get("bottom_depth_ft"),
                "target_swim_depth_ft": metrics.get("target_swim_depth_ft"),
                "feeding_zone": "lower-surface-to-midwater" if species == "leopard" else "warm-current-edge-watch",
                "confidence": round(min(0.92, max(0.25, score)), 2),
                "reason": "sandy nearshore/pier/dock access with favorable SST/current" if species != "tiger" else "rare warm-water anomaly watch; low regional confidence",
            }
            legal = {
                "status": "legal_slot_check_local_rules" if size_class in {"primeSlot", "large"} else "caution_do_not_target_undersize_or_nursery",
                "target_size_in": [36, 42] if size_class == "primeSlot" else ([42, None] if size_class == "large" else [0, 35.99]),
                "notes": ["Verify current CDFW rules, MPA boundaries, pier/dock/local closures before fishing.", "Under 36 in leopard shark is caution/no-target in this layer."],
            }
            contours.append({
                "id": f"shark:{species}:{size_class}:{band}:{counters[key]}",
                "species": species,
                "size_class": size_class,
                "fishing_mode": metrics.get("fishing_mode") if mode == "shore-pier-dock-ocean" else mode,
                "probability": round(score, 4),
                "band": band,
                "threshold": threshold,
                "path": path,
                "geometry_hash": _polygon_hash(path, f"{species}:{size_class}:{band}:{threshold}"),
                "metrics": metrics,
                "depth_intel": depth,
                "legal": legal,
                "style_key": style_key,
            })
    contours.sort(key=lambda c: float(c.get("probability") or 0.0), reverse=True)
    return contours[:160]


def shark_intel_payload(bbox: dict[str, Any] | None = None, visible_bbox: dict[str, Any] | None = None, ocean_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    b = _norm_bbox(visible_bbox or bbox)
    points, mask_meta = _grid_for_bbox(b, ocean_payload=ocean_payload)
    contours = _cell_contours(points, b)
    ts = datetime.now(timezone.utc).isoformat()
    return {
        "ok": True,
        "schema": "lftr_shark_intel_v1",
        "source": "lftr_shark_intel_hycom_sst_only_cache_layer",
        "status": "ok" if contours else ("waiting_for_sst_points" if not points else "sst_points_ready_no_shark_threshold"),
        "payload_state": "derived_probability_contours" if contours else ("waiting_for_hycom_sst" if not points else "sst_mask_ready_no_species_threshold"),
        "resolved_time": ts,
        "bbox": b,
        "bbox_used": b,
        "target": {
            "primary_species": "leopard",
            "primary_size_in": [36, 42],
            "secondary": ["tiger warm-water anomaly watch", "sand shark/sandy nearshore watch"],
            "modes": ["shore", "pier", "dock", "nearshore-ocean"],
            "ethics": "legal-slot intel; under-36/nursery zones are caution/no-target overlays",
        },
        "species": {
            "leopard": {"role": "primary legal-slot SoCal model", "target_size_in": [36, 42], "large_size_in": [42, None]},
            "tiger": {"role": "rare migratory/warm-water anomaly watch", "confidence_policy": "low unless warm anomaly and current edge are strong"},
            "sand-shark": {"role": "sandy nearshore shark/mis-ID watch", "confidence_policy": "habitat proxy until species observations are connected"},
        },
        "score_points": points,
        "points": points,
        "contours": contours,
        "polygons": contours,
        "count": len(contours),
        "sst_land_mask": mask_meta,
        "marching_squares": {
            "shared_with_bait": True,
            "server_mode": "marching_square_style_cell_contours_sst_land_masked",
            "frontend_can_recontour_score_points": False,
            "thresholds": [0.20, 0.36, 0.42, 0.58, 0.72],
        },
        "legal_caution": {
            "summary": "Check current CDFW rules, MPAs, pier/dock/local closures before fishing. This layer prioritizes legal-slot leopard shark intel and flags under-36/nursery areas as caution/no-target.",
        },
    }

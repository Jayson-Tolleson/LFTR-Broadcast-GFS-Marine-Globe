from __future__ import annotations

import gzip
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

VIRIDIAN = "#40826D"
_INLAND_WATER_ROUTE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INLAND_WATER_ROUTE_TTL_SEC = 120.0
_INLAND_WATER_MISS_TTL_SEC = 5.0  # fast recheck while source/build tiles are still arriving
INLAND_WATER_CACHE_RETENTION_DAYS = 31
# Inland-water speed policy: filter/classify using full source geometry first.
# These degree buckets are strict per zoom/tier: world=1.0°, regional=0.5°,
# local/harbor=0.25°.  The visible read path must not mix those buckets.
INLAND_LOD_DEGREE = {"harbor": 0.25, "local": 0.25, "regional": 0.5, "world": 1.0}
INLAND_LOD_QUALITY = {"harbor": 95, "local": 90, "regional": 72, "world": 48}
try:
    INLAND_VIEW_TILE_LIMIT = max(24, int(float(os.getenv("LFTR_INLAND_WATER_VIEW_TILE_LIMIT", "96"))))
except Exception:
    INLAND_VIEW_TILE_LIMIT = 96
try:
    INLAND_VIEW_CACHE_TTL_SECONDS = max(10, int(float(os.getenv("LFTR_INLAND_WATER_VIEW_CACHE_TTL_SECONDS", "120"))))
except Exception:
    INLAND_VIEW_CACHE_TTL_SECONDS = 120
INLAND_DISK_CACHE_RETENTION_DAYS = INLAND_WATER_CACHE_RETENTION_DAYS
log = logging.getLogger(__name__)

def _env_float(name: str, fallback: float) -> float:
    try:
        value = float(os.getenv(name, str(fallback)))
        return value if math.isfinite(value) else fallback
    except Exception:
        return fallback


def _env_int(name: str, fallback: int) -> int:
    try:
        value = int(float(os.getenv(name, str(fallback))))
        return value if value > 0 else fallback
    except Exception:
        return fallback


SURFACE_TEMP_CANDIDATES: list[tuple[str, list[str]]] = [
    ("surface", ["t0m", "TMP:surface", "TMP_surface", "skt", "SKT", "t", "TMP", "tmp"]),
    ("2m", ["t2m", "TMP", "t", "tmp"]),
]

# Inland-water policy: request clean source classes and preserve received geometry.
MIN_HIGH_DEF_POLYGON_POINTS = max(8, _env_int("LFTR_INLAND_WATER_MIN_POLYGON_POINTS", 12))
MIN_HIGH_DEF_LINE_POINTS = max(4, _env_int("LFTR_INLAND_WATER_MIN_LINE_POINTS", 8))
# Draw only the most important inland-water features by default.  The high-detail
# NHD/NHDPlus runtime tile tiles can contain thousands of small reaches/ponds; keeping
# the largest 25% by polygon area or line length gives a cleaner map and makes the
# hover/description layer focus on prominent water bodies.
INLAND_WATER_PROMINENCE_FRACTION = 1.0  # deprecated; active path preserves all source-filtered features
INLAND_WATER_MIN_PROMINENT_FEATURES = 1  # deprecated
ACCEPTED_HIGH_DEF_SOURCE_HINTS = ("nhdplus", "3dhp", "nhdwaterbody", "nhdarea", "nhdflowline", "osm", "hydrolakes", "waterbody")
REJECT_SOURCE_HINTS = ("bootstrap", "seed", "demo", "coarse", "approx")
# Source-side filtering now does the heavy lifting.  NHD ArcGIS requests ask
# directly for Lake/Pond, Reservoir, and perennial Stream/River FCODE classes.
# Keep only a cheap server validator as a safety net for mixed service metadata;
# do not run the old dry-channel scoring model on every feature.
INLAND_WATER_DEBUG_DROPPED_LIMIT = _env_int("LFTR_INLAND_WATER_DEBUG_DROPPED_LIMIT", 24)
# Clean Inland Waters allowlist. Current visible mode is LAKES ONLY.
# 39000 = Lake/Pond, 43600 = Reservoir. 46006 streams are intentionally
# withheld until streams are brought back as a separate later layer.
INLAND_LAKES_ONLY = str(os.getenv("NHD_INLAND_LAKES_ONLY", "1")).lower() not in {"0", "false", "no", "off"}
NHD_LAKE_ONLY_FCODES = {"39000", "43600"}
NHD_LAKE_ONLY_FTYPES = {"390", "436"}
NHD_LAKE_ONLY_FCODE_PREFIXES = ("390", "436")
NHD_ALLOWED_CLEAN_WATER_FCODES = {"39000", "43600"} if INLAND_LAKES_ONLY else {"39000", "43600", "46006"}
NHD_REJECT_CLEAN_WATER_FCODES = {"46003", "46007", "36100", "33600", "55800"}
WATER_PRESENT_TERMS = ("lake", "pond", "reservoir", "waterbody", "water body", "lakepond") if INLAND_LAKES_ONLY else ("lake", "pond", "reservoir", "river", "stream", "waterbody", "water body", "lakepond")
DRY_OR_OLD_CHANNEL_TERMS = (
    "ephemeral", "intermittent", "dry", "wash", "arroyo", "wadi", "playa",
    "sink", "drainage", "storm", "stormwater", "gully", "gulch", "draw",
    "ravine", "connector", "artificial path", "pipeline", "underground",
    "canal/ditch", "ditch", "canal", "aqueduct", "abandoned", "historical", "old channel",
)
# Inland-water cache is intentionally viewport/tile-only.
# Active cache root: static/data/nhdplus_hr/tiles/{world,regional,local,harbor}/...json.gz

# Inland water geometry modes:
# - geometry=coarse: simplified real USGS/NHD world overview; no fake/seed data.
# - geometry=vector: detailed real vertices for regional/local/harbor and lake bait.
# Coarse/seed fallback geometry is intentionally not shipped.


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        out = float(v)
        return out if math.isfinite(out) else fallback
    except Exception:
        return fallback


def _bbox_dict(bbox: dict[str, float] | None) -> dict[str, float]:
    b = bbox or {"west": -180, "south": -80, "east": 180, "north": 80}
    return {"west": _num(b.get("west"), -180), "south": _num(b.get("south"), -80), "east": _num(b.get("east"), 180), "north": _num(b.get("north"), 80)}


def _point_in_bbox(lat: float, lng: float, bbox: dict[str, float], pad: float = 0.0) -> bool:
    west, east = bbox["west"] - pad, bbox["east"] + pad
    south, north = bbox["south"] - pad, bbox["north"] + pad
    return south <= lat <= north and west <= lng <= east


def _path_intersects(path: list[dict[str, Any]], bbox: dict[str, float]) -> bool:
    if not path:
        return False
    for p in path:
        if _point_in_bbox(_num(p.get("lat")), _num(p.get("lng", p.get("lon"))), bbox, pad=0.05):
            return True
    lats = [_num(p.get("lat"), float("nan")) for p in path]
    lngs = [_num(p.get("lng", p.get("lon")), float("nan")) for p in path]
    lats = [x for x in lats if math.isfinite(x)]
    lngs = [x for x in lngs if math.isfinite(x)]
    if not lats or not lngs:
        return False
    return not (max(lats) < bbox["south"] or min(lats) > bbox["north"] or max(lngs) < bbox["west"] or min(lngs) > bbox["east"])


def _normalize_path(raw: Any) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for p in raw or []:
        if isinstance(p, dict):
            lat = _num(p.get("lat"), float("nan"))
            lng = _num(p.get("lng", p.get("lon")), float("nan"))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            lng = _num(p[0], float("nan"))
            lat = _num(p[1], float("nan"))
        else:
            continue
        if math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180:
            prev = out[-1] if out else None
            if not prev or abs(prev["lat"] - lat) > 1e-8 or abs(prev["lng"] - lng) > 1e-8:
                out.append({"lat": lat, "lng": lng})
    if len(out) >= 2 and abs(out[0]["lat"] - out[-1]["lat"]) < 1e-8 and abs(out[0]["lng"] - out[-1]["lng"]) < 1e-8:
        out.pop()
    return out


def _lod_tier(scene_tier: str | None, lod: str | None = None) -> str:
    raw_lod = str(lod or "auto").lower()
    raw = str(scene_tier or "world").lower() if raw_lod in {"auto", ""} else raw_lod
    if raw in {"harbor", "close", "micro", "2048", "high", "0.25", "025"}:
        return "harbor"
    if raw in {"local", "coastal", "1024", "full"}:
        return "local"
    if raw in {"regional", "medium", "512", "0.5", "05"}:
        return "regional"
    if raw in {"world", "coarse", "256", "auto", "1.0", "1"}:
        return "world"
    return "world"


def _lod_max_points(scene_tier: str | None, lod: str | None = None) -> int:
    tier = _lod_tier(scene_tier, lod)
    return {"world": 1200, "regional": 2400, "local": 5000, "harbor": 9000}.get(tier, 1200)


def _lod_grid(scene_tier: str | None, lod: str | None = None) -> int:
    tier = _lod_tier(scene_tier, lod)
    return {"world": 1024, "regional": 2048, "local": 4096, "harbor": 8192}.get(tier, 1024)


def _lod_degree(scene_tier: str | None, lod: str | None = None) -> float:
    return float(INLAND_LOD_DEGREE.get(_lod_tier(scene_tier, lod), 1.0))


def _bbox_tile_degree(tb: list[float] | tuple[float, float, float, float] | dict[str, Any]) -> float | None:
    try:
        if isinstance(tb, dict):
            w, s, e, n = float(tb["west"]), float(tb["south"]), float(tb["east"]), float(tb["north"])
        else:
            w, s, e, n = float(tb[0]), float(tb[1]), float(tb[2]), float(tb[3])
        width = abs(e - w)
        height = abs(n - s)
        deg = max(width, height)
        return deg if math.isfinite(deg) and deg > 0 else None
    except Exception:
        return None


def _strict_tile_degree_matches(tb: list[float] | tuple[float, float, float, float] | dict[str, Any], target_deg: float) -> bool:
    actual = _bbox_tile_degree(tb)
    if actual is None:
        return False
    # Tile bboxes are exact degree buckets in this cache.  Allow a tiny epsilon
    # for JSON float roundoff, but do not allow .25° or .5° to satisfy world 1°.
    return abs(actual - float(target_deg)) <= max(1e-6, float(target_deg) * 0.001)


def _lod_quality_rank(scene_tier: str | None, lod: str | None = None) -> int:
    return int(INLAND_LOD_QUALITY.get(_lod_tier(scene_tier, lod), 48))


def _rdp_distance(p: dict[str, float], a: dict[str, float], b: dict[str, float]) -> float:
    # Equirectangular-ish distance in degrees. This is for render simplification
    # only; filtering/classification always happens on source geometry first.
    ax, ay = float(a["lng"]), float(a["lat"])
    bx, by = float(b["lng"]), float(b["lat"])
    px, py = float(p["lng"]), float(p["lat"])
    dx, dy = bx - ax, by - ay
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy)


def _rdp_simplify(path: list[dict[str, float]], tolerance_deg: float) -> list[dict[str, float]]:
    if len(path) <= 2 or tolerance_deg <= 0:
        return path
    best_idx = 0
    best_dist = -1.0
    a, b = path[0], path[-1]
    for idx in range(1, len(path) - 1):
        dist = _rdp_distance(path[idx], a, b)
        if dist > best_dist:
            best_dist = dist
            best_idx = idx
    if best_dist > tolerance_deg:
        left = _rdp_simplify(path[: best_idx + 1], tolerance_deg)
        right = _rdp_simplify(path[best_idx:], tolerance_deg)
        return left[:-1] + right
    return [path[0], path[-1]]


def _render_tolerance_deg(path: list[dict[str, float]], scene_tier: str | None, lod: str | None = None, *, is_line: bool = False) -> float:
    """RDP tolerance for *draw* geometry only.

    The source path remains untouched for lake bounds, masking, and future
    lake-environment requests.  This tolerance only lowers Google Maps 3D draw
    cost for the visible teal shoreline stroke.
    """
    tier = _lod_tier(scene_tier, lod)
    bounds = _path_bounds(path)
    span = 0.0
    if bounds:
        south, west, north, east = bounds
        span = max(abs(east - west), abs(north - south))
    floor_by_tier = {
        "world": 0.0100,
        "regional": 0.0040,
        "local": 0.0012,
        "harbor": 0.00045,
    }
    # Bigger lakes can tolerate slightly more simplification while retaining
    # their recognizable shoreline shape.
    span_tol = span * (0.010 if tier in {"world", "regional"} else 0.0045)
    return max(floor_by_tier.get(tier, 0.0012), span_tol * (0.85 if is_line else 1.0))


def _render_point_cap(scene_tier: str | None, lod: str | None = None, *, is_line: bool = False) -> int:
    tier = _lod_tier(scene_tier, lod)
    caps = {
        "world": 54,
        "regional": 86,
        "local": 144,
        "harbor": 220,
    }
    return caps.get(tier, 144 if not is_line else 120)


def _cap_path_points(path: list[dict[str, float]], max_points: int) -> list[dict[str, float]]:
    if len(path) <= max_points or max_points < 3:
        return path
    step = max(1, math.ceil(len(path) / max_points))
    kept = path[::step]
    if path[-1] not in kept:
        kept.append(path[-1])
    return kept[:max_points]


def _thin_path_for_lod(path: list[dict[str, float]], scene_tier: str | None, lod: str | None = None, *, is_line: bool = False) -> list[dict[str, float]]:
    """Return draw geometry for Inland Waters.

    Shoreline quality now wins over old draw thinning.  We no longer simplify
    or cap vertices on the way into the client; tile/lake quantity filtering
    controls world-scale cost instead.  This keeps islands/coves/shorelines from
    turning into jagged angular strokes while still letting world view show only
    the largest lake portion per selected tile.
    """
    return list(path or [])


def _path_bounds(path: list[dict[str, float]]) -> tuple[float, float, float, float] | None:
    lats = [p["lat"] for p in path if math.isfinite(float(p.get("lat", float("nan"))))]
    lngs = [p["lng"] for p in path if math.isfinite(float(p.get("lng", float("nan"))))]
    if not lats or not lngs:
        return None
    return min(lats), min(lngs), max(lats), max(lngs)


def _path_length_km(path: list[dict[str, float]]) -> float:
    if len(path) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(path, path[1:]):
        lat1 = math.radians(float(a["lat"])); lat2 = math.radians(float(b["lat"]))
        lon1 = math.radians(float(a["lng"])); lon2 = math.radians(float(b["lng"]))
        mean_lat = (lat1 + lat2) * 0.5
        x = (lon2 - lon1) * math.cos(mean_lat)
        y = lat2 - lat1
        total += 6371.0088 * math.sqrt(x * x + y * y)
    return total


def _polygon_area_sqkm(path: list[dict[str, float]]) -> float:
    if len(path) < 3:
        return 0.0
    lat0 = math.radians(sum(float(p["lat"]) for p in path) / len(path))
    pts: list[tuple[float, float]] = []
    for p in path:
        x = 6371.0088 * math.radians(float(p["lng"])) * math.cos(lat0)
        y = 6371.0088 * math.radians(float(p["lat"]))
        pts.append((x, y))
    area = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5



def _feature_text(item: dict[str, Any]) -> str:
    keys = (
        "kind", "name", "ftype", "FType", "fcode", "FCode", "source_class", "source",
        "source_path", "gnis_name", "gnis_id", "feature_type", "nhdplus_comid",
        "visibility", "flow_type", "water_presence", "hydro_status",
    )
    return " ".join(str(item.get(k, "")) for k in keys if item.get(k) is not None).lower()


def _nhd_fcode(item: dict[str, Any]) -> str:
    raw = item.get("fcode", item.get("FCode", ""))
    try:
        return str(int(float(raw)))
    except Exception:
        return str(raw or "").strip()


def _nhd_ftype(item: dict[str, Any]) -> str:
    raw = item.get("ftype", item.get("FType", item.get("FTYPE", item.get("kind", ""))))
    try:
        return str(int(float(raw)))
    except Exception:
        return str(raw or "").strip()


def _lake_kind_from_codes(item: dict[str, Any]) -> str:
    """Normalize NHD waterbody FTYPE/FCODE into app-visible lake classes.

    ArcGIS layer 12 is queried with FTYPE=390 OR FTYPE=436, but the
    attrs often arrive as numeric strings ("390", "436").  The renderer and
    bait code need semantic names or reservoirs/lakes fall through to the
    river/channel branch and look like a Colorado River zone.
    """
    ftype = _nhd_ftype(item)
    fcode = _nhd_fcode(item)
    text = _feature_text(item)
    if ftype == "436" or fcode.startswith("436") or "reservoir" in text:
        return "reservoir"
    if ftype == "390" or fcode.startswith("390") or "lakepond" in text or "lake" in text or "pond" in text:
        return "lake"
    return str(item.get("kind") or "waterbody").lower()


def _water_presence_score(item: dict[str, Any], *, is_line: bool = False) -> dict[str, Any]:
    """Cheap safety validator after source-side clean-water requests.

    The ArcGIS fetcher now requests only Lake/Pond, Reservoir, and perennial
    Stream/River classes, so the server should not spend time scoring every dry
    drainage possibility.  This function only catches obvious source leakage and
    annotates why a feature was kept/dropped.
    """
    path = _normalize_path(item.get("path") or [])
    text = _feature_text(item)
    fcode = _nhd_fcode(item)
    ftype = _nhd_ftype(item)
    reasons: list[str] = []

    if INLAND_LAKES_ONLY and (fcode in NHD_LAKE_ONLY_FCODES or ftype in NHD_LAKE_ONLY_FTYPES or any(fcode.startswith(prefix) for prefix in NHD_LAKE_ONLY_FCODE_PREFIXES)):
        allowed = True
        reasons.append(f"allowed_lake_family_fcode_{fcode}_ftype_{ftype}")
    elif fcode in NHD_ALLOWED_CLEAN_WATER_FCODES:
        allowed = True
        reasons.append(f"allowed_fcode_{fcode}")
    else:
        allowed = any(term in text for term in WATER_PRESENT_TERMS)
        if allowed:
            reasons.append("allowed_clean_water_term")

    dry_terms = [term for term in DRY_OR_OLD_CHANNEL_TERMS if term in text]
    if fcode in NHD_REJECT_CLEAN_WATER_FCODES:
        allowed = False
        reasons.append(f"rejected_fcode_{fcode}")
    if dry_terms:
        allowed = False
        reasons.append("rejected_term=" + ",".join(dry_terms[:4]))

    # Preserve the basic high-detail geometry floor.  This is not a hydrology
    # classifier; it simply prevents malformed slivers from becoming map objects.
    min_pts = MIN_HIGH_DEF_LINE_POINTS if is_line else MIN_HIGH_DEF_POLYGON_POINTS
    if len(path) < min_pts:
        allowed = False
        reasons.append(f"too_few_vertices_{len(path)}_lt_{min_pts}")

    disposition = "active_water" if allowed else "dropped_not_lake_pond_reservoir_river_stream"
    return {
        "score": 100.0 if allowed else 0.0,
        "disposition": disposition,
        "fcode": fcode,
        "reasons": reasons[:8],
        "source_path_points": len(path),
        "source_metric": round(_path_length_km(path) if is_line else _polygon_area_sqkm(path), 5),
    }

def _accepted_high_def_item(item: dict[str, Any], *, is_line: bool = False) -> bool:
    # Lakes-only mode is the current Inland Waters contract.  This also filters
    # older cached runtime tiles that may still contain stream/river flowlines.
    path = _normalize_path(item.get("path") or [])
    if INLAND_LAKES_ONLY and is_line:
        return False
    if len(path) < (2 if is_line else 3):
        return False
    if INLAND_LAKES_ONLY:
        text = _feature_text(item)
        # New runtime tiles are source-filtered at the REST URL itself:
        # MapServer/12 WHERE FTYPE=390 OR FTYPE=436. Trust those polygons and
        # only keep the fallback text/FTYPE checks for old cached tiles.
        if "layer12" in text or "ftype=390" in text or "ftype_390_436" in text or "rest_query_layer12" in text:
            return True
        fcode = _nhd_fcode(item)
        ftype = _nhd_ftype(item)
        if fcode in NHD_LAKE_ONLY_FCODES or ftype in NHD_LAKE_ONLY_FTYPES or any(fcode.startswith(prefix) for prefix in NHD_LAKE_ONLY_FCODE_PREFIXES):
            return True
        return any(term in text for term in ("lake", "pond", "reservoir", "waterbody", "water body", "lakepond"))
    return True


def _annotate_geometry(items: list[dict[str, Any]], *, scene_tier: str | None, lod: str | None, is_line: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tier = _lod_tier(scene_tier, lod)
    for item in items:
        raw_path = item.get("path") or []
        path = _normalize_path(raw_path)
        accepted = _accepted_high_def_item(item, is_line=is_line)
        min_len = 2 if is_line else 3
        if len(path) < min_len:
            continue
        annotated = dict(item)
        if INLAND_LAKES_ONLY and not is_line:
            annotated["kind"] = _lake_kind_from_codes(annotated)
            annotated["source_contract"] = "rest_layer12_ftype_390_436_lake_reservoir_polygon"
        # Source geometry is also the render geometry now.  Do not thin vertices
        # on the way in; world-scale cost is controlled by lake quantity per tile.
        render_path = _thin_path_for_lod(path, scene_tier, lod, is_line=is_line)
        bounds = _path_bounds(path)
        annotated["path"] = path
        annotated["render_path"] = render_path
        annotated["source_path_points"] = len(path)
        annotated["render_path_points"] = len(render_path)
        annotated["render_simplified"] = False
        annotated["raw_vertices_preserved"] = True
        annotated["render_vertices_preserved"] = True
        if bounds:
            south, west, north, east = bounds
            annotated["lake_bounds"] = {"west": west, "south": south, "east": east, "north": north}
            annotated["lake_environment_provider"] = {
                "provider": "lake_environment",
                "bbox": [west, south, east, north],
                "request_scope": "lake_bounds_not_viewport",
                "fields": ["surface_temp", "surface_wind_u", "surface_wind_v", "pressure", "humidity"],
            }
        annotated["source_prominence_score"] = round((_path_length_km(path) if is_line else (_polygon_area_sqkm(path) + _path_length_km(path) * 0.015)), 5)
        annotated["lod_grid"] = _lod_grid(scene_tier, lod)
        annotated["lod_tier"] = tier
        annotated["lod_tile_deg"] = _lod_degree(scene_tier, lod)
        annotated["quality_rank"] = _lod_quality_rank(scene_tier, lod)
        annotated["shoreline_truth"] = "source_filtered_raw_vertices_preserved" if accepted else "rejected_low_detail_or_untrusted_source"
        if accepted:
            out.append(annotated)
    return out


def _tile_key_for_feature(item: dict[str, Any], fallback_index: int = 0) -> str:
    tb = item.get("tile_bbox") or item.get("bbox") or item.get("lake_bounds")
    try:
        if isinstance(tb, dict):
            w, s, e, n = float(tb["west"]), float(tb["south"]), float(tb["east"]), float(tb["north"])
        elif isinstance(tb, (list, tuple)) and len(tb) >= 4:
            w, s, e, n = float(tb[0]), float(tb[1]), float(tb[2]), float(tb[3])
        else:
            raise ValueError("no tile bbox")
        return f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}"
    except Exception:
        sp = item.get("source_path") or item.get("tile_path") or item.get("source") or "unknown"
        return f"source:{sp}:{fallback_index}"


def _largest_lake_per_tile(polygons: list[dict[str, Any]], *, scene_tier: str | None, lod: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """At world/coarse scale, keep exactly the largest accepted lake per tile.

    Zoomed-in tiers return all lakes so smaller lakes queue as the user moves
    closer.  The filter runs after raw vertices are preserved and after source
    validation, so the chosen lake portion still carries the real shoreline.
    """
    tier = _lod_tier(scene_tier, lod)
    if tier != "world" or not polygons:
        return polygons, {
            "enabled": False,
            "tier": tier,
            "policy": "all_lakes_queue_at_regional_local_harbor",
            "input": len(polygons),
            "output": len(polygons),
        }
    winners: dict[str, dict[str, Any]] = {}
    dropped = 0
    for idx, item in enumerate(polygons):
        key = _tile_key_for_feature(item, idx)
        metric = _num(item.get("source_prominence_score"), 0.0)
        if metric <= 0:
            metric = _polygon_area_sqkm(item.get("path") or []) + (_path_length_km(item.get("path") or []) * 0.015)
        cur = winners.get(key)
        cur_metric = _num(cur.get("source_prominence_score"), 0.0) if cur else -1.0
        if cur is None or metric > cur_metric:
            if cur is not None:
                dropped += 1
            tagged = dict(item)
            tagged["world_largest_lake_per_tile"] = True
            tagged["world_tile_key"] = key
            tagged["world_tile_rank"] = 1
            tagged["quantity_filter"] = "world_largest_lake_or_lake_portion_per_tile"
            winners[key] = tagged
        else:
            dropped += 1
    kept = sorted(winners.values(), key=lambda x: (str(x.get("world_tile_key") or ""), -_num(x.get("source_prominence_score"), 0.0)))
    return kept, {
        "enabled": True,
        "tier": tier,
        "policy": "world_visible_exactly_largest_lake_or_lake_portion_per_tile; zoom_in_queues_all_lakes",
        "input": len(polygons),
        "output": len(kept),
        "dropped": dropped,
        "tile_count": len(winners),
        "vertices_preserved": True,
    }


def _geometry_diagnostics(polygons: list[dict[str, Any]], lines: list[dict[str, Any]], source: str, scene_tier: str | None, lod: str | None) -> dict[str, Any]:
    counts = [int(x.get("source_path_points") or len(x.get("path") or [])) for x in polygons + lines]
    render_counts = [int(x.get("render_path_points") or len(x.get("path") or [])) for x in polygons + lines]
    avg = (sum(counts) / len(counts)) if counts else 0.0
    lower_source = str(source).lower()
    quality = "high_detail_nhd_ready" if counts and avg >= 60 and not any(h in lower_source for h in REJECT_SOURCE_HINTS) else "no_accepted_high_def_shoreline"
    return {
        "geometry_quality": quality,
        "source_path_points_total": sum(counts),
        "render_path_points_total": sum(render_counts),
        "avg_source_path_points": round(avg, 2),
        "min_source_path_points": min(counts) if counts else 0,
        "max_source_path_points": max(counts) if counts else 0,
        "lod_tier": _lod_tier(scene_tier, lod),
        "lod_grid": _lod_grid(scene_tier, lod),
        "honest_shoreline_mode": "source-filtered high-def water only; raw vertices are preserved for masks and drawing; world view filters quantity to largest lake per tile",
    }


def _read_json_or_gzip(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text())


def _features_from_geojson_data(data: Any, source_path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    polygons: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    features = data.get("features", []) if isinstance(data, dict) else []
    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        name = props.get("gnis_name") or props.get("name") or props.get("Name") or "Unnamed inland water"
        kind = props.get("kind") or props.get("ftype") or props.get("FType") or props.get("type") or "water"
        fcode = props.get("fcode") or props.get("FCode")
        ftype_raw = props.get("ftype") or props.get("FType") or props.get("FTYPE") or kind
        source_class = props.get("source_class") or props.get("source") or "NHDPlus HR GeoJSON high detail"
        base = {
            "kind": str(kind).lower(),
            "name": name,
            "source_class": source_class,
            "fcode": fcode,
            "FType": ftype_raw,
            "ftype": ftype_raw,
            "source_path": source_path,
        }
        if INLAND_LAKES_ONLY:
            base["kind_raw"] = str(kind).lower()
            base["kind"] = _lake_kind_from_codes(base)
        if gtype == "Polygon":
            for ring in coords[:1]:
                p = _normalize_path(ring)
                if len(p) >= 3:
                    polygons.append({**base, "path": p})
        elif gtype == "MultiPolygon":
            for poly in coords:
                if poly:
                    p = _normalize_path(poly[0])
                    if len(p) >= 3:
                        polygons.append({**base, "path": p})
        elif gtype == "LineString":
            p = _normalize_path(coords)
            if len(p) >= 2:
                lines.append({**base, "source_class": source_class or "NHDFlowline high detail", "path": p})
        elif gtype == "MultiLineString":
            for line in coords:
                p = _normalize_path(line)
                if len(p) >= 2:
                    lines.append({**base, "source_class": source_class or "NHDFlowline high detail", "path": p})
    return polygons, lines


def _bbox_intersects_bbox(a: dict[str, float], b: dict[str, float]) -> bool:
    return not (a["east"] < b["west"] or a["west"] > b["east"] or a["north"] < b["south"] or a["south"] > b["north"])


def _runtime_cache_root(static_dir: Path) -> Path:
    """Single active Inland Waters cache root.

    The route reads one deterministic tile manifest only:
      static/data/nhdplus_hr/tiles/index.json
    """
    return static_dir / "data" / "nhdplus_hr" / "tiles"


def _legacy_runtime_tiles_root(static_dir: Path) -> Path:
    return _runtime_cache_root(static_dir)


def _tile_roots_for_bbox(static_dir: Path, bbox: dict[str, float]) -> list[tuple[str, Path]]:
    root = _runtime_cache_root(static_dir)
    if (root / "index.json").exists() or any(root.glob("*/*.json.gz")):
        return [("tiles", root)]
    return []


def _runtime_cache_status(static_dir: Path, bbox: dict[str, float] | None = None) -> dict[str, Any]:
    b = _bbox_dict(bbox) if bbox else None
    root = _runtime_cache_root(static_dir)
    index = root / "index.json"
    tile_count = 0
    bytes_gz = 0
    discovered_gz_tiles = 0
    if index.exists():
        try:
            data = json.loads(index.read_text())
            tiles = data.get("tiles", []) if isinstance(data, dict) else []
            tile_count = len(tiles)
        except Exception:
            tile_count = 0
    if root.exists():
        for f in root.rglob("*.json.gz"):
            try:
                discovered_gz_tiles += 1
                bytes_gz += f.stat().st_size
            except Exception:
                pass
    return {
        "status": "ok" if (index.exists() or discovered_gz_tiles > 0) else "empty",
        "mode": "single_viewport_runtime_tile_cache",
        "bbox": [b["west"], b["south"], b["east"], b["north"]] if b else None,
        "root": str(root),
        "index": str(index),
        "installed": index.exists() or discovered_gz_tiles > 0,
        "index_exists": index.exists(),
        "tiles": tile_count,
        "discovered_gz_tiles": discovered_gz_tiles,
        "bytes_gz": bytes_gz,
        "retention_days": INLAND_WATER_CACHE_RETENTION_DAYS,
        "accepted_runtime_sources": ["tiles/index.json", "tiles/**/*.json.gz"],
        "note": "Viewport bbox + active LOD select only the shared runtime tile cache. Reads never launch builders.",
    }


def _nhdplus_tiles_root(static_dir: Path) -> Path:
    return _runtime_cache_root(static_dir)


def _high_def_source_installed(static_dir: Path, bbox: dict[str, float] | None = None) -> bool:
    root = static_dir / "data" / "nhdplus_hr"
    b = _bbox_dict(bbox) if bbox else None
    if b and _tile_roots_for_bbox(static_dir, b):
        return True
    if (root / "tiles" / "index.json").exists():
        return True
    for p in (
        root / "inland_water.geojson",
        root / "inland_water.geojson.gz",
    ):
        if p.exists():
            return True
    return False


def _tile_candidates_from_manifest(static_dir: Path, bbox: dict[str, float], tier: str, max_tiles: int = 24, tier_cascade: list[str] | None = None) -> tuple[list[Path], list[dict[str, Any]], dict[str, float] | None]:
    """Return installed NHDPlus/state-cache tile squares intersecting bbox.

    Selection is viewport-balanced, not center-biased: collect all intersecting
    candidates, bucket them into a 4x4 viewport grid, then round-robin through
    buckets so small `max_tiles` batches cover the visible viewport first.
    """
    candidates: list[tuple[Path, dict[str, Any], dict[str, float]]] = []
    seen: set[str] = set()
    tiers = tier_cascade or [tier]
    target_tier = _lod_tier(tier, None)
    target_deg = float(INLAND_LOD_DEGREE.get(target_tier, 1.0))

    def add_candidate(root_label: str, root: Path, rel: str, tb: list[float] | tuple[float, float, float, float], lod: str, extra: dict[str, Any] | None = None) -> None:
        path = root / rel
        key = str(path.resolve())
        if key in seen or not path.exists():
            return
        tile_bbox = {"west": float(tb[0]), "south": float(tb[1]), "east": float(tb[2]), "north": float(tb[3])}
        if not _bbox_intersects_bbox(tile_bbox, bbox):
            return
        actual_deg = _bbox_tile_degree(tile_bbox)
        if not _strict_tile_degree_matches(tile_bbox, target_deg):
            return
        seen.add(key)
        meta = {
            "root": root_label,
            "lod": lod,
            "path": rel,
            "full_path": str(path),
            "bbox": [tile_bbox["west"], tile_bbox["south"], tile_bbox["east"], tile_bbox["north"]],
            "actual_tile_deg": actual_deg,
            "target_tile_deg": target_deg,
            "strict_lod_match": True,
            **(extra or {}),
        }
        candidates.append((path, meta, tile_bbox))

    roots = _tile_roots_for_bbox(static_dir, bbox)
    if not roots:
        root = _nhdplus_tiles_root(static_dir)
        if root.exists():
            roots = [("global", root)]

    for root_label, root in roots:
        manifest = root / "index.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text())
                tiles = data.get("tiles", []) if isinstance(data, dict) else []
                for tile in tiles:
                    if not isinstance(tile, dict):
                        continue
                    tb = tile.get("bbox")
                    rel = tile.get("path")
                    lod = _lod_tier(str(tile.get("lod") or tile.get("tier") or tier).lower(), None)
                    if not tb or not rel or (lod not in tiers and lod != "auto"):
                        continue
                    add_candidate(root_label, root, str(rel), tb, lod, {"polygons": tile.get("polygons", 0), "lines": tile.get("lines", 0), "bytes_gz": tile.get("bytes_gz", 0), "quality_rank": INLAND_LOD_QUALITY.get(lod, 40), "tile_deg": _bbox_tile_degree(tb) or INLAND_LOD_DEGREE.get(lod, 1.0)})
            except Exception:
                pass
        else:
            for scan_tier in tiers:
                if not (root / scan_tier).exists():
                    continue
                for path in sorted((root / scan_tier).rglob("*.json.gz")):
                    try:
                        data = _read_json_or_gzip(path)
                        tb = data.get("bbox") if isinstance(data, dict) else None
                        if not tb:
                            continue
                        rel = str(path.relative_to(root))
                        add_candidate(root_label, root, rel, tb, scan_tier, {"polygons": len(data.get("polygons") or []), "lines": len(data.get("lines") or []), "bytes_gz": path.stat().st_size, "quality_rank": INLAND_LOD_QUALITY.get(scan_tier, 40), "tile_deg": INLAND_LOD_DEGREE.get(scan_tier, 1.0)})
                    except Exception:
                        continue

    if not candidates:
        return [], [], None

    # Round-robin by viewport bucket.  Higher-quality/smaller-degree tiles sort
    # first inside each bucket, but every bucket gets a chance before a central
    # cluster consumes the whole max_tiles budget.
    w, s, e, n = float(bbox["west"]), float(bbox["south"]), float(bbox["east"]), float(bbox["north"])
    width = max(1e-9, e - w); height = max(1e-9, n - s)
    buckets: dict[tuple[int, int], list[tuple[int, float, str, Path, dict[str, Any], dict[str, float]]]] = {}
    for path, meta, tb in candidates:
        cx = (tb["west"] + tb["east"]) * 0.5
        cy = (tb["south"] + tb["north"]) * 0.5
        nx = min(0.999999, max(0.0, (cx - w) / width))
        ny = min(0.999999, max(0.0, (cy - s) / height))
        bx = int(nx * 4); by = int(ny * 4)
        q = int(meta.get("quality_rank") or INLAND_LOD_QUALITY.get(str(meta.get("lod") or tier), 40))
        deg = float(meta.get("tile_deg") or INLAND_LOD_DEGREE.get(str(meta.get("lod") or tier), 1.0))
        meta["viewport_bucket"] = [bx, by]
        meta["selection_policy"] = "viewport_4x4_round_robin_high_quality_inside_bucket"
        buckets.setdefault((bx, by), []).append((-q, deg, str(path), path, meta, tb))
    for vals in buckets.values():
        vals.sort(key=lambda x: (x[0], x[1], x[2]))
    bucket_order: list[tuple[int, int]] = []
    for by in range(4):
        xs = range(4) if by % 2 == 0 else range(3, -1, -1)
        for bx in xs:
            if (bx, by) in buckets:
                bucket_order.append((bx, by))
    chosen: list[tuple[Path, dict[str, Any], dict[str, float]]] = []
    while bucket_order and len(chosen) < max_tiles:
        nxt: list[tuple[int, int]] = []
        for key in bucket_order:
            vals = buckets.get(key) or []
            if vals and len(chosen) < max_tiles:
                _q, _deg, _sp, path, meta, tb = vals.pop(0)
                chosen.append((path, meta, tb))
            if vals:
                nxt.append(key)
        bucket_order = nxt

    selected_paths = [x[0] for x in chosen]
    selected_meta = [x[1] for x in chosen]
    if not selected_meta:
        return [], [], None
    union = {
        "west": min(float(t["bbox"][0]) for t in selected_meta),
        "south": min(float(t["bbox"][1]) for t in selected_meta),
        "east": max(float(t["bbox"][2]) for t in selected_meta),
        "north": max(float(t["bbox"][3]) for t in selected_meta),
    }
    for i, meta in enumerate(selected_meta):
        meta["selection_index"] = i
        meta["candidate_count"] = len(candidates)
        meta["selected_count"] = len(selected_meta)
        meta["viewport_grid"] = "4x4"
    return selected_paths, selected_meta, union

def _inland_tier_cascade(tier: str) -> list[str]:
    # Simple Inland Waters rule: the active zoom owns the active degree.
    # world -> 1.0°, regional -> 0.5°, local/harbor -> 0.25°.
    # Do not mix cascade tiers in the visible read path; mixed-degree reads were
    # the source of confusing partial/crossed cache behavior.  The builder may
    # still emit all three cached products, but the route selects one bucket.
    tier = _lod_tier(tier, None)
    return [tier]



def _tile_indices_for_bbox(bbox: dict[str, float], tile_deg: float) -> set[tuple[int, int]]:
    w, s, e, n = float(bbox["west"]), float(bbox["south"]), float(bbox["east"]), float(bbox["north"])
    ix0 = math.floor(w / tile_deg)
    ix1 = math.ceil(e / tile_deg) - 1
    iy0 = math.floor(s / tile_deg)
    iy1 = math.ceil(n / tile_deg) - 1
    return {(ix, iy) for ix in range(ix0, ix1 + 1) for iy in range(iy0, iy1 + 1)}


def _tile_index_for_bbox(tb: dict[str, float], tile_deg: float) -> tuple[int, int]:
    return (math.floor(float(tb["west"]) / tile_deg), math.floor(float(tb["south"]) / tile_deg))


def _degree_aware_coverage(bbox: dict[str, float], selected_tiles: list[dict[str, Any]], scene_tier: str | None, lod: str | None) -> dict[str, Any]:
    target_deg = _lod_degree(scene_tier, lod)
    required = _tile_indices_for_bbox(bbox, target_deg)
    exact: set[tuple[int, int]] = set()
    finer: list[dict[str, Any]] = []
    coarse: list[dict[str, Any]] = []
    for t in selected_tiles:
        tb = t.get("bbox") or []
        if not isinstance(tb, list) or len(tb) != 4:
            continue
        tile_deg = float(t.get("tile_deg") or target_deg)
        tile_bbox = {"west": float(tb[0]), "south": float(tb[1]), "east": float(tb[2]), "north": float(tb[3])}
        if abs(tile_deg - target_deg) < 1e-9:
            exact.add(_tile_index_for_bbox(tile_bbox, target_deg))
        elif tile_deg < target_deg:
            finer.append({"tile_deg": tile_deg, "bbox": tb, "lod": t.get("lod"), "path": t.get("path")})
        else:
            coarse.append({"tile_deg": tile_deg, "bbox": tb, "lod": t.get("lod"), "path": t.get("path")})
    covered = exact
    missing = sorted(required - covered)
    return {
        "target_degree": target_deg,
        "required_target_tiles": len(required),
        "exact_target_hits": len(exact),
        "finer_fallback_rejected_hits": len(finer),
        "coarse_fallback_rejected_hits": len(coarse),
        "missing_target_tiles": len(missing),
        "coverage_percent_strict_active_lod": round((len(covered) / len(required) * 100.0) if required else 100.0, 2),
        "fallback_tiles_do_not_satisfy_target_detail": True,
        "strict_lod_only": True,
        "missing_target_tile_indices_sample": missing[:16],
        "finer_fallback_rejected_sample": finer[:8],
        "coarse_fallback_rejected_sample": coarse[:8],
    }

def _tile_json_gz_features(static_dir: Path, bbox: dict[str, float], scene_tier: str | None, lod: str | None, max_tiles: int = 24) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, list[dict[str, Any]], dict[str, float] | None]:
    tier = _lod_tier(scene_tier, lod)
    cascade = _inland_tier_cascade(tier)
    polys: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    paths, selected_tiles, tile_union_bbox = _tile_candidates_from_manifest(static_dir, bbox, tier, max_tiles=max_tiles, tier_cascade=cascade)
    for path in paths:
        try:
            data = _read_json_or_gzip(path)
        except Exception:
            continue
        tile_bbox = data.get("bbox") if isinstance(data, dict) else None
        tile_lod = _lod_tier(data.get("lod") or data.get("tier") or path.parent.name if isinstance(data, dict) else path.parent.name, None)
        if isinstance(data, dict) and ("polygons" in data or "lines" in data):
            for item in data.get("polygons") or []:
                item = dict(item)
                item.setdefault("source_class", "USGS NHD ArcGIS maximum-detail runtime water tile")
                item.setdefault("source_path", str(path))
                item.setdefault("tile_bbox", tile_bbox)
                item.setdefault("tile_lod", tile_lod)
                item.setdefault("tile_deg", INLAND_LOD_DEGREE.get(tile_lod, 1.0))
                item.setdefault("tile_quality_rank", INLAND_LOD_QUALITY.get(tile_lod, 40))
                polys.append(item)
            for item in data.get("lines") or []:
                item = dict(item)
                item.setdefault("source_class", "USGS NHD ArcGIS maximum-detail runtime water tile")
                item.setdefault("source_path", str(path))
                item.setdefault("tile_bbox", tile_bbox)
                item.setdefault("tile_lod", tile_lod)
                item.setdefault("tile_deg", INLAND_LOD_DEGREE.get(tile_lod, 1.0))
                item.setdefault("tile_quality_rank", INLAND_LOD_QUALITY.get(tile_lod, 40))
                lines.append(item)
        else:
            p, l = _features_from_geojson_data(data, str(path))
            for item in p + l:
                item.setdefault("tile_bbox", tile_bbox)
            polys.extend(p); lines.extend(l)
    if selected_tiles:
        selected_tiles.sort(key=lambda t: int(t.get("quality_rank") or 0), reverse=True)
    return polys, lines, f"runtime_nhd_arcgis_strict_active_lod_json_gz_full_tiles:{len(paths)}:{tier}:{INLAND_LOD_DEGREE.get(tier, 1.0)}deg" if paths else "no_nhdplus_hr_tiles", selected_tiles, tile_union_bbox



def _runtime_cache_signature(static_dir: Path, bbox: dict[str, float]) -> str:
    """Cheap disk signature for the runtime tiles that can affect this bbox.

    The route may be polled while the background NHD builder is appending new
    json.gz tiles and rewriting index.json.  If the in-memory route cache key is
    only the bbox/lod, a partial draw can mask newly written disk tiles for the
    full TTL.  Include index mtimes/counts for affected roots so every completed
    progressive write becomes visible on the next poll/reload.
    """
    parts: list[str] = []
    try:
        roots = _tile_roots_for_bbox(static_dir, bbox)
    except Exception:
        roots = []
    if not roots:
        root = _nhdplus_tiles_root(static_dir)
        roots = [("global", root)] if root.exists() else []
    for label, root in roots:
        index = root / "index.json"
        try:
            st = index.stat()
            count = 0
            try:
                data = json.loads(index.read_text())
                if isinstance(data, dict):
                    count = len(data.get("tiles") or [])
            except Exception:
                count = 0
            parts.append(f"{label}:{int(st.st_mtime)}:{st.st_size}:{count}")
        except Exception:
            # Partial progressive writes may have produced json.gz before an
            # index is visible.  Add a tiny directory signature so a new index or
            # first tile invalidates the miss quickly without scanning file bytes.
            try:
                gz_count = 0
                newest = 0
                for f in root.rglob("*.json.gz"):
                    try:
                        fs = f.stat()
                        gz_count += 1
                        newest = max(newest, int(fs.st_mtime))
                    except Exception:
                        pass
                if gz_count:
                    parts.append(f"{label}:noindex:{newest}:{gz_count}")
            except Exception:
                pass
    return "|".join(parts) or "no-runtime-index"


def _inland_payload_ttl(payload: dict[str, Any]) -> float:
    """Keep incomplete progressive results fresh; keep complete-ish ones longer."""
    try:
        count = int(payload.get("count") or 0)
        cov = payload.get("degree_aware_cache_coverage") or (payload.get("diagnostics") or {}).get("degree_aware_cache_coverage") or {}
        missing = int(cov.get("missing_target_tiles") or 0) if isinstance(cov, dict) else 0
        coarse = int(cov.get("coarse_fallback_rejected_hits") or cov.get("coarse_fallback_hits") or 0) if isinstance(cov, dict) else 0
        if count <= 0 or payload.get("status") in {"cache_miss", "no_high_def_source", "building", "warming"}:
            return _INLAND_WATER_MISS_TTL_SEC
        if missing > 0 or coarse > 0:
            return 20.0
        return _INLAND_WATER_ROUTE_TTL_SEC
    except Exception:
        return 20.0

def _geojson_features(static_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    roots = [
        static_dir / "data" / "nhdplus_hr" / "inland_water.geojson",
        static_dir / "data" / "nhdplus_hr" / "inland_water.geojson.gz",
    ]
    polygons: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    used: list[str] = []
    for path in roots:
        if not path.exists():
            continue
        try:
            data = _read_json_or_gzip(path)
            p, l = _features_from_geojson_data(data, str(path))
        except Exception:
            continue
        polygons.extend(p); lines.extend(l); used.append(str(path))
    if used:
        return polygons, lines, ";".join(used)
    return [], [], "runtime_nhd_arcgis_tiles_missing"


def inland_water_payload(static_dir: Path, bbox: dict[str, float] | None, *, source: str = "auto", geometry: str = "vector", lod: str = "auto", scene_tier: str | None = None, max_tiles: int = 24) -> dict[str, Any]:
    b = _bbox_dict(bbox)
    geometry_mode = "coarse" if str(geometry or "").lower() in {"coarse", "overview", "world_coarse"} else "vector"
    if not _high_def_source_installed(static_dir, b):
        return {
            "ok": True,
            "status": "cache_miss",
            "source": "runtime_nhd_arcgis_tiles_missing_index_missing",
            "source_path": "none",
            "bbox": [b["west"], b["south"], b["east"], b["north"]],
            "query_bbox": [b["west"], b["south"], b["east"], b["north"]],
            "tile_bbox": None,
            "tile_square_mode": "index_missing_instant_no_data",
            "selected_tiles": [],
                "runtime_cache_status": _runtime_cache_status(static_dir, b),
            "polygons": [],
            "lines": [],
            "temperature_points": [],
            "temperature_point_count": 0,
            "count": 0,
            "cache": {"hit": False, "mode": "instant_manifest_missing", "ttl_seconds": int(_INLAND_WATER_MISS_TTL_SEC), "retention_days": INLAND_WATER_CACHE_RETENTION_DAYS},
            "message": "No matching runtime water tile is ready yet. A real USGS NHD ArcGIS viewport tile build may be queued; partial tiles will draw as they complete.",
            "geometry_mode": geometry_mode,
            "contract": "lftr_inland_water_v15_raw_vertices_world_largest_lake_per_tile_stable",
        }
    try:
        mt = max(1, min(int(max_tiles or INLAND_VIEW_TILE_LIMIT), INLAND_VIEW_TILE_LIMIT))
    except Exception:
        mt = INLAND_VIEW_TILE_LIMIT
    disk_sig = _runtime_cache_signature(static_dir, b)
    cache_key = ":".join(f"{b[k]:.4f}" for k in ("west", "south", "east", "north")) + f":{source}:{geometry}:{lod}:{scene_tier or 'auto'}:tiles{mt}:sig:{disk_sig}"
    now = time.monotonic()
    cached = _INLAND_WATER_ROUTE_CACHE.get(cache_key)
    if cached:
        age = now - cached[0]
        cached_payload = cached[1]
        cached_status = str(cached_payload.get("status") or "") if isinstance(cached_payload, dict) else ""
        cached_count = int((cached_payload.get("count") or 0) if isinstance(cached_payload, dict) else 0)
        # Positive/renderable memory cache is useful, but progressive inland
        # builds can improve coverage every few seconds.  The cache key includes
        # the runtime index signature, and incomplete coverage also gets a short
        # TTL so new/wider tiles are not hidden behind an old partial response.
        ttl = _inland_payload_ttl(cached_payload) if isinstance(cached_payload, dict) else _INLAND_WATER_MISS_TTL_SEC
        if age < ttl:
            out = dict(cached_payload)
            out["cache"] = {"hit": True, "key": f"inland-water:{cache_key}", "ttl_seconds": int(ttl), "mode": "memory_runtime_signature_cache" if cached_count > 0 else "short_memory_miss_cache_disk_recheck_soon", "disk_signature": disk_sig}
            return out

    tile_polys, tile_lines, tile_source, selected_tiles, tile_union_bbox = _tile_json_gz_features(static_dir, b, scene_tier, lod, max_tiles=mt)
    geo_polys, geo_lines, geo_source = ([], [], "geojson_not_used_when_tile_manifest_present") if selected_tiles else _geojson_features(static_dir)
    cached_polys = tile_polys + geo_polys
    cached_lines = tile_lines + geo_lines
    source = tile_source if tile_polys or tile_lines or selected_tiles else geo_source
    polygons = cached_polys
    lines = cached_lines
    # Full-tile mode: bbox chooses the tile squares, but we return every accepted
    # high-definition feature inside those tiles. Do not clip features back to the
    # visible bbox, otherwise lakes/river reaches get partial and visually weird.
    clipped_polys_raw = polygons if selected_tiles else [p for p in polygons if _path_intersects(p.get("path") or [], b)]
    clipped_lines_raw = lines if selected_tiles else [l for l in lines if _path_intersects(l.get("path") or [], b)]
    accepted_polys = _annotate_geometry(clipped_polys_raw, scene_tier=scene_tier, lod=lod, is_line=False)
    accepted_lines = _annotate_geometry(clipped_lines_raw, scene_tier=scene_tier, lod=lod, is_line=True)
    # Source-filtered runtime requests currently limit data to lakes/ponds/
    # reservoirs only. Preserve every accepted lake feature and every
    # received vertex; do not run dry-channel scoring, prominence thinning, or
    # render simplification here.
    clipped_polys, quantity_filter = _largest_lake_per_tile(accepted_polys, scene_tier=scene_tier, lod=lod)
    clipped_lines = [] if INLAND_LAKES_ONLY else accepted_lines
    diagnostics = _geometry_diagnostics(clipped_polys, clipped_lines, source, scene_tier, lod)
    diagnostics["quantity_filter"] = quantity_filter
    diagnostics["degree_aware_cache_coverage"] = _degree_aware_coverage(b, selected_tiles, scene_tier, lod)
    diagnostics["filter_contract"] = "LAKES_ONLY: source_request_only_nhdwaterbody_lake_pond_reservoir; world visible quantity is largest lake per active tile; raw vertices preserved" if _lod_tier(scene_tier, lod) == "world" else "LAKES_ONLY: source_request_only_nhdwaterbody_lake_pond_reservoir; all lakes queue when zoomed in; raw vertices preserved"
    diagnostics["geometry_mode"] = geometry_mode
    # Temperature labels are real-only. Do not emit bootstrap/estimated 68°F
    # points from geometry. Real labels arrive only when live surface-temperature
    # candidates or observed/gauge values are available.
    temperature_points: list[dict[str, Any]] = []
    out = {
        "ok": True,
        "status": "ok" if (clipped_polys or clipped_lines) else "no_high_def_source",
        "source": "runtime_nhd_arcgis_json_gz_or_geojson" if (clipped_polys or clipped_lines) else "runtime_nhd_arcgis_tiles_missing",
        "source_path": source,
        "bbox": [tile_union_bbox["west"], tile_union_bbox["south"], tile_union_bbox["east"], tile_union_bbox["north"]] if tile_union_bbox else [b["west"], b["south"], b["east"], b["north"]],
        "query_bbox": [b["west"], b["south"], b["east"], b["north"]],
        "tile_bbox": [tile_union_bbox["west"], tile_union_bbox["south"], tile_union_bbox["east"], tile_union_bbox["north"]] if tile_union_bbox else None,
        "selected_tiles": selected_tiles,
        "runtime_cache_status": _runtime_cache_status(static_dir, b),
        "tile_square_mode": "full_selected_tile_squares_displayed" if selected_tiles else "direct_geojson_bbox_selection",
        "query": {"source": source, "geometry": geometry, "lod": lod, "scene_tier": scene_tier or "auto", "max_tiles": mt, "active_lod": _lod_tier(scene_tier, lod), "active_degree": _lod_degree(scene_tier, lod), "strict_lod_only": True, "active_lod_only": _inland_tier_cascade(_lod_tier(scene_tier, lod)), "active_degree_only": _lod_degree(scene_tier, lod)},
        "cache_quality": {"quality_rank": _lod_quality_rank(scene_tier, lod), "resolution_deg": _lod_degree(scene_tier, lod), "policy": "strict active LOD only; no finer/coarser cascade fallback; geometry=coarse means simplified real USGS world overview" if geometry_mode == "coarse" else "strict active LOD only; no finer/coarser cascade fallback"},
        "geometry_mode": geometry_mode,
        "style": {"fillColor": "rgba(0,0,0,0)", "strokeColor": VIRIDIAN, "strokeOpacity": 0.92, "fillOpacity": 0.0, "strokeWidth": 4.8, "extrudedHeight": 0, "render": "shoreline_only_one_teal_polyline"},
        "polygons": clipped_polys,
        "lines": [] if INLAND_LAKES_ONLY else clipped_lines,
        "temperature_points": temperature_points,
        "temperature_point_count": len(temperature_points),
        "count": len(clipped_polys) + (0 if INLAND_LAKES_ONLY else len(clipped_lines)),
        "degree_aware_cache_coverage": diagnostics.get("degree_aware_cache_coverage"),
        "diagnostics": diagnostics,
        "geometry_quality": diagnostics.get("geometry_quality"),
        "cache": {"hit": False, "key": f"inland-water:{cache_key}", "ttl_seconds": int(_inland_payload_ttl({"count": len(clipped_polys) + len(clipped_lines), "status": "ok" if (clipped_polys or clipped_lines) else "no_high_def_source", "degree_aware_cache_coverage": diagnostics.get("degree_aware_cache_coverage")})), "mode": "memory_runtime_signature_cache" if (clipped_polys or clipped_lines) else "short_memory_miss_cache_disk_recheck_soon", "disk_signature": disk_sig, "retention_days": INLAND_WATER_CACHE_RETENTION_DAYS},
        "tile_cache_contract": f"LAKES ONLY REST-SOURCE-FILTERED: viewport bbox selects up to {INLAND_VIEW_TILE_LIMIT} active-LOD tiles; world renders only the largest lake/lake-portion per selected 1° tile; regional/local/harbor queue and render all accepted lakes; build requests MapServer/12 Waterbody polygons WHERE (FTYPE=390 OR FTYPE=436); raw GeoJSON vertices are preserved for drawing, islands/coves, masks, temp labels, and bait; disk cache retained 31 days",
        "message": None if (clipped_polys or clipped_lines) else "No matching runtime water tile is ready yet. A real USGS NHD ArcGIS viewport tile build may be queued; partial tiles will draw as they complete.",
        "contract": "lftr_inland_water_v15_raw_vertices_world_largest_lake_per_tile_stable",
    }
    # Only keep renderable payloads in long-lived memory.  Missing-source/no-data
    # responses are intentionally short-lived so freshly written disk runtime tile
    # tiles can pop on the next viewport refresh or reload.
    if out.get("status") not in {"cache_miss", "no_high_def_source"} and int(out.get("count") or 0) > 0:
        _INLAND_WATER_ROUTE_CACHE[cache_key] = (now, out)
    else:
        _INLAND_WATER_ROUTE_CACHE[cache_key] = (now, out)
    return out


def _centroid(path: list[dict[str, Any]]) -> tuple[float, float] | None:
    pts = [(float(p["lat"]), float(p["lng"])) for p in path if isinstance(p, dict) and "lat" in p and "lng" in p]
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _path_sample_points(path: list[dict[str, Any]], *, max_points: int = 5) -> list[tuple[float, float]]:
    pts = [(float(p["lat"]), float(p["lng"])) for p in path if isinstance(p, dict) and "lat" in p and "lng" in p]
    if not pts:
        return []
    if len(pts) <= max_points:
        return pts
    out: list[tuple[float, float]] = []
    for i in range(max_points):
        idx = round(i * (len(pts) - 1) / max(1, max_points - 1))
        out.append(pts[idx])
    return out


def _temperature_point(item: dict[str, Any], lat: float, lng: float, temp_f: float | None, source: str | None, confidence: str | None, idx: int = 0) -> dict[str, Any]:
    name = item.get("name") or "Inland water"
    return {
        "id": f"{str(name).lower().replace(' ', '-')[:48]}-{idx}",
        "name": name,
        "kind": item.get("kind"),
        "lat": round(float(lat), 6),
        "lng": round(float(lng), 6),
        "water_temp_f": round(float(temp_f), 1) if temp_f is not None else None,
        "source": source or "estimated_surface_temp",
        "confidence": confidence or "low",
    }


def _meters_to_lat_deg(meters: float) -> float:
    return float(meters) / 111320.0


def _meters_to_lon_deg(meters: float, lat: float) -> float:
    return float(meters) / max(1e-6, 111320.0 * max(0.15, math.cos(math.radians(lat))))


def _ellipse_zone(lat: float, lng: float, radius_m: float, *, aspect: float = 1.6, angle_deg: float = 0.0, points: int = 10) -> list[dict[str, float]]:
    pts: list[dict[str, float]] = []
    ang = math.radians(angle_deg)
    for i in range(points):
        t = (math.pi * 2.0 * i) / points
        x = math.cos(t) * radius_m * aspect
        y = math.sin(t) * radius_m
        xr = (x * math.cos(ang)) - (y * math.sin(ang))
        yr = (x * math.sin(ang)) + (y * math.cos(ang))
        pts.append({
            "lat": round(lat + _meters_to_lat_deg(yr), 6),
            "lng": round(lng + _meters_to_lon_deg(xr, lat), 6),
        })
    return pts


def _segment_zone(a: tuple[float, float], b: tuple[float, float], width_m: float) -> list[dict[str, float]]:
    lat1, lon1 = a
    lat2, lon2 = b
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) * 0.5))
    dy = (lat2 - lat1)
    mag = math.hypot(dx, dy)
    if mag <= 1e-9:
        return _ellipse_zone(lat1, lon1, width_m * 0.9, aspect=1.3, angle_deg=0.0, points=8)
    px = -dy / mag
    py = dx / mag
    off_lat = _meters_to_lat_deg(width_m)
    off_lon = _meters_to_lon_deg(width_m, (lat1 + lat2) * 0.5)
    return [
        {"lat": round(lat1 + py * off_lat, 6), "lng": round(lon1 + px * off_lon, 6)},
        {"lat": round(lat2 + py * off_lat, 6), "lng": round(lon2 + px * off_lon, 6)},
        {"lat": round(lat2 - py * off_lat, 6), "lng": round(lon2 - px * off_lon, 6)},
        {"lat": round(lat1 - py * off_lat, 6), "lng": round(lon1 - px * off_lon, 6)},
    ]


def _inland_bait_zones_for_item(item: dict[str, Any], temp_f: float, score: float, source: str | None, confidence: str | None) -> list[dict[str, Any]]:
    path = item.get("path") or []
    pts = [(float(p["lat"]), float(p["lng"])) for p in path if isinstance(p, dict) and "lat" in p and "lng" in p]
    if len(pts) < 2:
        return []
    zones: list[dict[str, Any]] = []
    base_radius = 180 + max(0.0, score - 1.0) * 90.0
    width_m = 110 + max(0.0, score - 1.0) * 55.0
    name = item.get("name") or "Inland water"
    kind = _lake_kind_from_codes(item) if INLAND_LAKES_ONLY else str(item.get("kind") or "water").lower()
    if kind in {"reservoir", "lake", "pond", "lakepond", "waterbody"}:
        samples = _path_sample_points(path, max_points=4 if score >= 3 else 3)
        centroid = _centroid(path)
        for idx, (plat, plng) in enumerate(samples):
            zone_kind = "lake_shoreline_bait_boil"
            angle = (idx * 37.0) % 180.0
            zones.append({
                "id": f"{str(name).lower().replace(' ', '-')[:40]}-lake-zone-{idx}",
                "name": name,
                "kind": zone_kind,
                "path": _ellipse_zone(plat, plng, base_radius * (0.95 + idx * 0.10), aspect=1.8, angle_deg=angle, points=10),
                "bait_score": round(score, 2),
                "water_temp_est_f": round(temp_f, 1),
                "source": source or "estimated_surface_temp",
                "confidence": confidence or "low",
                "reason": "shoreline structure + freshwater thermal bait heuristic",
            })
        if centroid and score >= 2.2:
            zones.append({
                "id": f"{str(name).lower().replace(' ', '-')[:40]}-lake-core",
                "name": name,
                "kind": "lake_open_water_bait_boil",
                "path": _ellipse_zone(centroid[0], centroid[1], base_radius * 0.72, aspect=1.45, angle_deg=12.0, points=9),
                "bait_score": round(max(1.0, score - 0.25), 2),
                "water_temp_est_f": round(temp_f, 1),
                "source": source or "estimated_surface_temp",
                "confidence": confidence or "low",
                "reason": "open-water suspended bait possibility",
            })
    else:
        segment_count = min(4, max(2, len(pts) - 1))
        if len(pts) - 1 <= segment_count:
            indices = list(range(len(pts) - 1))
        else:
            indices = [round(i * (len(pts) - 2) / max(1, segment_count - 1)) for i in range(segment_count)]
        seen = set()
        for idx in indices:
            if idx in seen or idx >= len(pts) - 1:
                continue
            seen.add(idx)
            a = pts[idx]
            b = pts[idx + 1]
            zones.append({
                "id": f"{str(name).lower().replace(' ', '-')[:40]}-river-zone-{idx}",
                "name": name,
                "kind": "channel_bait_zone",
                "path": _segment_zone(a, b, width_m),
                "bait_score": round(score, 2),
                "water_temp_est_f": round(temp_f, 1),
                "source": source or "estimated_surface_temp",
                "confidence": confidence or "low",
                "reason": "current seam / bend reach + freshwater thermal bait heuristic",
            })
    return zones


def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * math.cos(math.radians((a[0] + b[0]) / 2))) ** 2


def nearest_water(static_dir: Path, lat: float, lon: float, bbox: dict[str, float] | None = None) -> dict[str, Any] | None:
    b = _bbox_dict(bbox or {"west": lon - 0.8, "south": lat - 0.8, "east": lon + 0.8, "north": lat + 0.8})
    payload = inland_water_payload(static_dir, b)
    candidates = []
    for item in payload.get("polygons", []) + payload.get("lines", []):
        c = _centroid(item.get("path") or [])
        if c:
            candidates.append((_dist2((lat, lon), c), c, item))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    d2, c, item = candidates[0]
    return {**item, "centroid": {"lat": c[0], "lng": c[1]}, "distance_deg2": d2}


def _k_to_f(k: float | None) -> float | None:
    if k is None or not math.isfinite(k):
        return None
    return round(((k - 273.15) * 9.0 / 5.0) + 32.0, 1)


def _sample_nearest(lat2d: Any, lon2d: Any, arr: Any, lat: float, lon: float) -> float | None:
    try:
        import numpy as np
        la = np.asarray(lat2d, dtype=float)
        lo = np.asarray(lon2d, dtype=float)
        va = np.asarray(arr, dtype=float)
        if la.shape != va.shape or lo.shape != va.shape or va.size == 0:
            return None
        # normalize 0..360 longitude grids if needed
        target_lon = lon if np.nanmin(lo) < 0 else (lon + 360 if lon < 0 else lon)
        d = np.square(la - lat) + np.square((lo - target_lon) * np.cos(np.radians(lat)))
        idx = np.unravel_index(np.nanargmin(d), d.shape)
        v = float(va[idx])
        return v if math.isfinite(v) else None
    except Exception:
        return None



def _cached_surface_groups_for_temp(svc: Any) -> tuple[dict[str, Any] | None, str]:
    """Return already-decoded GFS/NCSS surface groups without waking a new provider fetch."""
    try:
        snap = getattr(svc, "_gfs_snapshot", None) or {}
        groups = snap.get("groups") if isinstance(snap, dict) else None
        if isinstance(groups, dict) and groups:
            return groups, "process_decoded_gfs_surface_cache"
    except Exception:
        pass
    try:
        state = getattr(svc, "state", None)
        lkg = getattr(state, "last_good_model_state", None) or {}
        path_raw = (lkg.get("fetch") or {}).get("path") if isinstance(lkg, dict) else None
        if path_raw and hasattr(svc, "_decode_groups_cached"):
            groups, _backend, _owned = svc._decode_groups_cached(Path(path_raw))
            if isinstance(groups, dict) and groups:
                return groups, "last_good_decoded_gfs_surface_cache"
    except Exception:
        pass
    return None, "surface_cache_missing"

def _sample_surface_temperature_from_groups(svc: Any, groups: dict[str, Any] | None, lat: float, lon: float, result: dict[str, Any], source_prefix: str) -> dict[str, Any] | None:
    try:
        groups = groups or {}
        ds = groups.get("surface") or groups.get("2m")
        if ds is None:
            out = dict(result)
            out.update({"source": f"{source_prefix}_surface_group_missing", "method": "cached_surface_group_missing"})
            return out
        da = svc.safe_data_var(ds, ["t0m", "TMP:surface", "TMP_surface", "skt", "SKT", "t", "TMP", "tmp", "t2m"])
        da = svc.squeeze_forecast_array(da)
        if da is None:
            out = dict(result)
            out.update({"source": f"{source_prefix}_surface_temp_var_missing", "method": "cached_surface_temp_var_missing"})
            return out
        lat2d, lon2d = svc.ensure_lat_lon_2d(ds)
        k = _sample_nearest(lat2d, lon2d, getattr(da, "values", None), lat, lon)
        if k is None:
            out = dict(result)
            out.update({"source": f"{source_prefix}_surface_sample_missing", "method": "cached_surface_sample_missing"})
            return out
        value_f = _k_to_f(float(k))
        if value_f is not None and -2.0 <= float(value_f) <= 105.0:
            out = dict(result)
            out.update({
                "used": "cached_surface:t0m_or_skt",
                "value_k": round(float(k), 2),
                "value_f": round(float(value_f), 1),
                "source": f"{source_prefix}_ncss_surface_cache",
                "method": "cached_ncss_surface_t0m_only_no_fetch",
                "cache_only": True,
                "temp_source_log": "inland-water/temp-source source=ncss_surface_cache status=estimated_closed_lake_average",
            })
            try:
                log.info("inland-water/temp-source source=ncss_surface_cache status=estimated_closed_lake_average lat=%.5f lon=%.5f", lat, lon)
            except Exception:
                pass
            return out
        out = dict(result)
        out.update({
            "used": "cached_surface:t0m_or_skt",
            "value_k": round(float(k), 2),
            "value_f": None,
            "suppressed_value_f": round(float(value_f), 1) if value_f is not None else None,
            "source": f"{source_prefix}_ncss_surface_cache_suppressed",
            "method": "cached_surface_candidate_outside_plausible_water_range",
            "cache_only": True,
        })
        return out
    except Exception as exc:
        out = dict(result)
        out.update({"source": f"{source_prefix}_ncss_surface_cache_failed", "error": str(exc), "method": "cached_surface_failed", "cache_only": True})
        return out

def sample_surface_temperature(svc: Any, bbox: dict[str, float], lat: float, lon: float, live: bool = False) -> dict[str, Any]:
    result = {
        "requested_candidates": ["t0m", "TMP:surface", "skt"],
        "used": None,
        "value_k": None,
        "value_f": None,
        "source": "not_sampled",
        "method": "ncss_surface_only_not_run",
        "t2m_policy": "debug_context_only_not_used_for_water_temperature",
    }
    if not live:
        groups, cache_source = _cached_surface_groups_for_temp(svc)
        cached = _sample_surface_temperature_from_groups(svc, groups, lat, lon, result, cache_source)
        if cached and cached.get("value_f") is not None:
            return cached
        result.update({
            "source": cache_source,
            "method": "cache_only_surface_temp_unavailable_no_live_fetch",
            "cache_only": True,
            "temp_source_log": f"inland-water/temp-source source=ncss_surface_cache status={cache_source}",
        })
        try:
            log.info("inland-water/temp-source source=ncss_surface_cache status=%s lat=%.5f lon=%.5f", cache_source, lat, lon)
        except Exception:
            pass
        return result
    try:
        # Live mode is only used by explicit temp/bait warmers. Normal shoreline
        # label reads above are cache-only so the browser TTL never wakes extra NCSS
        # fetches just to draw inland-water temperatures.
        ingest = svc.ingest_latest_model_fields(bbox)
        groups = ingest.get("groups") or {}
        ds = groups.get("surface")
        if ds is None:
            result.update({"source": "gfs_ncss_surface_t0m_candidate_missing", "method": "surface_group_missing"})
            return result

        da = svc.safe_data_var(ds, ["t0m", "TMP:surface", "TMP_surface", "skt", "SKT", "t", "TMP", "tmp"])
        da = svc.squeeze_forecast_array(da)
        if da is None:
            result.update({"source": "gfs_ncss_surface_t0m_candidate_missing", "method": "surface_temp_var_missing"})
            return result

        lat2d, lon2d = svc.ensure_lat_lon_2d(ds)
        vals = getattr(da, "values", None)
        k = _sample_nearest(lat2d, lon2d, vals, lat, lon)
        if k is None:
            result.update({"source": "gfs_ncss_surface_t0m_candidate_missing", "method": "surface_sample_missing"})
            return result

        value_f = _k_to_f(float(k))
        if value_f is None:
            result.update({"source": "gfs_ncss_surface_t0m_candidate_missing", "method": "surface_sample_nonfinite"})
            return result

        # Broad sanity only. Do not correct with air temp. If the NCSS surface
        # candidate is outside a plausible water-label range, suppress it.
        if -2.0 <= float(value_f) <= 105.0:
            result.update({
                "used": "surface:t0m_or_skt",
                "value_k": round(float(k), 2),
                "value_f": round(float(value_f), 1),
                "source": "gfs_ncss_surface_t0m_candidate",
                "method": "ncss_surface_t0m_only",
            })
            return result

        result.update({
            "used": "surface:t0m_or_skt",
            "value_k": round(float(k), 2),
            "value_f": None,
            "suppressed_value_f": round(float(value_f), 1),
            "source": "gfs_ncss_surface_t0m_candidate_suppressed",
            "method": "surface_candidate_outside_plausible_water_range",
        })
        return result
    except Exception as exc:
        result.update({"source": "gfs_ncss_surface_t0m_candidate_failed", "error": str(exc), "method": "failed"})
    return result

def _inland_temp_factor_pct(temp_f: float | None) -> float:
    if temp_f is None or not math.isfinite(float(temp_f)):
        return 50.0
    t = float(temp_f)
    return max(0.0, min(100.0, 100.0 - min(100.0, abs(t - 67.0) * 5.4)))

def _inland_current_factor_pct(speed_m_s: float | None, colorado: bool = False) -> float:
    if speed_m_s is None or not math.isfinite(float(speed_m_s)):
        return 48.0
    mph = float(speed_m_s) * 2.23694
    ideal = 1.3 if colorado else 0.8
    span = 1.3 if colorado else 0.9
    return max(0.0, min(100.0, 100.0 - min(100.0, abs(mph - ideal) / max(0.25, span) * 52.0)))

def _inland_depth_factor_pct(depth_ft: float | None, colorado: bool = False) -> float:
    if depth_ft is None or not math.isfinite(float(depth_ft)):
        return 52.0
    d = float(depth_ft)
    ideal = 18.0 if colorado else 14.0
    span = 16.0 if colorado else 12.0
    return max(0.0, min(100.0, 100.0 - min(100.0, abs(d - ideal) / max(4.0, span) * 58.0)))

def _inland_wind_factor_pct(speed_m_s: float | None) -> float:
    if speed_m_s is None or not math.isfinite(float(speed_m_s)):
        return 50.0
    mph = float(speed_m_s) * 2.23694
    if mph <= 3.0:
        return 58.0
    if mph <= 10.0:
        return min(100.0, 64.0 + (mph - 3.0) * 4.5)
    if mph <= 18.0:
        return max(32.0, 95.0 - (mph - 10.0) * 6.0)
    return max(6.0, 47.0 - (mph - 18.0) * 4.0)

def _inland_factor_breakdown(temp_f: float | None, current_speed_m_s: float | None, bait_depth_ft: float | None, wind_speed_m_s: float | None, colorado: bool = False) -> dict[str, float]:
    return {
        'temp_factor_pct': round(_inland_temp_factor_pct(temp_f), 1),
        'current_factor_pct': round(_inland_current_factor_pct(current_speed_m_s, colorado=colorado), 1),
        'depth_factor_pct': round(_inland_depth_factor_pct(bait_depth_ft, colorado=colorado), 1),
        'wind_factor_pct': round(_inland_wind_factor_pct(wind_speed_m_s), 1),
    }

# --- LFTR appended inland environment helpers: wind/current/depth + Colorado corridor intelligence ---

def _sample_surface_wind(svc: Any, bbox: dict[str, float], lat: float, lon: float, live: bool = False) -> dict[str, Any]:
    result = {"used": None, "u_m_s": None, "v_m_s": None, "speed_m_s": None, "speed_mph": None, "heading_deg": None, "source": "not_sampled"}
    if not live:
        result["source"] = "set_live=1_to_fetch_ncss_surface_wind"
        return result
    try:
        ingest = svc.ingest_latest_model_fields(bbox)
        groups = ingest.get("groups") or {}
        ds = groups.get("10m") or groups.get("surface") or groups.get("2m") or groups.get("isobaricInhPa")
        if ds is None:
            return result
        u_da = None
        v_da = None
        for names in (["u10", "UGRD"], ["u", "UGRD"]):
            u_da = svc.safe_data_var(ds, names)
            if u_da is not None:
                break
        for names in (["v10", "VGRD"], ["v", "VGRD"]):
            v_da = svc.safe_data_var(ds, names)
            if v_da is not None:
                break
        u_da = svc.squeeze_forecast_array(u_da)
        v_da = svc.squeeze_forecast_array(v_da)
        if u_da is None or v_da is None:
            return result
        try:
            lat2d, lon2d = svc.ensure_lat_lon_2d(ds)
        except Exception:
            sample_ds = groups.get("surface") or groups.get("2m") or groups.get("10m") or groups.get("isobaricInhPa")
            lat2d, lon2d = svc.ensure_lat_lon_2d(sample_ds)
        u = _sample_nearest(lat2d, lon2d, getattr(u_da, 'values', None), lat, lon)
        v = _sample_nearest(lat2d, lon2d, getattr(v_da, 'values', None), lat, lon)
        if u is None or v is None:
            return result
        speed = math.hypot(float(u), float(v))
        heading = (math.degrees(math.atan2(float(u), float(v))) + 360.0) % 360.0
        result.update({
            "used": "10m:u10/v10",
            "u_m_s": round(float(u), 3),
            "v_m_s": round(float(v), 3),
            "speed_m_s": round(speed, 3),
            "speed_mph": round(speed * 2.23694, 2),
            "heading_deg": round(heading, 1),
            "source": "gfs_ncss_surface_wind",
        })
    except Exception as exc:
        result.update({"source": "gfs_ncss_surface_wind_failed", "error": str(exc)})
    return result


def _dominant_axis_heading_deg(item: dict[str, Any]) -> float | None:
    path = item.get('path') or []
    pts = [(float(p['lat']), float(p['lng'])) for p in path if isinstance(p, dict) and 'lat' in p and 'lng' in p]
    if len(pts) < 2:
        return None
    best = None
    best_d2 = -1.0
    for i in range(0, len(pts), max(1, len(pts)//16)):
        for j in range(i + 1, len(pts), max(1, len(pts)//16)):
            d2 = _dist2(pts[i], pts[j])
            if d2 > best_d2:
                best_d2 = d2
                best = (pts[i], pts[j])
    if not best:
        a = pts[0]; b = pts[-1]
    else:
        a, b = best
    # downstream/in-flow guess: prefer north-to-south when the geometry is more N/S than E/W
    dlat = b[0] - a[0]
    dlon = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) * 0.5))
    if abs(dlat) >= abs(dlon):
        upstream, downstream = (a, b) if a[0] >= b[0] else (b, a)
    else:
        upstream, downstream = (a, b) if a[1] <= b[1] else (b, a)
    dy = downstream[0] - upstream[0]
    dx = (downstream[1] - upstream[1]) * math.cos(math.radians((upstream[0] + downstream[0]) * 0.5))
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def _is_colorado_corridor_item(item: dict[str, Any]) -> bool:
    name = str(item.get('name') or '').lower()
    if any(k in name for k in ('havasu', 'colorado', 'mohave', 'parker', 'needle', 'topock')):
        return True
    c = _centroid(item.get('path') or [])
    if not c:
        return False
    lat, lon = c
    return (33.4 <= lat <= 35.7) and (-115.6 <= lon <= -113.5)


def _estimate_inland_current(item: dict[str, Any], wind: dict[str, Any] | None = None) -> dict[str, Any]:
    wind = wind or {}
    wind_speed_m_s = float(wind.get('speed_m_s') or 0.0)
    wind_heading_deg = wind.get('heading_deg')
    axis_heading = _dominant_axis_heading_deg(item)
    area_km2 = float(item.get('areasqkm') or item.get('area_km2') or 0.0)
    colorado = _is_colorado_corridor_item(item)
    kind = str(item.get('kind') or '').lower()
    elongated_bonus = 0.22 if kind in {'reservoir', 'streamriver', 'canalditch', 'artificialpath'} else 0.0
    if colorado:
        heading = axis_heading if axis_heading is not None else 165.0
        speed_m_s = 0.55 + min(1.25, wind_speed_m_s * 0.12) + elongated_bonus
        reason = 'lower_colorado_corridor_flow_heuristic_plus_surface_wind'
        confidence = 'medium'
    else:
        heading = axis_heading if axis_heading is not None else (wind_heading_deg if wind_heading_deg is not None else 0.0)
        speed_m_s = min(0.65, 0.03 + (wind_speed_m_s * 0.055) + min(0.18, area_km2 * 0.003) + elongated_bonus * 0.2)
        reason = 'lake_surface_drift_heuristic_from_geometry_plus_wind'
        confidence = 'low'
    u = math.sin(math.radians(heading)) * speed_m_s
    v = math.cos(math.radians(heading)) * speed_m_s
    return {
        'current_heading_deg': round(float(heading), 1),
        'current_speed_m_s': round(speed_m_s, 3),
        'current_speed_mph': round(speed_m_s * 2.23694, 2),
        'current_u_m_s': round(u, 3),
        'current_v_m_s': round(v, 3),
        'current_source': 'inland_current_heuristic',
        'current_reason': reason,
        'current_confidence': confidence,
        'colorado_corridor': colorado,
    }


def _estimate_bait_depth(item: dict[str, Any], temp_f: float | None, current_speed_m_s: float | None) -> dict[str, Any]:
    area_km2 = float(item.get('areasqkm') or item.get('area_km2') or 0.0)
    kind = str(item.get('kind') or '').lower()
    mean_depth_ft = 12.0 + min(55.0, math.sqrt(max(area_km2, 0.0)) * 6.5)
    if kind in {'reservoir', 'lake'}:
        mean_depth_ft += 8.0
    if _is_colorado_corridor_item(item):
        mean_depth_ft += 9.0
    temp_term = 0.0 if temp_f is None else max(0.0, abs(float(temp_f) - 68.0) * 0.22)
    current_term = 0.0 if current_speed_m_s is None else min(10.0, float(current_speed_m_s) * 8.0)
    bait_depth_ft = max(4.0, min(mean_depth_ft * 0.72, 7.0 + temp_term + current_term + math.sqrt(max(area_km2, 0.0)) * 1.7))
    band_ft = [round(max(1.0, bait_depth_ft - 5.0), 1), round(min(max(mean_depth_ft, bait_depth_ft + 3.0), bait_depth_ft + 7.0), 1)]
    depth_intel = {
        'source': 'inland_geometry_area_plus_temp_current_heuristic',
        'bottom_depth_ft': round(mean_depth_ft, 1),
        'estimated_mean_depth_ft': round(mean_depth_ft, 1),
        'preferred_bait_depth_ft': round(bait_depth_ft, 1),
        'bait_depth_ft': round(bait_depth_ft, 1),
        'bait_depth_band_ft': band_ft,
        'visual_policy': 'above_water_extrusion_represents_bait_depth',
    }
    return {
        'estimated_mean_depth_ft': round(mean_depth_ft, 1),
        'bottom_depth_ft': round(mean_depth_ft, 1),
        'preferred_bait_depth_ft': round(bait_depth_ft, 1),
        'bait_depth_ft': round(bait_depth_ft, 1),
        'bait_depth_band_ft': band_ft,
        'depth_intel': depth_intel,
        'depth_source': 'inland_geometry_area_plus_temp_current_heuristic',
        'depth_confidence': 'low' if temp_f is None else 'medium',
    }


def _enrich_inland_feature(item: dict[str, Any], svc: Any, lat: float, lon: float, temp_f: float | None, temp_source: str | None, confidence: str | None, live: bool = False) -> dict[str, Any]:
    wind = _sample_surface_wind(svc, {'west': lon - 0.18, 'south': lat - 0.18, 'east': lon + 0.18, 'north': lat + 0.18}, lat, lon, live=live)
    current = _estimate_inland_current(item, wind)
    depth = _estimate_bait_depth(item, temp_f, current.get('current_speed_m_s'))
    area = item.get('areasqkm') if item.get('areasqkm') is not None else item.get('area_km2')
    out = {
        'area_km2': round(float(area), 4) if area is not None and math.isfinite(float(area)) else None,
        'temperature_source': temp_source,
        'confidence': confidence,
        **wind,
        **current,
        **depth,
    }
    return out


def _temperature_point(item: dict[str, Any], lat: float, lng: float, temp_f: float | None, source: str | None, confidence: str | None, idx: int = 0) -> dict[str, Any]:
    name = item.get('name') or 'Inland water'
    point = {
        'id': f"{str(name).lower().replace(' ', '-')[:48]}-{idx}",
        'name': name,
        'kind': item.get('kind'),
        'lat': round(float(lat), 6),
        'lng': round(float(lng), 6),
        'water_temp_f': round(float(temp_f), 1) if temp_f is not None else None,
        'source': source or 'estimated_surface_temp',
        'confidence': confidence or 'low',
    }
    for key in ('area_km2', 'current_heading_deg', 'current_speed_m_s', 'current_speed_mph', 'current_u_m_s', 'current_v_m_s', 'current_source', 'current_reason', 'current_confidence', 'colorado_corridor', 'estimated_mean_depth_ft', 'bait_depth_ft', 'bait_depth_band_ft', 'depth_source', 'depth_confidence', 'u_m_s', 'v_m_s', 'speed_m_s', 'speed_mph', 'heading_deg'):
        if key in item:
            point[key] = item.get(key)
    return point


def inland_conditions_payload(static_dir: Path, svc: Any, bbox: dict[str, float] | None, lat: float, lon: float, live: bool = False) -> dict[str, Any]:
    b = _bbox_dict(bbox or {'west': lon - 0.2, 'south': lat - 0.2, 'east': lon + 0.2, 'north': lat + 0.2})
    water = nearest_water(static_dir, lat, lon, b)
    temp = sample_surface_temperature(svc, b, lat, lon, live=live)
    water_temp_f = temp.get('value_f')
    confidence = 'medium' if water_temp_f is not None and live else 'unavailable'
    env = _enrich_inland_feature(water or {'name': 'viewport inland water', 'kind': 'sample'}, svc, lat, lon, water_temp_f, temp.get('used') or temp.get('source'), confidence, live=live)
    temp_points = []
    if water_temp_f is not None and live:
        tp_item = {**(water or {'name': 'viewport inland water', 'kind': 'sample'}), **env}
        temp_points.append(_temperature_point(tp_item, lat, lon, water_temp_f, temp.get('used') or temp.get('source'), confidence, 0))
    return {
        'ok': True,
        'status': 'ok',
        'source': 'usgs_nhdplus_hr_plus_gfs_ncss_surface_candidates',
        'bbox': [b['west'], b['south'], b['east'], b['north']],
        'query': {'lat': lat, 'lon': lon},
        'inland_water': bool(water),
        'nearest_water': {**water, **env} if water else None,
        'surface_temperature': temp,
        'surface_wind': {k: env.get(k) for k in ('used', 'u_m_s', 'v_m_s', 'speed_m_s', 'speed_mph', 'heading_deg', 'source')},
        'inland_current': {k: env.get(k) for k in ('current_heading_deg', 'current_speed_m_s', 'current_speed_mph', 'current_u_m_s', 'current_v_m_s', 'current_source', 'current_reason', 'current_confidence', 'colorado_corridor')},
        'estimated_depth': {k: env.get(k) for k in ('estimated_mean_depth_ft', 'bait_depth_ft', 'bait_depth_band_ft', 'depth_source', 'depth_confidence')},
        'water_temp_f': water_temp_f,
        'water_temp_est_f': None,
        'water_temp_confidence': confidence,
        'temperature_points': temp_points,
        'temperature_point_count': len(temp_points),
        'note': 'Real-only temperature policy: no bootstrap 68°F and no estimated labels. Added inland wind/current/depth heuristics, including a Colorado-corridor current model without re-enabling visible stream rendering.',
        'contract': 'lftr_inland_conditions_v3_real_temperature_plus_current_depth',
    }


def inland_bait_payload(static_dir: Path, svc: Any, bbox: dict[str, float] | None, lat: float | None = None, lon: float | None = None, live: bool = False) -> dict[str, Any]:
    b = _bbox_dict(bbox)
    water = inland_water_payload(static_dir, b)
    targets = []
    bait_score_rows: list[dict[str, Any]] = []
    temperature_points: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    for item in water.get('polygons', []) + water.get('lines', []):
        c = _centroid(item.get('path') or [])
        if not c:
            continue
        temp: float | None = None
        source: str | None = None
        confidence: str | None = 'unavailable'
        if live:
            cond = inland_conditions_payload(static_dir, svc, {'west': c[1] - 0.15, 'south': c[0] - 0.15, 'east': c[1] + 0.15, 'north': c[0] + 0.15}, c[0], c[1], live=True)
            temp = cond.get('water_temp_f') if cond.get('water_temp_f') is not None else None
            source = cond.get('surface_temperature', {}).get('used') or cond.get('surface_temperature', {}).get('source')
            confidence = cond.get('water_temp_confidence')
        env = _enrich_inland_feature(item, svc, c[0], c[1], temp, source, confidence, live=live)
        factor_breakdown = _inland_factor_breakdown(temp, env.get('current_speed_m_s'), env.get('bait_depth_ft'), env.get('speed_m_s'), colorado=bool(env.get('colorado_corridor')))
        kind = str(item.get('kind') or 'water').lower()
        type_bonus = 0.45 if kind in {'reservoir', 'lake', 'pond'} else 0.25
        current_bonus = min(0.35, float(env.get('current_speed_m_s') or 0.0) * 0.22)
        depth_bonus = min(0.28, max(0.0, 1.0 - abs(float(env.get('bait_depth_ft') or 10.0) - 16.0) / 18.0) * 0.28)
        if temp is not None:
            temp_score = max(0.0, 1.0 - abs(float(temp) - 67.0) / 24.0)
            factor_score = ((factor_breakdown['temp_factor_pct'] / 100.0) * 0.36) + ((factor_breakdown['current_factor_pct'] / 100.0) * 0.16) + ((factor_breakdown['depth_factor_pct'] / 100.0) * 0.16) + ((factor_breakdown['wind_factor_pct'] / 100.0) * 0.10)
            score = max(1.0, min(5.0, 1.0 + 4.0 * (0.38 * temp_score + type_bonus * 0.14 + current_bonus * 0.12 + depth_bonus * 0.10 + factor_score)))
        else:
            factor_score = ((factor_breakdown['current_factor_pct'] / 100.0) * 0.22) + ((factor_breakdown['depth_factor_pct'] / 100.0) * 0.20) + ((factor_breakdown['wind_factor_pct'] / 100.0) * 0.10)
            score = max(1.0, min(5.0, 1.9 + type_bonus + current_bonus + depth_bonus * 0.35 + factor_score))
        target = {
            'name': item.get('name'),
            'kind': item.get('kind'),
            'centroid': {'lat': c[0], 'lng': c[1]},
            'bait_score': round(score, 2),
            'temperature_source': source,
            'confidence': confidence,
            'reasons': ['NHD/NHDPlus HR waterbody geometry', 'freshwater structure heuristic', 'temperature omitted unless live t0m/TMP:surface/skt sampling succeeds', 'wind/current/depth heuristic enrichment', 'selected-location inland bait score now wires temp/current/depth/wind factors directly'],
            **factor_breakdown,
            **env,
        }
        if temp is not None:
            target['water_temp_f'] = round(float(temp), 1)
        targets.append(target)
        row = {
            'name': item.get('name') or 'Inland water',
            'kind': item.get('kind'),
            'lat': round(float(c[0]), 6),
            'lon': round(float(c[1]), 6),
            'lng': round(float(c[1]), 6),
            'probability': round(max(0.0, min(1.0, score / 5.0)), 4),
            'bait_score': round(score, 2),
            'score_5': round(score, 2),
            'water_temp_f': round(float(temp), 1) if temp is not None else None,
            'surface_temp_f': round(float(temp), 1) if temp is not None else None,
            'source': 'inland_lake_ncss_temperature_bait_score' if temp is not None else 'inland_lake_geometry_bait_score_waiting_for_ncss_temp',
            'method': 'inland_lake_dense_advanced_bait_square_contours_plus_current_depth',
            'temperature_source': source,
            'confidence': confidence,
            **factor_breakdown,
            **env,
        }
        bait_score_rows.append(row)
        if temp is not None:
            sample_count = 5 if item.get('kind') in {'reservoir', 'lake', 'pond'} else 4
            sample_pts = _path_sample_points(item.get('path') or [], max_points=sample_count)
            for idx, (plat, plng) in enumerate(sample_pts):
                temp_item = {**item, **env}
                tp = _temperature_point(temp_item, plat, plng, float(temp), source, confidence, idx)
                tp['bait_score'] = round(score, 2)
                temperature_points.append(tp)
    return {
        'ok': True,
        'status': 'ok',
        'source': 'inland_water_conditions_bait_v4_real_temperature_current_depth',
        'bbox': water.get('bbox'),
        'targets': targets,
        'bait_score': bait_score_rows,
        'advancedBaitRows': bait_score_rows,
        'advanced_bait_rows': bait_score_rows,
        'zones': zones,
        'renderer': 'inland_advanced_bait_grid_squares_positive_depth_extrusion',
        'style_contract': 'bright_green_fill_orange_glow_outline_small_lake_squares_above_water',
        'temperature_points': temperature_points,
        'count': len(targets),
        'zone_count': len(zones),
        'bait_score_count': len(bait_score_rows),
        'advanced_bait_row_count': len(bait_score_rows),
        'temperature_point_count': len(temperature_points),
        'temperature_policy': 'no estimated labels; no fallback 68F; real temps only when live candidate sampling succeeds',
        'current_policy': 'Colorado-corridor heuristic plus generic wind-driven inland drift heuristic without visible stream rendering',
        'depth_policy': 'bait depth is estimated from geometry area, temperature, and current, and is intended for visualization/hud guidance',
        'contract': 'lftr_inland_bait_v5_temp_bait_score_current_depth',
    }

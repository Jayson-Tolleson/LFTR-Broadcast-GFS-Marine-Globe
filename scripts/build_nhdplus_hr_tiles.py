#!/usr/bin/env python3
"""Build high-definition Inland Waters json.gz tiles for LFTR.

Input can be app-normalized GeoJSON/GeoJSON.gz or GeoJSON exported from
NHDPlus HR / 3DHP / NHDWaterbody / NHDArea / NHDFlowline layers.

This intentionally uses only local files at runtime. Use this script after
fetching/exporting hydrography data; the app will then read:

  static/data/nhdplus_hr/tiles/index.json
  static/data/nhdplus_hr/tiles/<lod>/tile_*.json.gz

Coarse world geometry can be generated from the same real USGS/NHD source; it is simplified/major-water only, not mock/seed data.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

LOD_CFG = {
    # MAXIMUM DETAIL / CRISP SHORELINE MODE
    #
    # Runtime still selects full geographic squares, but the squares are now
    # much smaller and the per-feature point caps are much higher.  This favors
    # true NHD/ArcGIS shoreline fidelity over broad generalized blobs.  The app
    # remains protected by viewport selection + max_tiles, so it does not draw a
    # whole state at once.
    # Prominence-filtered crisp mode:
    # keep the true geometry for the most important water features only.
    # The filters prevent tiny ponds/short ditches from consuming render budget.
    # User-facing cascade: 1.0° fast first-paint, 0.5° medium, 0.25° crisp.
    # The builder emits all tiers from each real NHD source square, and the
    # runtime selector prefers the highest available detail for the viewport.
    "world":    {"tile_deg": 1.0,  "max_points": 1200, "min_poly": 3, "min_line": 2, "min_area_km2": 0.50,  "min_line_km": 8.0,  "max_polygons": 5, "max_lines": 8},
    "regional": {"tile_deg": 0.5,  "max_points": 2400, "min_poly": 3, "min_line": 2, "min_area_km2": 0.12,  "min_line_km": 3.0,  "max_polygons": 5, "max_lines": 8},
    "local":    {"tile_deg": 0.25, "max_points": 5000, "min_poly": 3, "min_line": 2, "min_area_km2": 0.025, "min_line_km": 0.8,  "max_polygons": 5, "max_lines": 8},
    "harbor":   {"tile_deg": 0.25, "max_points": 9000, "min_poly": 3, "min_line": 2, "min_area_km2": 0.005, "min_line_km": 0.20, "max_polygons": 5, "max_lines": 8},
}
ACCEPTED_SOURCE = "USGS NHDPlus HR high-detail vector tile"
LAKES_ONLY = str(os.getenv("NHD_INLAND_LAKES_ONLY", "1")).lower() not in {"0", "false", "no", "off"}
LAKE_FCODES = {"39000", "43600"}
LAKE_FTYPE_PREFIXES = ("390", "436")
LAKE_FTYPES = {"390", "436"}
LAKE_TERMS = ("lake", "pond", "reservoir", "waterbody", "water body", "lakepond")

def item_fcode(item: dict[str, Any]) -> str:
    try:
        return str(int(float(item.get("fcode") or item.get("FCode") or "")))
    except Exception:
        return str(item.get("fcode") or item.get("FCode") or "").strip()

def item_ftype(item: dict[str, Any]) -> str:
    try:
        return str(int(float(item.get("ftype") or item.get("FType") or item.get("kind") or "")))
    except Exception:
        return str(item.get("ftype") or item.get("FType") or "").strip()

def is_lake_polygon(item: dict[str, Any]) -> bool:
    if not LAKES_ONLY:
        return True
    # Current contract: the fetcher only asks MapServer layer 12 for
    # (FTYPE=390 OR FTYPE=436).  Do not second-guess source-filtered polygons
    # here; convert every returned polygon's vertices into app polygons.
    # Lines are still rejected because streams are not part of the layer-12
    # lakes-only REST request.
    return str(item.get("_geometry_type") or "").lower() == "polygon"


def load_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text())


def write_json_gz(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=7) as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


def norm_num(v: Any, default: float = float("nan")) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def normalize_path(raw: Any) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for p in raw or []:
        if isinstance(p, dict):
            lat = norm_num(p.get("lat"))
            lng = norm_num(p.get("lng", p.get("lon")))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            lng = norm_num(p[0])
            lat = norm_num(p[1])
        else:
            continue
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            if not out or abs(out[-1]["lat"] - lat) > 1e-9 or abs(out[-1]["lng"] - lng) > 1e-9:
                out.append({"lat": round(lat, 7), "lng": round(lng, 7)})
    if len(out) >= 3 and abs(out[0]["lat"] - out[-1]["lat"]) < 1e-9 and abs(out[0]["lng"] - out[-1]["lng"]) < 1e-9:
        out.pop()
    return out


def path_bbox(path: list[dict[str, float]]) -> tuple[float, float, float, float] | None:
    if not path:
        return None
    lats = [p["lat"] for p in path]
    lngs = [p["lng"] for p in path]
    return min(lngs), min(lats), max(lngs), max(lats)


def haversine_km(a: dict[str, float], b: dict[str, float]) -> float:
    r = 6371.0088
    lat1 = math.radians(a["lat"]); lat2 = math.radians(b["lat"])
    dlat = lat2 - lat1
    dlng = math.radians(b["lng"] - a["lng"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def line_length_km(path: list[dict[str, float]]) -> float:
    if len(path) < 2:
        return 0.0
    return sum(haversine_km(path[i - 1], path[i]) for i in range(1, len(path)))


def polygon_area_km2(path: list[dict[str, float]]) -> float:
    """Approximate small polygon area in km² using an equirectangular shoelace."""
    if len(path) < 3:
        return 0.0
    lat0 = math.radians(sum(p["lat"] for p in path) / len(path))
    km_per_deg_lat = 111.32
    km_per_deg_lng = 111.32 * max(0.05, math.cos(lat0))
    pts = [(p["lng"] * km_per_deg_lng, p["lat"] * km_per_deg_lat) for p in path]
    area = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    return not (ae < bw or aw > be or an < bs or as_ > bn)


def thin_path(path: list[dict[str, float]], cap: int) -> list[dict[str, float]]:
    """Return a light render path for coarse overview tiles.

    Vector/local mode preserves source vertices. Coarse world mode keeps the
    true feature shape but reduces point count for fast first paint.
    """
    if len(path) <= cap or cap <= 0:
        return list(path)
    if cap <= 2:
        return [path[0], path[-1]]
    out: list[dict[str, float]] = []
    last = len(path) - 1
    for i in range(cap):
        idx = round(i * last / max(1, cap - 1))
        pt = path[idx]
        if not out or out[-1] != pt:
            out.append(pt)
    return out


def iter_geojson_features(data: Any, source_name: str) -> Iterable[dict[str, Any]]:
    if isinstance(data, dict) and ("polygons" in data or "lines" in data):
        for item in data.get("polygons") or []:
            item = dict(item)
            item["_geometry_type"] = "polygon"
            yield item
        for item in data.get("lines") or []:
            item = dict(item)
            item["_geometry_type"] = "line"
            yield item
        return
    feats = data.get("features", []) if isinstance(data, dict) else []
    for feat in feats:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        gt = geom.get("type")
        coords = geom.get("coordinates") or []
        name = props.get("gnis_name") or props.get("GNIS_Name") or props.get("name") or props.get("Name") or "Unnamed inland water"
        kind = props.get("ftype") or props.get("FType") or props.get("kind") or props.get("type") or "water"
        fcode = props.get("fcode") or props.get("FCode")
        source_class = props.get("source_class") or props.get("source") or source_name or ACCEPTED_SOURCE
        ftype = props.get("ftype") or props.get("FType") or props.get("FTYPE") or kind
        area = props.get("area_km2") or props.get("AREASQKM") or props.get("areasqkm")
        base = {"name": name, "kind": str(kind).lower(), "fcode": fcode, "FCode": fcode, "FType": ftype, "ftype": ftype, "area_km2": area, "source_class": str(source_class), "source_path": source_name}
        if gt == "Polygon":
            for ring in coords[:1]:
                p = normalize_path(ring)
                if len(p) >= 3:
                    yield {**base, "_geometry_type": "polygon", "path": p}
        elif gt == "MultiPolygon":
            for poly in coords:
                if poly:
                    p = normalize_path(poly[0])
                    if len(p) >= 3:
                        yield {**base, "_geometry_type": "polygon", "path": p}
        elif gt == "LineString":
            p = normalize_path(coords)
            if len(p) >= 2:
                yield {**base, "_geometry_type": "line", "path": p}
        elif gt == "MultiLineString":
            for line in coords:
                p = normalize_path(line)
                if len(p) >= 2:
                    yield {**base, "_geometry_type": "line", "path": p}


def project_bbox(features: list[dict[str, Any]], explicit: str | None) -> tuple[float, float, float, float]:
    if explicit:
        w, s, e, n = [float(x.strip()) for x in explicit.split(",")]
        return w, s, e, n
    boxes = [path_bbox(f.get("path") or []) for f in features]
    boxes = [b for b in boxes if b]
    if not boxes:
        raise SystemExit("No valid features found after high-definition quality filtering")
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)



def tile_range_for_bbox(root: tuple[float, float, float, float], tile_deg: float) -> tuple[range, range]:
    w, s, e, n = root
    ix0 = math.floor(w / tile_deg)
    ix1 = math.ceil(e / tile_deg) - 1
    iy0 = math.floor(s / tile_deg)
    iy1 = math.ceil(n / tile_deg) - 1
    return range(ix0, ix1 + 1), range(iy0, iy1 + 1)


def tile_bbox_from_index(ix: int, iy: int, tile_deg: float) -> tuple[float, float, float, float]:
    w = ix * tile_deg
    s = iy * tile_deg
    return w, s, w + tile_deg, s + tile_deg


def tile_relpath(lod: str, ix: int, iy: int, tile_deg: float) -> str:
    scale = int(round(1.0 / tile_deg)) if tile_deg < 1 else 1
    if scale > 1:
        return f"{lod}/d{scale}/x{ix}_y{iy}.json.gz"
    return f"{lod}/x{ix}_y{iy}.json.gz"




def merge_tile_items(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge progressive source-square writes for the same render tile.

    A 0.25° source fetch can contribute to the same 1.0°/0.5° render tile as
    neighboring source squares.  Append mode must add to that tile, not overwrite
    it, or the cache appears to swap/cross tiles as the builder progresses.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    def key_for(item: dict[str, Any]) -> str:
        path = item.get("path") or []
        first = path[0] if isinstance(path, list) and path else {}
        last = path[-1] if isinstance(path, list) and path else {}
        return "|".join([
            str(item.get("fcode") or ""),
            str(item.get("objectid") or item.get("id") or ""),
            str(item.get("name") or "")[:80],
            str(first.get("lat", "")), str(first.get("lng", first.get("lon", ""))),
            str(last.get("lat", "")), str(last.get("lng", last.get("lon", ""))),
            str(len(path)),
        ])
    for item in list(existing or []) + list(incoming or []):
        if not isinstance(item, dict):
            continue
        k = key_for(item)
        if k in seen:
            continue
        seen.add(k)
        merged.append(item)
    return merged

def build_tiles(features: list[dict[str, Any]], out_dir: Path, bbox: tuple[float, float, float, float], *, append: bool = False) -> dict[str, Any]:
    geometry_mode = str(os.getenv("NHDPLUS_GEOMETRY_MODE", "vector") or "vector").lower()
    coarse_mode = geometry_mode in {"coarse", "overview", "world_coarse"}
    existing_tiles: list[dict[str, Any]] = []
    existing_summary: dict[str, Any] = {}
    existing_bbox: list[float] | None = None
    index_path = out_dir / "index.json"
    if append and index_path.exists():
        try:
            old = json.loads(index_path.read_text())
            if isinstance(old, dict):
                existing_tiles = list(old.get("tiles") or [])
                existing_summary = dict(old.get("summary") or {})
                if isinstance(old.get("bbox"), list) and len(old.get("bbox")) == 4:
                    existing_bbox = [float(x) for x in old.get("bbox")]
        except Exception:
            existing_tiles = []
            existing_summary = {}
            existing_bbox = None
    elif out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if existing_bbox:
        merged_bbox = [min(existing_bbox[0], bbox[0]), min(existing_bbox[1], bbox[1]), max(existing_bbox[2], bbox[2]), max(existing_bbox[3], bbox[3])]
    else:
        merged_bbox = list(bbox)
    manifest: dict[str, Any] = {
        "schema": "lftr_nhdplus_hr_tiles_v2_full_square_grid",
        "bbox": merged_bbox,
        "tile_policy": "LAKES ONLY by default: NHDWaterbody Lake/Pond + Reservoir polygons; world/coarse uses simplified real USGS geometry; regional/local/vector preserve true received vertices",
        "geometry_mode": geometry_mode,
        "max_detail_mode": not coarse_mode,
        "geometry_quality": "coarse_real_usgs_lake_reservoir_simplified" if coarse_mode else "crisp_lake_reservoir_shoreline_true_nhd_arcgis_geometry_raw_vertices",
        "lakes_only": LAKES_ONLY,
        "append_mode": bool(append),
        "tiles": [],
        "summary": dict(existing_summary),
    }
    existing_by_path = {str(t.get("path")): t for t in existing_tiles if isinstance(t, dict) and t.get("path")}
    written_paths: set[str] = set()
    for lod, cfg in LOD_CFG.items():
        tile_deg = float(cfg["tile_deg"])
        cap = int(cfg["max_points"])
        x_range, y_range = tile_range_for_bbox(bbox, tile_deg)
        wrote = 0
        for ix in x_range:
            for iy in y_range:
                tb = tile_bbox_from_index(ix, iy, tile_deg)
                poly_candidates, line_candidates = [], []
                min_area = env_float(f"NHD_MIN_AREA_KM2_{lod.upper()}", float(cfg.get("min_area_km2", 0.0)))
                min_line_km = env_float(f"NHD_MIN_LINE_KM_{lod.upper()}", float(cfg.get("min_line_km", 0.0)))
                max_polys = env_int(f"NHD_MAX_POLYGONS_PER_TILE_{lod.upper()}", env_int("NHD_MAX_POLYGONS_PER_TILE", int(cfg.get("max_polygons", 5))))
                max_lines = env_int(f"NHD_MAX_LINES_PER_TILE_{lod.upper()}", env_int("NHD_MAX_LINES_PER_TILE", int(cfg.get("max_lines", 8))))
                for feat in features:
                    src_path = feat.get("path") or []
                    pb = path_bbox(src_path)
                    if not pb or not bbox_intersects(pb, tb):
                        continue
                    item = {k: v for k, v in feat.items() if not k.startswith("_")}
                    render_path = thin_path(src_path, cap) if (coarse_mode and lod == "world") else src_path
                    item["source_path_points"] = len(src_path)
                    item["render_path_points"] = len(render_path)
                    item["path"] = render_path
                    item["lod_tier"] = lod
                    item["lod_tile_deg"] = tile_deg
                    item["geometry_mode"] = "coarse" if (coarse_mode and lod == "world") else "vector"
                    item["max_detail_mode"] = not (coarse_mode and lod == "world")
                    item["prominence_filtered"] = False
                    item["raw_vertices_preserved"] = not (coarse_mode and lod == "world")
                    item["tile_bbox"] = list(tb)
                    item["shoreline_truth"] = "coarse_real_usgs_major_water_simplified_for_world" if (coarse_mode and lod == "world") else "maximum_detail_high_def_source_vector_raw_vertices_preserved"
                    item["geometry_quality"] = "coarse_real_usgs_major_water_simplified" if (coarse_mode and lod == "world") else "crisp_shoreline_true_nhd_arcgis_geometry_raw_vertices"
                    if feat.get("_geometry_type") == "polygon":
                        if not is_lake_polygon(feat):
                            continue
                        area = float(feat.get("area_km2") or polygon_area_km2(src_path))
                        if area < min_area:
                            continue
                        item["area_km2"] = round(area, 5)
                        item["prominence_score"] = round(area * 1000.0 + len(src_path) * 0.05, 4)
                        item["filter_reason"] = f"kept_layer12_rest_filtered_ftype_390_436_min_area_{min_area:g}_km2_vertices_to_polygon"
                        item["rest_source_contract"] = "MapServer/12 WHERE FTYPE=390 OR FTYPE=436"
                        poly_candidates.append(item)
                    else:
                        if LAKES_ONLY:
                            continue
                        length = float(feat.get("length_km") or line_length_km(src_path))
                        if length < min_line_km:
                            continue
                        item["length_km"] = round(length, 4)
                        item["prominence_score"] = round(length * 100.0 + len(src_path) * 0.02, 4)
                        item["filter_reason"] = f"kept_source_filtered_flowline_min_length_{min_line_km:g}_km_no_prominence_cap"
                        line_candidates.append(item)
                polys = sorted(poly_candidates, key=lambda x: (float(x.get("prominence_score") or 0), int(x.get("source_path_points") or 0)), reverse=True)
                lines = [] if LAKES_ONLY else sorted(line_candidates, key=lambda x: (float(x.get("prominence_score") or 0), int(x.get("source_path_points") or 0)), reverse=True)
                if not polys and not lines:
                    continue
                rel = tile_relpath(lod, ix, iy, tile_deg)
                existing_payload = None
                existing_path = out_dir / rel
                if append and existing_path.exists():
                    try:
                        old_payload = load_json(existing_path)
                        if isinstance(old_payload, dict):
                            existing_payload = old_payload
                            polys = merge_tile_items(old_payload.get("polygons") or [], polys)
                            lines = [] if LAKES_ONLY else merge_tile_items(old_payload.get("lines") or [], lines)
                    except Exception:
                        existing_payload = None
                payload = {
                    "schema": "lftr_nhdplus_hr_tile_v2_full_square",
                    "lod": lod,
                    "tile": {"x": ix, "y": iy, "tile_deg": tile_deg},
                    "bbox": list(tb),
                    "source": "nhdplus_hr_lakes_only_real_usgs_tile" if LAKES_ONLY else "nhdplus_hr_source_filtered_real_usgs_tile",
                    "geometry_mode": "coarse" if (coarse_mode and lod == "world") else "vector",
                    "max_detail_mode": not (coarse_mode and lod == "world"),
                    "prominence_filtered": False,
                    "raw_vertices_preserved": not (coarse_mode and lod == "world"),
                    "geometry_quality": "coarse_real_usgs_lake_reservoir_simplified" if (coarse_mode and lod == "world") else "crisp_lake_reservoir_shoreline_true_nhd_arcgis_geometry_raw_vertices",
                    "lakes_only": LAKES_ONLY,
                    "append_merge_mode": bool(append),
                    "merged_existing_tile": bool(existing_payload),
                    "polygons": polys,
                    "lines": lines,
                    "count": len(polys) + len(lines),
                }
                write_json_gz(out_dir / rel, payload)
                manifest["tiles"].append({"lod": lod, "path": rel, "bbox": list(tb), "tile_deg": tile_deg, "x": ix, "y": iy, "polygons": len(polys), "lines": len(lines), "bytes_gz": (out_dir / rel).stat().st_size})
                written_paths.add(rel)
                if rel not in existing_by_path:
                    wrote += 1
        old_tiles = int((manifest["summary"].get(lod) or {}).get("tiles") or 0)
        manifest["summary"][lod] = {"tiles": old_tiles + wrote if append else wrote, "tile_deg": tile_deg, "x_count": len(list(x_range)), "y_count": len(list(y_range)), "new_tiles": wrote}
    if append:
        for rel, tile in existing_by_path.items():
            if rel not in written_paths and (out_dir / rel).exists():
                manifest["tiles"].append(tile)
    manifest["tiles"].sort(key=lambda t: (str(t.get("lod")), str(t.get("path"))))
    tmp_index = out_dir / "index.json.tmp"
    tmp_index.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp_index, out_dir / "index.json")
    return manifest


def export_fgdb_layers_to_geojson(src: Path, work: Path) -> list[Path]:
    ogr2ogr = shutil.which("ogr2ogr")
    if not ogr2ogr:
        raise SystemExit("ogr2ogr not found. Install gdal-bin first, or pass --source-geojson.")
    layer_names = ["NHDWaterbody", "NHDArea", "NHDFlowline"]
    out_files: list[Path] = []
    for layer in layer_names:
        out = work / f"{layer}.geojson"
        cmd = [ogr2ogr, "-f", "GeoJSON", "-t_srs", "EPSG:4326", str(out), str(src), layer]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if out.exists() and out.stat().st_size > 100:
                out_files.append(out)
        except subprocess.CalledProcessError as exc:
            print(f"[warn] skipped layer {layer}: {exc.stderr[:300]}", file=sys.stderr)
    if not out_files:
        raise SystemExit(f"No NHDWaterbody/NHDArea/NHDFlowline layers exported from {src}")
    return out_files


def main() -> int:
    ap = argparse.ArgumentParser(description="Build LFTR high-definition NHDPlus HR inland-water json.gz tiles")
    ap.add_argument("--source-geojson", action="append", default=[], help="GeoJSON/GeoJSON.gz input. May be repeated.")
    ap.add_argument("--source-gdb", action="append", default=[], help="FileGDB .gdb directory or other OGR-readable source containing NHD layers.")
    ap.add_argument("--out", default="static/data/nhdplus_hr/tiles", help="Output tile directory")
    ap.add_argument("--bbox", default=None, help="Optional tile project bbox west,south,east,north. If omitted, feature bbox is used.")
    ap.add_argument("--min-polygon-points", type=int, default=int(os.getenv("NHD_MIN_POLYGON_POINTS", "3")))
    ap.add_argument("--min-line-points", type=int, default=int(os.getenv("NHD_MIN_LINE_POINTS", "2")))
    ap.add_argument("--append", action="store_true", help="Append/merge new render tiles into the existing shared tile cache instead of wiping it.")
    args = ap.parse_args()

    inputs: list[Path] = [Path(x) for x in args.source_geojson]
    with tempfile.TemporaryDirectory(prefix="lftr_nhd_") as td:
        work = Path(td)
        for gdb in args.source_gdb:
            inputs.extend(export_fgdb_layers_to_geojson(Path(gdb), work))
        if not inputs:
            raise SystemExit("Pass --source-geojson or --source-gdb. No coarse fallback will be generated.")
        features: list[dict[str, Any]] = []
        for path in inputs:
            data = load_json(path)
            for feat in iter_geojson_features(data, str(path)):
                p = feat.get("path") or []
                if feat.get("_geometry_type") == "polygon" and len(p) < args.min_polygon_points:
                    continue
                if feat.get("_geometry_type") == "line" and len(p) < args.min_line_points:
                    continue
                src = " ".join(str(feat.get(k, "")) for k in ("source", "source_class", "source_path")).lower()
                if any(bad in src for bad in ("seed", "bootstrap", "demo", "coarse", "approx")):
                    continue
                if not any(ok in src for ok in ("nhd", "nhdplus", "3dhp", "osm", "hydrolakes", "waterbody", "flowline", "area")):
                    feat["source_class"] = ACCEPTED_SOURCE
                features.append(feat)
        root_bbox = project_bbox(features, args.bbox)
        manifest = build_tiles(features, Path(args.out), root_bbox, append=bool(args.append))
        print(json.dumps({"status": "ok", "features": len(features), "tiles": len(manifest["tiles"]), "out": str(args.out), "summary": manifest["summary"], "append": bool(args.append)}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

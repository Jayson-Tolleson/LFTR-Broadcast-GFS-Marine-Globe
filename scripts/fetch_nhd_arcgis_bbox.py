#!/usr/bin/env python3
"""Fetch high-detail NHD geometry from The National Map ArcGIS service by bbox.

This is a runtime viewport source fetcher. It is NOT blocking during page boot.
It is a strict real-source path, not mock/coarse fallback: it queries small
geographic tiles from the public NHD MapServer, converts Esri features to
app-ready GeoJSON, and writes one GeoJSON/GeoJSON.gz source file for
scripts/build_nhdplus_hr_tiles.py.

Default layer is NHD Waterbody only:
  12 Waterbody - Large Scale

LFTR current Inland Waters mode is LAKES ONLY. Streams/rivers/flowlines are
intentionally excluded from the live ArcGIS request. They can be brought back
later by setting NHD_INLAND_LAKES_ONLY=0 and NHD_ARCGIS_LAYERS=12,9,6.
"""
from __future__ import annotations
import os
import argparse, gzip, json, math, os, sys, time, urllib.parse, urllib.request
from pathlib import Path
from typing import Any

SERVICE = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"
DEFAULT_LAYERS = {
    6:  {"name": "NHDFlowline Large Scale", "kind": "river", "geometry": "line"},
    9:  {"name": "NHDArea Large Scale", "kind": "area", "geometry": "polygon"},
    12: {"name": "NHDWaterbody Large Scale", "kind": "waterbody", "geometry": "polygon"},
}

# Source-side Inland Waterways contract.  The public NHD ArcGIS service exposes
# FCODE metadata, so request only the classes the app is supposed to draw.
# This keeps dry drainage networks, ditches, connectors, and washes out of the
# payload before our server ever needs to classify/render them.  The downstream
# app still keeps a tiny safety validator in case a source service leaks a mixed
# feature through.
CLEAN_WATER_ONLY = str(os.getenv("NHD_ARCGIS_CLEAN_WATER_ONLY", "1")).lower() not in {"0", "false", "no", "off"}
# Default is lakes/ponds/reservoirs only. Rivers/streams are not requested or rendered.
LAKES_ONLY = str(os.getenv("NHD_INLAND_LAKES_ONLY", "1")).lower() not in {"0", "false", "no", "off"}
SOURCE_WHERE_BY_LAYER = {
    # Flowline and Area are intentionally not used in lakes-only mode.
    6: "FCODE = 46006",
    9: "FTYPE = 390 OR FTYPE = 436",
    # Waterbody layer 12 is dynamically filtered by zoom/tier via env.
    12: "FTYPE = 390 OR FTYPE = 436",
}


def source_min_area_km2() -> float:
    raw = os.getenv("NHD_ARCGIS_SOURCE_MIN_AREA_KM2", "0.6475")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 0.6475


def source_where_for_layer(layer_id: int) -> str:
    base = SOURCE_WHERE_BY_LAYER.get(layer_id, "1=1")
    if not CLEAN_WATER_ONLY:
        return "1=1"
    if int(layer_id) != 12:
        return base
    min_area = source_min_area_km2()
    if min_area <= 0.0:
        return f"({base})"
    return f"({base}) AND AREASQKM >= {min_area:.6g}"


def parse_bbox(s: str) -> tuple[float, float, float, float]:
    vals = [float(x.strip()) for x in str(s).split(",")]
    if len(vals) != 4:
        raise ValueError("bbox must be west,south,east,north")
    w, south, e, n = vals
    if not (w < e and south < n):
        raise ValueError(f"invalid bbox {s!r}")
    return w, south, e, n


def split_bbox(bbox: str, step_deg: float) -> list[tuple[float, float, float, float]]:
    w, s, e, n = parse_bbox(bbox)
    out: list[tuple[float, float, float, float]] = []
    y = s
    while y < n - 1e-9:
        y2 = min(n, y + step_deg)
        x = w
        while x < e - 1e-9:
            x2 = min(e, x + step_deg)
            out.append((x, y, x2, y2))
            x = x2
        y = y2
    return out


def prioritize_tiles_center_first(tiles: list[tuple[float, float, float, float]], bbox: str) -> list[tuple[float, float, float, float]]:
    """Return source tiles in viewport-coverage order, not center-biased order.

    Earlier builds fetched source squares nearest the bbox center first.  On a
    wide California startup view that made South/Central Valley tiles appear
    immediately while the rest of the visible viewport waited.  Runtime Inland
    Waters should serve the *visible viewport* evenly: every first batch should
    include west/east/north/south coverage before spending extra requests near
    the center.

    The function name is kept for compatibility with older callers, but the
    policy is now viewport-balanced-first.
    """
    if not tiles:
        return []
    w, s, e, n = parse_bbox(bbox)
    width = max(1e-9, e - w)
    height = max(1e-9, n - s)

    def tile_center(tb: tuple[float, float, float, float]) -> tuple[float, float]:
        tw, ts, te, tn = tb
        return ((tw + te) * 0.5, (ts + tn) * 0.5)

    # Bucket the viewport into a small screen-like grid and round-robin one tile
    # per bucket.  When max_source_tiles slices the result, it gets broad
    # viewport coverage instead of a tight center cluster.
    buckets: dict[tuple[int, int], list[tuple[float, tuple[float, float, float, float]]]] = {}
    for tb in tiles:
        tx, ty = tile_center(tb)
        nx = min(0.999999, max(0.0, (tx - w) / width))
        ny = min(0.999999, max(0.0, (ty - s) / height))
        bx = int(nx * 4)
        by = int(ny * 4)
        # Tie-break inside a bucket by tile area then stable coordinate order.
        tw, ts, te, tn = tb
        area = max(0.0, (te - tw) * (tn - ts))
        buckets.setdefault((bx, by), []).append((area, tb))

    for vals in buckets.values():
        vals.sort(key=lambda item: (item[0], item[1][1], item[1][0]))

    # Visit buckets in a screen sweep that emphasizes visible coastline/edges
    # and avoids all first requests landing in one central inland cluster.
    bucket_order = []
    for by in range(4):
        xs = range(4) if by % 2 == 0 else range(3, -1, -1)
        for bx in xs:
            if (bx, by) in buckets:
                bucket_order.append((bx, by))

    out: list[tuple[float, float, float, float]] = []
    while bucket_order:
        next_order = []
        for key in bucket_order:
            vals = buckets.get(key) or []
            if vals:
                out.append(vals.pop(0)[1])
            if vals:
                next_order.append(key)
        bucket_order = next_order

    # Include any stragglers exactly once.
    seen = {tuple(x) for x in out}
    for tb in tiles:
        if tuple(tb) not in seen:
            out.append(tb)
    return out


def request_json(url: str, *, retries: int | None = None, timeout: int | None = None, debug_dir: Path | None = None) -> dict[str, Any] | None:
    if retries is None:
        retries = int(os.getenv("NHD_ARCGIS_RETRIES", "1") or "1")
    if timeout is None:
        timeout = int(os.getenv("NHD_ARCGIS_TIMEOUT_SECONDS", "14") or "14")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LFTR-NHD-ArcGIS-viewport-cache/1.0", "Accept": "application/json,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                text = body.decode("utf-8", errors="replace")
                try:
                    return json.loads(text)
                except Exception as exc:
                    if debug_dir:
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        p = debug_dir / f"arcgis_non_json_{int(time.time()*1000)}.txt"
                        p.write_text("URL: " + url + "\n\n" + text[:200000], encoding="utf-8")
                        print(f"[arcgis] non-json saved {p}")
                    last = exc
        except Exception as exc:
            last = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    print(f"[arcgis] request failed after retries: {last} url={url[:240]}", file=sys.stderr)
    return None


def esri_polygon_to_features(geom: dict[str, Any], attrs: dict[str, Any], layer_info: dict[str, str], layer_id: int) -> list[dict[str, Any]]:
    feats = []
    for ring in geom.get("rings") or []:
        if not isinstance(ring, list) or len(ring) < 4:
            continue
        coords = []
        for pt in ring:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                coords.append([float(pt[0]), float(pt[1])])
        if len(coords) >= 4:
            feats.append({
                "type": "Feature",
                "properties": props_from_attrs(attrs, layer_info, layer_id),
                "geometry": {"type": "Polygon", "coordinates": [coords]},
            })
    return feats


def esri_polyline_to_features(geom: dict[str, Any], attrs: dict[str, Any], layer_info: dict[str, str], layer_id: int) -> list[dict[str, Any]]:
    feats = []
    for path in geom.get("paths") or []:
        if not isinstance(path, list) or len(path) < 2:
            continue
        coords = []
        for pt in path:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                coords.append([float(pt[0]), float(pt[1])])
        if len(coords) >= 2:
            feats.append({
                "type": "Feature",
                "properties": props_from_attrs(attrs, layer_info, layer_id),
                "geometry": {"type": "LineString", "coordinates": coords},
            })
    return feats


def props_from_attrs(attrs: dict[str, Any], layer_info: dict[str, str], layer_id: int) -> dict[str, Any]:
    def pick(*names: str) -> Any:
        lower = {str(k).lower(): v for k, v in attrs.items()}
        for name in names:
            if name in attrs:
                return attrs.get(name)
            if name.lower() in lower:
                return lower.get(name.lower())
        return None
    name = pick("GNIS_NAME", "gnis_name", "Name") or "Unnamed inland water"
    ftype = pick("FTYPE", "FType", "FTypeDesc") or layer_info.get("kind") or "water"
    fcode = pick("FCODE", "FCode")
    oid = pick("OBJECTID", "ObjectID", "FID")
    area = pick("AREASQKM", "AreaSqKm", "areasqkm")
    shape_area = pick("Shape_Area", "shape_area")
    pid = pick("permanent_identifier", "Permanent_Identifier")
    return {
        "name": name,
        "gnis_name": name,
        "kind": str(ftype).lower(),
        "fcode": fcode,
        "FCode": fcode,
        "ftype": ftype,
        "FType": ftype,
        "AREASQKM": area,
        "area_km2": area,
        "Shape_Area": shape_area,
        "shape_area": shape_area,
        "permanent_identifier": pid,
        "objectid": oid,
        "source": "USGS NHD ArcGIS layer12 REST source-filtered lakes/reservoirs",
        "source_class": layer_info.get("name") or f"NHD layer {layer_id}",
        "shoreline_truth": "rest_query_layer12_ftype_390_436_geojson_vertices",
        "geometry_quality": "source_filtered_lake_reservoir_geojson",
    }


def query_layer(layer_id: int, layer_info: dict[str, str], bbox: tuple[float, float, float, float], *, page_size: int, max_records: int, debug_dir: Path) -> list[dict[str, Any]]:
    """Query a single NHD ArcGIS layer using the proven GeoJSON envelope contract.

    The hydro.nationalmap.gov NHD service accepts f=geojson with comma-envelope
    geometry. Earlier versions used f=json + selected outFields + Esri envelope
    JSON, which returned 400 "Failed to execute query" on the server.
    """
    w, s, e, n = bbox
    envelope = f"{w:.8f},{s:.8f},{e:.8f},{n:.8f}"
    features: list[dict[str, Any]] = []
    offset = 0
    empty_pages = 0
    while offset < max_records:
        source_where = source_where_for_layer(layer_id)
        params = {
            "f": "geojson",
            "where": source_where,
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": "4326",
            "outFields": "OBJECTID,permanent_identifier,gnis_name,areasqkm,Shape_Area,ftype,fcode,resolution,visibilityfilter,elevation",
            "returnGeometry": "true",
            "returnZ": "false",
            "returnM": "false",
            "returnTrueCurves": "false",
            "returnExceededLimitFeatures": "true",
            "sqlFormat": "standard",
            "resultRecordCount": str(page_size),
            "resultOffset": str(offset),
        }
        url = f"{SERVICE}/{layer_id}/query?" + urllib.parse.urlencode(params)
        data = request_json(url, debug_dir=debug_dir)
        if not data:
            break
        if data.get("error"):
            # Some NHD ArcGIS layers reject larger resultRecordCount values with
            # a generic 400. Retry the same envelope with a very small page size
            # before giving up. This uses the exact f=geojson comma-envelope
            # contract verified from the server.
            err = data.get("error")
            if page_size > 50:
                small_params = dict(params)
                small_params["resultRecordCount"] = "50"
                small_url = f"{SERVICE}/{layer_id}/query?" + urllib.parse.urlencode(small_params)
                small = request_json(small_url, debug_dir=debug_dir)
                if small and not small.get("error"):
                    data = small
                else:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    p = debug_dir / f"arcgis_error_layer_{layer_id}_{int(time.time()*1000)}.json"
                    p.write_text(json.dumps({"url": url, "retry_url": small_url, "error": err, "retry_error": (small or {}).get("error")}, indent=2), encoding="utf-8")
                    print(f"[arcgis] layer={layer_id} error={err} saved={p}", file=sys.stderr)
                    break
            else:
                debug_dir.mkdir(parents=True, exist_ok=True)
                p = debug_dir / f"arcgis_error_layer_{layer_id}_{int(time.time()*1000)}.json"
                p.write_text(json.dumps({"url": url, "error": err}, indent=2), encoding="utf-8")
                print(f"[arcgis] layer={layer_id} error={err} saved={p}", file=sys.stderr)
                break
        rows = data.get("features") or []
        if not rows:
            empty_pages += 1
            if empty_pages >= 1:
                break
        empty_pages = 0
        for row in rows:
            geom = row.get("geometry") or {}
            props = props_from_attrs(row.get("properties") or {}, layer_info, layer_id)
            props["rest_where"] = source_where
            props["rest_layer"] = layer_id
            props["rest_source_url_sample"] = url[:500]
            # The service already returns valid GeoJSON geometry. Keep it directly.
            gtype = geom.get("type")
            coords = geom.get("coordinates")
            if not gtype or coords is None:
                continue
            if layer_info.get("geometry") == "polygon" and gtype not in {"Polygon", "MultiPolygon"}:
                continue
            if layer_info.get("geometry") == "line" and gtype not in {"LineString", "MultiLineString"}:
                continue
            features.append({"type": "Feature", "properties": props, "geometry": geom})
        print(f"[arcgis] layer={layer_id} bbox={w:.3f},{s:.3f},{e:.3f},{n:.3f} where={source_where!r} offset={offset} rows={len(rows)} features={len(features)}")
        # ArcGIS GeoJSON uses exceededTransferLimit when more pages are available.
        if len(rows) < page_size and not data.get("exceededTransferLimit"):
            break
        if not data.get("exceededTransferLimit") and len(rows) == 0:
            break
        offset += page_size
    return features


def write_geojson(path: Path, features: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "metadata": meta, "features": features}
    if path.suffix == ".gz":
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=7) as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp, path)
    else:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch NHD high-detail ArcGIS features by bbox into app-ready GeoJSON")
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tile-deg", type=float, default=1.0, help="Query source service in small bbox squares")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-records-per-layer-tile", type=int, default=1200)
    ap.add_argument("--layers", default=os.getenv("NHD_ARCGIS_LAYERS", "12"), help="Comma-separated NHD MapServer layer ids; default 12 = NHDWaterbody only, lakes/ponds/reservoirs. Use 12,9,6 later to re-enable areas/streams.")
    ap.add_argument("--timeout-seconds", type=int, default=int(os.getenv("NHD_ARCGIS_TIMEOUT_SECONDS", "14") or "14"), help="Per ArcGIS page request timeout; kept short for first-run progressive tiles")
    ap.add_argument("--retries", type=int, default=int(os.getenv("NHD_ARCGIS_RETRIES", "1") or "1"), help="Per ArcGIS page retry count")
    ap.add_argument("--cache-days", type=int, default=31)
    ap.add_argument("--max-source-tiles", type=int, default=0, help="Fetch at most this many source bbox tiles, center-first. 0 means all.")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all-nhd-classes", action="store_true", help="Debug escape hatch: disable source-side lakes/reservoirs/perennial-rivers-only filter")
    args = ap.parse_args()
    out = Path(args.out)
    meta_path = out.with_suffix(out.suffix + ".meta.json")
    if out.exists() and out.stat().st_size > 0 and not args.force:
        age_days = (time.time() - out.stat().st_mtime) / 86400.0
        if age_days <= args.cache_days:
            print(json.dumps({"status": "cache_hit", "out": str(out), "age_days": round(age_days, 2), "cache_days": args.cache_days}, indent=2))
            return 0
    if args.all_nhd_classes:
        os.environ["NHD_ARCGIS_CLEAN_WATER_ONLY"] = "0"
        global CLEAN_WATER_ONLY
        CLEAN_WATER_ONLY = False
    debug_dir = out.parent / "_arcgis_debug"
    layers = []
    layer_spec = str(args.layers or "12")
    if LAKES_ONLY:
        layer_spec = "12"
    for raw in layer_spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        lid = int(raw)
        layers.append((lid, DEFAULT_LAYERS.get(lid, {"name": f"NHD layer {lid}", "kind": "water", "geometry": "polygon"})))
    all_tiles = prioritize_tiles_center_first(split_bbox(args.bbox, args.tile_deg), args.bbox)
    total_tiles_available = len(all_tiles)
    if int(args.max_source_tiles or 0) > 0:
        tiles = all_tiles[: max(1, int(args.max_source_tiles))]
    else:
        tiles = all_tiles
    where_debug = {lid: source_where_for_layer(lid) for lid, _ in layers} if CLEAN_WATER_ONLY else {}
    print(f"[arcgis] source tile plan selected={len(tiles)} available={total_tiles_available} tile_deg={args.tile_deg} viewport_balanced=true lakes_only={LAKES_ONLY} clean_water_only={CLEAN_WATER_ONLY} source_min_area_km2={source_min_area_km2()} layers={[lid for lid, _ in layers]} where_by_layer={where_debug}")
    all_features: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ti, tb in enumerate(tiles, 1):
        print(f"[arcgis tile {ti}/{len(tiles)}] bbox={','.join(f'{v:.6f}' for v in tb)}")
        for lid, info in layers:
            old_timeout = os.environ.get("NHD_ARCGIS_TIMEOUT_SECONDS")
            old_retries = os.environ.get("NHD_ARCGIS_RETRIES")
            os.environ["NHD_ARCGIS_TIMEOUT_SECONDS"] = str(args.timeout_seconds)
            os.environ["NHD_ARCGIS_RETRIES"] = str(args.retries)
            feats = query_layer(lid, info, tb, page_size=args.page_size, max_records=args.max_records_per_layer_tile, debug_dir=debug_dir)
            if old_timeout is None: os.environ.pop("NHD_ARCGIS_TIMEOUT_SECONDS", None)
            else: os.environ["NHD_ARCGIS_TIMEOUT_SECONDS"] = old_timeout
            if old_retries is None: os.environ.pop("NHD_ARCGIS_RETRIES", None)
            else: os.environ["NHD_ARCGIS_RETRIES"] = old_retries
            for feat in feats:
                props = feat.get("properties") or {}
                geom = feat.get("geometry") or {}
                key = f"{lid}:{props.get('objectid')}:{geom.get('type')}:{str(geom.get('coordinates'))[:160]}"
                if key in seen:
                    continue
                seen.add(key)
                all_features.append(feat)
    meta = {
        "status": "ok" if all_features else "empty",
        "source": "USGS The National Map NHD ArcGIS MapServer layer12 source-filtered lake polygons",
        "service": SERVICE,
        "query_contract": f"MapServer/12/query?f=geojson&where={urllib.parse.quote_plus(source_where_for_layer(12))}&geometry=<west,south,east,north>&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326&outFields=OBJECTID,permanent_identifier,gnis_name,areasqkm,Shape_Area,ftype,fcode,resolution,visibilityfilter,elevation&returnGeometry=true",
        "source_min_area_km2": source_min_area_km2(),
        "bbox": args.bbox,
        "tile_deg": args.tile_deg,
        "source_tiles_selected": len(tiles),
        "source_tiles_available": total_tiles_available,
        "max_source_tiles": int(args.max_source_tiles or 0),
        "partial_cache": bool(int(args.max_source_tiles or 0) > 0 and len(tiles) < total_tiles_available),
        "layers": [lid for lid, _ in layers],
        "lakes_only": LAKES_ONLY,
        "features": len(all_features),
        "cache_days": args.cache_days,
        "fetched_at": int(time.time()),
    }
    write_geojson(out, all_features, meta)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0 if all_features else 3


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations
import os

from typing import Any
import math
from server.gfs.ocean_landmask import ocean_mask_from_grids, resample_mask_nearest, mask_stats, erode_ocean_mask


def _safe(v: Any, default: float = float('nan')) -> float:
    try:
        value = float(v)
    except Exception:
        return default
    return value if math.isfinite(value) else default


def _norm(v: float, lo: float, hi: float) -> float:
    if not math.isfinite(v) or hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _lat_lon(i: int, j: int, ny: int, nx: int, bbox: list[float]) -> tuple[float, float]:
    west, south, east, north = bbox
    lat = south + ((i + 0.5) * (north - south) / max(1, ny))
    lon = west + ((j + 0.5) * (east - west) / max(1, nx))
    return lat, lon


def _resample_nearest(grid: list[list[float]], ny: int, nx: int) -> list[list[float]]:
    src_ny = len(grid)
    src_nx = len(grid[0]) if src_ny else 0
    if src_ny < 1 or src_nx < 1 or ny < 1 or nx < 1:
        return [[float('nan') for _ in range(max(0, nx))] for _ in range(max(0, ny))]
    out: list[list[float]] = []
    for i in range(ny):
        si = min(src_ny - 1, max(0, int(i * src_ny / ny)))
        row: list[float] = []
        for j in range(nx):
            sj = min(src_nx - 1, max(0, int(j * src_nx / nx)))
            row.append(_safe(grid[si][sj]))
        out.append(row)
    return out


def _resample_bilinear(grid: list[list[float]], ny: int, nx: int) -> list[list[float]]:
    src_ny = len(grid)
    src_nx = len(grid[0]) if src_ny else 0
    if src_ny < 1 or src_nx < 1 or ny < 1 or nx < 1:
        return [[float('nan') for _ in range(max(0, nx))] for _ in range(max(0, ny))]
    out: list[list[float]] = []
    for i in range(ny):
        y = ((i + 0.5) * src_ny / ny) - 0.5
        y0 = max(0, min(src_ny - 1, int(math.floor(y))))
        y1 = max(0, min(src_ny - 1, y0 + 1))
        fy = y - y0
        row: list[float] = []
        for j in range(nx):
            x = ((j + 0.5) * src_nx / nx) - 0.5
            x0 = max(0, min(src_nx - 1, int(math.floor(x))))
            x1 = max(0, min(src_nx - 1, x0 + 1))
            fx = x - x0
            q00 = _safe(grid[y0][x0])
            q10 = _safe(grid[y0][x1], q00)
            q01 = _safe(grid[y1][x0], q00)
            q11 = _safe(grid[y1][x1], q10)
            if not any(math.isfinite(v) for v in (q00, q10, q01, q11)):
                row.append(float('nan'))
                continue

            valid_corners = [v for v in (q00, q10, q01, q11) if math.isfinite(v)]
            fill_val = sum(valid_corners) / len(valid_corners) if valid_corners else 0.0
            if not math.isfinite(q00): q00 = fill_val
            if not math.isfinite(q10): q10 = fill_val
            if not math.isfinite(q01): q01 = fill_val
            if not math.isfinite(q11): q11 = fill_val
            top = q00 + ((q10 - q00) * fx)
            bottom = q01 + ((q11 - q01) * fx)
            row.append(top + ((bottom - top) * fy))
        out.append(row)
    return out


def _central_diff(grid: list[list[float]], i: int, j: int) -> tuple[float, float]:
    ny = len(grid)
    nx = len(grid[0]) if ny else 0
    if i <= 0 or j <= 0 or i >= ny - 1 or j >= nx - 1:
        return 0.0, 0.0
    left = _safe(grid[i][j - 1])
    right = _safe(grid[i][j + 1])
    down = _safe(grid[i - 1][j])
    up = _safe(grid[i + 1][j])
    if not all(math.isfinite(v) for v in (left, right, down, up)):
        return 0.0, 0.0
    return (right - left) * 0.5, (up - down) * 0.5


def _harbor_fill(grid: list[list[float]], i: int, j: int) -> float:
    vals: list[float] = []
    ny = len(grid)
    nx = len(grid[0]) if ny else 0
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
        yi = i + dy
        xj = j + dx
        if 0 <= yi < ny and 0 <= xj < nx:
            v = _safe(grid[yi][xj])
            if math.isfinite(v):
                vals.append(v)
    return (sum(vals) / len(vals)) if vals else float('nan')


def _components(mask: list[list[bool]]) -> list[list[tuple[int, int]]]:
    ny = len(mask)
    nx = len(mask[0]) if ny else 0
    seen: set[tuple[int, int]] = set()
    comps: list[list[tuple[int, int]]] = []
    for i in range(ny):
        for j in range(nx):
            if not mask[i][j] or (i, j) in seen:
                continue
            stack = [(i, j)]
            comp: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                if (y, x) in seen or not mask[y][x]:
                    continue
                seen.add((y, x))
                comp.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == 0 and dx == 0:
                            continue
                        nyy = y + dy
                        nxx = x + dx
                        if 0 <= nyy < ny and 0 <= nxx < nx and (nyy, nxx) not in seen:
                            stack.append((nyy, nxx))
            comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def _poly_from_cells(comp: list[tuple[int, int]], bbox: list[float], ny: int, nx: int) -> list[list[float]]:
    if not comp:
        return []
    lats: list[float] = []
    lons: list[float] = []
    west, south, east, north = bbox
    half_lat = (north - south) / max(1, ny) * 0.52
    half_lon = (east - west) / max(1, nx) * 0.52
    for i, j in comp:
        lat, lon = _lat_lon(i, j, ny, nx, bbox)
        lats.extend([lat - half_lat, lat + half_lat])
        lons.extend([lon - half_lon, lon + half_lon])
    min_lat = min(lats)
    max_lat = max(lats)
    min_lon = min(lons)
    max_lon = max(lons)
    return [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]


def _component_probability(comp: list[tuple[int, int]], score_grid: list[list[float]]) -> float:
    vals = [_safe(score_grid[i][j], 0.0) for i, j in comp]
    vals = [v for v in vals if math.isfinite(v)]
    return (sum(vals) / len(vals)) if vals else 0.0



def _build_polygons(mask: list[list[bool]], score_grid: list[list[float]], bbox: list[float], ny: int, nx: int, max_shapes: int) -> list[dict[str, Any]]:
    """Build marching-square-style cell-edge contours from threshold masks.

    This is intentionally server-side so /gfs/api/bait-advanced is a real live
    bait polygon endpoint, not a location/marker proxy. Each active cell emits
    only the outer edges where the neighboring cell is inactive; edges are then
    stitched into closed rings.
    """
    west, south, east, north = bbox
    dlat = (north - south) / max(1, ny)
    dlon = (east - west) / max(1, nx)

    def xy(i: int, j: int) -> tuple[float, float, float, float]:
        y0 = south + i * dlat
        y1 = south + (i + 1) * dlat
        x0 = west + j * dlon
        x1 = west + (j + 1) * dlon
        return x0, y0, x1, y1

    def pkey(p: tuple[float, float]) -> str:
        return f"{p[0]:.7f},{p[1]:.7f}"

    def active(i: int, j: int) -> bool:
        return 0 <= i < ny and 0 <= j < nx and bool(mask[i][j])

    def stitch(edges: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[list[list[float]]]:
        nxt: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]] = {}
        for e in edges:
            nxt.setdefault(pkey(e[0]), []).append(e)
        used: set[int] = set()
        loops: list[list[list[float]]] = []
        for start_idx, edge in enumerate(edges):
            if start_idx in used:
                continue
            ring: list[tuple[float, float]] = []
            cur = edge
            guard = 0
            while cur is not None and guard <= len(edges) + 4:
                try:
                    idx = edges.index(cur)
                except ValueError:
                    break
                if idx in used:
                    break
                used.add(idx)
                ring.append(cur[0])
                end_key = pkey(cur[1])
                candidates = nxt.get(end_key) or []
                cur = None
                for cand in candidates:
                    try:
                        cidx = edges.index(cand)
                    except ValueError:
                        continue
                    if cidx not in used:
                        cur = cand
                        break
                guard += 1
            if len(ring) >= 3:
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                loops.append([[round(x, 7), round(y, 7)] for x, y in ring])
        return loops

    polygons: list[dict[str, Any]] = []
    for comp in _components(mask):
        if len(comp) < 2:
            continue
        comp_set = set(comp)
        edges: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for i, j in comp:
            x0, y0, x1, y1 = xy(i, j)
            # Clockwise edges; emit only exterior cell edges.
            if (i - 1, j) not in comp_set and not active(i - 1, j):
                edges.append(((x0, y0), (x1, y0)))
            if (i, j + 1) not in comp_set and not active(i, j + 1):
                edges.append(((x1, y0), (x1, y1)))
            if (i + 1, j) not in comp_set and not active(i + 1, j):
                edges.append(((x1, y1), (x0, y1)))
            if (i, j - 1) not in comp_set and not active(i, j - 1):
                edges.append(((x0, y1), (x0, y0)))
        for ring in stitch(edges):
            if len(ring) < 4:
                continue
            polygons.append({
                'coordinates': ring,
                'probability': round(_component_probability(comp, score_grid), 3),
                'solve_method': 'server_cell_edge_marching_squares',
            })
            if len(polygons) >= max_shapes:
                return polygons
    return polygons


def _wrap_lon_pm180(lon: float) -> float:
    x = float(lon)
    while x > 180.0:
        x -= 360.0
    while x <= -180.0:
        x += 360.0
    return x


def _polygon_path(poly: dict[str, Any]) -> list[dict[str, float]]:
    coords = poly.get('coordinates') or poly.get('path') or []
    path: list[dict[str, float]] = []
    for point in coords if isinstance(coords, list) else []:
        if isinstance(point, dict):
            lat = _safe(point.get('lat', point.get('latitude')))
            lon = _safe(point.get('lng', point.get('lon', point.get('longitude'))))
            alt = _safe(point.get('altitude'), 0.0)
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            lon = _safe(point[0])
            lat = _safe(point[1])
            alt = _safe(point[2], 0.0) if len(point) >= 3 else 0.0
        else:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0):
            continue
        out = {'lat': round(float(lat), 6), 'lng': round(_wrap_lon_pm180(float(lon)), 6)}
        if math.isfinite(alt):
            out['altitude'] = round(float(alt), 3)
        path.append(out)
    if len(path) >= 2 and abs(path[0]['lat'] - path[-1]['lat']) < 1e-7 and abs(path[0]['lng'] - path[-1]['lng']) < 1e-7:
        path.pop()
    return path if len(path) >= 3 else []


def _finalize_polygon_contract(polygons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for poly in polygons or []:
        if not isinstance(poly, dict):
            continue
        path = _polygon_path(poly)
        if len(path) < 3:
            continue
        row = dict(poly)
        row['path'] = path
        row['point_count'] = len(path)
        row['water_validated'] = True
        row.setdefault('type', row.get('band') or 'bait_zone')
        out.append(row)
    return out

def _derive_front_lines_from_sst(sst: list[list[float]], bbox: list[float]) -> list[dict[str, Any]]:
    ny = len(sst)
    nx = len(sst[0]) if ny else 0
    lines: list[dict[str, Any]] = []
    if ny < 3 or nx < 3:
        return lines
    for i in range(1, ny - 1, max(1, ny // 20)):
        segment: list[list[float]] = []
        for j in range(1, nx - 1):
            gx, gy = _central_diff(sst, i, j)
            if math.hypot(gx, gy) < 0.22:
                continue
            lat, lon = _lat_lon(i, j, ny, nx, bbox)
            segment.append([lon, lat])
        if len(segment) >= 2:
            lines.append({'coordinates': segment[:48]})
        if len(lines) >= 8:
            break
    return lines


def derive_bait_payload(atmospheric: dict[str, Any], ocean: dict[str, Any], bio: dict[str, Any], *, bbox: list[float]) -> dict[str, Any]:
    weather = atmospheric or {}
    wind_u_raw = weather.get('wind_u') or weather.get('u') or []
    wind_v_raw = weather.get('wind_v') or weather.get('v') or []
    precip_raw = weather.get('precip_rate') or []
    cloud_raw = weather.get('cloud_total') or []

    sst_raw = (ocean or {}).get('sst') or []
    current_u_raw = (ocean or {}).get('current_u') or []
    current_v_raw = (ocean or {}).get('current_v') or []
    chlorophyll_raw = (bio or {}).get('chlorophyll') or []

    source_grid = sst_raw or wind_u_raw or wind_v_raw or chlorophyll_raw
    src_ny = len(source_grid)
    src_nx = len(source_grid[0]) if src_ny else 0
    if src_ny < 1 or src_nx < 1:
        return {
            'bait': {
                'status': 'incomplete',
                'source': 'suppressed_incomplete',
                'polygons': [],
                'outer_polygons': [],
                'inner_polygons': [],
                'core_polygons': [],
                'meta': {'reason': 'missing_source_grid', 'valid_cells': 0},
            },
            'bait_score': [],
            'front_lines': [],
            'convergence_polygons': [],
            'boil_probability_polygons': [],
            'confidence': {'overall': 0.0},
        }

    bait_detail_multiplier = max(2, min(8, int(os.getenv('GFS_ADVANCED_BAIT_DETAIL_MULTIPLIER', '4') or '4')))
    bait_grid_cap = max(160, min(640, int(os.getenv('GFS_ADVANCED_BAIT_GRID_CAP', '420') or '420')))
    target_ny = min(bait_grid_cap, max(96, src_ny * bait_detail_multiplier))
    target_nx = min(bait_grid_cap, max(96, src_nx * bait_detail_multiplier))
    sst = _resample_bilinear(sst_raw, target_ny, target_nx) if sst_raw else [[float('nan') for _ in range(target_nx)] for _ in range(target_ny)]
    ocean_mask_raw = (ocean or {}).get('ocean_mask') or []
    if ocean_mask_raw:
        ocean_mask = resample_mask_nearest(ocean_mask_raw, target_ny, target_nx)
        mask_source = 'provider_ocean_mask_strict_interior'
    else:
        ocean_mask_raw, _mask_meta_tmp = ocean_mask_from_grids(sst=sst_raw, current_u=current_u_raw, current_v=current_v_raw, salinity=(ocean or {}).get('salinity') or [])
        ocean_mask = resample_mask_nearest(ocean_mask_raw, target_ny, target_nx) if ocean_mask_raw else [[math.isfinite(_safe(sst[i][j])) for j in range(target_nx)] for i in range(target_ny)]
        mask_source = 'derived_from_finite_sst'
    ocean_mask, ocean_mask_erode_meta = erode_ocean_mask(ocean_mask, 1)
    chlorophyll = _resample_bilinear(chlorophyll_raw, target_ny, target_nx) if chlorophyll_raw else [[0.12 for _ in range(target_nx)] for _ in range(target_ny)]
    current_u = _resample_bilinear(current_u_raw, target_ny, target_nx) if current_u_raw else [[0.0 for _ in range(target_nx)] for _ in range(target_ny)]
    current_v = _resample_bilinear(current_v_raw, target_ny, target_nx) if current_v_raw else [[0.0 for _ in range(target_nx)] for _ in range(target_ny)]
    wind_u = _resample_bilinear(wind_u_raw, target_ny, target_nx) if wind_u_raw else [[0.0 for _ in range(target_nx)] for _ in range(target_ny)]
    wind_v = _resample_bilinear(wind_v_raw, target_ny, target_nx) if wind_v_raw else [[0.0 for _ in range(target_nx)] for _ in range(target_ny)]
    precip = _resample_bilinear(precip_raw, target_ny, target_nx) if precip_raw else [[0.0 for _ in range(target_nx)] for _ in range(target_ny)]
    cloud = _resample_bilinear(cloud_raw, target_ny, target_nx) if cloud_raw else [[50.0 for _ in range(target_nx)] for _ in range(target_ny)]

    outer_mask = [[False for _ in range(target_nx)] for __ in range(target_ny)]
    inner_mask = [[False for _ in range(target_nx)] for __ in range(target_ny)]
    core_mask = [[False for _ in range(target_nx)] for __ in range(target_ny)]
    score_grid = [[0.0 for _ in range(target_nx)] for __ in range(target_ny)]
    bait_score: list[dict[str, Any]] = []

    valid_cells = 0
    harbor_filled_cells = 0
    land_masked_cells = 0
    for i in range(target_ny):
        for j in range(target_nx):
            if not bool(ocean_mask[i][j]):
                land_masked_cells += 1
                continue
            sst_v = _safe(sst[i][j])
            # Do not smear SST across land. A missing SST cell may only borrow from
            # immediate finite neighbors when the shared SST/ocean mask says this
            # target cell is water.
            if not math.isfinite(sst_v):
                sst_v = _harbor_fill(sst, i, j)
                if math.isfinite(sst_v):
                    harbor_filled_cells += 1
            if not math.isfinite(sst_v):
                continue
            chl_v = _safe(chlorophyll[i][j], 0.12)
            cu = _safe(current_u[i][j], 0.0)
            cv = _safe(current_v[i][j], 0.0)
            wu = _safe(wind_u[i][j], 0.0)
            wv = _safe(wind_v[i][j], 0.0)
            pr = _safe(precip[i][j], 0.0)
            cl = _safe(cloud[i][j], 50.0)

            sst_gx, sst_gy = _central_diff(sst, i, j)
            chl_gx, chl_gy = _central_diff(chlorophyll, i, j)
            cu_gx, cu_gy = _central_diff(current_u, i, j)
            cv_gx, cv_gy = _central_diff(current_v, i, j)
            sst_grad = math.hypot(sst_gx, sst_gy)
            chl_grad = math.hypot(chl_gx, chl_gy)
            convergence = max(0.0, -((cu_gx) + (cv_gy)))
            shear = math.hypot(cu_gy, cv_gx)
            wind_speed = math.hypot(wu, wv)
            current_speed = math.hypot(cu, cv)

            front_score = _norm(sst_grad, 0.04, 0.9)
            bio_edge_score = _norm(chl_grad, 0.005, 0.18)
            convergence_score = _norm(convergence, 0.0, 0.12)
            shear_score = _norm(shear, 0.0, 0.25)
            sst_score = 1.0 - abs(_norm(sst_v, 12.0, 28.0) - 0.5)
            chl_score = _norm(chl_v, 0.05, 2.0)
            current_score = _norm(current_speed, 0.02, 1.0)
            wind_score = 1.0 - _norm(wind_speed, 0.0, 18.0)
            rain_score = 1.0 - _norm(pr, 0.0, 1.0)
            cloud_score = 1.0 - abs(_norm(cl, 5.0, 95.0) - 0.45)

            score = (
                0.24 * front_score +
                0.15 * bio_edge_score +
                0.12 * convergence_score +
                0.08 * shear_score +
                0.16 * sst_score +
                0.12 * chl_score +
                0.07 * current_score +
                0.03 * wind_score +
                0.02 * rain_score +
                0.01 * cloud_score
            )
            score = max(0.0, min(1.0, score))
            score_grid[i][j] = score
            valid_cells += 1

            preferred_depth_m = max(2.0, min(45.0, 10.0 + (1.0 - chl_score) * 14.0 + max(0.0, sst_v - 20.0) * 0.9 - (front_score * 5.0)))
            depth_band_min = max(0.0, preferred_depth_m - 6.0)
            depth_band_max = preferred_depth_m + 8.0
            lat, lon = _lat_lon(i, j, target_ny, target_nx, bbox)
            depth_intel = {
                'source': 'advanced_bait_marching_squares_depth_model',
                'preferred_bait_depth_m': round(preferred_depth_m, 1),
                'preferred_bait_depth_ft': round(preferred_depth_m * 3.28084, 1),
                'bait_depth_m': round(preferred_depth_m, 1),
                'bait_depth_ft': round(preferred_depth_m * 3.28084, 1),
                'bait_depth_band_m': [round(depth_band_min, 1), round(depth_band_max, 1)],
                'bait_depth_band_ft': [round(depth_band_min * 3.28084, 1), round(depth_band_max * 3.28084, 1)],
                'visual_policy': 'above_water_extrusion_represents_bait_depth',
            }
            bait_score.append({
                'lat': lat,
                'lon': lon,
                'probability': round(score, 3),
                'preferred_depth_m': round(preferred_depth_m, 1),
                'preferred_bait_depth_m': round(preferred_depth_m, 1),
                'preferred_bait_depth_ft': round(preferred_depth_m * 3.28084, 1),
                'bait_depth_m': round(preferred_depth_m, 1),
                'bait_depth_ft': round(preferred_depth_m * 3.28084, 1),
                'depth_min_m': round(depth_band_min, 1),
                'depth_max_m': round(depth_band_max, 1),
                'bait_depth_band_m': [round(depth_band_min, 1), round(depth_band_max, 1)],
                'bait_depth_band_ft': [round(depth_band_min * 3.28084, 1), round(depth_band_max * 3.28084, 1)],
                'depth_intel': depth_intel,
                'renderer': 'advanced-bait-depth-contour',
                'water_mask_source': mask_source,
                'mask': 'hycom_sst_ocean_mask',
                'valid': True,
                'water': True,
                'driver': 'front' if front_score >= max(bio_edge_score, convergence_score) else ('bio_edge' if bio_edge_score >= convergence_score else 'current_convergence'),
            })

            if score >= 0.40:
                outer_mask[i][j] = True
            if score >= 0.53:
                inner_mask[i][j] = True
            if score >= 0.66:
                core_mask[i][j] = True

    outer_polygons = _build_polygons(outer_mask, score_grid, bbox, target_ny, target_nx, max_shapes=24)
    inner_polygons = _build_polygons(inner_mask, score_grid, bbox, target_ny, target_nx, max_shapes=18)
    core_polygons = _build_polygons(core_mask, score_grid, bbox, target_ny, target_nx, max_shapes=12)

    if not inner_polygons and bait_score:
        strongest = sorted(bait_score, key=lambda b: b['probability'], reverse=True)[:18]
        west, south, east, north = bbox
        cell_dx = (east - west) / max(1, target_nx)
        cell_dy = (north - south) / max(1, target_ny)
        for c in strongest:
            lon = c['lon']
            lat = c['lat']
            dx = max(0.01, cell_dx * 0.6)
            dy = max(0.01, cell_dy * 0.6)
            inner_polygons.append({
                'coordinates': [[lon - dx, lat - dy], [lon + dx, lat - dy], [lon + dx, lat + dy], [lon - dx, lat + dy], [lon - dx, lat - dy]],
                'probability': c['probability'],
                'preferred_depth_m': c['preferred_depth_m'],
                'depth_min_m': c['depth_min_m'],
                'depth_max_m': c['depth_max_m'],
                'driver': c['driver'],
            })

    def _attach_depth(polygons: list[dict[str, Any]], band: str = 'inner') -> list[dict[str, Any]]:
        for idx, poly in enumerate(polygons):
            p = _safe(poly.get('probability'), 0.5)
            if 'preferred_depth_m' not in poly:
                depth = max(2.0, min(45.0, 9.0 + ((1.0 - p) * 18.0)))
                poly['preferred_depth_m'] = round(depth, 1)
                poly['depth_min_m'] = round(max(0.0, depth - 6.0), 1)
                poly['depth_max_m'] = round(depth + 8.0, 1)
            depth = _safe(poly.get('preferred_depth_m'), 10.0)
            dmin = _safe(poly.get('depth_min_m'), max(0.0, depth - 6.0))
            dmax = _safe(poly.get('depth_max_m'), depth + 8.0)
            poly['band'] = poly.get('band') or band
            poly['preferred_bait_depth_m'] = round(depth, 1)
            poly['preferred_bait_depth_ft'] = round(depth * 3.28084, 1)
            poly['bait_depth_m'] = round(depth, 1)
            poly['bait_depth_ft'] = round(depth * 3.28084, 1)
            poly['bait_depth_band_m'] = [round(dmin, 1), round(dmax, 1)]
            poly['bait_depth_band_ft'] = [round(dmin * 3.28084, 1), round(dmax * 3.28084, 1)]
            poly['depth_intel'] = {
                'source': 'advanced_bait_marching_squares_depth_model',
                'preferred_bait_depth_m': round(depth, 1),
                'preferred_bait_depth_ft': round(depth * 3.28084, 1),
                'bait_depth_band_m': [round(dmin, 1), round(dmax, 1)],
                'bait_depth_band_ft': [round(dmin * 3.28084, 1), round(dmax * 3.28084, 1)],
                'visual_policy': 'above_water_extrusion_represents_bait_depth',
            }
            poly['renderer'] = 'advanced-bait-depth-contour'
            poly['contour_method'] = 'server_marching_squares_edge_stitched'
            poly['id'] = poly.get('id') or f"advanced-bait:{band}:{idx}:{round(p * 100)}"
            poly['driver'] = poly.get('driver') or 'surface_front'
        return polygons

    outer_polygons = _finalize_polygon_contract(_attach_depth(outer_polygons, 'outer'))
    inner_polygons = _finalize_polygon_contract(_attach_depth(inner_polygons, 'inner'))
    core_polygons = _finalize_polygon_contract(_attach_depth(core_polygons, 'core'))
    fronts = _derive_front_lines_from_sst(sst, bbox) if valid_cells > 0 else []
    overall = round((sum(item['probability'] for item in bait_score) / len(bait_score)), 3) if bait_score else 0.0
    polygon_total = len(outer_polygons) + len(inner_polygons) + len(core_polygons)

    return {
        'bait': {
            'status': 'ready' if valid_cells > 0 else 'incomplete',
            'source': 'full_stack' if valid_cells > 0 else 'suppressed_incomplete',
            'polygons': inner_polygons,
            'outer_polygons': outer_polygons,
            'inner_polygons': inner_polygons,
            'core_polygons': core_polygons,
            'meta': {
                'valid_cells': valid_cells,
                'harbor_filled_cells': harbor_filled_cells,
                'grid_ny': target_ny,
                'grid_nx': target_nx,
                'chlorophyll_available': bool(chlorophyll_raw),
                'sst_landmask_enabled': True,
                'sst_landmask_source': mask_source,
                'sst_landmask': mask_stats(ocean_mask),
                'land_masked_cells': land_masked_cells,
                'landmask_contract': 'bait_scores_and_polygons_only_from_eroded_shared_sst_ocean_mask',
                'sst_landmask_erode': ocean_mask_erode_meta,
                'renderer': 'advanced-bait-depth-contours',
                'contour_method': 'server_marching_squares_edge_stitched',
                'depth_policy': 'bait_depth_ft_and_depth_intel_attached_to_all_contour_polygons',
                'polygon_total': polygon_total,
                'outer_polygon_count': len(outer_polygons),
                'inner_polygon_count': len(inner_polygons),
                'core_polygon_count': len(core_polygons),
            },
        },
        'bait_score': bait_score,
        'front_lines': fronts,
        'convergence_polygons': inner_polygons[: min(6, len(inner_polygons))],
        'boil_probability_polygons': [p for p in core_polygons if _safe(p.get('probability'), 0.0) >= 0.72],
        'confidence': {'overall': overall},
    }

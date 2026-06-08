from __future__ import annotations

import math
from typing import Any


def finite_num(value: Any) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else float('nan')
    except Exception:
        return float('nan')


def grid_shape(grid: list[list[Any]] | None) -> tuple[int, int]:
    if not grid:
        return 0, 0
    return len(grid), len(grid[0]) if grid and grid[0] else 0


def ocean_mask_from_grids(
    *,
    sst: list[list[Any]] | None = None,
    current_u: list[list[Any]] | None = None,
    current_v: list[list[Any]] | None = None,
    salinity: list[list[Any]] | None = None,
) -> tuple[list[list[bool]], dict[str, Any]]:
    """Build the shared ocean/land gate for SST, currents, bait, boats, and shark intel.

    Policy:
    - finite SST is the highest-trust water mask;
    - when SST is unavailable, finite paired current U/V can keep a strict live-current mask;
    - salinity is only a last supporting signal, never enough to override an SST NaN.
    """
    grids = [g for g in (sst, current_u, current_v, salinity) if g]
    if not grids:
        return [], {"enabled": True, "method": "no_source_grid", "shape": [0, 0], "water_cells": 0, "land_cells": 0}
    ny = max(grid_shape(g)[0] for g in grids)
    nx = max(grid_shape(g)[1] for g in grids)
    out: list[list[bool]] = []
    water = 0
    land = 0
    used_sst = bool(sst)
    used_current = bool(current_u and current_v)
    used_salinity = bool(salinity)

    def val(grid: list[list[Any]] | None, y: int, x: int) -> float:
        if not grid or y >= len(grid) or y < 0:
            return float('nan')
        row = grid[y]
        if not row or x >= len(row) or x < 0:
            return float('nan')
        return finite_num(row[x])

    for y in range(ny):
        row: list[bool] = []
        for x in range(nx):
            s = val(sst, y, x)
            if used_sst:
                ok = math.isfinite(s)
            else:
                u = val(current_u, y, x)
                v = val(current_v, y, x)
                sal = val(salinity, y, x)
                ok = (used_current and math.isfinite(u) and math.isfinite(v)) or (used_salinity and math.isfinite(sal))
            row.append(bool(ok))
            if ok:
                water += 1
            else:
                land += 1
        out.append(row)
    total = water + land
    return out, {
        "enabled": True,
        "method": "finite_sst_primary_current_secondary",
        "shape": [ny, nx],
        "water_cells": water,
        "land_cells": land,
        "water_fraction": round(water / total, 4) if total else 0.0,
        "sst_primary": used_sst,
        "current_secondary": used_current,
        "salinity_support": used_salinity,
    }


def mask_grid(grid: list[list[Any]] | None, mask: list[list[bool]] | None, *, fill: float = float('nan')) -> list[list[float]]:
    if not grid:
        return []
    if not mask:
        return [[finite_num(v) for v in row] for row in grid]
    out: list[list[float]] = []
    for y, row in enumerate(grid):
        mrow = mask[y] if y < len(mask) else []
        new: list[float] = []
        for x, value in enumerate(row):
            ok = bool(mrow[x]) if x < len(mrow) else False
            new.append(finite_num(value) if ok else fill)
        out.append(new)
    return out


def resample_mask_nearest(mask: list[list[bool]] | None, ny: int, nx: int) -> list[list[bool]]:
    src_ny = len(mask or [])
    src_nx = len(mask[0]) if src_ny and mask and mask[0] else 0
    if src_ny < 1 or src_nx < 1 or ny < 1 or nx < 1:
        return [[False for _ in range(max(0, nx))] for __ in range(max(0, ny))]
    out: list[list[bool]] = []
    for y in range(ny):
        sy = min(src_ny - 1, max(0, int(y * src_ny / ny)))
        row: list[bool] = []
        for x in range(nx):
            sx = min(src_nx - 1, max(0, int(x * src_nx / nx)))
            row.append(bool(mask[sy][sx]))
        out.append(row)
    return out


def mask_stats(mask: list[list[bool]] | None) -> dict[str, Any]:
    ny = len(mask or [])
    nx = len(mask[0]) if ny and mask and mask[0] else 0
    water = sum(1 for row in (mask or []) for v in row if v)
    total = ny * nx
    return {"shape": [ny, nx], "water_cells": water, "land_cells": max(0, total - water), "water_fraction": round(water / total, 4) if total else 0.0}


def erode_ocean_mask(mask: list[list[bool]] | None, cells: int = 1) -> tuple[list[list[bool]], dict[str, Any]]:
    """Shrink water mask inward so render layers do not use shoreline/land-adjacent cells.

    HYCOM/OISST SST is coarser than Google 3D coastline. Finite SST is good
    water truth, but a finite coastal cell can visually overlap land. Eroding
    the mask by one cell gives downstream renderers an interior-water gate.
    """
    src = mask or []
    ny = len(src)
    nx = len(src[0]) if ny and src[0] else 0
    if ny < 1 or nx < 1 or int(cells or 0) <= 0:
        water = sum(1 for row in src for v in row if v)
        total = ny * nx
        return [[bool(v) for v in row] for row in src], {
            "enabled": bool(src), "cells": 0, "shape": [ny, nx],
            "water_cells_before": water, "water_cells_after": water,
            "rejected_edge_cells": 0, "water_fraction_after": round(water / total, 4) if total else 0.0,
        }
    cur = [[bool(v) for v in row] for row in src]
    before = sum(1 for row in cur for v in row if v)
    for _ in range(max(1, int(cells or 1))):
        out = [[False for _ in range(nx)] for __ in range(ny)]
        for y in range(ny):
            for x in range(nx):
                if not cur[y][x]:
                    continue
                ok = True
                for yy in range(max(0, y - 1), min(ny, y + 2)):
                    for xx in range(max(0, x - 1), min(nx, x + 2)):
                        if not cur[yy][xx]:
                            ok = False
                            break
                    if not ok:
                        break
                out[y][x] = ok
        cur = out
    after = sum(1 for row in cur for v in row if v)
    total = ny * nx
    return cur, {
        "enabled": True,
        "cells": max(1, int(cells or 1)),
        "shape": [ny, nx],
        "water_cells_before": before,
        "water_cells_after": after,
        "rejected_edge_cells": max(0, before - after),
        "water_fraction_after": round(after / total, 4) if total else 0.0,
        "contract": "finite_sst_mask_eroded_to_interior_water_before_any_render_layer",
    }

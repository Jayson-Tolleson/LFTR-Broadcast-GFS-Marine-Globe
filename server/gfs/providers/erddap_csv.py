from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class ErddapParseDiagnostics:
    row_count: int
    accepted_rows: int
    lat_count: int
    lon_count: int
    parser_rejected_rows: int
    preview_lines: list[str]


def parse_erddap_grid(text: str | None, *, preferred_value_columns: tuple[str, ...]) -> tuple[list[list[float]], ErddapParseDiagnostics]:
    if not text:
        return [], ErddapParseDiagnostics(0, 0, 0, 0, 0, [])

    lines = text.splitlines()
    preview = lines[:4]
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if len(rows) < 2:
        return [], ErddapParseDiagnostics(len(rows), 0, 0, 0, 0, preview)

    header = [str(c).strip().lower() for c in rows[0]]
    lat_idx = header.index("latitude") if "latitude" in header else 1
    lon_idx = header.index("longitude") if "longitude" in header else 2

    value_idx = None
    for col in preferred_value_columns:
        if col.lower() in header:
            value_idx = header.index(col.lower())
            break
    if value_idx is None:
        value_idx = len(header) - 1

    values_by_lat_lon: dict[float, dict[float, float]] = {}
    parser_rejected = 0
    for row in rows[1:]:
        if len(row) <= max(lat_idx, lon_idx, value_idx):
            parser_rejected += 1
            continue
        try:
            lat = float(row[lat_idx])
            lon = float(row[lon_idx])
            val = float(row[value_idx])
        except Exception:
            parser_rejected += 1
            continue
        if not math.isfinite(lat) or not math.isfinite(lon):
            parser_rejected += 1
            continue
        values_by_lat_lon.setdefault(lat, {})[lon] = val if math.isfinite(val) else float("nan")

    if not values_by_lat_lon:
        return [], ErddapParseDiagnostics(len(rows), 0, 0, 0, parser_rejected, preview)

    lats = sorted(values_by_lat_lon.keys())
    all_lons = sorted({lon for lon_map in values_by_lat_lon.values() for lon in lon_map.keys()})
    grid: list[list[float]] = []
    accepted = 0
    for lat in lats:
        lon_map = values_by_lat_lon[lat]
        row = []
        for lon in all_lons:
            if lon in lon_map:
                accepted += 1
            row.append(lon_map.get(lon, float("nan")))
        grid.append(row)

    return grid, ErddapParseDiagnostics(
        row_count=max(0, len(rows) - 1),
        accepted_rows=accepted,
        lat_count=len(lats),
        lon_count=len(all_lons),
        parser_rejected_rows=parser_rejected,
        preview_lines=preview,
    )



def normalize_erddap_text_url(url: str, *, default_ext: str = ".csv") -> str:
    """Normalize an ERDDAP griddap URL to a text-friendly endpoint for parser-based reads."""
    if not url:
        return url
    parts = urlsplit(url)
    path = parts.path
    for ext in (".nc", ".csvp", ".csv", ".json", ".jsonl", ".tsv"):
        if path.endswith(ext):
            path = path[: -len(ext)]
            break
    path = f"{path}{default_ext}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

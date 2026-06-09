from __future__ import annotations

import asyncio
import logging
import math
import urllib.request
from datetime import datetime

from server.gfs.models import BBox
from server.gfs.providers.adapters import build_erddap_subset_request, split_antimeridian, viewport_from_bbox
from server.gfs.providers.erddap_csv import ErddapParseDiagnostics, normalize_erddap_text_url, parse_erddap_grid
from server.gfs.serializers import iso_utc


log = logging.getLogger("server.gfs.provider.coastwatch")

import os

NASA_ERDDAP_CHL_CSV = os.getenv(
    "GFS_NASA_ERDDAP_CHL_CSV",
    "https://coastwatch.pfeg.noaa.gov/erddap/griddap/erdMH1chla8day.csv",
)
COASTWATCH_ERDDAP_CHL_CSV = os.getenv(
    "GFS_ERDDAP_CHL_CSV",
    "https://coastwatch.noaa.gov/erddap/griddap/noaacwNPPN20VIIRSDINEOFDaily.csv",
)




# Backward-compatible primary chlorophyll metadata used by source_check.py
CHL_DATASET_META = {
    "dataset": NASA_ERDDAP_CHL_CSV,
    "var_name": "chlorophyll",
    "lon_convention": "pm180",
    "lat_descending": True,
    "extra_dimensions": [],
}
CHL_DATASET_SOURCES = [
    {
        "name": "nasa_8day",
        "dataset": NASA_ERDDAP_CHL_CSV,
        "var_name": "chlorophyll",
        "lon_convention": "pm180",
        "lat_descending": True,
        "extra_dimensions": [],
    },
    {
        "name": "coastwatch",
        "dataset": COASTWATCH_ERDDAP_CHL_CSV,
        "var_name": "chlor_a",
        "lon_convention": "pm180",
        "lat_descending": True,
        "extra_dimensions": ["0.0"],
    },
]


class CoastwatchProvider:
    """CoastWatch bio provider fetching real viewport subsets only."""

    def __init__(self) -> None:
        self._last_error: str | None = None
        self._last_fetch_at: datetime | None = None

    @staticmethod
    def _http_text(url: str, timeout_s: float = 12.5) -> str | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LFTR-GFS/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as res:
                return res.read().decode("utf-8", errors="replace")
        except Exception:
            return None

    @staticmethod
    def _parse_erddap_grid(text: str | None) -> tuple[list[list[float]], ErddapParseDiagnostics]:
        return parse_erddap_grid(text, preferred_value_columns=("chlorophyll", "chlor_a"))

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


    @staticmethod
    def _chunked_viewports(viewport, *, max_lon_span: float = 8.0):
        width = viewport.east - viewport.west if viewport.west <= viewport.east else (viewport.east + 360.0 - viewport.west)
        if width <= max_lon_span or viewport.west > viewport.east:
            return [viewport]
        parts = []
        start = viewport.west
        while start < viewport.east - 1e-9:
            stop = min(start + max_lon_span, viewport.east)
            parts.append(type(viewport)(west=start, south=viewport.south, east=stop, north=viewport.north))
            start = stop
        return parts or [viewport]

    @staticmethod
    def _merge_lon_chunks(grids: list[list[list[float]]]) -> list[list[float]]:
        usable = [g for g in grids if g]
        if not usable:
            return []
        merged = usable[0]
        for nxt in usable[1:]:
            min_rows = min(len(merged), len(nxt))
            merged = [merged[i] + nxt[i] for i in range(min_rows)]
        return merged

    def _fetch_candidate_parts(self, viewport, dataset_url: str, *, var_name: str, lon_convention: str, lat_descending_default: bool, extra_dimensions_default: list[str], stride: int, valid_time: datetime | None):
        attempts = [
            (lon_convention, lat_descending_default, list(extra_dimensions_default), max(1, int(stride))),
            (lon_convention, not lat_descending_default, list(extra_dimensions_default), 1),
            (lon_convention, lat_descending_default, [], 1),
        ]
        last_urls: list[str] = []
        last_raw_parts: list[str | None] = []
        last_diagnostics: list[ErddapParseDiagnostics] = []
        last_meta = attempts[-1]
        for lon_convention, lat_descending, extra_dimensions, attempt_stride in attempts:
            viewports = self._chunked_viewports(viewport, max_lon_span=8.0)
            all_urls: list[str] = []
            all_raw_parts: list[str | None] = []
            all_diagnostics: list[ErddapParseDiagnostics] = []
            chunk_grids: list[list[list[float]]] = []
            for vp in viewports:
                urls = build_erddap_subset_request(
                    vp, dataset_url, [var_name], attempt_stride, valid_time,
                    lon_convention=lon_convention, lat_descending=lat_descending, extra_dimensions=extra_dimensions,
                )
                raw_parts = [self._http_text(url) for url in urls]
                parsed_parts = [self._parse_erddap_grid(body) for body in raw_parts]
                grids = [grid for grid, _diag in parsed_parts]
                merged = self._merge_antimeridian_parts(grids)
                diagnostics = [diag for _grid, diag in parsed_parts]
                all_urls.extend(urls)
                all_raw_parts.extend(raw_parts)
                all_diagnostics.extend(diagnostics)
                if merged:
                    chunk_grids.append(merged)
            merged_all = self._merge_lon_chunks(chunk_grids)
            last_urls, last_raw_parts, last_diagnostics, last_meta = all_urls, all_raw_parts, all_diagnostics, (lon_convention, lat_descending, extra_dimensions, attempt_stride)
            if merged_all:
                return merged_all, lon_convention, lat_descending, attempt_stride, extra_dimensions, all_urls, all_raw_parts, all_diagnostics
        lon_convention, lat_descending, extra_dimensions, attempt_stride = last_meta
        return [], lon_convention, lat_descending, attempt_stride, extra_dimensions, last_urls, last_raw_parts, last_diagnostics

    @staticmethod
    def _water_color_grid(chlorophyll: list[list[float]]) -> list[list[float]]:
        out: list[list[float]] = []
        for row in chlorophyll:
            out_row: list[float] = []
            for ch in row:
                if not math.isfinite(ch) or ch <= 0:
                    out_row.append(float("nan"))
                else:
                    out_row.append(max(0.0, min(1.0, ch / 2.5)))
            out.append(out_row)
        return out

    def _fetch_subset_sync(self, *, bbox: BBox, stride: int, valid_time: datetime | None) -> tuple[dict[str, object], datetime | None]:
        viewport = viewport_from_bbox(bbox)
        slices = split_antimeridian(viewport)
        chlorophyll: list[list[float]] = []
        lon_convention = "pm180"
        lat_descending = True
        effective_stride = max(1, int(stride))
        extra_dimensions: list[str] = []
        urls: list[str] = []
        raw_parts: list[str | None] = []
        diagnostics: list[ErddapParseDiagnostics] = []
        selected_source = CHL_DATASET_SOURCES[0]
        source_attempts: list[dict[str, object]] = []
        for source in CHL_DATASET_SOURCES:
            selected_source = source
            chlorophyll, lon_convention, lat_descending, effective_stride, extra_dimensions, urls, raw_parts, diagnostics = self._fetch_candidate_parts(
                viewport,
                str(source["dataset"]),
                var_name=str(source["var_name"]),
                lon_convention=str(source["lon_convention"]),
                lat_descending_default=bool(source["lat_descending"]),
                extra_dimensions_default=list(source["extra_dimensions"]),
                stride=stride,
                valid_time=valid_time,
            )
            source_attempts.append({
                "source": source["name"],
                "dataset": source["dataset"],
                "urls": urls,
                "real_subset": bool(chlorophyll),
            })
            if chlorophyll:
                break
        payload = {
            "chlorophyll": chlorophyll,
            "water_color_index": self._water_color_grid(chlorophyll) if chlorophyll else [],
            "optional_ssh_anomaly": [],
            "source_meta": {
                "bio_source": "erddap_griddap",
                "subset_urls": len(urls),
                "lon_convention": lon_convention,
                "real_subset": bool(chlorophyll),
                "lat_descending": lat_descending,
                "effective_stride": effective_stride,
                "extra_dimensions": extra_dimensions,
                "dataset_url": selected_source["dataset"],
                "bio_dataset": selected_source["name"],
                "source_attempts": source_attempts,
            },
        }
        self._last_fetch_at = datetime.utcnow()
        self._last_error = None
        ny = len(chlorophyll)
        nx = len(chlorophyll[0]) if ny else 0
        log.info(
            "coastwatch subset fetched bbox=%s viewport=%s erddap_slices=%s stride=%s chlorophyll_shape=%sx%s real_subset=%s lat_descending=%s source=%s",
            bbox.as_list(),
            {"west": viewport.west, "south": viewport.south, "east": viewport.east, "north": viewport.north},
            [{"lon_start": s.lon_start, "lon_stop": s.lon_stop} for s in slices],
            effective_stride,
            ny,
            nx,
            bool(chlorophyll),
            lat_descending,
            selected_source["name"],
        )
        if not chlorophyll:
            diag_rows = [d.row_count for d in diagnostics]
            diag_lat = [d.lat_count for d in diagnostics]
            diag_lon = [d.lon_count for d in diagnostics]
            diag_rejected = [d.parser_rejected_rows for d in diagnostics]
            preview = [line for d in diagnostics for line in d.preview_lines[:2]][:4]
            log.warning(
                "coastwatch subset empty bbox=%s dataset=%s vars=%s urls=%s rows=%s lat=%s lon=%s parser_rejected=%s http_success_no_data=%s lon_convention=%s lat_descending=%s preview=%s",
                bbox.as_list(),
                selected_source["dataset"],
                [selected_source["var_name"]],
                urls,
                diag_rows,
                diag_lat,
                diag_lon,
                diag_rejected,
                all(r is not None for r in raw_parts),
                lon_convention,
                lat_descending,
                preview,
            )
            log.warning(
                "coastwatch subset diagnostics bbox=%s source_attempts=%s",
                bbox.as_list(),
                source_attempts,
            )
        return payload, valid_time

    async def fetch_subset(self, *, bbox: BBox, stride: int, valid_time: datetime | None) -> tuple[dict[str, object], datetime | None]:
        try:
            return await asyncio.to_thread(self._fetch_subset_sync, bbox=bbox, stride=stride, valid_time=valid_time)
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("coastwatch subset failed bbox=%s horizStride=%s err=%s", bbox.as_list(), stride, exc)
            return {
                "chlorophyll": [],
                "water_color_index": [],
                "optional_ssh_anomaly": [],
                "source_meta": {"bio_source": "erddap_griddap", "real_subset": False, "error": str(exc)},
            }, valid_time

    def health(self) -> dict[str, object]:
        return {
            "provider": "coastwatch",
            "status": "viewport_subset_only",
            "upstreams": ["erddap_chlorophyll"],
            "dataset_url": NASA_ERDDAP_CHL_CSV,
            "fallback_dataset_url": COASTWATCH_ERDDAP_CHL_CSV,
            "last_fetch_at": iso_utc(self._last_fetch_at),
            "last_error": self._last_error,
        }

from __future__ import annotations

import builtins
import os
import server.gfs_service as _svc
builtins.ALLOW_SYNTHETIC_FALLBACK = False
ALLOW_SYNTHETIC_FALLBACK = False
globals().update({k: v for k, v in vars(_svc).items() if not k.startswith('__')})
ALLOW_SYNTHETIC_FALLBACK = False

# Compatibility guard: some older mixin shards reference this quality-policy
# flag, but the current service no longer exports it. Keep live payloads strict
# by default and prevent cache warmers for bait/boater from crashing before
# HYCOM SST/current data can reach the scene cache.
ALLOW_SYNTHETIC_FALLBACK = False


class OceanBaitFrameMixin:
    def _hycom_live_bbox_policy(self, bbox: dict[str, float] | None, scene: dict[str, Any] | None = None, *, layer: str = "ocean") -> dict[str, Any]:
        """Decide whether a HYCOM NCSS live fetch is safe for one request.

        HYCOM is the provider, not a whole-world renderer.  Large visible bboxes
        should be satisfied by existing quantized scene-cache tiles or wait for
        smaller tiles, not by one giant blocking NCSS query.
        """
        b = self._normalize_bbox(bbox)
        width = abs(float(b.get("east", 0)) - float(b.get("west", 0)))
        height = abs(float(b.get("north", 0)) - float(b.get("south", 0)))
        area = width * height
        max_span = float(os.getenv("GFS_HYCOM_MAX_LIVE_SPAN_DEG", "1.0") or "1.0")
        max_area = float(os.getenv("GFS_HYCOM_MAX_LIVE_AREA_DEG2", "1.0") or "1.0")
        allowed = bool(width <= max_span and height <= max_span and area <= max_area)
        return {
            "allowed": allowed,
            "layer": layer,
            "bbox": b,
            "span_deg": round(max(width, height), 3),
            "area_deg2": round(area, 3),
            "max_span_deg": max_span,
            "max_area_deg2": max_area,
            "policy": "tile_cache_only_for_wide_views; provider_fetches_split_to_1deg_tiles; no_large_one_shot_hycom_ncss",
        }

    def _hycom_large_bbox_shell(self, bbox: dict[str, float] | None, scene: dict[str, Any] | None, *, layer: str, reason: str = "large_bbox_cache_only") -> dict[str, Any]:
        b = self._normalize_bbox(bbox)
        policy = self._hycom_live_bbox_policy(b, scene, layer=layer)
        return {
            "ok": False,
            "source": f"hycom_{layer}_cache_only_large_bbox",
            "payload_state": "cache_only_large_bbox",
            "reason": reason,
            "bbox": b,
            "bbox_object": b,
            "scene_plan": scene or {},
            "visible_bbox": (scene or {}).get("visible_bbox"),
            "fetch_bbox": (scene or {}).get("fetch_bbox"),
            "boats": [],
            "points": [],
            "ocean_points": [],
            "oceanPoints": [],
            "count": 0,
            "grid": {"real_grid": False, "reason": reason},
            "hycom_live_policy": policy,
            "quality_policy": {"allow_synthetic_fallback": False, "allow_proxy_fallback": False},
            "ts": self._now_ms(),
        }


    def _ocean_provider_tile_deg(self) -> float:
        try:
            return max(0.1, min(float(os.getenv("GFS_OCEAN_PROVIDER_TILE_DEG", "1.0") or "1.0"), 1.0))
        except Exception:
            return 1.0

    def _split_ocean_provider_tiles(self, bbox: dict[str, float] | None, tile_deg: float | None = None) -> list[dict[str, float]]:
        """Split a render/visible bbox into provider-safe HYCOM/RTOFS tiles."""
        b = self._normalize_bbox(bbox)
        step = float(tile_deg or self._ocean_provider_tile_deg())
        tiles: list[dict[str, float]] = []
        lon = float(b["west"])
        while lon < float(b["east"]):
            east = min(lon + step, float(b["east"]))
            lat = float(b["south"])
            while lat < float(b["north"]):
                north = min(lat + step, float(b["north"]))
                tiles.append({"west": round(lon, 6), "south": round(lat, 6), "east": round(east, 6), "north": round(north, 6)})
                lat = north
            lon = east
        return tiles

    def _is_wide_ocean_provider_bbox(self, bbox: dict[str, float] | None, tile_deg: float | None = None) -> bool:
        b = self._normalize_bbox(bbox)
        step = float(tile_deg or self._ocean_provider_tile_deg())
        return abs(float(b["east"]) - float(b["west"])) > step or abs(float(b["north"]) - float(b["south"])) > step

    def _log_ocean_provider_tile_policy(self, layer: str, bbox: dict[str, float], tiles: list[dict[str, float]]) -> None:
        try:
            log.info("ocean/provider-wide-request-skipped policy=tile_cache_only layer=%s visible_bbox=%s tile_deg=%.2f tiles=%s", layer, bbox, self._ocean_provider_tile_deg(), len(tiles))
            log.info("ocean/tile-refresh scheduled=%s tile_deg=%.1f layer=%s", len(tiles), self._ocean_provider_tile_deg(), layer)
        except Exception:
            pass

    def _merge_ocean_tile_live_payloads(self, bbox: dict[str, float], scene: dict[str, Any], tile_payloads: list[dict[str, Any]], started: float) -> dict[str, Any]:
        good = [p for p in tile_payloads if isinstance(p, dict) and p.get("ok")]
        boats: list[dict[str, Any]] = []
        points: list[dict[str, Any]] = []
        current_points: list[dict[str, Any]] = []
        for payload in good:
            boats.extend(payload.get("boats") or [])
            points.extend(payload.get("points") or payload.get("ocean_points") or [])
            current_points.extend(payload.get("current_points") or payload.get("points") or [])
        max_boats = int(os.getenv("GFS_BOAT_COUNT_MAX", "10") or "10")
        out = {
            "ok": bool(points or current_points or boats),
            "source": "hycom_espc_d_v02_ncss_tile_merged" if good else "hycom_espc_d_v02_ncss_tile_empty",
            "mode": "tile_cache_only_merged_ocean_provider_payload",
            "engine": "provider-safe <=1deg HYCOM tile merge",
            "bbox": bbox,
            "scene_plan": scene,
            "visible_bbox": scene.get("visible_bbox"),
            "fetch_bbox": scene.get("fetch_bbox"),
            "render_budget": scene.get("render_budget"),
            "boats": boats[:max_boats],
            "boat_count": min(len(boats), max_boats),
            "points": points,
            "ocean_points": points,
            "current_points": current_points or points,
            "current_zone_points_count": len(current_points or points),
            "swell_components": [],
            "grid": {"real_grid": bool(points or current_points), "tile_count": len(tile_payloads), "good_tile_count": len(good)},
            "source_meta": {"tile_cache_only": True, "provider_tile_deg": self._ocean_provider_tile_deg(), "tile_count": len(tile_payloads), "good_tile_count": len(good)},
            "quality_policy": self._live_payload_policy(),
            "fallback": {"used": False, "allowed": False},
            "payload_state": "live_tile_merged" if good else "provider_empty",
            "latency_ms": int((time.time() - started) * 1000),
            "ts": self._now_ms(),
        }
        if not out["ok"]:
            out.setdefault("cache", {}).update({"hit": False, "mode": "provider_empty_not_cached", "write_policy": "do_not_promote_empty_ocean_tile_merge"})
        return out

    def source_diagnostics_payload(self, bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        b = self._normalize_bbox(bbox)
        lat, lon = self._bbox_center(b)
        gfs_vars = [
            "u-component_of_wind_height_above_ground", "v-component_of_wind_height_above_ground",
            "Temperature_height_above_ground", "Relative_humidity_height_above_ground",
            "Precipitation_rate_surface", "Total_cloud_cover_entire_atmosphere",
            "Low_cloud_cover_low_cloud", "Medium_cloud_cover_middle_cloud", "High_cloud_cover_high_cloud",
            "Pressure_reduced_to_MSL_msl",
        ]
        from urllib.parse import urlencode
        gfs_query = urlencode({
            "north": round(b["north"], 4), "south": round(b["south"], 4),
            "west": round(b["west"], 4), "east": round(b["east"], 4),
            "time": "present", "accept": "netCDF4", "addLatLon": "true", "horizStride": 1,
        }) + "&" + "&".join(f"var={v}" for v in gfs_vars)
        gfs_ncss = f"https://thredds.ucar.edu/thredds/ncss/grid/grib/NCEP/GFS/Global_0p25deg/TwoD?{gfs_query}"
        rows = [
            self._source_row("weather", "GFS THREDDS NCSS", "weather/clouds/rain/wind", gfs_ncss, engine="netcdf4/h5netcdf target", ttl_seconds=0, status="target_next_live_first", details="Preferred fast path: one NetCDF4 subset shared by weather, clouds, and rain. Cloud/rain freshness is live-first: retained payloads are display-only while fresh GFS is requested.", variables=gfs_vars),
            self._source_row("weather_retained_display", "NOAA NOMADS GFS", "cloud/rain retained last-good display bridge", NOMADS_FILTER_BASE, engine="grib2+cfgrib current", ttl_seconds=0, status="current_live_first", details="Current working path. No 15-60 minute cloud source TTL: each active cloud request schedules a fresh GFS attempt; last-known-good is labeled retained_display only for instant pop-on/off."),
            self._source_row("ocean", "HYCOM ESPC-D-V02 all_best NCSS", "surface SST + salinity + U/V currents", "https://ncss.hycom.org/thredds/ncss/grid/FMRC_ESPC-D-V02_all/FMRC_ESPC-D-V02_all_best.ncd", engine="netcdf4/h5netcdf live", ttl_seconds=180, status="current_live_if_bbox_small", details="Primary live NCSS NetCDF4 subset uses compact all_best with sst, sss, ssu, ssv and lowercase accept=netcdf4. The default quality policy is strict: no marker/proxy/mock ocean is drawn when HYCOM NCSS fails; payloads return explicit provider_empty/provider_failed diagnostics instead. HYCOM downloader uses curl-style no-sudo timeouts and NetCDF validation.", variables=["sst", "sss", "ssu", "ssv"]),
            self._source_row("swells", "NOAA WaveWatch III", "3 swell components", "https://nomads.ncep.noaa.gov/cgi-bin/filter_wave.pl", engine="netcdf4/grib2 target", ttl_seconds=180, status="target_next", details="Use multi-swell height/period/direction where fields are available. In strict mode, unavailable WW3 data remains diagnostic only and must not be presented as live measured swell."),
            self._source_row("sst", "NOAA OISST / ERDDAP", "bait temperature band", "https://coastwatch.pfeg.noaa.gov/erddap/griddap/ncdcOisst21Agg_LonPM180.csv", engine="csv/netcdf target", ttl_seconds=300, status="target_next", details="Observed SST for bait species likelihood and water/land validity."),
            self._source_row("chlorophyll", "NOAA CoastWatch ERDDAP", "bait productivity", "https://coastwatch.pfeg.noaa.gov/erddap/griddap/erdMH1chlamday.csv", engine="csv/netcdf target", ttl_seconds=300, status="target_next", details="Chlorophyll/productivity multiplier for bait polygons."),
            self._source_row("markers", "local CSV", "fish locations/intelligence", str(self._fish_csv_path()), engine="csv", ttl_seconds=0, status="current", details="Fish marker locations and fishing history reports."),
        ]
        api_calls = [
            {"endpoint": "/gfs/api/weather", "function": "weather fields", "source_role": "weather", "target_ttl_seconds": WEATHER_REFRESH_TTL_SECONDS},
            {"endpoint": "/gfs/api/clouds", "function": "cloud/rain scene", "source_role": "weather", "target_ttl_seconds": 0, "policy": "live_first_retained_display_only", "dedupe_seconds": CLOUD_LIVE_DEDUPE_SECONDS},
            {"endpoint": "/gfs/api/ocean", "function": "currents/ocean solve", "source_role": "ocean", "target_ttl_seconds": 180},
            {"endpoint": "/gfs/api/boats", "function": "boating solve + 3 swells", "source_role": "ocean/swells", "target_ttl_seconds": 180},
            {"endpoint": "/gfs/api/bait-advanced", "function": "bait polygons/scores", "source_role": "sst/chl/ocean live grid; markers sample grid only", "target_ttl_seconds": 300},
            {"endpoint": "/gfs/api/scene-cache", "function": "fast composite assembler", "source_role": "cached split payloads", "target_ttl_seconds": 15},
        ]
        return {"ok": True, "bbox": b, "center": {"lat": round(lat, 4), "lon": round(lon, 4)}, "api_calls": api_calls, "sources": rows, "ts": self._now_ms()}

    def _ocean_stride_for_bbox(self, bbox: dict[str, float], target_cells: int | None = None) -> int:
        """Budget-derived HYCOM NCSS stride.

        The old fixed harbor/regional/world buckets made stride and LOD hard to
        reason about.  This keeps tiny harbor views native, then derives stride
        from a scene target-cell budget so provider fetch coarseness is explicit.
        Final visual density is still controlled later by render budgets.
        """
        return self._estimate_provider_stride_for_bbox(bbox, target_cells or int(os.getenv("GFS_SCENE_PROVIDER_TARGET_CELLS", "14000") or "14000"))

    @staticmethod
    def _current_dir_deg(u_ms: float, v_ms: float) -> float | None:
        if not (math.isfinite(u_ms) and math.isfinite(v_ms)):
            return None
        if abs(u_ms) < 1e-9 and abs(v_ms) < 1e-9:
            return None
        return (math.degrees(math.atan2(u_ms, v_ms)) + 360.0) % 360.0

    @staticmethod
    def _safety_from_current(speed_kt: float, sst_c: float | None = None) -> dict[str, Any]:
        speed = max(0.0, float(speed_kt or 0.0))
        if speed >= 2.5:
            color, label, score = "red", "Strong current / rough drift caution", 0.82
        elif speed >= 1.5:
            color, label, score = "orange", "Active current / plan drift", 0.62
        elif speed >= 0.75:
            color, label, score = "yellow", "Moderate current", 0.42
        else:
            color, label, score = "green", "Light current", 0.22
        if sst_c is not None and math.isfinite(float(sst_c)) and (sst_c < 8 or sst_c > 31):
            score = min(1.0, score + 0.08)
        return {"color": color, "label": label, "score": round(score, 3), "risk": round(score, 3)}

    def _proxy_swells_for_point(self, lat: float, lon: float, regional_boats: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best = None
        best_d = 1e9
        for boat in regional_boats or []:
            try:
                d = (float(boat.get("lat")) - lat) ** 2 + (float(boat.get("lon")) - lon) ** 2
            except Exception:
                continue
            if d < best_d:
                best, best_d = boat, d
        if best:
            sw = best.get("swells") or best.get("swell_components") or []
            if sw:
                return sw[:3]
            waves = best.get("waves") or {}
            cand = []
            for k in ("primary", "secondary", "tertiary"):
                if isinstance(waves.get(k), dict):
                    cand.append(waves[k])
            if cand:
                return cand[:3]
        return []

    @staticmethod
    def _lon_pm180(value: float) -> float:
        lon = float(value)
        while lon > 180.0:
            lon -= 360.0
        while lon <= -180.0:
            lon += 360.0
        return lon

    @staticmethod
    def _grid_spacing(values: list[float], index: int, fallback: float) -> float:
        vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
        if len(vals) >= 2 and 0 <= index < len(vals):
            if index == 0:
                return abs(vals[1] - vals[0])
            if index >= len(vals) - 1:
                return abs(vals[-1] - vals[-2])
            return max(abs(vals[index] - vals[index - 1]), abs(vals[index + 1] - vals[index]))
        return abs(float(fallback or 0.08))



    @staticmethod
    def _estimate_ocean_bottom_depth_m(lat: float, lon: float, bbox: dict[str, float] | None = None) -> float:
        """Deterministic bathymetry companion until a live topo grid is wired.

        This is explicitly NOT SST/current truth and never proves water by itself.
        It only adds a bottom-depth estimate to points that already passed the
        HYCOM SST/current/ocean-mask gate.  Positive is water depth below surface.
        """
        lat_f = float(lat)
        lon_f = float(lon)
        # SoCal/West-Coast shelf approximation: distance east/west from a gently
        # bending shoreline.  Other regions still get a stable shelf/slope estimate
        # tied to bbox/longitude texture, but only after HYCOM already proved water.
        coast_lon = -117.18 - 0.64 * max(0.0, min(2.4, lat_f - 32.5)) + 0.10 * math.sin((lat_f - 32.2) * 3.1)
        deg_lon_m = max(1.0, 111_320.0 * math.cos(math.radians(lat_f)))
        offshore_m = max(0.0, (lon_f - coast_lon) * deg_lon_m)
        # Shelf grows from beach/harbor shallows through nearshore and outer shelf.
        shelf_m = 2.0 + (offshore_m / 62.0)
        slope_m = max(0.0, offshore_m - 22_000.0) / 18.0
        texture_m = 4.0 * (0.5 + 0.5 * math.sin(lat_f * 8.0 + lon_f * 5.0))
        depth_m = shelf_m + slope_m + texture_m
        return max(1.5, min(1800.0, depth_m))

    @staticmethod
    def _bait_depth_band_from_bottom_m(bottom_depth_m: float, sst_c: float | None = None, speed_kt: float | None = None) -> dict[str, Any]:
        bottom = max(1.5, float(bottom_depth_m))
        # Bait generally rides upper water column, but can slide deeper with warm
        # surface water and stronger current. Clamp by bottom depth.
        temp_push_m = 0.0
        if sst_c is not None and math.isfinite(float(sst_c)):
            temp_push_m = max(0.0, float(sst_c) - 18.0) * 0.55
        current_push_m = 0.0
        if speed_kt is not None and math.isfinite(float(speed_kt)):
            current_push_m = min(5.0, max(0.0, float(speed_kt) - 0.35) * 2.5)
        preferred = max(1.0, min(bottom * 0.72, 5.0 + bottom * 0.18 + temp_push_m + current_push_m))
        band_min = max(0.5, preferred - max(1.5, bottom * 0.10))
        band_max = min(max(1.0, bottom - 0.5), preferred + max(2.5, bottom * 0.18))
        return {
            "bottom_depth_m": round(bottom, 1),
            "bottom_depth_ft": round(bottom * 3.28084, 1),
            "preferred_bait_depth_m": round(preferred, 1),
            "preferred_bait_depth_ft": round(preferred * 3.28084, 1),
            "bait_depth_band_m": [round(band_min, 1), round(band_max, 1)],
            "bait_depth_band_ft": [round(band_min * 3.28084, 1), round(band_max * 3.28084, 1)],
            "source": "hycom_gated_bathymetry_estimate_v1",
        }


    def _ocean_points_from_grid(self, *, bbox: dict[str, float], ocean: dict[str, Any], lod: str = "auto") -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build a true lat/lon HYCOM sea-of-points payload.

        HYCOM valid SST/current cells are treated as the implicit water mask.
        Output longitude is always normalized back to -180..180 for Google Maps.
        """
        u_grid = ocean.get("current_u") or []
        v_grid = ocean.get("current_v") or []
        sst_grid = ocean.get("sst") or []
        sal_grid = ocean.get("salinity") or []
        ocean_mask_grid = ocean.get("ocean_mask") or []
        lat_values = [float(x) for x in (ocean.get("lat_values") or []) if isinstance(x, (int, float)) and math.isfinite(float(x))]
        lon_values_raw = [float(x) for x in (ocean.get("lon_values") or []) if isinstance(x, (int, float)) and math.isfinite(float(x))]
        lon_values = [self._lon_pm180(x) for x in lon_values_raw]
        # Prefer full SST+U/V current grids, but do not collapse the ocean
        # point payload to zero when HYCOM returns SST while U/V is unavailable.
        # Bait can still draw SST-backed school cells without current vectors.
        has_uv_grid = bool(u_grid and v_grid)
        if has_uv_grid:
            ny = min(len(u_grid), len(v_grid), len(sst_grid) if sst_grid else 10**9)
            nx = min(
                len(u_grid[0]) if u_grid and u_grid[0] else 0,
                len(v_grid[0]) if v_grid and v_grid[0] else 0,
                len(sst_grid[0]) if sst_grid and sst_grid[0] else 10**9,
            )
        else:
            ny = len(sst_grid)
            nx = len(sst_grid[0]) if sst_grid and sst_grid[0] else 0
        if ny < 1 or nx < 1:
            return [], {"real_grid": False, "reason": "missing_sst_and_u_v_grid", "grid_shape": [ny, nx], "has_uv_grid": has_uv_grid}

        b = self._normalize_bbox(bbox)
        west, east = float(b["west"]), float(b["east"])
        east_i = east + 360.0 if east < west else east
        south, north = float(b["south"]), float(b["north"])
        fallback_dlat = abs(north - south) / max(1, ny - 1)
        fallback_dlon = abs(east_i - west) / max(1, nx - 1)

        span = max(abs(east_i - west), abs(north - south))
        lod_key = str(lod or "auto").lower()
        # Ocean analysis points are the shared intelligence field for boats,
        # shark, HUD, current squares, and sea-mask checks. Keep many real
        # finite HYCOM SST/current cells for analysis, while renderers select
        # a smaller subset. This is intentionally separate from advancedBaitRows.
        env_data_max = int(os.getenv("GFS_OCEAN_POINTS_DATA_MAX", os.getenv("GFS_OCEAN_POINTS_MAX", "12000")) or "5000")
        env_render_max = int(os.getenv("GFS_OCEAN_POINTS_RENDER_MAX", "1200") or "600")
        if lod_key in {"harbor", "dense", "high"} or span < 1.5:
            max_points = max(3000, env_data_max)
        elif lod_key in {"world", "coarse", "low"} or span > 8:
            max_points = max(1200, min(max(env_data_max, 8000), 14000))
        else:
            max_points = max(2000, min(max(env_data_max, 9000), 16000))
        render_max_points = max(160, env_render_max)
        step_y = max(1, int(math.ceil(ny / math.sqrt(max_points))))
        step_x = max(1, int(math.ceil(nx / math.sqrt(max_points))))

        def mask_allows(y: int, x: int) -> bool:
            if ocean_mask_grid:
                try:
                    return bool(ocean_mask_grid[y][x])
                except Exception:
                    return False
            return True

        def valid_at(y: int, x: int) -> bool:
            if y < 0 or x < 0 or y >= ny or x >= nx or not mask_allows(y, x):
                return False
            if has_uv_grid:
                try:
                    u = float(u_grid[y][x]); v = float(v_grid[y][x])
                except Exception:
                    return False
                if not (math.isfinite(u) and math.isfinite(v)):
                    return False
            if sst_grid:
                try:
                    sst = float(sst_grid[y][x])
                except Exception:
                    return False
                if not math.isfinite(sst):
                    return False
            return True

        points: list[dict[str, Any]] = []
        speeds: list[float] = []
        skipped_land = 0
        skipped_nan = 0
        edge_points = 0
        for iy in range(0, ny, step_y):
            for ix in range(0, nx, step_x):
                if not valid_at(iy, ix):
                    if sst_grid:
                        skipped_land += 1
                    else:
                        skipped_nan += 1
                    continue
                u = float(u_grid[iy][ix]) if has_uv_grid else 0.0
                v = float(v_grid[iy][ix]) if has_uv_grid else 0.0
                if lat_values and iy < len(lat_values):
                    lat = lat_values[iy]
                else:
                    frac_y = 0.5 if ny == 1 else iy / max(1, ny - 1)
                    lat = south + (north - south) * frac_y
                if lon_values and ix < len(lon_values):
                    lon = lon_values[ix]
                else:
                    frac_x = 0.5 if nx == 1 else ix / max(1, nx - 1)
                    lon = self._lon_pm180(west + (east_i - west) * frac_x)
                if not (math.isfinite(lat) and math.isfinite(lon)):
                    skipped_nan += 1
                    continue

                sst_c = None
                sal_psu = None
                if sst_grid:
                    try: sst_c = float(sst_grid[iy][ix])
                    except Exception: sst_c = None
                if sal_grid:
                    try: sal_psu = float(sal_grid[iy][ix])
                    except Exception: sal_psu = None
                speed_ms = math.hypot(u, v)
                speed_kt = speed_ms * 1.9438444924
                dir_deg = self._current_dir_deg(u, v)
                dlat = max(0.003, min(1.2, abs(self._grid_spacing(lat_values, iy, fallback_dlat))))
                dlon = max(0.003, min(1.2, abs(self._grid_spacing(lon_values_raw, ix, fallback_dlon))))

                neighbor_checks = [(iy-1,ix),(iy+1,ix),(iy,ix-1),(iy,ix+1)]
                valid_neighbors = sum(1 for y,x in neighbor_checks if valid_at(y,x))
                if valid_neighbors < 4:
                    edge_points += 1
                    skipped_land += 1
                    continue
                confidence = 0.55 + 0.1 * min(valid_neighbors, 4)
                if sst_grid and sst_c is not None and math.isfinite(sst_c):
                    confidence += 0.05
                if valid_neighbors < 2:
                    confidence -= 0.28
                confidence = max(0.12, min(0.98, confidence))

                safety = self._safety_from_current(speed_kt, sst_c)
                bait_temp = 0.5
                if sst_c is not None and math.isfinite(sst_c):
                    # Broad SoCal-ish marine bait comfort window: ~13C-22C.
                    bait_temp = max(0.0, min(1.0, 1.0 - abs(float(sst_c) - 17.5) / 8.5))
                current_edge = 1.0 - min(1.0, valid_neighbors / 4.0)
                bait_score = max(0.0, min(1.0, (bait_temp * 0.58) + (min(speed_kt, 2.0) / 2.0 * 0.24) + (current_edge * 0.18)))
                bottom_depth_m = self._estimate_ocean_bottom_depth_m(float(lat), float(lon), b)
                depth_intel = self._bait_depth_band_from_bottom_m(bottom_depth_m, sst_c, speed_kt)

                sst_c_out = round(sst_c, 4) if sst_c is not None and math.isfinite(sst_c) else None
                sst_f_out = round((sst_c * 9 / 5) + 32, 2) if sst_c is not None and math.isfinite(sst_c) else None
                sal_psu_out = round(sal_psu, 4) if sal_psu is not None and math.isfinite(sal_psu) else None
                current_dir_out = round(dir_deg, 2) if dir_deg is not None else None
                point = {
                    "id": f"hycom-point-{iy}-{ix}",
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                    "lng": round(float(lon), 6),

                    # Canonical HYCOM aliases. Keep old short names too for legacy JS.
                    "u": round(u, 5),
                    "v": round(v, 5),
                    "current_u": round(u, 5),
                    "current_v": round(v, 5),
                    "ssu": round(u, 5),
                    "ssv": round(v, 5),

                    "speedMs": round(speed_ms, 5),
                    "speedKt": round(speed_kt, 4),
                    "current_speed_m_s": round(speed_ms, 5),
                    "current_speed_kt": round(speed_kt, 4),
                    "heading": current_dir_out,
                    "current_dir_deg": current_dir_out,

                    "sst": sst_c_out,
                    "sst_c": sst_c_out,
                    "water_temp_c": sst_c_out,
                    "water_temp_f": sst_f_out,
                    "salinity": sal_psu_out,
                    "sss": sal_psu_out,
                    "salinity_psu": sal_psu_out,

                    "ocean_vars": {
                        "sst_c": sst_c_out,
                        "sst_f": sst_f_out,
                        "ssu_m_s": round(u, 5),
                        "ssv_m_s": round(v, 5),
                        "current_speed_m_s": round(speed_ms, 5),
                        "current_speed_kt": round(speed_kt, 4),
                        "current_dir_deg": current_dir_out,
                        "sss_psu": sal_psu_out,
                        "bottom_depth_m": depth_intel["bottom_depth_m"],
                        "bottom_depth_ft": depth_intel["bottom_depth_ft"],
                        "preferred_bait_depth_ft": depth_intel["preferred_bait_depth_ft"],
                        "source": "hycom_espc_d_v02_surface_sst_ssu_ssv",
                    },

                    "bottom_depth_m": depth_intel["bottom_depth_m"],
                    "bottom_depth_ft": depth_intel["bottom_depth_ft"],
                    "preferred_bait_depth_m": depth_intel["preferred_bait_depth_m"],
                    "preferred_bait_depth_ft": depth_intel["preferred_bait_depth_ft"],
                    "bait_depth_m": depth_intel["preferred_bait_depth_m"],
                    "bait_depth_ft": depth_intel["preferred_bait_depth_ft"],
                    "bait_depth_band_m": depth_intel["bait_depth_band_m"],
                    "bait_depth_band_ft": depth_intel["bait_depth_band_ft"],
                    "depth_intel": depth_intel,
                    "baitScore": round(bait_score, 4),
                    "boatingRisk": safety.get("risk"),
                    "confidence": round(confidence, 4),
                    "valid": True,
                    "water": True,
                    "mask": "hycom_valid_sst_current" if (sst_grid and has_uv_grid) else ("hycom_valid_sst_only" if sst_grid else "hycom_valid_current"),
                    "hasCurrentUv": bool(has_uv_grid),
                    "edgeConfidence": round(valid_neighbors / 4.0, 3),
                    "cell": {"dLat": round(dlat, 6), "dLon": round(dlon, 6), "validNeighbors": valid_neighbors},
                    "current": {"u": round(u, 5), "v": round(v, 5), "speedMs": round(speed_ms, 5), "speedKt": round(speed_kt, 4), "dirDeg": round(dir_deg, 2) if dir_deg is not None else None},
                    "safety": safety,
                }
                speeds.append(speed_kt)
                points.append(point)
                if len(points) >= max_points:
                    break
            if len(points) >= max_points:
                break

        return points, {
            "real_grid": bool(points),
            "grid_shape": [ny, nx],
            "point_count": len(points),
            "max_points": max_points,
            "lod": lod_key,
            "lod_step": {"y": step_y, "x": step_x},
            "mask_method": "shared_sst_landmask_hycom_valid_sst_current" if (sst_grid and has_uv_grid) else ("shared_sst_landmask_hycom_valid_sst_only" if sst_grid else "shared_sst_landmask_hycom_valid_current"),
            "shared_ocean_mask": bool(ocean_mask_grid),
            "has_uv_grid": bool(has_uv_grid),
            "sst_only_points_allowed": bool(sst_grid and not has_uv_grid),
            "skipped_land_or_invalid_sst": skipped_land,
            "coastline_guard": "eroded_sst_mask_plus_4_of_4_cardinal_neighbors_required",
            "skipped_nan_current": skipped_nan,
            "edge_points": edge_points,
            "lat_values": len(lat_values),
            "lon_values": len(lon_values),
            "avg_current_kt": round(sum(speeds) / max(1, len(speeds)), 4),
            "max_current_kt": round(max(speeds) if speeds else 0.0, 4),
            "has_sst": bool(sst_grid),
            "finite_sst_point_count": len(points),
            "ocean_analysis_point_count": len(points),
            "ocean_analysis_data_max": max_points,
            "ocean_analysis_render_max": render_max_points,
            "analysis_step_y": step_y,
            "analysis_step_x": step_x,
            "sea_mask_contract": "finite_hycom_sst_cells_are_the_shared_ocean_mask",
            "depth_contract": "bathymetry_estimate_attached_only_after_hycom_sst_current_gate",
            "depth_source": "hycom_gated_bathymetry_estimate_v1",
        }

    def _advected_point(self, lat: float, lon: float, u_ms: float, v_ms: float, seconds: float = 900.0) -> dict[str, float]:
        lat_rad = math.radians(float(lat))
        meters_north = float(v_ms) * float(seconds)
        meters_east = float(u_ms) * float(seconds)
        dlat = meters_north / 111320.0
        denom = max(15000.0, 111320.0 * max(0.08, math.cos(lat_rad)))
        dlon = meters_east / denom
        return {"lat": round(float(lat) + dlat, 6), "lon": round(self._lon_pm180(float(lon) + dlon), 6)}

    def _boats_from_ocean_grid(self, *, bbox: dict[str, float], ocean: dict[str, Any], regional_boats: list[dict[str, Any]], scene: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build renderable GLB boats from live HYCOM water cells without grid/row lines.

        Older builds walked the HYCOM grid with step_y/step_x and stopped as soon
        as render_limit boats were found. On a tilted globe that made boats line
        up on a row, often near the lower padded fetch bbox. This version scans a
        deterministic pool of live water/current/SST candidates, ranks them by a
        stable pseudo-random seed, then accepts candidates only if they are spaced
        apart geographically. The result is stable between refreshes, but visually
        scattered instead of equidistant.
        """
        u_grid = ocean.get("current_u") or []
        v_grid = ocean.get("current_v") or []
        sst_grid = ocean.get("sst") or []
        sal_grid = ocean.get("salinity") or []
        ocean_mask_grid = ocean.get("ocean_mask") or []
        lat_values = [float(x) for x in (ocean.get("lat_values") or []) if isinstance(x, (int, float)) and math.isfinite(float(x))]
        lon_values_raw = [float(x) for x in (ocean.get("lon_values") or []) if isinstance(x, (int, float)) and math.isfinite(float(x))]
        lon_values = [self._lon_pm180(x) for x in lon_values_raw]
        ny = min(len(u_grid), len(v_grid), len(sst_grid) if sst_grid else 10**9)
        nx = min(
            len(u_grid[0]) if ny and u_grid and u_grid[0] else 0,
            len(v_grid[0]) if ny and v_grid and v_grid[0] else 0,
            len(sst_grid[0]) if sst_grid and sst_grid[0] else 10**9,
        )
        if ny < 1 or nx < 1:
            return [], {"real_grid": False, "reason": "missing_u_v_or_sst_grid", "grid_shape": [ny, nx], "rejection_counts": {"missing_grid": 1}}

        west, east = float(bbox["west"]), float(bbox["east"])
        south, north = float(bbox["south"]), float(bbox["north"])
        east_i = east + 360.0 if east < west else east
        scene_budget = ((scene or {}).get("render_budget") or {}) if isinstance(scene, dict) else {}
        render_limit = max(1, min(18, int(scene_budget.get("max_boats") or OCEAN_NCSS_RENDER_BOATS or 18)))
        # Keep a larger live candidate pool so the frontend can also scatter/filter
        # by the true screen viewport while the backend remains cache/fetch-bbox based.
        candidate_limit = max(render_limit * 6, min(144, int(OCEAN_NCSS_MAX_BOATS) * 3))
        fallback_dlat = abs(north - south) / max(1, ny - 1)
        fallback_dlon = abs(east_i - west) / max(1, nx - 1)

        def finite_grid_value(grid, y: int, x: int) -> float | None:
            try:
                value = float(grid[y][x])
                return value if math.isfinite(value) else None
            except Exception:
                return None

        def mask_allows_boat(y: int, x: int) -> bool:
            if ocean_mask_grid:
                try:
                    return bool(ocean_mask_grid[y][x])
                except Exception:
                    return False
            return True

        def valid_sst_at(y: int, x: int) -> bool:
            if not sst_grid or y < 0 or x < 0 or y >= ny or x >= nx or not mask_allows_boat(y, x):
                return False
            return finite_grid_value(sst_grid, y, x) is not None

        def marine_neighbor_count(y: int, x: int) -> tuple[int, int]:
            valid = 0
            possible = 0
            for jy in range(max(0, y - 1), min(ny, y + 2)):
                for jx in range(max(0, x - 1), min(nx, x + 2)):
                    possible += 1
                    if valid_sst_at(jy, jx):
                        valid += 1
            return valid, possible

        def rand01(seed: str) -> float:
            h = hashlib.blake2b(seed.encode("utf-8", "ignore"), digest_size=8).digest()
            return int.from_bytes(h, "big") / float(2**64 - 1)

        def geo_dist2(a: dict[str, Any], b: dict[str, Any]) -> float:
            alat = float(a.get("lat") or 0.0); blat = float(b.get("lat") or 0.0)
            alon = float(a.get("lon") or 0.0); blon = float(b.get("lon") or 0.0)
            mean_lat = math.radians((alat + blat) * 0.5)
            dx = (alon - blon) * max(0.25, math.cos(mean_lat))
            dy = alat - blat
            return dx * dx + dy * dy

        def make_boat(iy: int, ix: int) -> tuple[dict[str, Any] | None, str | None]:
            try:
                u = float(u_grid[iy][ix]); v = float(v_grid[iy][ix])
            except Exception:
                return None, "nan_current"
            if not (math.isfinite(u) and math.isfinite(v)):
                return None, "nan_current"

            if not mask_allows_boat(iy, ix):
                return None, "land_or_nan_sst"
            sst_c = finite_grid_value(sst_grid, iy, ix) if sst_grid else None
            if sst_grid and sst_c is None:
                return None, "land_or_nan_sst"

            # Stricter coastline/island guard for GLB boats. Bait/ocean points may
            # use softer edge cells, but boats should only spawn in established water.
            valid_neighbors, possible_neighbors = marine_neighbor_count(iy, ix) if sst_grid else (9, 9)
            if sst_grid and possible_neighbors >= 6 and valid_neighbors < 8:
                return None, "coastline_guard"

            sal_psu = finite_grid_value(sal_grid, iy, ix) if sal_grid else None
            if lat_values and iy < len(lat_values):
                lat = lat_values[iy]
            else:
                frac_y = 0.5 if ny == 1 else iy / max(1, ny - 1)
                lat = south + (north - south) * frac_y
            if lon_values and ix < len(lon_values):
                lon = lon_values[ix]
            else:
                frac_x = 0.5 if nx == 1 else ix / max(1, nx - 1)
                lon = self._lon_pm180(west + (east_i - west) * frac_x)

            if not (math.isfinite(lat) and math.isfinite(lon)):
                return None, "nan_lat_lon"
            if not (south <= lat <= north and min(west, east) <= lon <= max(west, east)):
                return None, "outside_bbox"

            speed_ms = math.hypot(u, v)
            speed_kt = speed_ms * 1.9438444924
            if not math.isfinite(speed_kt) or speed_kt < 0.03:
                return None, "low_current"

            dir_deg = self._current_dir_deg(u, v)
            swells = self._proxy_swells_for_point(lat, lon, regional_boats)
            dlat = max(0.004, min(1.2, abs(self._grid_spacing(lat_values, iy, fallback_dlat))))
            dlon = max(0.004, min(1.2, abs(self._grid_spacing(lon_values_raw, ix, fallback_dlon))))
            safety = self._safety_from_current(speed_kt, sst_c)
            seed = f"boat:{round(west,3)}:{round(south,3)}:{round(east,3)}:{round(north,3)}:{iy}:{ix}:{round(speed_kt,3)}"
            # Put the rendered boat inside the live water cell, not exactly on the
            # HYCOM node. The jitter is deterministic and bounded by cell spacing.
            jitter_lat = (rand01(seed + ':lat') - 0.5) * dlat * 0.18
            jitter_lon = (rand01(seed + ':lon') - 0.5) * dlon * 0.18
            boat = {
                "id": f"hycom-current-{iy}-{ix}",
                "lat": round(float(lat + jitter_lat), 5),
                "lon": round(float(lon + jitter_lon), 5),
                "source": "hycom_espc_d_v02_ncss",
                "derivedFrom": "HYCOM ESPC-D-V02 NCSS selected surface/current variables",
                "gridIndex": {"iy": iy, "ix": ix},
                "cell": {
                    "dLat": round(dlat, 6),
                    "dLon": round(dlon, 6),
                    "water": True,
                    "mask": "eroded_shared_sst_landmask_interior_water",
                    "validNeighbors": valid_neighbors,
                    "possibleNeighbors": possible_neighbors,
                    "placement": "deterministic_random_inside_strict_interior_sst_cell",
                },
                "current": {
                    "u": round(u, 4),
                    "v": round(v, 4),
                    "speedMs": round(speed_ms, 4),
                    "speedKt": round(speed_kt, 3),
                    "dirDeg": round(dir_deg, 1) if dir_deg is not None else None,
                },
                "water": {
                    "sst_c": round(sst_c, 3) if sst_c is not None else None,
                    "sst_f": round(sst_c * 9 / 5 + 32, 1) if sst_c is not None else None,
                    "salinity_psu": round(sal_psu, 3) if sal_psu is not None else None,
                },
                "waves": {"source": "proxy_swells_until_ww3_live", "components": swells, "sigHeightFt": (swells[0].get("heightFt") if swells and isinstance(swells[0], dict) else None)},
                "swells": swells,
                "swell_components": swells,
                "safety": safety,
                "hazard": safety["score"],
                "sst_c": round(sst_c, 3) if sst_c is not None else None,
                "sst_f": round(sst_c * 9 / 5 + 32, 1) if sst_c is not None else None,
                "salinity": round(sal_psu, 3) if sal_psu is not None else None,
                "current_speed_kt": round(speed_kt, 3),
                "current_dir_deg": round(dir_deg, 1) if dir_deg is not None else None,
                "ocean_context": {
                    "source": "hycom_sst_current_cell",
                    "sst_c": round(sst_c, 3) if sst_c is not None else None,
                    "sst_f": round(sst_c * 9 / 5 + 32, 1) if sst_c is not None else None,
                    "salinity_psu": round(sal_psu, 3) if sal_psu is not None else None,
                    "current_speed_kt": round(speed_kt, 3),
                    "current_dir_deg": round(dir_deg, 1) if dir_deg is not None else None,
                    "valid_neighbors": valid_neighbors,
                    "possible_neighbors": possible_neighbors,
                    "cell_dlat": round(dlat, 6),
                    "cell_dlon": round(dlon, 6),
                },
                "placement": {
                    "mode": "backend_scattered_water_cell",
                    "seed_rank": round(rand01(seed + ':rank'), 6),
                    "jitter_lat": round(jitter_lat, 6),
                    "jitter_lon": round(jitter_lon, 6),
                },
            }
            return boat, None

        rejection_counts: dict[str, int] = {
            "nan_current": 0,
            "nan_lat_lon": 0,
            "land_or_nan_sst": 0,
            "coastline_guard": 0,
            "outside_bbox": 0,
            "low_current": 0,
            "candidate_cap": 0,
            "spacing_rejected": 0,
        }
        considered = 0
        candidates: list[dict[str, Any]] = []
        speeds: list[float] = []

        # Adaptive pre-step keeps enormous bboxes safe, but we no longer stop after
        # the first row of valid water. The rank/spacing pass below decides the set.
        target_scan = 1800
        scan_step = max(1, int(math.sqrt(max(1, (ny * nx) / target_scan))))
        # Phase offsets prevent every cache refresh from sampling the same north/south
        # and west/east stripes while staying deterministic for the bbox.
        phase_seed = f"{round(west,3)}:{round(south,3)}:{round(east,3)}:{round(north,3)}:{ny}x{nx}"
        off_y = int(rand01(phase_seed + ':y') * scan_step) if scan_step > 1 else 0
        off_x = int(rand01(phase_seed + ':x') * scan_step) if scan_step > 1 else 0

        for iy in range(off_y, ny, scan_step):
            for ix in range(off_x, nx, scan_step):
                considered += 1
                boat, reason = make_boat(iy, ix)
                if boat is None:
                    if reason in rejection_counts:
                        rejection_counts[reason] += 1
                    continue
                candidates.append(boat)
                try:
                    speeds.append(float(boat.get("current", {}).get("speedKt") or 0.0))
                except Exception:
                    pass

        # If the offset/stride pass missed too much water, do a sparse second pass
        # at cell centers. This helps narrow harbor bboxes without creating lines.
        if len(candidates) < render_limit and scan_step > 1:
            for iy in range(0, ny, max(1, scan_step // 2)):
                for ix in range(0, nx, max(1, scan_step // 2)):
                    if any((c.get("gridIndex") or {}) == {"iy": iy, "ix": ix} for c in candidates):
                        continue
                    considered += 1
                    boat, reason = make_boat(iy, ix)
                    if boat is None:
                        if reason in rejection_counts:
                            rejection_counts[reason] += 1
                        continue
                    candidates.append(boat)
                    try:
                        speeds.append(float(boat.get("current", {}).get("speedKt") or 0.0))
                    except Exception:
                        pass
                    if len(candidates) >= candidate_limit:
                        break
                if len(candidates) >= candidate_limit:
                    break

        # Stable random order, then greedy geo-spacing. The min distance is tied to
        # the fetch bbox size so a wide view does not create clumps and a harbor view
        # does not reject everything.
        span = max(abs(north - south), abs(east_i - west), 0.05)
        min_sep_deg = max(0.012, min(0.65, span / max(3.0, math.sqrt(render_limit) * 3.2)))
        min_sep2 = min_sep_deg * min_sep_deg
        candidates.sort(key=lambda b: (float(((b.get("placement") or {}).get("seed_rank") or 0.5)), -float((b.get("current") or {}).get("speedKt") or 0.0)))
        selected: list[dict[str, Any]] = []
        for boat in candidates:
            if all(geo_dist2(boat, prev) >= min_sep2 for prev in selected):
                selected.append(boat)
                if len(selected) >= render_limit:
                    break
            else:
                rejection_counts["spacing_rejected"] += 1
        if len(selected) < render_limit:
            seen = {b.get("id") for b in selected}
            for boat in candidates:
                if boat.get("id") in seen:
                    continue
                selected.append(boat)
                if len(selected) >= render_limit:
                    break

        if len(candidates) > candidate_limit:
            rejection_counts["candidate_cap"] = len(candidates) - candidate_limit
        avg_speed = sum(speeds) / max(1, len(speeds))
        return selected, {
            "real_grid": bool(selected),
            "grid_shape": [ny, nx],
            "sampled_count": len(selected),
            "render_limit": render_limit,
            "candidate_limit": candidate_limit,
            "candidate_pool_count": len(candidates),
            "considered_cells": considered,
            "rejection_counts": rejection_counts,
            "skipped_land_or_invalid_sst": rejection_counts["land_or_nan_sst"] + rejection_counts["coastline_guard"],
            "coastline_guard": "boat_glb_requires_8_of_9_valid_sst_neighbors_inside_eroded_shared_sst_landmask",
            "shared_ocean_mask": bool(ocean_mask_grid),
            "skipped_nan_current": rejection_counts["nan_current"],
            "lat_values": len(lat_values),
            "lon_values": len(lon_values),
            "lod_step": {"scan_step": scan_step, "phase_y": off_y, "phase_x": off_x},
            "placement_policy": "deterministic_random_scatter_with_geo_spacing_not_grid_rows",
            "min_spacing_deg": round(min_sep_deg, 5),
            "avg_current_kt": round(avg_speed, 3),
            "max_current_kt": round(max(speeds) if speeds else 0.0, 3),
        }

    def _fetch_live_ocean_ncss(self, bbox: dict[str, float], regional_boats: list[dict[str, Any]], scene: dict[str, Any] | None = None) -> dict[str, Any]:
        if not ENABLE_LIVE_OCEAN_NCSS:
            return {"ok": False, "reason": "disabled_by_GFS_ENABLE_LIVE_OCEAN_NCSS"}
        started = time.time()
        b = self._normalize_bbox(bbox)
        scene = scene or self.build_scene_plan(b, None, layer="ocean")
        tile_deg = self._ocean_provider_tile_deg()
        if self._is_wide_ocean_provider_bbox(b, tile_deg):
            tiles = self._split_ocean_provider_tiles(b, tile_deg)
            self._log_ocean_provider_tile_policy("ocean", b, tiles)
            max_sync_tiles = int(os.getenv("GFS_OCEAN_PROVIDER_MAX_SYNC_TILES", "16") or "16")
            if len(tiles) > max_sync_tiles:
                shell = self._hycom_large_bbox_shell(b, scene, layer="ocean", reason="wide_bbox_tile_cache_only_too_many_sync_tiles")
                shell.update({"provider_tiles": tiles[:max_sync_tiles], "provider_tile_count": len(tiles), "tile_deg": tile_deg, "latency_ms": int((time.time() - started) * 1000)})
                return shell
            tile_payloads = []
            for tile in tiles:
                tile_scene = self.build_scene_plan(tile, scene.get("visible_bbox") or b, layer="ocean")
                tile_payloads.append(self._fetch_live_ocean_ncss(tile, regional_boats, scene=tile_scene))
            return self._merge_ocean_tile_live_payloads(b, scene, tile_payloads, started)
        live_policy = self._hycom_live_bbox_policy(b, scene, layer="ocean")
        if not live_policy.get("allowed"):
            shell = self._hycom_large_bbox_shell(b, scene, layer="ocean")
            shell["latency_ms"] = int((time.time() - started) * 1000)
            return shell
        stride = int(scene.get("provider_stride") or self._ocean_stride_for_bbox(b, scene.get("provider_target_cells")))
        try:
            raw, valid_time = self.ocean_provider._fetch_subset_sync(
                bbox=BBox(west=b["west"], south=b["south"], east=b["east"], north=b["north"]),
                stride=stride,
                valid_time=None,
            )
        except Exception as exc:
            return {"ok": False, "reason": "hycom_fetch_exception", "error": str(exc), "latency_ms": int((time.time() - started) * 1000)}
        meta = raw.get("source_meta") or {}
        boats, grid_meta = self._boats_from_ocean_grid(bbox=b, ocean=raw, regional_boats=regional_boats, scene=scene)
        try:
            current_zone_points, current_zone_grid = self._ocean_points_from_grid(bbox=b, ocean=raw, lod=(scene or {}).get("tier") or "auto")
        except Exception as exc:
            current_zone_points, current_zone_grid = [], {"real_grid": False, "reason": "current_zone_point_derivation_failed", "error": str(exc)}
        quality_gate = meta.get("quality_gate") or {}
        live_ncss_ok = bool(quality_gate.get("live_ncss_ok") if isinstance(quality_gate, dict) else meta.get("real_subset"))
        ok = bool(live_ncss_ok)
        return {
            "ok": ok,
            "source": "hycom_espc_d_v02_ncss" if ok else "hycom_espc_d_v02_ncss_empty",
            "mode": "live_hycom_ncss_surface_currents" if ok else "live_hycom_attempt_no_quality_grid",
            "engine": "xarray->netcdf4/h5netcdf/scipy",
            "valid_time": valid_time.isoformat() if hasattr(valid_time, "isoformat") and valid_time else None,
            "latency_ms": int((time.time() - started) * 1000),
            "bbox": b,
            "scene_plan": scene,
            "visible_bbox": scene.get("visible_bbox"),
            "fetch_bbox": scene.get("fetch_bbox"),
            "render_budget": scene.get("render_budget"),
            "stride": stride,
            "hycom_resolution_mode": ("native_no_horizStride" if stride <= 1 else "adaptive_horizStride"),
            "bbox_area_deg2": round(abs((b["east"] - b["west"]) * (b["north"] - b["south"])), 4),
            "boats": boats,
            "boat_count": len(boats),
            # Dense point field for the frontend current-zone marching-squares renderer.
            # Boats are only a selected subset; current-zone polygons need the larger HYCOM/SST grid.
            "points": current_zone_points,
            "ocean_points": current_zone_points,
            "oceanAnalysisPoints": {
                "ok": bool(current_zone_points),
                "source": "hycom_provider_ocean_analysis_points",
                "schema": "hycom_ocean_analysis_points_v1",
                "bbox": [b["west"], b["south"], b["east"], b["north"]],
                "bbox_object": b,
                "points": current_zone_points,
                "count": len(current_zone_points),
                "data_max": int((grid_meta or {}).get("ocean_analysis_data_max") or len(current_zone_points)),
                "render_max": int((grid_meta or {}).get("ocean_analysis_render_max") or 600),
                "contract": "large_finite_sst_current_data_field_not_visual_boat_count",
            },
            "ocean_analysis_points": current_zone_points,
            "ocean_analysis_point_count": len(current_zone_points),
            "oceanPoints": {
                "ok": bool(current_zone_points),
                "source": "hycom_provider_ocean_points_embedded_in_boater",
                "schema": "hycom_ocean_points_v1",
                "bbox": [b["west"], b["south"], b["east"], b["north"]],
                "bbox_object": b,
                "points": current_zone_points,
                "count": len(current_zone_points),
                "grid": current_zone_grid,
                "contract": "hycom_provider_field_feeds_boats_shark_hud_current__advanced_bait_uses_separate_dense_rows",
            },
            "current_points": current_zone_points or [{"lat": x.get("lat"), "lon": x.get("lon"), "current": x.get("current"), "safety": x.get("safety")} for x in boats],
            "current_zone_points_count": len(current_zone_points),
            "current_zone_grid": current_zone_grid,
            "grid": {**(grid_meta or {}), "current_zone_points": len(current_zone_points), "ocean_analysis_points": len(current_zone_points), "current_zone_grid": current_zone_grid},
            "provider": {
                "name": "hycom",
                "module": "server.gfs.providers.hycom.HycomProvider",
                "contract": "first_class_ocean_provider_sst_sss_ssu_ssv",
                "consumers": ["oceanAnalysisPoints", "boats", "shark-intel", "HUD", "current-squares", "advancedBaitRows"],
            },
            "source_meta": meta,
            "diagnostics": {
                "provider": "HYCOM provider ESPC-D-V02 all_best NCSS strict-live",
                "dataset_url": meta.get("current_dataset_url") or meta.get("sst_dataset_url"),
                "real_subset": bool(meta.get("real_subset")),
                "quality_gate": meta.get("quality_gate"),
                "fallback_used": bool(meta.get("fallback_used")),
                "fallback_sources": meta.get("fallback_sources") or [],
                "grid_shape": meta.get("grid_shape") or grid_meta.get("grid_shape"),
                "current_source": meta.get("current_source"),
                "selected_attempt": meta.get("selected_attempt"),
                "selected_dataset": meta.get("selected_dataset"),
                "selected_vars": meta.get("selected_vars"),
                "selected_u_var": meta.get("selected_u_var"),
                "selected_v_var": meta.get("selected_v_var"),
                "attempt_order": meta.get("attempt_order"),
                "hycom_slices": meta.get("hycom_slices"),
                "hycom_lon_diagnostics": meta.get("hycom_lon_diagnostics"),
                "diagnostics": meta.get("diagnostics", [])[:4],
                "debug_previews": meta.get("debug_previews", [])[:2],
            },
            "cache": {"hit": False, "ttl_seconds": 180},
            "ts": self._now_ms(),
        }


    def ocean_points_payload(self, bbox: dict[str, float] | None = None, lod: str = "auto", visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        b = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(b, visible_bbox, layer="ocean-points")
        lod_key = str(lod or scene.get("tier") or "auto").lower()
        if lod_key == "auto":
            lod_key = str(scene.get("tier") or "regional")
        key = self._bbox_cache_key(f"ocean-points-{lod_key}", b)
        def _shell():
            return {
                "ok": True,
                "source": "hycom_ocean_points_cache_warming",
                "schema": "hycom_ocean_points_v1",
                "bbox": [b["west"], b["south"], b["east"], b["north"]],
                "bbox_object": b,
                "scene_plan": scene,
                "visible_bbox": scene.get("visible_bbox"),
                "fetch_bbox": scene.get("fetch_bbox"),
                "render_budget": scene.get("render_budget"),
                "lod": lod_key,
                "points": [],
                "count": 0,
                "mask": {"method": "cache_first_hold", "valid_count": 0, "rejected_count": 0},
                "grid": {"grid_shape": [0, 0], "real_grid": False, "reason": "cache_warming_no_direct_provider_block"},
                "source_meta": {"cache_first": True, "note": "HYCOM provider request queued; use stale cache or wait for warm pull."},
            }
        return self._cache_first_split_payload(
            key=key,
            label="ocean-points",
            ttl_seconds=int(os.getenv("GFS_OCEAN_POINTS_CACHE_TTL_SECONDS", "600") or "300"),
            stale_seconds=int(os.getenv("GFS_OCEAN_POINTS_STALE_SECONDS", "1800") or "1800"),
            builder=lambda: self._ocean_points_payload_heavy(b, lod_key, scene.get("visible_bbox")),
            shell_factory=_shell,
        )

    def _ocean_points_payload_heavy(self, bbox: dict[str, float] | None = None, lod: str = "auto", visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        """HYCOM sea-of-points foundation for ocean/bait/current LOD rendering."""
        b = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(b, visible_bbox, layer="ocean-points")
        lod_key = str(lod or scene.get("tier") or "auto").lower()
        if lod_key == "auto":
            lod_key = str(scene.get("tier") or "regional")
        key = self._bbox_cache_key(f"ocean-points-{lod_key}", b)
        cached = self._split_cache_get(key, 180)
        if cached:
            out = dict(cached)
            out["cache"] = {"hit": True, "ttl_seconds": 180}
            return out
        started = time.time()
        live_policy = self._hycom_live_bbox_policy(b, scene, layer="ocean-points")
        if not live_policy.get("allowed"):
            payload = self._hycom_large_bbox_shell(b, scene, layer="ocean_points")
            payload.update({"schema": "hycom_ocean_points_v1", "lod": lod_key, "cache": {"hit": False, "mode": "large_bbox_cache_only_no_live_ncss"}})
            return payload
        stride = int(scene.get("provider_stride") or self._ocean_stride_for_bbox(b, scene.get("provider_target_cells")))
        try:
            raw, valid_time = self.ocean_provider._fetch_subset_sync(
                bbox=BBox(west=b["west"], south=b["south"], east=b["east"], north=b["north"]),
                stride=stride,
                valid_time=None,
            )
        except Exception as exc:
            return {"ok": False, "source": "hycom_ocean_points_error", "bbox": b, "error": str(exc), "points": [], "count": 0, "ts": self._now_ms()}
        points, grid_meta = self._ocean_points_from_grid(bbox=b, ocean=raw, lod=lod_key)
        for p in points:
            try:
                adv = self._advected_point(float(p["lat"]), float(p["lon"]), float(p.get("u") or 0.0), float(p.get("v") or 0.0), seconds=900.0)
                p["advected15m"] = adv
            except Exception:
                pass
        meta = raw.get("source_meta") or {}
        payload = {
            "ok": bool(points),
            "source": "hycom_all_best_ocean_points" if points and bool((raw.get("source_meta") or {}).get("real_subset")) else "hycom_ocean_points_empty_or_quality_gate_failed",
            "schema": "hycom_ocean_points_v1",
            "bbox": [b["west"], b["south"], b["east"], b["north"]],
            "bbox_object": b,
            "lon_mode": "0_360_internal__minus180_180_output",
            "lod": lod_key,
            "scene_plan": scene,
            "visible_bbox": scene.get("visible_bbox"),
            "fetch_bbox": scene.get("fetch_bbox"),
            "render_budget": scene.get("render_budget"),
            "stride": stride,
            "hycom_resolution_mode": ("native_no_horizStride" if stride <= 1 else "adaptive_horizStride"),
            "bbox_area_deg2": round(abs((b["east"] - b["west"]) * (b["north"] - b["south"])), 4),
            "valid_time": valid_time.isoformat() if hasattr(valid_time, "isoformat") and valid_time else None,
            "points": points,
            "count": len(points),
            "ocean_var_contract": {
                "sst_c": "HYCOM sst sea surface temperature Celsius",
                "sst_f": "derived from sst_c",
                "ssu_m_s": "HYCOM ssu eastward surface current",
                "ssv_m_s": "HYCOM ssv northward surface current",
                "sss_psu": "HYCOM sss surface salinity",
                "current_speed_kt": "derived from ssu/ssv",
                "current_dir_deg": "derived from ssu/ssv",
                "bottom_depth_ft": "HYCOM-gated bathymetry companion estimate",
                "preferred_bait_depth_ft": "derived depth band for bait visuals/intel",
            },
            "mask": {
                "method": grid_meta.get("mask_method"),
                "valid_count": len(points),
                "rejected_count": int(grid_meta.get("skipped_land_or_invalid_sst") or 0) + int(grid_meta.get("skipped_nan_current") or 0),
                "shoreline_edge_points": grid_meta.get("edge_points"),
            },
            "grid": grid_meta,
            "source_meta": {
                "quality_gate": meta.get("quality_gate"),
                "fallback_used": bool(meta.get("fallback_used")),
                "fallback_sources": meta.get("fallback_sources") or [],
                "selected_attempt": meta.get("selected_attempt"),
                "selected_dataset": meta.get("selected_dataset"),
                "selected_vars": meta.get("selected_vars"),
                "selected_u_var": meta.get("selected_u_var"),
                "selected_v_var": meta.get("selected_v_var"),
                "hycom_slices": meta.get("hycom_slices"),
                "hycom_lon_diagnostics": meta.get("hycom_lon_diagnostics"),
                "real_subset": bool(meta.get("real_subset")),
                "grid_shape": meta.get("grid_shape"),
                "diagnostics": (meta.get("diagnostics") or [])[:3],
                "debug_previews": (meta.get("debug_previews") or [])[:2],
            },
            "cache": {"hit": False, "ttl_seconds": 180},
            "latency_ms": int((time.time() - started) * 1000),
            "ts": self._now_ms(),
        }
        payload = self._attach_truth_contract(self._attach_scene_plan(payload, scene), bbox=b, stride=stride, source_resolution_deg=0.25, derived_resolution_deg=0.03125, extra={"input_truth": "live_source_resolution_viewport_fetch"})
        if points:
            return self._split_cache_set(key, payload)
        payload.setdefault("cache", {}).update({"hit": False, "mode": "provider_empty_not_cached", "write_policy": "do_not_promote_empty_ocean_points"})
        return payload

    def ocean_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        b = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(b, visible_bbox, layer="ocean")
        key = self._bbox_cache_key(f"ocean-live-{scene.get('tier')}", b)
        def _shell():
            return {
                "ok": True,
                "bbox": b,
                "scene_plan": scene,
                "visible_bbox": scene.get("visible_bbox"),
                "fetch_bbox": scene.get("fetch_bbox"),
                "render_budget": scene.get("render_budget"),
                "source": "deferred_tile_cache",
                "mode": "cache_first_ocean_warming",
                "engine": "cache-first; HYCOM provider warm queued",
                "boats": [],
                "boat_count": 0,
                "swell_components": [],
                "grid": {"grid_shape": [0, 0], "real_grid": False, "reason": "cache_warming_no_direct_provider_block"},
                "fallback": {"used": False, "reason": "cache_warming"},
            }
        return self._cache_first_split_payload(
            key=key,
            label="ocean-live",
            ttl_seconds=int(os.getenv("GFS_OCEAN_CACHE_TTL_SECONDS", "600") or "300"),
            stale_seconds=int(os.getenv("GFS_OCEAN_STALE_SECONDS", "1800") or "1800"),
            builder=lambda: self._ocean_payload_heavy(b, scene.get("visible_bbox")),
            shell_factory=_shell,
        )

    def _live_payload_policy(self) -> dict[str, Any]:
        return {
            "strict_live_payloads": bool(GFS_STRICT_LIVE_PAYLOADS),
            "allow_proxy_fallback": False,
            "allow_synthetic_fallback": bool(ALLOW_SYNTHETIC_FALLBACK),
            "rule": "live_ncss_erddap_or_empty_no_mock_draw",
        }

    def _empty_live_required_payload(self, *, label: str, bbox: dict[str, float], reason: str, error: str | None = None, live_attempt: dict[str, Any] | None = None) -> Dict[str, Any]:
        now = self._now_ms()
        payload: Dict[str, Any] = {
            "ok": False,
            "bbox": bbox,
            "source": f"{label}_live_required_unavailable",
            "mode": "live_required_no_proxy_or_mock",
            "payload_state": "provider_failed" if error else "provider_empty",
            "status": "unavailable",
            "reason": reason,
            "error": error,
            "quality_policy": self._live_payload_policy(),
            "fallback": {"used": False, "allowed": False, "reason": "proxy_fallback_removed_by_quality_policy"},
            "live_attempt": live_attempt or {},
            "diagnostics": {
                "quality_gate": "failed_live_required",
                "label": label,
                "reason": reason,
                "error": error,
            },
            "cache": {"hit": False, "ttl_seconds": 45, "mode": "explicit_unavailable_not_fallback"},
            "ts": now,
        }
        if label in {"ocean", "boats"}:
            payload.update({
                "boats": [],
                "boat_count": 0,
                "current_points": [],
                "points": [],
                "grid": {"real_grid": False, "reason": reason, "quality_gate": "failed_live_required"},
                "swell_components": [],
            })
        if label == "bait":
            payload.update({
                "schema": "bait_live_required_unavailable_v1",
                "bait": {"status": "unavailable", "source": payload["source"], "polygons": [], "outer_polygons": [], "inner_polygons": [], "core_polygons": []},
                "bait_score": [],
                "oceanPoints": [],
                "ocean_points": [],
                "confidence": {"overall": 0.0, "reason": reason},
            })
        return payload

    def _ocean_payload_heavy(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        b = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(b, visible_bbox, layer="ocean")
        key = self._bbox_cache_key(f"ocean-live-{scene.get('tier')}", b)
        cached = self._split_cache_get(key, 180)
        if cached:
            out = dict(cached)
            out["cache"] = {"hit": True, "ttl_seconds": 180}
            return out
        started = time.time()
        live = self._fetch_live_ocean_ncss(b, [], scene=scene)
        if live.get("ok"):
            live["swell_components"] = live.get("swell_components") or []
            live["fallback"] = {"used": False, "allowed": False}
            live["quality_policy"] = self._live_payload_policy()
            live["payload_state"] = "live"
            live["latency_ms"] = int((time.time() - started) * 1000)
            return self._split_cache_set(key, self._attach_scene_plan(live, scene))

        payload = self._empty_live_required_payload(
            label="ocean",
            bbox=b,
            reason=str(live.get("reason") or live.get("mode") or "hycom_ncss_unavailable_or_empty"),
            error=live.get("error"),
            live_attempt=live,
        )
        payload["latency_ms"] = int((time.time() - started) * 1000)
        payload["diagnostics"].update({"source_diagnostics": self.source_diagnostics_payload(b)})
        payload = self._attach_scene_plan(payload, scene)
        payload.setdefault("cache", {}).update({"hit": False, "mode": "provider_empty_not_cached", "write_policy": "do_not_promote_empty_ocean_live"})
        return payload

    def boats_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        scene = self.build_scene_plan(bbox, visible_bbox, layer="boats")
        ocean = self.ocean_payload(bbox, visible_bbox=scene.get("visible_bbox"))
        boats = ocean.get("boats", []) or []
        source = str(ocean.get("source") or "")
        mode = str(ocean.get("mode") or "")
        is_fallback = ("fallback" in source.lower()) or ("marker_ocean_solve" in source.lower()) or ("fallback" in mode.lower()) or ("proxy" in mode.lower())
        # Keep fallback/proxy boats visible in diagnostics, but separate them from
        # renderable GLB boats. The frontend only draws GLB boats from live
        # SST/current-backed water samples; this avoids saying “34 boats” when
        # all 34 were intentionally rejected as marker/proxy fallback.
        return {
            "ok": bool(ocean.get("ok")),
            "source": ocean.get("source"),
            "mode": ocean.get("mode"),
            "engine": ocean.get("engine"),
            "bbox": ocean.get("bbox"),
            "scene_plan": scene,
            "visible_bbox": scene.get("visible_bbox"),
            "fetch_bbox": scene.get("fetch_bbox"),
            "render_budget": scene.get("render_budget"),
            "boats": boats,
            "points": ocean.get("points") or [],
            "ocean_points": ocean.get("ocean_points") or ocean.get("points") or [],
            "current_points": ocean.get("current_points") or ocean.get("points") or [],
            "current_zone_points_count": len(ocean.get("points") or ocean.get("current_points") or []),
            "count": len(boats),
            "renderable_count_hint": 0 if is_fallback else len(boats),
            "fallback_rejected_count_hint": len(boats) if is_fallback else 0,
            "render_contract": "glb_boats_require_live_sst_current_samples_and_eroded_shared_sst_landmask",
            "rejection_counts": ((ocean.get("grid") or {}).get("rejection_counts") or {}),
            "grid": ocean.get("grid"),
            "swell_components": ocean.get("swell_components", []),
            "source_meta": ocean.get("source_meta") or {},
            "sst_landmask": ((ocean.get("source_meta") or {}).get("sst_landmask") or {}),
            "landmask_contract": ((ocean.get("source_meta") or {}).get("landmask_contract") or "finite_sst_is_shared_water_gate_for_boater_bait"),
            "diagnostics": ocean.get("diagnostics"),
            "cache": ocean.get("cache"),
            "quality_policy": ocean.get("quality_policy") or self._live_payload_policy(),
            "payload_state": ocean.get("payload_state") or ("live" if not is_fallback and boats else "provider_empty"),
            "fallback": ocean.get("fallback") or {"used": bool(is_fallback)},
            "ts": self._now_ms(),
        }

    def _bait_tile_deg_for_bbox(self, bbox: dict[str, float]) -> float:
        b = self._normalize_bbox(bbox)
        span = max(abs(float(b["east"]) - float(b["west"])), abs(float(b["north"]) - float(b["south"])))
        if span <= 5.0:
            return BAIT_ADVANCED_TILE_DEG_LOCAL
        if span <= 14.0:
            return BAIT_ADVANCED_TILE_DEG_REGIONAL
        return BAIT_ADVANCED_TILE_DEG_WORLD

    def _quantize_bait_bbox(self, bbox: dict[str, float]) -> dict[str, float]:
        import math as _math
        b = self._normalize_bbox(bbox)
        step = self._bait_tile_deg_for_bbox(b)
        return {
            "west": max(-179.9, _math.floor(float(b["west"]) / step) * step),
            "south": max(-89.9, _math.floor(float(b["south"]) / step) * step),
            "east": min(179.9, _math.ceil(float(b["east"]) / step) * step),
            "north": min(89.9, _math.ceil(float(b["north"]) / step) * step),
        }

    def _bait_advanced_cache_key(self, scene: dict[str, Any], bbox: dict[str, float]) -> str:
        q = self._quantize_bait_bbox(bbox)
        tier = scene.get("tier") or "auto"
        return "bait-advanced:%s:%.2f,%.2f,%.2f,%.2f" % (tier, q["west"], q["south"], q["east"], q["north"])

    def _bait_advanced_refresh_allowed(self, key: str) -> bool:
        marker_key = f"bait-advanced-refresh-marker:{key}"
        if self._split_cache_get(marker_key, BAIT_ADVANCED_REFRESH_MIN_GAP_SECONDS):
            return False
        try:
            self._split_cache_set(marker_key, {"ts": self._now_ms(), "key": key})
        except Exception:
            pass
        return True

    def _cached_gfs_weather_fields_for_bait(self, bbox: dict[str, float]) -> dict[str, Any]:
        """Return cached GFS/weather fields for bait without waking GFS.

        Advanced bait needs wind/weather context, but it should not trigger a new
        cfgrib/GFS decode. This helper scans already-built split-cache payloads
        and reuses weather/cloud/frame fields if present.
        """
        if not BAIT_ADVANCED_USE_CACHED_GFS_WEATHER:
            return {}
        cache = getattr(self, "_split_payload_cache", None)
        if not isinstance(cache, dict):
            return {}
        now = time.time()
        b = self._normalize_bbox(bbox)

        def overlaps(pb: Any) -> bool:
            try:
                if isinstance(pb, (list, tuple)) and len(pb) >= 4:
                    west, south, east, north = map(float, pb[:4])
                    box = {"west": west, "south": south, "east": east, "north": north}
                elif isinstance(pb, dict):
                    box = self._normalize_bbox(pb)
                else:
                    return False
                return not (box["east"] < b["west"] or box["west"] > b["east"] or box["north"] < b["south"] or box["south"] > b["north"])
            except Exception:
                return False

        best: tuple[float, dict[str, Any], str] | None = None
        for key, row in list(cache.items()):
            if not isinstance(row, dict):
                continue
            age = now - float(row.get("time", 0) or 0)
            if age > SCENE_CACHE_STALE_SECONDS * 2:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            candidates = []
            # Common frame/weather shapes.
            if isinstance(payload.get("weather"), dict):
                candidates.append((payload.get("weather"), "weather"))
            if isinstance(payload.get("frame"), dict) and isinstance(payload["frame"].get("weather"), dict):
                candidates.append((payload["frame"].get("weather"), "frame.weather"))
            # Scene-cache contract layer shapes.
            layers = payload.get("layers") if isinstance(payload.get("layers"), dict) else {}
            if isinstance(layers.get("weather"), dict):
                candidates.append((layers.get("weather"), "layers.weather"))
            if isinstance(layers.get("clouds"), dict):
                candidates.append((layers.get("clouds"), "layers.clouds"))
            for candidate, source in candidates:
                fields = candidate.get("fields") if isinstance(candidate, dict) else None
                if not isinstance(fields, dict) or not fields:
                    continue
                pb = candidate.get("bbox") or payload.get("bbox") or payload.get("bbox_used") or payload.get("visible_bbox")
                if pb is not None and not overlaps(pb):
                    continue
                score = -age
                # Prefer candidates with wind fields.
                if any(k in fields for k in ("wind_u", "u", "wind_v", "v")):
                    score += 1000.0
                if best is None or score > best[0]:
                    best = (score, dict(fields), f"{source}:{key}")
        if best:
            fields = best[1]
            fields["_bait_weather_source"] = f"cached_gfs_weather:{best[2]}"
            return fields
        return {}

    def bait_advanced_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        requested_b = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(requested_b, visible_bbox, layer="bait")
        qb = self._quantize_bait_bbox(scene.get("fetch_bbox") or requested_b)
        key = self._bait_advanced_cache_key(scene, qb)
        def _shell():
            return {
                "ok": True,
                "bbox": qb,
                "requested_bbox": requested_b,
                "quantized_ocean_bbox": qb,
                "scene_plan": scene,
                "visible_bbox": scene.get("visible_bbox"),
                "fetch_bbox": scene.get("fetch_bbox"),
                "render_budget": scene.get("render_budget"),
                "source": "deferred_tile_cache",
                "mode": "cache_first_bait_warming_quantized",
                "bait": {"status": "warming", "source": "cache_first", "polygons": []},
                "bait_score": [],
                "oceanPoints": [],
                "ocean_points": [],
                "confidence": {"overall": 0.0, "reason": "cache_warming"},
            }
        return self._cache_first_split_payload(
            key=key,
            label="bait",
            ttl_seconds=BAIT_ADVANCED_CACHE_TTL_SECONDS,
            stale_seconds=BAIT_ADVANCED_STALE_SECONDS,
            builder=lambda: self._bait_advanced_payload_heavy(qb, scene.get("visible_bbox")),
            shell_factory=_shell,
        )


    def _bait_advanced_payload_heavy(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        """Live bait solve: HYCOM/RTOFS SST+current + CoastWatch chlorophyll + weather.

        This replaces the old marker-history proxy bait polygons. Fish CSV locations
        still exist as selectable nodes, but their glass-pane bait score should now
        be sampled from this live grid/bait_score contract instead of driving the
        polygon solve.
        """
        requested_b = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(requested_b, visible_bbox, layer="bait")
        b = self._quantize_bait_bbox(scene.get("fetch_bbox") or requested_b)
        key = self._bait_advanced_cache_key(scene, b)
        if not self._bbox_has_probable_ocean_overlap(b):
            payload = self._empty_live_required_payload(label="bait", bbox=b, reason="no_ocean_overlap")
            payload.update({"requested_bbox": requested_b, "quantized_ocean_bbox": b, "payload_state": "skipped_no_ocean_overlap", "mode": "bait_advanced_skipped_inland_only"})
            return self._split_cache_set(key, self._attach_scene_plan(payload, scene))
        cached = self._split_cache_get(key, BAIT_ADVANCED_CACHE_TTL_SECONDS)
        if cached:
            cached_bait = cached.get("bait") if isinstance(cached.get("bait"), dict) else {}
            cached_has_content = bool((cached.get("bait_score") or cached.get("advanced_bait_rows") or cached_bait.get("polygons") or cached.get("polygons") or []))
            if cached_has_content:
                out = dict(cached)
                out.setdefault("cache", {})["hit"] = True
                out["cache"]["mode"] = "fresh_live_bait_grid_cache_quantized"
                return out

        if not self._bait_advanced_refresh_allowed(key):
            stale = self._split_cache_get(key, BAIT_ADVANCED_STALE_SECONDS)
            if isinstance(stale, dict):
                out = dict(stale)
                out.setdefault("cache", {})["hit"] = True
                out["cache"]["mode"] = "stale_bait_advanced_refresh_throttled"
                out["cache"]["refresh_throttled"] = True
                return out
            shell = self._empty_live_required_payload(label="bait", bbox=b, reason="bait_refresh_throttled")
            shell.update({"requested_bbox": requested_b, "quantized_ocean_bbox": b, "payload_state": "refresh_throttled", "mode": "bait_advanced_refresh_min_gap"})
            return self._attach_scene_plan(shell, scene)

        started = time.time()
        diagnostics: dict[str, Any] = {
            "solve": "live_grid_bait_probability",
            "states": [],
            "cache_warm_state": "builder_running",
        }

        def _bait_fail(reason: str, error: str | None = None) -> Dict[str, Any]:
            payload = self._empty_live_required_payload(label="bait", bbox=b, reason=reason, error=error)
            payload["latency_ms"] = int((time.time() - started) * 1000)
            payload.setdefault("diagnostics", {}).update(diagnostics)
            payload["payload_state"] = "provider_failed" if error else "provider_empty"
            payload["data_quality"] = {"live_ncss_erddap": False, "mock": False, "proxy": False, "fallback_used": False}
            payload = self._attach_scene_plan(payload, scene)
            payload.setdefault("cache", {}).update({"hit": False, "mode": "provider_empty_not_cached", "write_policy": "do_not_promote_empty_bait_grid"})
            return payload

        tile_deg = self._ocean_provider_tile_deg()
        if self._is_wide_ocean_provider_bbox(b, tile_deg):
            tiles = self._split_ocean_provider_tiles(b, tile_deg)
            self._log_ocean_provider_tile_policy("bait", b, tiles)
            shell = self._empty_live_required_payload(label="bait", bbox=b, reason="wide_bbox_tile_cache_only_no_hycom_one_shot")
            shell.update({"requested_bbox": requested_b, "quantized_ocean_bbox": b, "payload_state": "cache_only_large_bbox", "mode": "bait_large_bbox_tile_cache_only", "provider_tile_count": len(tiles), "tile_deg": tile_deg, "provider_tiles": tiles[:int(os.getenv("GFS_OCEAN_PROVIDER_MAX_SYNC_TILES", "16") or "16")]})
            return self._attach_scene_plan(shell, scene)
        live_policy = self._hycom_live_bbox_policy(b, scene, layer="bait")
        if not live_policy.get("allowed"):
            shell = self._empty_live_required_payload(label="bait", bbox=b, reason="large_bbox_cache_only_no_hycom_one_shot")
            shell.update({"requested_bbox": requested_b, "quantized_ocean_bbox": b, "payload_state": "cache_only_large_bbox", "mode": "bait_large_bbox_tile_cache_only", "hycom_live_policy": live_policy})
            return self._attach_scene_plan(shell, scene)
        stride = int(scene.get("provider_stride") or self._ocean_stride_for_bbox(b, scene.get("provider_target_cells")))
        try:
            ocean_raw, ocean_time = self.ocean_provider._fetch_subset_sync(
                bbox=BBox(west=b["west"], south=b["south"], east=b["east"], north=b["north"]),
                stride=stride,
                valid_time=None,
            )
            diagnostics["states"].append({"provider": "ocean", "state": "ok", "stride": stride, "grid_shape": (ocean_raw.get("source_meta") or {}).get("grid_shape")})
        except Exception as exc:
            return _bait_fail("ocean_provider_exception", str(exc))

        bio_raw: dict[str, Any] = {}
        if getattr(self, "bio_provider", None) is not None:
            try:
                bio_raw, bio_time = self.bio_provider._fetch_subset_sync(
                    bbox=BBox(west=b["west"], south=b["south"], east=b["east"], north=b["north"]),
                    stride=max(1, min(stride, 2)),
                    valid_time=None,
                )
                diagnostics["states"].append({"provider": "coastwatch_chlorophyll", "state": "ok" if bio_raw.get("chlorophyll") else "empty", "grid_shape": [len(bio_raw.get("chlorophyll") or []), len((bio_raw.get("chlorophyll") or [[]])[0] if bio_raw.get("chlorophyll") else [])]})
            except Exception as exc:
                diagnostics["states"].append({"provider": "coastwatch_chlorophyll", "state": "failed_optional", "error": str(exc)})
        else:
            diagnostics["states"].append({"provider": "coastwatch_chlorophyll", "state": "unavailable_optional"})

        weather_fields: dict[str, Any] = {}
        cached_weather_fields = self._cached_gfs_weather_fields_for_bait(b)
        if cached_weather_fields:
            weather_fields = cached_weather_fields
            diagnostics["states"].append({
                "provider": "gfs_weather_fields",
                "state": "cached",
                "field_keys": list(weather_fields.keys())[:12],
                "policy": "cached_scene_weather_only_no_gfs_wake",
                "source": weather_fields.get("_bait_weather_source"),
            })
        elif BAIT_ADVANCED_ALLOW_LIVE_GFS_WEATHER:
            try:
                weather_payload = self.generate_weather_payload(b)
                weather_fields = dict(weather_payload.get("fields") or {})
                diagnostics["states"].append({"provider": "gfs_weather_fields", "state": "live_explicit", "field_keys": list(weather_fields.keys())[:12], "policy": "GFS_BAIT_ADVANCED_ALLOW_LIVE_GFS_WEATHER=true"})
            except Exception as exc:
                diagnostics["states"].append({"provider": "gfs_weather_fields", "state": "failed_optional", "error": str(exc), "policy": "explicit_live_allowed"})
        else:
            diagnostics["states"].append({
                "provider": "gfs_weather_fields",
                "state": "cache_miss_skipped_live",
                "policy": "bait wants GFS wind/weather when already cached; live GFS decode disabled by default",
            })

        if derive_bait_payload is None:
            return _bait_fail("derive_bait_payload_import_missing")

        try:
            solved = derive_bait_payload(
                weather_fields,
                ocean_raw or {},
                bio_raw or {},
                bbox=[b["west"], b["south"], b["east"], b["north"]],
            )
        except Exception as exc:
            log.exception("live bait grid solve failed")
            return _bait_fail("derive_bait_payload_exception", str(exc))

        bait = dict(solved.get("bait") or {})
        score_rows = list(solved.get("bait_score") or solved.get("advanced_bait_rows") or solved.get("advancedBaitRows") or [])
        advanced_rows = list(solved.get("advanced_bait_rows") or solved.get("advancedBaitRows") or score_rows)
        if not score_rows:
            return _bait_fail("live_bait_grid_empty")

        # Keep bait.source as "full_stack" for the frontend renderer readiness contract.
        # Put the live provider name in live_source/provider_source instead of replacing it;
        # older JS checks bait.source === "full_stack" before trusting server polygons.
        bait["source"] = "full_stack"
        bait["live_source"] = "live_hycom_coastwatch_weather_grid"
        bait["provider_source"] = "live_hycom_coastwatch_weather_grid"
        bait["status"] = "ready"
        bait["bait_score"] = score_rows
        bait.setdefault("meta", {})
        bait["meta"].update({
            "solve_method": "server_live_marching_squares_grid",
            "input_ocean": "HYCOM/RTOFS SST/current grid",
            "input_bio": "CoastWatch chlorophyll when available",
            "input_weather": "cached GFS wind/cloud/precip when available; bait does not wake live GFS unless explicitly enabled",
            "marker_locations_drive_polygons": False,
            "location_intel_should_sample_grid": True,
            "advanced_bait_row_count": len(advanced_rows),
            "advanced_bait_rows_contract": "dense_hycom_sst_current_probability_rows_not_boat_ocean_points",
            "contour_contract": "server_marching_square_polygons_with_depth_extrusion_metadata",
        })

        payload: Dict[str, Any] = {
            **solved,
            "ok": True,
            "bbox": b,
            "requested_bbox": requested_b,
            "quantized_ocean_bbox": b,
            "bbox_object": b,
            "source": "live_hycom_coastwatch_bait_grid",
            "mode": "live_full_stack_marching_squares_bait_solve",
            "schema": "bait_live_marching_squares_v2",
            "engine": "HYCOM/RTOFS SST/current + CoastWatch chlorophyll + GFS weather -> bait_score grid -> marching-square contours",
            "source_meta": (ocean_raw or {}).get("source_meta") or {},
            "sst_landmask": (((ocean_raw or {}).get("source_meta") or {}).get("sst_landmask") or {}),
            "landmask_contract": (((ocean_raw or {}).get("source_meta") or {}).get("landmask_contract") or "finite_sst_is_shared_water_gate_for_bait"),
            "quality_policy": self._live_payload_policy(),
            "bait": bait,
            "bait_score": score_rows,
            "advanced_bait_rows": advanced_rows,
            "advancedBaitRows": advanced_rows,
            "bait_rows": advanced_rows,
            "ocean_points": [],
            "oceanPoints": {"ok": False, "source": "not_boat_ocean_points_dense_bait_rows_are_advancedBaitRows", "points": [], "count": 0},
            "dense_bait_field": {
                "ok": True,
                "source": "advanced_hycom_sst_current_bait_probability_field",
                "rows": advanced_rows,
                "count": len(advanced_rows),
                "contract": "dense_rows_for_bait_contours_not_boat_ocean_points",
            },
            "valid_time": ocean_time.isoformat() if hasattr(ocean_time, "isoformat") and ocean_time else None,
            "latency_ms": int((time.time() - started) * 1000),
            "diagnostics": diagnostics,
            "cache": {"hit": False, "ttl_seconds": BAIT_ADVANCED_CACHE_TTL_SECONDS, "mode": "live_grid_bait_solve_quantized"},
            "payload_state": "live",
            "data_quality": {"live_ncss_erddap": True, "mock": False, "proxy": False, "fallback_used": False},
            "ts": self._now_ms(),
        }
        payload["stride"] = stride
        payload["source_resolution_deg"] = 0.25
        payload["derived_resolution_deg"] = round(0.25 / max(1, int((bait.get("meta") or {}).get("detail_multiplier") or os.getenv("GFS_ADVANCED_BAIT_DETAIL_MULTIPLIER", "4") or "4")), 6)
        return self._split_cache_set(key, self._attach_truth_contract(self._attach_scene_plan(payload, scene), bbox=b, stride=stride, source_resolution_deg=0.25, derived_resolution_deg=payload["derived_resolution_deg"], extra={"input_truth": "live_source_resolution_viewport_fetch", "layer": "bait", "advanced_bait_rows": len(advanced_rows)}))

    def _split_cache_peek(self, key: str) -> Any:
        if not hasattr(self, "_split_payload_cache"):
            return None
        row = self._split_payload_cache.get(key)
        if not row:
            return None
        return row.get("payload")

    def _split_cache_row(self, key: str) -> dict[str, Any] | None:
        if not hasattr(self, "_split_payload_cache"):
            return None
        row = self._split_payload_cache.get(key)
        return row if isinstance(row, dict) else None

    def _split_cache_age_seconds(self, key: str) -> int | None:
        row = self._split_cache_row(key)
        if not row:
            return None
        try:
            return max(0, int(time.time() - float(row.get("time", 0))))
        except Exception:
            return None

    def _split_cache_inflight(self) -> set[str]:
        if not hasattr(self, "_split_warm_inflight"):
            self._split_warm_inflight = set()
        return self._split_warm_inflight

    def _schedule_split_warm(self, key: str, label: str, builder) -> bool:
        """Run a split payload builder in the background.

        This is the core cache-first rule for viewport changes: API callers get
        memory/stale/queued cache immediately while the slow provider work happens
        off the request path.  Errors stay visible in provider/payload debug and
        are not swallowed as fake data.
        """
        inflight = self._split_cache_inflight()
        if key in inflight:
            return False
        inflight.add(key)

        def _run() -> None:
            try:
                payload = builder()
                if isinstance(payload, dict):
                    payload.setdefault("cache", {})["warmed_by"] = label
                    payload["cache"]["warmed_at"] = self._now_ms()
                    payload["cache"]["key"] = key
                    lower_label = str(label).lower()
                    is_ocean_backed = any(tok in lower_label for tok in ("ocean", "bait", "boater", "boat", "shark"))
                    point_count = len(payload.get("points") or payload.get("ocean_points") or payload.get("current_points") or [])
                    op = payload.get("oceanPoints")
                    if isinstance(op, dict):
                        point_count = max(point_count, len(op.get("points") or []), int(op.get("count") or 0))
                    bait = payload.get("bait") if isinstance(payload.get("bait"), dict) else {}
                    poly_count = len(bait.get("polygons") or []) + len(payload.get("polygons") or []) + len(payload.get("contours") or []) + len(payload.get("score_points") or [])
                    if is_ocean_backed and point_count <= 0 and poly_count <= 0:
                        payload["cache"].update({"write_policy": "warm_empty_ocean_payload_not_promoted", "empty_write_rejected": True})
                        return
                    self._split_cache_set(key, payload)
            except Exception as exc:
                level = log.info if isinstance(exc, FileNotFoundError) else log.warning
                level("[gfs/cache] split warm failed label=%s key=%s err=%s", label, key, exc)
            finally:
                try:
                    inflight.discard(key)
                except Exception:
                    pass

        threading.Thread(target=_run, name=f"gfs-{label}-warm", daemon=True).start()
        return True

    def _live_first_retained_split_payload(self, *, key: str, label: str, dedupe_seconds: int, retained_max_age_seconds: int, builder, shell_factory):
        """Return last-known-good display data while always trying to refresh live.

        Clouds/rain should not be considered fresh just because a split cache row
        exists.  The cache row is a retained display bridge: draw it instantly if
        available, but schedule a fresh provider attempt on each active request
        cycle unless an identical refresh is already in-flight or was started a
        few seconds ago.
        """
        row = self._split_cache_row(key)
        retained = row.get("payload") if isinstance(row, dict) else None
        age = self._split_cache_age_seconds(key) if isinstance(retained, dict) else None
        try:
            age_ok = age is not None and int(age) <= int(retained_max_age_seconds)
        except Exception:
            age_ok = False

        refresh_marker_key = f"live-refresh-marker:{key}"
        # Clouds/rain/lightning share the same expensive GFS/cfgrib source. In
        # practice the journal showed multiple bboxes waking full 721x1440
        # canonicalization within seconds. Gate both exact-key and global GFS live
        # refreshes so empty/warming placeholders cannot stampede NOMADS/cfgrib.
        is_gfs_live_label = any(tok in str(label).lower() for tok in ("cloud", "rain", "lightning", "jetstream", "gfs"))
        global_marker_key = "live-refresh-marker:gfs-global-provider"
        recent_marker = self._split_cache_get(refresh_marker_key, max(1, int(dedupe_seconds or 1)))
        global_recent = self._split_cache_get(global_marker_key, max(1, int(CLOUD_LIVE_DEDUPE_SECONDS))) if is_gfs_live_label else None
        scheduled = False
        if not recent_marker and not global_recent:
            self._split_cache_set(refresh_marker_key, {"ts": self._now_ms(), "label": label})
            if is_gfs_live_label:
                self._split_cache_set(global_marker_key, {"ts": self._now_ms(), "label": label, "key": key})
            scheduled = self._schedule_split_warm(key, f"{label}-live", builder)

        if isinstance(retained, dict) and age_ok:
            out = dict(retained)
            out.setdefault("cache", {})["hit"] = True
            out["cache"].update({
                "mode": "retained_last_good_display_while_fetching_fresh",
                "policy": "live_first_no_cloud_source_ttl",
                "age_sec": age,
                "key": key,
                "refresh_scheduled": scheduled,
                "dedupe_seconds": int(dedupe_seconds or 0),
                "retained_max_age_seconds": int(retained_max_age_seconds or 0),
            })
            out["display_state"] = "retained_last_good"
            out["request_state"] = "fresh_gfs_in_flight" if scheduled else "fresh_gfs_deduped_or_in_flight"
            out["payload_state"] = "retained_display_fetching_fresh"
            out.setdefault("debug", {})["cloud_cache_policy"] = "retained display; fresh GFS is globally throttled"
            return out

        shell = shell_factory() if callable(shell_factory) else {}
        if not isinstance(shell, dict):
            shell = {}
        shell.setdefault("ok", True)
        shell.setdefault("status", "fetching_fresh")
        shell.setdefault("source", "live_first_queued")
        shell.setdefault("cache", {})["hit"] = False
        shell["cache"].update({
            "mode": "no_retained_display_fetching_fresh",
            "policy": "live_first_no_cloud_source_ttl",
            "refresh_scheduled": scheduled,
            "key": key,
            "dedupe_seconds": int(dedupe_seconds or 0),
            "retained_max_age_seconds": int(retained_max_age_seconds or 0),
        })
        shell["display_state"] = "empty_until_first_live_payload"
        shell["request_state"] = "fresh_gfs_in_flight" if scheduled else "fresh_gfs_deduped_or_in_flight"
        shell["payload_state"] = "fetching_fresh"
        shell["ts"] = self._now_ms()
        return shell

    def _cache_first_split_payload(self, *, key: str, label: str, ttl_seconds: int, stale_seconds: int, builder, shell_factory):
        def _has_ocean_renderable(p: Any) -> bool:
            if not isinstance(p, dict):
                return False
            point_count = len(p.get("points") or p.get("ocean_points") or p.get("current_points") or [])
            op = p.get("oceanPoints")
            if isinstance(op, dict):
                point_count = max(point_count, len(op.get("points") or []), int(op.get("count") or 0))
            bait = p.get("bait") if isinstance(p.get("bait"), dict) else {}
            poly_count = len(bait.get("polygons") or []) + len(p.get("polygons") or []) + len(p.get("contours") or []) + len(p.get("score_points") or [])
            return point_count > 0 or poly_count > 0

        ocean_label = any(tok in str(label).lower() for tok in ("ocean", "bait", "boater", "boat", "shark"))
        cached = self._split_cache_get(key, ttl_seconds)
        if isinstance(cached, dict) and (not ocean_label or _has_ocean_renderable(cached)):
            out = dict(cached)
            out.setdefault("cache", {})["hit"] = True
            out["cache"]["mode"] = "fresh_memory_cache"
            out["cache"]["age_sec"] = self._split_cache_age_seconds(key)
            return out
        stale = self._split_cache_peek(key)
        scheduled = self._schedule_split_warm(key, label, builder)
        if isinstance(stale, dict) and (not ocean_label or _has_ocean_renderable(stale)):
            out = dict(stale)
            out.setdefault("cache", {})["hit"] = True
            out["cache"]["mode"] = "stale_while_revalidate"
            out["cache"]["age_sec"] = self._split_cache_age_seconds(key)
            out["cache"]["refresh_scheduled"] = scheduled
            out["payload_state"] = "stale_while_revalidate"
            return out
        shell = shell_factory() if callable(shell_factory) else {}
        if not isinstance(shell, dict):
            shell = {}
        shell.setdefault("ok", True)
        shell.setdefault("status", "warming")
        shell.setdefault("source", "cache_first_queued")
        shell.setdefault("cache", {})["hit"] = False
        shell["cache"].update({"mode": "queued_background_warm", "refresh_scheduled": scheduled, "key": key, "ttl_seconds": ttl_seconds, "stale_seconds": stale_seconds})
        shell["payload_state"] = "warming"
        shell["ts"] = self._now_ms()
        return shell

    def _cache_warm_status_payload(self) -> dict[str, Any]:
        state = getattr(self, "_globe_cache_warm_state", None)
        if not isinstance(state, dict):
            return {"running": False, "started": False, "message": "no warm has been scheduled", "ts": self._now_ms()}
        out = dict(state)
        out["ts"] = self._now_ms()
        return out

    def cache_warm_status_payload(self) -> dict[str, Any]:
        return {"ok": True, "schema": "lftr_globe_cache_warm_status_v1", "warm": self._cache_warm_status_payload()}

    def _schedule_priority_tile_warm(self, tiles: list[dict[str, Any]], scene: dict[str, Any], requested_layers: set[str], reason: str = "priority") -> dict[str, Any]:
        """Small foreground-adjacent warm for a new viewport while a large warm is still running."""
        if not tiles:
            return {"scheduled": False, "reason": "no_tiles"}
        if not hasattr(self, "_priority_tile_warm_lock"):
            self._priority_tile_warm_lock = threading.Lock()
            self._priority_tile_warm_keys = set()
        key = str((tiles[0] or {}).get("tile_id") or "") + ":" + ",".join(sorted(requested_layers))
        with self._priority_tile_warm_lock:
            if key in self._priority_tile_warm_keys:
                return {"scheduled": False, "reason": "priority_already_running", "key": key}
            self._priority_tile_warm_keys.add(key)
        limit = max(1, min(int(os.getenv("GFS_TILE_PRIORITY_WARM_LIMIT", "3") or "3"), len(tiles), 6))
        selected = [t for t in tiles[:limit] if t.get("tile_id")]
        def _run_priority() -> None:
            try:
                for item in selected:
                    try:
                        self.tile_intel_payload(str(item.get("tile_id")), ttl_seconds=0, visible_bbox=scene.get("visible_bbox"), allowed_layers=requested_layers)
                    except Exception as exc:
                        log.info("[gfs/cache] priority tile warm skipped tile=%s err=%s", item.get("tile_id"), exc)
            finally:
                try:
                    with self._priority_tile_warm_lock:
                        self._priority_tile_warm_keys.discard(key)
                except Exception:
                    pass
        threading.Thread(target=_run_priority, name="gfs-priority-tile-warm", daemon=True).start()
        return {"scheduled": True, "reason": reason, "key": key, "tiles": len(selected), "policy": "small_new_viewport_priority_warm_runs_alongside_long_warm"}

    def schedule_globe_cache_warm(self, bbox: dict[str, float] | None = None, max_tiles: int = 512, reason: str = "website_load", visible_bbox: dict[str, float] | None = None, layers: list[str] | None = None) -> dict[str, Any]:
        """Kick a center-first global cache warm without blocking the web request.

        The browser should load from whatever disk/memory cache exists immediately.
        This warmer then refreshes the selected center tile, visible tiles, and rings
        outward. It writes compact tile products to disk and releases memory per tile.
        This build is tuned for the LFTR e2 2-vCPU / 16GB RAM VM profile: bounded
        I/O parallelism, larger live build batches, and conservative CPU decode.
        """
        requested = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(requested, visible_bbox, layer="cache-refresh")
        try:
            max_tiles_i = max(1, min(int(max_tiles or 512), 512))
        except Exception:
            max_tiles_i = 512
        plan = self.tile_plan_payload(requested, max_tiles=max_tiles_i)
        requested_layers = {str(x).strip().lower() for x in (layers or []) if str(x).strip()}
        if not hasattr(self, "_globe_cache_warm_lock"):
            self._globe_cache_warm_lock = threading.Lock()
        with self._globe_cache_warm_lock:
            state = getattr(self, "_globe_cache_warm_state", {}) or {}
            if state.get("running"):
                priority = self._schedule_priority_tile_warm(list(plan.get("tiles") or []), scene, requested_layers, reason=reason)
                warm_status = self._cache_warm_status_payload()
                warm_status["priority"] = priority
                return {"ok": True, "schema": "lftr_globe_cache_warm_v1", "scheduled": bool(priority.get("scheduled")), "reason": "already_running_priority_tile_warm" if priority.get("scheduled") else "already_running", "plan": plan, "warm": warm_status, "priority": priority}
            run_id = f"warm-{self._now_ms()}"
            self._globe_cache_warm_state = {
                "running": True,
                "started": True,
                "run_id": run_id,
                "reason": reason,
                "requested_bbox": requested,
                "scene_plan": scene,
                "visible_bbox": scene.get("visible_bbox"),
                "fetch_bbox": scene.get("fetch_bbox"),
                "center": plan.get("center"),
                "total_tiles_requested": len(plan.get("tiles") or []),
                "concurrency": int(os.getenv("GFS_TILE_WARM_WORKERS", "1") or "1"),
                "tile_count_policy": "plan_up_to_512_tiles_but_build_live_only_center_first_limit_env_GFS_TILE_WARM_BUILD_LIMIT_default_32",
                "download_policy": "bounded_parallel_live_tile_refresh_cache_first_read_only_browser_tiles",
                "scene_tile_product": "gzip_json_large_diverse_payload_contracts",
                "completed_tiles": 0,
                "failed_tiles": 0,
                "last_tile_id": None,
                "started_at": self._now_ms(),
                "finished_at": None,
                "message": "warming bounded center-first gzip scene-tile point cache",
                "requested_layers": sorted(requested_layers) if requested_layers else ["all"],
                "route_policy": "only requested layer contracts are live-built; omitted pills return not_requested shells",
            }

        def _run() -> None:
            try:
                # Do not build the full frame inside the cache warmer.  The frame
                # endpoint is independently cache-first; doing a full frame plus 512
                # tile contract builds in one background run can starve /ws/gfs and
                # cause nginx 502s on small VMs.
                tiles = list(plan.get("tiles") or [])
                try:
                    live_build_limit = max(1, min(int(os.getenv("GFS_TILE_WARM_BUILD_LIMIT", "8") or "8"), len(tiles), 64))
                except Exception:
                    live_build_limit = min(32, len(tiles), 160)
                tiles_to_build = tiles[:live_build_limit]
                with self._globe_cache_warm_lock:
                    st = self._globe_cache_warm_state
                    st["live_build_limit"] = live_build_limit
                    st["message"] = "warming bounded live scene-tile point cache; uncached tiles stay cache-miss placeholders"
                # Warm many small tiles in parallel. Each worker is synchronous for a
                # single tile, but the thread pool lets NCSS/cache exchanges overlap.
                # Existing disk cache is read first by tile_intel_payload(); ttl=0 then
                # refreshes the compact tile product while provider-level caches keep
                # network use bounded.
                workers = max(1, min(int(os.getenv("GFS_TILE_WARM_WORKERS", "1") or "1"), 4))

                def _warm_one(item: dict[str, Any]) -> dict[str, Any]:
                    tid = str(item.get("tile_id") or "")
                    if not tid:
                        return {"ok": False, "tile_id": None, "error": "missing_tile_id"}
                    payload = self.tile_intel_payload(tid, ttl_seconds=0, visible_bbox=scene.get("visible_bbox"), allowed_layers=requested_layers)
                    return {"ok": True, "tile_id": tid, "latency_ms": payload.get("latency_ms"), "cache_hit": bool((payload.get("cache") or {}).get("hit"))}

                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gfs-tile-warm") as pool:
                    futures = [pool.submit(_warm_one, item) for item in tiles_to_build if item.get("tile_id")]
                    for fut in as_completed(futures):
                        try:
                            result = fut.result()
                            tid = str(result.get("tile_id") or "")
                            with self._globe_cache_warm_lock:
                                st = self._globe_cache_warm_state
                                if result.get("ok"):
                                    st["completed_tiles"] = int(st.get("completed_tiles") or 0) + 1
                                else:
                                    st["failed_tiles"] = int(st.get("failed_tiles") or 0) + 1
                                    st["last_error"] = str(result.get("error") or "tile warm failed")
                                st["last_tile_id"] = tid
                                st["message"] = "warming parallel small scene-tile point cache"
                        except Exception as exc:
                            log.warning("[gfs] tile warm failed err=%s", exc)
                            with self._globe_cache_warm_lock:
                                st = self._globe_cache_warm_state
                                st["failed_tiles"] = int(st.get("failed_tiles") or 0) + 1
                                st["last_error"] = str(exc)
                with self._globe_cache_warm_lock:
                    st = self._globe_cache_warm_state
                    st["running"] = False
                    st["finished_at"] = self._now_ms()
                    st["message"] = "scene-tile point cache warm complete"
            except Exception as exc:
                log.warning("[gfs] globe cache warm crashed err=%s", exc)
                try:
                    with self._globe_cache_warm_lock:
                        st = self._globe_cache_warm_state
                        st["running"] = False
                        st["finished_at"] = self._now_ms()
                        st["message"] = "cache warm failed"
                        st["last_error"] = str(exc)
                except Exception:
                    pass

        threading.Thread(target=_run, name="gfs-globe-cache-refresh", daemon=True).start()
        return {"ok": True, "schema": "lftr_globe_cache_warm_v1", "scheduled": True, "reason": reason, "requested_layers": sorted(requested_layers) if requested_layers else ["all"], "route_policy": "cache_refresh_builds_only_requested_visual_contracts", "scene_plan": scene, "plan": plan, "warm": self._cache_warm_status_payload()}

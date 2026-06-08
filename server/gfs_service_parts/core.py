from __future__ import annotations
import os

import server.gfs_service as _svc
globals().update({k: v for k, v in vars(_svc).items() if not k.startswith("__")})


class CoreMixin:
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _default_bbox(self) -> dict[str, float]:
        return {"west": -180.0, "south": -80.0, "east": 180.0, "north": 80.0}

    def _normalize_bbox(self, bbox: dict[str, float] | None = None) -> dict[str, float]:
        raw = bbox or self._default_bbox()
        return {
            "west": float(raw.get("west", -180.0)),
            "south": float(raw.get("south", -80.0)),
            "east": float(raw.get("east", 180.0)),
            "north": float(raw.get("north", 80.0)),
        }

    def _bbox_key_fragment(self, bbox: dict[str, float] | None) -> str:
        b = self._normalize_bbox(bbox)
        return f"{b['west']:.3f},{b['south']:.3f},{b['east']:.3f},{b['north']:.3f}"

    def _point_inside_bbox_padded(self, lat: float, lon: float, bbox: dict[str, float], pad_factor: float = 0.18) -> bool:
        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            return False
        if not (-90.0 <= lat <= 90.0 and -1800.0 <= lon <= 1800.0):
            return False
        lon = ((lon + 180.0) % 360.0) - 180.0
        b = self._normalize_bbox(bbox)
        lat_span = abs(b["north"] - b["south"])
        lon_span = abs(b["east"] - b["west"])
        lat_pad = max(0.30, min(4.0, lat_span * pad_factor))
        lon_pad = max(0.30, min(6.0, lon_span * pad_factor))
        south = min(b["south"], b["north"]) - lat_pad
        north = max(b["south"], b["north"]) + lat_pad
        if lat < south or lat > north:
            return False
        west = ((b["west"] - lon_pad + 180.0) % 360.0) - 180.0
        east = ((b["east"] + lon_pad + 180.0) % 360.0) - 180.0
        if west <= east:
            return west <= lon <= east
        return lon >= west or lon <= east

    def _cloud_tile_center_lonlat(self, tile: dict[str, Any]) -> tuple[float | None, float | None]:
        """Return a stable cloud-tile center across all backend tile contracts.

        The live GFS region solver can produce normal rectangular tiles, region
        contour tiles, and enriched band-footprint tiles.  Older clipping code
        only trusted bounds.center and could reject perfectly valid live tiles
        when the center field was missing/renamed.  This helper intentionally
        falls back through every known geometry shape before declaring the tile
        invalid, so diagnostics distinguish true out-of-bbox tiles from schema
        drift.
        """
        if not isinstance(tile, dict):
            return None, None

        def _finite_pair(lat: Any, lon: Any) -> tuple[float | None, float | None]:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                if math.isfinite(lat_f) and math.isfinite(lon_f):
                    return lat_f, lon_f
            except Exception:
                pass
            return None, None

        bounds = tile.get("bounds")
        if isinstance(bounds, dict):
            lat = bounds.get("lat_center", bounds.get("center_lat"))
            lon = bounds.get("lon_center", bounds.get("center_lon"))
            lat_f, lon_f = _finite_pair(lat, lon)
            if lat_f is not None and lon_f is not None:
                return lat_f, lon_f
            # Some contours only carry extents.  The midpoint is a safe contract
            # center for clipping and frontend hover placement.
            lat_min = bounds.get("lat_min", bounds.get("south"))
            lat_max = bounds.get("lat_max", bounds.get("north"))
            lon_min = bounds.get("lon_min", bounds.get("west"))
            lon_max = bounds.get("lon_max", bounds.get("east"))
            try:
                return (float(lat_min) + float(lat_max)) / 2.0, (float(lon_min) + float(lon_max)) / 2.0
            except Exception:
                pass

        for key in ("center", "centroid"):
            center = tile.get(key)
            if isinstance(center, dict):
                lat_f, lon_f = _finite_pair(center.get("lat", center.get("latitude")), center.get("lon", center.get("lng", center.get("longitude"))))
                if lat_f is not None and lon_f is not None:
                    return lat_f, lon_f
            elif isinstance(center, (list, tuple)) and len(center) >= 2:
                # Accept either [lon, lat] GeoJSON-style or [lat, lon].
                a, b = center[0], center[1]
                lat_f, lon_f = _finite_pair(b, a)
                if lat_f is not None and lon_f is not None and -90.0 <= lat_f <= 90.0:
                    return lat_f, lon_f
                lat_f, lon_f = _finite_pair(a, b)
                if lat_f is not None and lon_f is not None:
                    return lat_f, lon_f

        lat_f, lon_f = _finite_pair(tile.get("lat", tile.get("latitude")), tile.get("lon", tile.get("lng", tile.get("longitude"))))
        if lat_f is not None and lon_f is not None:
            return lat_f, lon_f

        # Region contour fallback: average the ring points.  Points can be
        # dictionaries or [lon, lat] pairs.
        point_sets: list[Any] = []
        for key in ("region_footprint", "footprint", "points"):
            val = tile.get(key)
            if isinstance(val, list):
                point_sets.append(val)
        bands = tile.get("bands")
        if isinstance(bands, dict):
            for band in bands.values():
                if not isinstance(band, dict):
                    continue
                for fp in band.get("footprints") or []:
                    if isinstance(fp, dict) and isinstance(fp.get("points"), list):
                        point_sets.append(fp.get("points"))
        lats: list[float] = []
        lons: list[float] = []
        for pts in point_sets:
            for p in pts or []:
                lat = lon = None
                if isinstance(p, dict):
                    lat = p.get("lat", p.get("latitude"))
                    lon = p.get("lon", p.get("lng", p.get("longitude")))
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    # Cloud footprints are emitted as [lon, lat].
                    lon, lat = p[0], p[1]
                lat_f, lon_f = _finite_pair(lat, lon)
                if lat_f is not None and lon_f is not None and -90.0 <= lat_f <= 90.0:
                    lats.append(lat_f)
                    lons.append(lon_f)
            if lats and lons:
                return sum(lats) / len(lats), sum(lons) / len(lons)

        return None, None

    def _normalize_cloud_tile_for_render(self, raw: dict[str, Any], lat: float, lon: float) -> dict[str, Any]:
        tile = dict(raw)
        lon = ((float(lon) + 180.0) % 360.0) - 180.0
        bounds = dict(tile.get("bounds") or {})
        bounds.setdefault("lat_center", round(float(lat), 5))
        bounds.setdefault("lon_center", round(lon, 5))
        tile["bounds"] = bounds
        tile["center"] = {"lat": round(float(lat), 5), "lon": round(lon, 5)}
        tile.setdefault("diagnostic_center_source", "server_normalized_cloud_tile_center")
        return tile

    def _clip_cloud_tiles_to_bbox(self, tiles: list[dict[str, Any]], bbox: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        rejected_invalid = 0
        rejected_out_of_bbox = 0
        lat_values: list[float] = []
        lon_values: list[float] = []
        all_valid_lats: list[float] = []
        all_valid_lons: list[float] = []
        valid_tiles: list[tuple[dict[str, Any], float, float]] = []
        for tile in tiles or []:
            if not isinstance(tile, dict):
                rejected_invalid += 1
                continue
            lat, lon = self._cloud_tile_center_lonlat(tile)
            if lat is None or lon is None or not math.isfinite(lat) or not math.isfinite(lon) or lat < -90.0 or lat > 90.0:
                rejected_invalid += 1
                continue
            lat_f = float(lat)
            lon_f = ((float(lon) + 180.0) % 360.0) - 180.0
            all_valid_lats.append(lat_f)
            all_valid_lons.append(lon_f)
            valid_tiles.append((tile, lat_f, lon_f))
            if not self._point_inside_bbox_padded(lat_f, lon_f, bbox):
                rejected_out_of_bbox += 1
                continue
            kept.append(self._normalize_cloud_tile_for_render(tile, lat_f, lon_f))
            lat_values.append(lat_f)
            lon_values.append(lon_f)

        # A tilted/boot world fetch is intentionally clamped to -80..80.  Live
        # GFS can still create high-latitude cloud regions just outside that
        # render clamp.  If every otherwise-valid tile gets rejected for such a
        # wide/world bbox, preserve the live payload instead of showing empty
        # clouds and flooding the journal with bbox-contract warnings.
        wide_or_edge_bbox = False
        try:
            b = self._normalize_bbox(bbox)
            lat_span = abs(float(b.get("north", 0.0)) - float(b.get("south", 0.0)))
            lon_span = abs(float(b.get("east", 0.0)) - float(b.get("west", 0.0)))
            wide_or_edge_bbox = lat_span >= 150.0 or lon_span >= 300.0 or float(b.get("north", 0.0)) >= 79.5 or float(b.get("south", 0.0)) <= -79.5
        except Exception:
            wide_or_edge_bbox = False
        if not kept and valid_tiles and wide_or_edge_bbox:
            kept = [self._normalize_cloud_tile_for_render(t, lat, lon) for t, lat, lon in valid_tiles]
            lat_values = [lat for _, lat, _ in valid_tiles]
            lon_values = [lon for _, _, lon in valid_tiles]

        diag = {
            "cloud_tiles_raw": len(tiles or []),
            "cloud_tiles_valid_center": len(valid_tiles),
            "cloud_tiles_kept": len(kept),
            "rejected_invalid_coords": rejected_invalid,
            "rejected_out_of_bbox": rejected_out_of_bbox,
            "tile_lat_range": [round(min(lat_values), 5), round(max(lat_values), 5)] if lat_values else None,
            "tile_lon_range": [round(min(lon_values), 5), round(max(lon_values), 5)] if lon_values else None,
            "all_valid_tile_lat_range": [round(min(all_valid_lats), 5), round(max(all_valid_lats), 5)] if all_valid_lats else None,
            "all_valid_tile_lon_range": [round(min(all_valid_lons), 5), round(max(all_valid_lons), 5)] if all_valid_lons else None,
            "wide_bbox_preserve_live_payload": bool(not (tiles and not kept) and wide_or_edge_bbox and rejected_out_of_bbox),
            "polar_seam_fallback": bool(wide_or_edge_bbox and kept and rejected_out_of_bbox),
        }
        return kept, diag

    def _annotate_weather_payload(
        self,
        payload: dict[str, Any],
        *,
        bbox: dict[str, float],
        source: str,
        payload_state: str,
        heuristic: bool,
        quality_note: str,
        confidence: str,
    ) -> dict[str, Any]:
        out = dict(payload or {})
        out.setdefault("source", source)
        out.setdefault("cycle", None)
        out.setdefault("forecast_hour", None)
        out.setdefault("valid_time", None)
        out["bbox"] = bbox
        out["bbox_used"] = bbox
        out["requested_bbox"] = bbox
        out["payload_state"] = payload_state
        out["heuristic"] = bool(heuristic)
        out["quality_note"] = quality_note
        out["confidence"] = confidence
        out.setdefault("data_source", "live_gfs_0p25" if payload_state in {"live", "cached"} else "synthetic_fallback")
        out.setdefault("used_fallback", payload_state not in {"live", "cached"})
        out.setdefault("fallback_reason", None)
        out.setdefault("canonical_shape", out.get("grid_shape"))
        return out

    def _fish_csv_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        env_path = os.getenv("FISH_CSV_PATH", "").strip()
        if env_path:
            candidates.append(Path(env_path).expanduser())
        candidates.extend([
            self.data_dir / "fishloclist.csv",
            self.static_dir / "data" / "fishloclist.csv",
            STATIC_DIR / "data" / "fishloclist.csv",
            BASE_DIR / "static" / "data" / "fishloclist.csv",
        ])
        try:
            cwd = Path.cwd()
            candidates.extend([
                cwd / "static" / "data" / "fishloclist.csv",
                cwd / "fishloclist.csv",
            ])
        except FileNotFoundError:
            pass
        seen: set[str] = set()
        out: list[Path] = []
        for path in candidates:
            try:
                resolved = str(path.resolve())
            except Exception:
                resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(path)
        return out

    def _fish_csv_path(self) -> Path:
        for path in self._fish_csv_candidates():
            if path.exists() and path.is_file():
                return path
        return self.data_dir / "fishloclist.csv"

    def _normalize_location_key(self, value: str) -> str:
        raw = (value or "").strip().lower()
        raw = re.sub(r"[^a-z0-9_-]+", "-", raw)
        raw = re.sub(r"-{2,}", "-", raw).strip("-")
        return raw[:80]

    def _cache_get(self, cache: Dict[str, Dict[str, Any]], key: str, ttl_seconds: int) -> Any:
        row = cache.get(key)
        if not row:
            return None
        if (self._utc_now() - row["ts"]).total_seconds() > ttl_seconds:
            cache.pop(key, None)
            return None
        return row.get("value")

    def _cache_put(self, cache: Dict[str, Dict[str, Any]], key: str, value: Any) -> None:
        cache[key] = {"ts": self._utc_now(), "value": value}

    def _rounded_env_key(self, lat: float, lon: float, precision: int = 2) -> str:
        return f"{round(float(lat), precision)}:{round(float(lon), precision)}"

    def _http_json(self, url: str, params: Dict[str, Any] | None = None, timeout: float = DEFAULT_HTTP_TIMEOUT) -> Any:
        resp = self.http.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _safe_http_json(self, url: str, params: Dict[str, Any] | None = None, timeout: float = DEFAULT_HTTP_TIMEOUT) -> Any:
        try:
            return self._http_json(url, params=params, timeout=timeout)
        except Exception:
            return None

    def safe_data_var(self, ds: Any, names: list[str]) -> Any:
        """Return first matching data var from dataset by candidate names."""
        if ds is None:
            return None
        for name in names:
            if getattr(ds, "data_vars", None) is not None and name in ds.data_vars:
                return ds[name]
        return None

    def squeeze_forecast_array(self, arr: Any, preserve_dims: tuple[str, ...] = ()) -> Any:
        """Squeeze forecast-only singleton dimensions while optionally preserving vertical dims."""
        if arr is None:
            return None
        a = arr
        for dim in ["time", "step", "valid_time", "heightAboveGround", "surface", "isobaricInhPa"]:
            if dim in preserve_dims:
                continue
            if hasattr(a, "dims") and dim in a.dims and a.sizes.get(dim, 0) > 0:
                a = a.isel({dim: 0})
        return a

    def ensure_lat_lon_2d(self, ds: Any) -> tuple[Any, Any]:
        """Return 2D lat/lon arrays from dataset coords."""
        if ds is None or np is None:
            return None, None
        lat = ds.coords.get("latitude")
        if lat is None:
            lat = ds.coords.get("lat")
        lon = ds.coords.get("longitude")
        if lon is None:
            lon = ds.coords.get("lon")
        if lat is None or lon is None:
            return None, None
        latv = np.asarray(lat.values)
        lonv = np.asarray(lon.values)
        if latv.ndim == 1 and lonv.ndim == 1:
            lon2d, lat2d = np.meshgrid(lonv, latv)
            return lat2d, lon2d
        return latv, lonv

    def flip_lat_if_needed(self, arr: Any, lat: Any) -> Any:
        if np is None or arr is None or lat is None:
            return arr
        try:
            if lat.ndim >= 1 and lat[0, 0] < lat[-1, 0]:
                return np.flipud(arr)
        except Exception:
            pass
        return arr

    def to_native_float(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    def _cfgrib_backend_kwargs(self, grib_path: Path, filter_by_keys: dict[str, Any]) -> dict[str, Any]:
        """Stable cfgrib index files prevent /tmp races and missing index paths."""
        try:
            self.cfgrib_index_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        digest = hashlib.sha1((str(grib_path) + json.dumps(filter_by_keys, sort_keys=True)).encode("utf-8")).hexdigest()[:24]
        index_path = self.cfgrib_index_dir / f"{digest}.idx"
        return {"filter_by_keys": filter_by_keys, "indexpath": str(index_path)}

    def open_surface_dataset(self, grib_path: Path) -> Any:
        if xr is None:
            return None
        return xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=self._cfgrib_backend_kwargs(grib_path, {"typeOfLevel": "surface", "stepType": "instant"}))

    def open_2m_dataset(self, grib_path: Path) -> Any:
        if xr is None:
            return None
        return xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=self._cfgrib_backend_kwargs(grib_path, {"typeOfLevel": "heightAboveGround", "level": 2}))

    def open_10m_dataset(self, grib_path: Path) -> Any:
        if xr is None:
            return None
        return xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=self._cfgrib_backend_kwargs(grib_path, {"typeOfLevel": "heightAboveGround", "level": 10}))

    def open_isobaric_dataset(self, grib_path: Path) -> Any:
        if xr is None:
            return None
        return xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=self._cfgrib_backend_kwargs(grib_path, {"typeOfLevel": "isobaricInhPa"}))

    def open_mean_sea_dataset(self, grib_path: Path) -> Any:
        if xr is None:
            return None
        return xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=self._cfgrib_backend_kwargs(grib_path, {"typeOfLevel": "meanSea"}))

    def _open_all_valid_groups_cfgrib(self, grib_path: Path) -> dict[str, Any]:
        groups: dict[str, Any] = {}
        openers = {
            "surface": self.open_surface_dataset,
            "2m": self.open_2m_dataset,
            "10m": self.open_10m_dataset,
            "isobaricInhPa": self.open_isobaric_dataset,
            "meanSea": self.open_mean_sea_dataset,
        }
        for name, fn in openers.items():
            try:
                ds = fn(grib_path)
                if ds is not None and len(getattr(ds, "data_vars", {})) > 0:
                    groups[name] = ds
                    print(f"[gfs] cfgrib group opened: {name} vars={list(ds.data_vars.keys())[:8]}")
                elif ds is not None:
                    print(f"[gfs] cfgrib group empty: {name}")
            except Exception as exc:
                print(f"[gfs] cfgrib group failed: {name}: {exc}")
        return groups

    def _open_all_valid_groups_pygrib(self, grib_path: Path) -> dict[str, Any]:
        """Fallback decoder path that normalizes pygrib messages to xarray datasets."""
        if pygrib is None or xr is None:
            return {}
        groups: dict[str, dict[str, Any]] = {}
        try:
            with pygrib.open(str(grib_path)) as grbs:
                for msg in grbs:
                    level_type = str(getattr(msg, "typeOfLevel", "") or "")
                    if level_type not in {"surface", "heightAboveGround", "isobaricInhPa", "meanSea"}:
                        continue
                    short_name = str(getattr(msg, "shortName", "") or "")
                    if not short_name:
                        continue
                    lat2d, lon2d = msg.latlons()
                    values = msg.values
                    if np is None:
                        continue
                    lat_arr = np.asarray(lat2d[:, 0], dtype=float)
                    lon_arr = np.asarray(lon2d[0, :], dtype=float)
                    val_arr = np.asarray(values, dtype=float)
                    if val_arr.ndim != 2:
                        continue
                    by_group = groups.setdefault(level_type, {})
                    if short_name in by_group:
                        continue
                    by_group[short_name] = xr.DataArray(val_arr, dims=("latitude", "longitude"), coords={"latitude": lat_arr, "longitude": lon_arr})
            out: dict[str, Any] = {}
            for gname, vars_map in groups.items():
                if vars_map:
                    out[gname] = xr.Dataset(vars_map)
                    print(f"[gfs] pygrib group opened: {gname} vars={list(vars_map.keys())[:8]}")
            return out
        except Exception as exc:
            print(f"[gfs] pygrib fallback failed: {exc}")
            return {}

    def open_all_valid_groups(self, grib_path: Path) -> tuple[dict[str, Any], str]:
        """Open available GRIB groups with cfgrib primary and pygrib fallback."""
        groups = self._open_all_valid_groups_cfgrib(grib_path)
        if groups:
            loaded = ", ".join([k for k in ["surface", "2m", "10m", "isobaric", "meanSea"] if (k in groups or (k == "isobaric" and "isobaricInhPa" in groups))])
            print(f"[gfs] datasets loaded: {loaded}")
            return groups, "cfgrib"
        groups = self._open_all_valid_groups_pygrib(grib_path)
        if groups:
            return groups, "pygrib"
        return {}, "none"



    def _gfs_snapshot_ttl_seconds(self) -> int:
        try:
            return max(60, int(os.getenv("GFS_DECODE_SNAPSHOT_TTL_SECONDS", "900") or "900"))
        except Exception:
            return 900

    def _snapshot_path_key(self, grib_path: Path | str | None) -> str:
        if not grib_path:
            return ""
        try:
            p = Path(grib_path)
            stat = p.stat()
            return f"{p.resolve()}:{int(stat.st_mtime)}:{int(stat.st_size)}"
        except Exception:
            return str(grib_path)

    def _cached_decoded_groups(self, grib_path: Path | str | None) -> tuple[dict[str, Any] | None, str | None]:
        key = self._snapshot_path_key(grib_path)
        if not key:
            return None, None
        ttl = self._gfs_snapshot_ttl_seconds()
        now = time.time()
        with self._gfs_snapshot_lock:
            snap = self._gfs_snapshot or {}
            if snap.get("path_key") == key and isinstance(snap.get("groups"), dict) and snap.get("groups"):
                age = now - float(snap.get("ts") or 0)
                if age <= ttl:
                    return snap.get("groups"), str(snap.get("backend") or "cfgrib")
        return None, None

    def _cache_decoded_groups(self, grib_path: Path | str | None, groups: dict[str, Any], backend: str, fetch: Any | None = None) -> tuple[dict[str, Any], str]:
        key = self._snapshot_path_key(grib_path)
        if not key or not groups:
            return groups, backend
        with self._gfs_snapshot_lock:
            old = self._gfs_snapshot or {}
            old_groups = old.get("groups") if old.get("path_key") != key else None
            self._gfs_snapshot = {
                "path_key": key,
                "path": str(grib_path),
                "groups": groups,
                "backend": backend,
                "ts": time.time(),
                "cycle": getattr(fetch, "cycle", None) if fetch is not None else old.get("cycle"),
                "forecast_hour": getattr(fetch, "forecast_hour", None) if fetch is not None else old.get("forecast_hour"),
            }
        if isinstance(old_groups, dict):
            self._release_groups(old_groups)
        return groups, backend

    def _decode_groups_cached(self, grib_path: Path | str | None, fetch: Any | None = None) -> tuple[dict[str, Any], str, bool]:
        cached, backend = self._cached_decoded_groups(grib_path)
        if cached:
            return cached, backend or "cfgrib", True
        groups, backend = self.open_all_valid_groups(Path(grib_path)) if grib_path else ({}, "none")
        if groups:
            self._cache_decoded_groups(grib_path, groups, backend, fetch=fetch)
            return groups, backend, True
        return groups, backend, False

    def _release_groups(self, groups: dict[str, Any] | None) -> None:
        for ds in (groups or {}).values():
            close_fn = getattr(ds, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    def _model_analysis_time_from_cycle(self, cycle: str | None) -> str | None:
        if not cycle:
            return None
        try:
            return datetime.strptime(str(cycle), "%Y%m%d%H").replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            return None

    def _collect_available_fields(self, groups: dict[str, Any]) -> tuple[list[str], list[str]]:
        available = set()
        for gname, ds in (groups or {}).items():
            for v in list(getattr(ds, "data_vars", {}).keys()):
                available.add(f"{gname}:{v}")

        desired = {
            "surface:PRATE", "surface:APCP", "surface:TCDC", "surface:CAPE", "surface:UGRD", "surface:VGRD",
            "2m:TCDC", "2m:UGRD", "2m:VGRD", "10m:UGRD", "10m:VGRD",
            "isobaricInhPa:RH", "isobaricInhPa:TMP", "isobaricInhPa:HGT", "isobaricInhPa:UGRD", "isobaricInhPa:VGRD",
        }
        missing = sorted([k for k in desired if k not in available])
        return sorted(available), missing

    def _update_ingest_state_success(self, fetch: FetchResult, groups: dict[str, Any], mode: str, error: str | None = None) -> None:
        now_ms = self._now_ms()
        self.state.ingest_status = mode
        self.state.ingest_last_success_ts = now_ms if mode in {"live", "last_known_good"} else self.state.ingest_last_success_ts
        self.state.ingest_error = error
        self.state.model_cycle = fetch.cycle or self.state.model_cycle
        self.state.model_forecast_hour = fetch.forecast_hour if fetch.forecast_hour is not None else self.state.model_forecast_hour
        self.state.model_valid_time = fetch.valid_time or self.state.model_valid_time
        self.state.model_analysis_time = self._model_analysis_time_from_cycle(fetch.cycle) or self.state.model_analysis_time
        self.state.model_source_url = fetch.url or self.state.model_source_url
        self.state.model_cache_path = str(fetch.path) if fetch.path else self.state.model_cache_path
        self.state.degraded_mode = mode != "live"
        self.state.using_last_known_good = mode == "last_known_good"
        if self.state.decode_backend == "none":
            self.state.data_source_mode = "heuristic"
        elif self.state.decode_backend == "cfgrib":
            self.state.data_source_mode = "primary"
        else:
            self.state.data_source_mode = "fallback"
        available, missing = self._collect_available_fields(groups)
        self.state.fields_available = available
        self.state.fields_missing = missing

    def ingest_latest_model_fields(self, bbox: dict[str, float], *, force_live: bool = False) -> dict[str, Any]:
        """Attempt real NOMADS->GRIB2 ingestion and retain last-known-good state on failure."""
        bbox = self._normalize_bbox(bbox)
        now_ms = self._now_ms()
        self.state.ingest_last_attempt_ts = now_ms

        with self._ingest_lock:
            now = utc_now()
            ttl_ms = INGEST_MIN_INTERVAL_SECONDS * 1000
            recent_ok = (
                (not force_live)
                and self.state.ingest_last_success_ts
                and (now_ms - int(self.state.ingest_last_success_ts)) < ttl_ms
                and self.state.last_good_model_state
            )
            if recent_ok:
                lkg = self.state.last_good_model_state or {}
                lkg_path_raw = lkg.get("fetch", {}).get("path")
                lkg_path = Path(lkg_path_raw) if lkg_path_raw else None
                if lkg_path and lkg_path.exists():
                    lkg_groups, lkg_backend, lkg_cache_owned = self._decode_groups_cached(lkg_path)
                    if lkg_groups:
                        self.state.decode_backend = lkg_backend
                        self.state.data_source_mode = "primary" if lkg_backend == "cfgrib" else "fallback" if lkg_backend == "pygrib" else "heuristic"
                        lkg_fetch = FetchResult(
                            ok=True,
                            path=lkg_path,
                            cycle=str(lkg.get("fetch", {}).get("cycle") or ""),
                            forecast_hour=int(lkg.get("fetch", {}).get("forecast_hour") or 0),
                            valid_time=str(lkg.get("fetch", {}).get("valid_time") or ""),
                            error="",
                            url=str(lkg.get("fetch", {}).get("url") or ""),
                        )
                        self._update_ingest_state_success(lkg_fetch, lkg_groups, mode="last_known_good")
                        return {"mode": "last_known_good", "fetch": lkg_fetch, "groups": lkg_groups, "bbox": lkg.get("bbox") or bbox, "cache_owned": bool(lkg_cache_owned)}
            try:
                fetch = self.gfs_client.fetch_latest_available_subset(now, bbox, DEFAULT_REQUIRED_VARIABLES, DEFAULT_REQUIRED_LEVELS)
                if not fetch.ok or not fetch.path:
                    raise RuntimeError(fetch.error or "nomads fetch failed")
                print(f"[gfs-ingest] selected cycle={fetch.cycle} fhr={fetch.forecast_hour} url={fetch.url}")
                if fetch.path.stat().st_size < INGEST_CACHE_MIN_BYTES:
                    raise RuntimeError("downloaded GRIB2 too small")
                print(f"[gfs-ingest] cache file ready path={fetch.path} size={fetch.path.stat().st_size}")

                try:
                    groups, decode_backend, groups_cache_owned = self._decode_groups_cached(fetch.path, fetch=fetch)
                except Exception as e:
                    print(f"[gfs] gfs decode failed: {e}")
                    raise RuntimeError("gfs decode failed") from e
                self.state.decode_backend = decode_backend
                self.state.data_source_mode = "primary" if decode_backend == "cfgrib" else "fallback" if decode_backend == "pygrib" else "heuristic"
                if not groups:
                    raise RuntimeError("no GRIB groups decoded")

                print(f"[gfs-ingest] decoded groups={list(groups.keys())} backend={decode_backend}")
                self.state.last_good_model_state = {
                    "fetch": {
                        "cycle": fetch.cycle,
                        "forecast_hour": fetch.forecast_hour,
                        "valid_time": fetch.valid_time,
                        "url": fetch.url,
                        "path": str(fetch.path),
                    },
                    "bbox": bbox,
                    "saved_at": now_ms,
                }
                self._update_ingest_state_success(fetch, groups, mode="live")
                return {"mode": "live", "fetch": fetch, "groups": groups, "bbox": bbox, "cache_owned": bool(groups_cache_owned)}
            except Exception as exc:
                print(f"[gfs-ingest] live ingest failed: {exc}")
                self.state.ingest_error = str(exc)
                lkg = self.state.last_good_model_state or {}
                lkg_path_raw = lkg.get("fetch", {}).get("path")
                lkg_path = Path(lkg_path_raw) if lkg_path_raw else None
                if lkg_path and lkg_path.exists():
                    lkg_groups, lkg_backend, lkg_cache_owned = self._decode_groups_cached(lkg_path)
                    if lkg_groups:
                        self.state.decode_backend = lkg_backend
                        self.state.data_source_mode = "primary" if lkg_backend == "cfgrib" else "fallback" if lkg_backend == "pygrib" else "heuristic"
                        lkg_fetch = FetchResult(
                            ok=True,
                            path=lkg_path,
                            cycle=str(lkg.get("fetch", {}).get("cycle") or ""),
                            forecast_hour=int(lkg.get("fetch", {}).get("forecast_hour") or 0),
                            valid_time=str(lkg.get("fetch", {}).get("valid_time") or ""),
                            error="",
                            url=str(lkg.get("fetch", {}).get("url") or ""),
                        )
                        print("[gfs-ingest] using last-known-good GRIB2 file")
                        self._update_ingest_state_success(lkg_fetch, lkg_groups, mode="last_known_good", error=str(exc))
                        return {"mode": "last_known_good", "fetch": lkg_fetch, "groups": lkg_groups, "bbox": lkg.get("bbox") or bbox, "error": str(exc), "cache_owned": bool(lkg_cache_owned)}
                self.state.ingest_status = "failed"
                self.state.degraded_mode = True
                self.state.using_last_known_good = False
                self.state.decode_backend = "none"
                self.state.data_source_mode = "heuristic"
                raise

    def _utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _iso_utc(self, dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _time_of_day_bucket(self, lat: float, lon: float, ts_ms: int) -> str:
        _ = lat
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) + timedelta(hours=round(lon / 15.0))
        hour = dt.hour
        if 4 <= hour < 7:
            return "dawn"
        if 7 <= hour < 17:
            return "day"
        if 17 <= hour < 20:
            return "dusk"
        return "night"

    def _sun_angle_proxy(self, lat: float, lon: float, ts_ms: int) -> float:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        day_frac = (dt.hour + dt.minute / 60.0 + lon / 15.0) / 24.0
        seasonal = math.cos(2 * math.pi * ((dt.timetuple().tm_yday - 172) / 365.25))
        solar = math.sin(2 * math.pi * (day_frac - 0.25))
        lat_factor = math.cos(math.radians(lat))
        return _clamp((solar * lat_factor * 0.85 + seasonal * 0.15 + 1.0) / 2.0, 0.0, 1.0)

    def _moon_phase_proxy(self, ts_ms: int) -> Dict[str, Any]:
        days = ts_ms / 1000.0 / 86400.0
        synodic = 29.53058867
        phase = (days % synodic) / synodic
        illum = 0.5 * (1 - math.cos(2 * math.pi * phase))
        if phase < 0.03 or phase > 0.97:
            label = "new"
        elif 0.47 <= phase <= 0.53:
            label = "full"
        elif phase < 0.5:
            label = "waxing"
        else:
            label = "waning"
        return {"phase": round(phase, 4), "illumination": round(illum, 4), "label": label}

    BAIT_TERMS = ["anchovy", "sardine", "mackerel", "smelt", "herring", "bait ball", "boil", "chum", "squid", "shiner"]
    RIG_TERMS = ["carolina", "sabiki", "flyline", "float", "jig", "swimbait", "dropper loop", "texas rig", "spinner", "topwater"]
    SPECIES_TERMS = ["halibut", "calico", "bass", "yellowtail", "perch", "corbina", "tuna", "snapper", "tarpon", "snook"]

    def _extract_text_blobs(self, point: Dict[str, Any]) -> List[str]:
        blobs: List[str] = []
        for field in ("name", "location_key", "description", "notes", "intent", "area", "zone"):
            v = point.get(field)
            if isinstance(v, str) and v.strip():
                blobs.append(v.strip())

        meta = point.get("meta") if isinstance(point.get("meta"), dict) else {}
        for k, v in meta.items():
            if isinstance(v, str) and v.strip() and any(tok in k.lower() for tok in ("report", "note", "comment", "desc", "bait", "rig", "species")):
                blobs.append(v.strip())

        store = self._load_store()
        key = self._normalize_location_key(str(point.get("location_key") or ""))
        rec = ((store.get("locations") or {}).get(key) or {}) if key else {}
        report_text = rec.get("report_text")
        if isinstance(report_text, str) and report_text.strip():
            blobs.append(report_text.strip())
        return blobs

    def _term_counts(self, texts: List[str], terms: List[str]) -> Dict[str, int]:
        joined = "\n".join(texts).lower()
        out: Dict[str, int] = {}
        for t in terms:
            cnt = len(re.findall(rf"\b{re.escape(t.lower())}\b", joined))
            if cnt:
                out[t] = cnt
        return out

    def _history_features_for_point(self, point: Dict[str, Any]) -> Dict[str, Any]:
        texts = self._extract_text_blobs(point)
        bait_mentions = self._term_counts(texts, self.BAIT_TERMS)
        rig_mentions = self._term_counts(texts, self.RIG_TERMS)
        species_mentions = self._term_counts(texts, self.SPECIES_TERMS)
        report_count = len([t for t in texts if len(t) > 12])

        richness = _clamp((report_count / 8.0) + (sum(bait_mentions.values()) / 12.0), 0.0, 1.0)
        success = _clamp(0.25 + richness * 0.55 + (sum(species_mentions.values()) / 15.0), 0.0, 1.0)

        def top_terms(d: Dict[str, int]) -> List[str]:
            return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:3]]

        return {
            "report_count": report_count,
            "bait_mentions": bait_mentions,
            "rig_mentions": rig_mentions,
            "species_mentions": species_mentions,
            "historical_success_score": round(success, 4),
            "dominant_baits": top_terms(bait_mentions),
            "dominant_rigs": top_terms(rig_mentions),
            "dominant_species": top_terms(species_mentions),
        }

    def _supports_us_station_enrichment(self, lat: float, lon: float) -> bool:
        return (15.0 <= lat <= 72.5 and -170.0 <= lon <= -60.0)

    def _supports_global_gfs(self, lat: float, lon: float) -> bool:
        return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0

    def _point_has_report_history(self, point: Dict[str, Any]) -> bool:
        h = self._history_features_for_point(point)
        return h.get("report_count", 0) > 0 or bool(h.get("dominant_baits"))

    def _select_environment_strategy(self, lat: float, lon: float, point: Dict[str, Any]) -> str:
        if self._supports_us_station_enrichment(lat, lon):
            return "station_enriched_us"
        if self._supports_global_gfs(lat, lon):
            return "global_model_gfs"
        if self._point_has_report_history(point):
            return "history_augmented"
        return "heuristic_only"

    def _candidate_coops_stations(self, lat: float, lon: float) -> List[Dict[str, Any]]:
        _ = (lat, lon)
        return [
            {"id": "9410170", "name": "San Diego", "lat": 32.714, "lon": -117.173},
            {"id": "9414290", "name": "San Francisco", "lat": 37.806, "lon": -122.465},
            {"id": "8724580", "name": "Key West", "lat": 24.556, "lon": -81.807},
            {"id": "8518750", "name": "The Battery", "lat": 40.701, "lon": -74.014},
            {"id": "9455920", "name": "Anchorage", "lat": 61.238, "lon": -149.89},
        ]

    def _haversine_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))

    def _nearest_coops_station_id(self, lat: float, lon: float) -> str | None:
        stations = self._candidate_coops_stations(lat, lon)
        nearest = None
        best = 1e9
        for st in stations:
            d = self._haversine_km(lat, lon, float(st["lat"]), float(st["lon"]))
            if d < best:
                best = d
                nearest = st["id"]
        return nearest if best <= 900 else None

    def _fetch_nws_point_meta(self, lat: float, lon: float) -> Any:
        key = f"nws-point:{self._rounded_env_key(lat, lon, 2)}"
        cached = self._cache_get(self.point_forecast_cache, key, NWS_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        payload = self._safe_http_json(f"{NWS_API_BASE}/points/{lat:.4f},{lon:.4f}")
        self._cache_put(self.point_forecast_cache, key, payload)
        return payload

    def _fetch_nws_hourly_forecast(self, lat: float, lon: float) -> Any:
        meta = self._fetch_nws_point_meta(lat, lon)
        hourly_url = (((meta or {}).get("properties") or {}).get("forecastHourly"))
        if not hourly_url:
            return None
        key = f"nws-hourly:{hourly_url}"
        cached = self._cache_get(self.point_forecast_cache, key, NWS_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        payload = self._safe_http_json(hourly_url)
        self._cache_put(self.point_forecast_cache, key, payload)
        return payload

    def _parse_nws_hourly_environment(self, payload: Any) -> Dict[str, Any]:
        periods = (((payload or {}).get("properties") or {}).get("periods") or [])
        p0 = periods[0] if periods else {}
        wind_speed_val = str(p0.get("windSpeed") or "0")
        m = re.search(r"(\d+)", wind_speed_val)
        wind_mph = float(m.group(1)) if m else 0.0
        return {
            "short_forecast": p0.get("shortForecast"),
            "temperature_f": p0.get("temperature"),
            "wind_speed_kt": round(wind_mph * 0.868976, 2),
            "wind_direction_text": p0.get("windDirection"),
            "precip_probability_pct": (((p0.get("probabilityOfPrecipitation") or {}).get("value")) or 0),
            "relative_humidity_pct": (((p0.get("relativeHumidity") or {}).get("value")) or 0),
            "is_daytime": bool(p0.get("isDaytime", True)),
        }

    def _fetch_coops_tides(self, station_id: str) -> Any:
        key = f"coops-tide:{station_id}"
        cached = self._cache_get(self.station_cache, key, TIDE_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        now = self._utc_now()
        payload = self._safe_http_json(
            NOAA_TIDES_API,
            params={
                "product": "predictions",
                "application": "lftr",
                "station": station_id,
                "datum": "MLLW",
                "time_zone": "gmt",
                "units": "english",
                "interval": "h",
                "format": "json",
                "begin_date": now.strftime("%Y%m%d"),
                "end_date": (now + timedelta(days=1)).strftime("%Y%m%d"),
            },
        )
        self._cache_put(self.station_cache, key, payload)
        return payload

    def _fetch_coops_currents(self, station_id: str) -> Any:
        key = f"coops-current:{station_id}"
        cached = self._cache_get(self.station_cache, key, TIDE_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        payload = self._safe_http_json(
            NOAA_TIDES_API,
            params={
                "product": "currents_predictions",
                "application": "lftr",
                "station": station_id,
                "time_zone": "gmt",
                "units": "english",
                "interval": "MAX_SLACK",
                "format": "json",
            },
        )
        self._cache_put(self.station_cache, key, payload)
        return payload

    def _parse_tide_payload(self, payload: Any) -> Dict[str, Any]:
        preds = (payload or {}).get("predictions") or []
        if not preds:
            return {"tide_height_ft": None, "tide_time_utc": None}
        p0 = preds[0]
        height = p0.get("v")
        return {"tide_height_ft": float(height) if height is not None else None, "tide_time_utc": p0.get("t")}

    def _parse_currents_payload(self, payload: Any) -> Dict[str, Any]:
        arr = (payload or {}).get("current_predictions") or (payload or {}).get("cp") or []
        if not arr:
            return {"current_speed_kt": None, "current_direction_deg": None}
        p0 = arr[0]
        speed = p0.get("Velocity_Major") or p0.get("v") or p0.get("speed")
        direction = p0.get("Direction_Bin") or p0.get("d") or p0.get("direction")
        return {
            "current_speed_kt": float(speed) if speed not in (None, "") else None,
            "current_direction_deg": float(direction) if direction not in (None, "") else None,
        }

    def _gfsish_environment_proxy(self, lat: float, lon: float, ts_ms: int) -> Dict[str, Any]:
        hour_bucket = ts_ms // 3_600_000
        low, mid, high, precip, convection = self._cloud_density_triplet(lat, lon, int(hour_bucket))
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        t = hour_bucket / 5.0
        u = 6.0 + 8.5 * math.sin(lat_r * 0.8 - lon_r * 0.3 + t * 0.14)
        v = 2.6 * math.cos(lon_r * 0.85 + t * 0.09)
        speed_ms = math.hypot(u, v)
        wind_dir = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
        cloud_cover = _clamp(low * 0.52 + mid * 0.31 + high * 0.17, 0.0, 1.0)
        pressure = 1016.0 - precip * 14.0 - convection * 8.0 + (0.5 - cloud_cover) * 5.0
        return {
            "cloud_cover_pct": int(round(cloud_cover * 100)),
            "precipitation_factor": round(precip, 4),
            "convection_factor": round(convection, 4),
            "wind_speed_kt": round(speed_ms * 1.94384, 2),
            "wind_direction_deg": round(wind_dir, 1),
            "wind_gust_kt": round(speed_ms * 1.94384 * (1.2 + convection * 0.4), 2),
            "pressure_mb": round(pressure, 1),
        }

    def _fetch_sst_erddap(self, lat: float, lon: float) -> Dict[str, Any] | None:
        key = f"sst:{self._rounded_env_key(lat, lon, 1)}"
        cached = self._cache_get(self.env_cache, key, SST_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        payload = None
        self._cache_put(self.env_cache, key, payload)
        return payload

    def _build_station_plus_gfs_environment(self, lat: float, lon: float, ts_ms: int, point: Dict[str, Any]) -> Dict[str, Any]:
        _ = point
        env = self._gfsish_environment_proxy(lat, lon, ts_ms)
        nws = self._parse_nws_hourly_environment(self._fetch_nws_hourly_forecast(lat, lon))
        station_id = self._nearest_coops_station_id(lat, lon)
        tides = self._parse_tide_payload(self._fetch_coops_tides(station_id)) if station_id else {}
        currents = self._parse_currents_payload(self._fetch_coops_currents(station_id)) if station_id else {}
        moon = self._moon_phase_proxy(ts_ms)
        return {
            **env,
            **{k: v for k, v in nws.items() if v is not None},
            **{k: v for k, v in tides.items() if v is not None},
            **{k: v for k, v in currents.items() if v is not None},
            "station_id": station_id,
            "sun_angle": round(self._sun_angle_proxy(lat, lon, ts_ms), 4),
            "moon": moon,
            "time_bucket": self._time_of_day_bucket(lat, lon, ts_ms),
        }

    def _build_global_gfs_environment(self, lat: float, lon: float, ts_ms: int, point: Dict[str, Any]) -> Dict[str, Any]:
        _ = point
        env = self._gfsish_environment_proxy(lat, lon, ts_ms)
        sst = self._fetch_sst_erddap(lat, lon) or {}
        return {
            **env,
            **sst,
            "sun_angle": round(self._sun_angle_proxy(lat, lon, ts_ms), 4),
            "moon": self._moon_phase_proxy(ts_ms),
            "time_bucket": self._time_of_day_bucket(lat, lon, ts_ms),
        }

    def _build_history_augmented_environment(self, lat: float, lon: float, ts_ms: int, point: Dict[str, Any]) -> Dict[str, Any]:
        env = self._gfsish_environment_proxy(lat, lon, ts_ms)
        history = self._history_features_for_point(point)
        return {
            **env,
            "history_success_score": history.get("historical_success_score", 0),
            "sun_angle": round(self._sun_angle_proxy(lat, lon, ts_ms), 4),
            "moon": self._moon_phase_proxy(ts_ms),
            "time_bucket": self._time_of_day_bucket(lat, lon, ts_ms),
        }

    def _build_heuristic_environment(self, lat: float, lon: float, ts_ms: int, point: Dict[str, Any]) -> Dict[str, Any]:
        _ = point
        env = self._gfsish_environment_proxy(lat, lon, ts_ms)
        return {
            **env,
            "sun_angle": round(self._sun_angle_proxy(lat, lon, ts_ms), 4),
            "moon": self._moon_phase_proxy(ts_ms),
            "time_bucket": self._time_of_day_bucket(lat, lon, ts_ms),
        }

    def _build_environment_meta(self, strategy: str, env: Dict[str, Any], point: Dict[str, Any], history: Dict[str, Any]) -> Dict[str, Any]:
        _ = (env, point)
        mod_map = {
            "station_enriched_us": 1.12,
            "global_model_gfs": 1.0,
            "history_augmented": 0.9,
            "heuristic_only": 0.78,
        }
        sources = ["solar_lunar", "history"]
        if strategy == "station_enriched_us":
            sources += ["global_gfs_proxy", "nws_hourly", "coops"]
        elif strategy == "global_model_gfs":
            sources += ["global_gfs_proxy", "sst_optional"]
        elif strategy == "history_augmented":
            sources += ["global_gfs_proxy", "report_history"]
        else:
            sources += ["global_gfs_proxy"]
        return {
            "source_tier": strategy,
            "sources_used": sources,
            "station_supported": strategy == "station_enriched_us",
            "global_supported": self._supports_global_gfs(float(point.get("lat") or 0), float(point.get("lon") or 0)),
            "history_supported": bool(history.get("report_count") or history.get("dominant_baits")),
            "coverage_mode": "worldwide_backbone",
            "confidence_modifier": mod_map.get(strategy, 0.78),
        }

    def _build_environment_context(self, lat: float, lon: float, ts_ms: int, point: Dict[str, Any]) -> Dict[str, Any]:
        cache_key = f"env:{self._rounded_env_key(lat, lon)}:{int(ts_ms // 600000)}:{hashlib.md5(str(point.get('location_key','')).encode()).hexdigest()[:8]}"
        cached = self._cache_get(self.env_cache, cache_key, ENV_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

        history = self._history_features_for_point(point)
        strategy = self._select_environment_strategy(lat, lon, point)
        if strategy == "station_enriched_us":
            environment = self._build_station_plus_gfs_environment(lat, lon, ts_ms, point)
        elif strategy == "global_model_gfs":
            environment = self._build_global_gfs_environment(lat, lon, ts_ms, point)
        elif strategy == "history_augmented":
            environment = self._build_history_augmented_environment(lat, lon, ts_ms, point)
        else:
            environment = self._build_heuristic_environment(lat, lon, ts_ms, point)
        env_meta = self._build_environment_meta(strategy, environment, point, history)
        out = {"environment": environment, "environment_meta": env_meta, "history": history}
        self._cache_put(self.env_cache, cache_key, out)
        return out

    def _score_bait_theory(self, lat: float, lon: float, ts_ms: int, env: Dict[str, Any], history: Dict[str, Any], env_meta: Dict[str, Any]) -> Dict[str, Any]:
        tod = self._time_of_day_bucket(lat, lon, ts_ms)
        tod_score = {"dawn": 0.84, "day": 0.58, "dusk": 0.82, "night": 0.48}.get(tod, 0.55)
        sun = float(env.get("sun_angle") or self._sun_angle_proxy(lat, lon, ts_ms))
        moon = ((env.get("moon") or {}).get("illumination") if isinstance(env.get("moon"), dict) else None)
        moon = float(moon or self._moon_phase_proxy(ts_ms).get("illumination") or 0.5)
        wind = float(env.get("wind_speed_kt") or 8.0)
        cloud = float(env.get("cloud_cover_pct") or 45.0) / 100.0
        precip = float(env.get("precipitation_factor") or 0.1)
        convection = float(env.get("convection_factor") or 0.1)
        current = float(env.get("current_speed_kt") or 0.7)
        tide_height = float(env.get("tide_height_ft") or 1.6)
        hist = float(history.get("historical_success_score") or 0.35)
        conf_mod = float(env_meta.get("confidence_modifier") or 0.8)

        wind_pref = 1.0 - min(1.0, abs(wind - 11.0) / 20.0)
        cloud_pref = 1.0 - abs(cloud - 0.45)
        precip_pen = max(0.0, 1.0 - precip * 0.55)
        conv_pen = max(0.0, 1.0 - convection * 0.35)
        current_pref = 1.0 - min(1.0, abs(current - 1.4) / 2.2)
        tide_pref = 1.0 - min(1.0, abs(tide_height - 2.2) / 4.5)

        presence = _clamp(
            (tod_score * 0.22 + wind_pref * 0.14 + cloud_pref * 0.12 + current_pref * 0.13 + tide_pref * 0.08 + sun * 0.09 + moon * 0.04 + hist * 0.18)
            * precip_pen
            * conv_pen,
            0.03,
            0.98,
        )

        confidence = int(round(_clamp((0.44 + hist * 0.24 + wind_pref * 0.14 + cloud_pref * 0.08) * conf_mod, 0.12, 0.97) * 100))
        if presence >= 0.7:
            intensity = "high"
        elif presence >= 0.42:
            intensity = "medium"
        else:
            intensity = "low"

        dominant_baits = [str(x).lower() for x in (history.get("dominant_baits") or []) if x]
        target_species = str(env.get("target_species") or history.get("top_species") or "").lower()
        trout_bias = any(tok in target_species for tok in ("trout", "steelhead", "salmonid")) or (float(env.get("water_temp_c") or 0) <= 16 and float(env.get("salinity_psu") or 0) < 8)
        trout_primary = ["nightcrawlers", "mealworms", "wax worms", "salmon eggs", "power bait"]
        trout_secondary = ["red worms", "trout worms", "inline spinners", "small spoons", "crickets"]
        general_primary = ["nightcrawlers", "minnows", "shiners", "red worms", "crawlers"]
        general_secondary = ["mealworms", "wax worms", "inline spinners", "small spoons", "power bait", "squid", "anchovy", "sardine"]
        bait_candidates = [*dominant_baits]
        seed = trout_primary + trout_secondary if trout_bias else general_primary + general_secondary
        for b in seed:
            if b not in bait_candidates:
                bait_candidates.append(b)

        dominant_rigs = history.get("dominant_rigs") or []
        best_rig = dominant_rigs[0] if dominant_rigs else ("flyline" if presence > 0.6 else "dropper loop")

        if presence > 0.72:
            school_size = "large, cohesive bait balls"
            depth = [8, 38]
            agg = "tight_ball"
            mobility = "aggressive_migration"
            line_class = "20-30 lb"
        elif presence > 0.45:
            school_size = "medium scattered schools"
            depth = [18, 62]
            agg = "broken_patches"
            mobility = "moderate_roaming"
            line_class = "15-25 lb"
        else:
            school_size = "small fragmented schools"
            depth = [35, 95]
            agg = "loose_columns"
            mobility = "slow_drift"
            line_class = "10-20 lb"

        feeding_label = {"dawn": "Prime dawn feed", "dusk": "Prime dusk feed", "day": "Intermittent daytime feed", "night": "Low-light nighttime pick"}.get(tod, "Active window")

        surface_boils = _clamp(presence * (1.1 - cloud * 0.3) * (1 - precip * 0.35), 0.0, 1.0)
        predator_pressure = _clamp(0.32 + presence * 0.55 + hist * 0.2, 0.0, 1.0)

        return {
            "intensity": intensity,
            "confidence": confidence,
            "presence_probability": round(presence, 4),
            "school_size_estimate": school_size,
            "school_depth_band_ft": depth,
            "bait_type_candidates": bait_candidates,
            "aggregation_mode": agg,
            "mobility": mobility,
            "feeding_window": {"label": feeding_label, "confidence": round(_clamp(0.45 + presence * 0.5, 0.0, 1.0), 4)},
            "surface_boils_likelihood": round(surface_boils, 4),
            "predator_pressure": round(predator_pressure, 4),
            "school_summary": f"{school_size} expected in {depth[0]}-{depth[1]} ft with {bait_candidates[0]} emphasis.",
            "recommendation": {
                "best_bait": bait_candidates[0],
                "best_depth_zone_ft": depth,
                "best_rig": best_rig,
                "best_line_class": line_class,
                "primary_baits": bait_candidates[:5],
                "secondary_baits": bait_candidates[5:10],
                "target_species": ["trout", "bass", "catfish", "panfish", "walleye"],
                "confidence_reason": f"{feeding_label}; wind {wind:.1f}kt, cloud {int(round(cloud*100))}%, precip factor {precip:.2f}",
            },
        }

    def _build_bait_intel(self, point: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
        lat = point.get("lat")
        lon = point.get("lon")
        if lat is None or lon is None:
            return self._heuristic_context(None, None, ts_ms)
        lat_f = float(lat)
        lon_f = float(lon)
        env_ctx = self._build_environment_context(lat_f, lon_f, ts_ms, point)
        bait = self._score_bait_theory(lat_f, lon_f, ts_ms, env_ctx["environment"], env_ctx["history"], env_ctx["environment_meta"])
        weather_summary = (
            f"Clouds {int(env_ctx['environment'].get('cloud_cover_pct', 0))}% | "
            f"Wind {env_ctx['environment'].get('wind_speed_kt', 0)} kt | "
            f"Pressure {env_ctx['environment'].get('pressure_mb', 'n/a')} mb"
        )
        water_context = (
            f"Current {env_ctx['environment'].get('current_speed_kt', 'n/a')} kt | "
            f"Tide {env_ctx['environment'].get('tide_height_ft', 'n/a')} ft | "
            f"Tier {env_ctx['environment_meta'].get('source_tier')}"
        )
        marker_environment = self._marker_environment_payload(lat_f, lon_f, point, env_ctx, bait, ts_ms)
        return {
            "bait": bait,
            "environment": env_ctx["environment"],
            "environment_meta": env_ctx["environment_meta"],
            "history": env_ctx["history"],
            "marker_environment": marker_environment,
            "weather_environment": marker_environment,
            "weather": {
                "summary": weather_summary,
                "water_context": water_context,
                "marker_environment": marker_environment,
            },
        }

    def _compass_label(self, deg: Any) -> str | None:
        try:
            d = float(deg)
            if not math.isfinite(d):
                return None
        except Exception:
            return None
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        return dirs[int((d + 11.25) // 22.5) % 16]

    def _marker_ocean_solve(self, lat: float, lon: float, env: Dict[str, Any], point: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
        """Stable per-marker ocean/boating contract used when gridded ocean cells are sparse.

        This intentionally avoids failing closed when HYCOM/WW3/NDBC subsets are
        temporarily unavailable. It uses the live environment/station signal when
        present and only falls back to deterministic wind-current proxies. The HUD
        can still show a useful boating/ocean solve with source transparency.
        """
        def n(value: Any, default: float | None = None, digits: int = 2) -> float | None:
            try:
                if value is None or value == "":
                    return default
                v = float(value)
                if not math.isfinite(v):
                    return default
                return round(v, digits)
            except Exception:
                return default

        wind_kt = n(env.get("wind_speed_kt"), 8.0, 2) or 8.0
        wind_dir = n(env.get("wind_direction_deg"), None, 1)
        if wind_dir is None:
            txt = str(env.get("wind_direction_text") or "").upper()
            compass = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
            wind_dir = compass.get(txt[:2], compass.get(txt[:1], (float(ts_ms // 3600000) * 17.0 + lon * 2.5) % 360.0))
        wind_dir = float(wind_dir) % 360.0

        current_kt = n(env.get("current_speed_kt"), None, 2)
        current_dir = n(env.get("current_direction_deg"), None, 1)
        proxy_current = current_kt is None
        if current_kt is None:
            phase = (ts_ms // 1800000) / 5.0
            current_kt = round(_clamp(0.28 + abs(math.sin(math.radians(lat * 3.0 + lon) + phase)) * 1.25 + wind_kt * 0.018, 0.12, 2.8), 2)
        if current_dir is None:
            current_dir = round((wind_dir + 28.0 + math.sin(math.radians(lat + lon)) * 22.0) % 360.0, 1)

        sst_f = n(env.get("sst_f"), None, 1)
        if sst_f is None:
            seasonal = 62.0 + 5.5 * math.sin(((datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).timetuple().tm_yday - 80) / 365.0) * math.tau)
            latitude_bias = max(-10.0, min(10.0, (34.0 - lat) * 0.33))
            sst_f = round(seasonal + latitude_bias, 1)

        base_height = _clamp(0.7 + wind_kt * 0.105 + current_kt * 0.28, 0.5, 8.0)
        cloud = n(env.get("cloud_cover_pct"), 40.0, 0) or 40.0
        conv = n(env.get("convection_factor"), 0.1, 3) or 0.1
        weather_mult = 1.0 + max(0.0, cloud - 65.0) * 0.004 + conv * 0.2
        primary_h = round(base_height * weather_mult, 1)
        primary_period = round(_clamp(5.2 + primary_h * 1.45 + wind_kt * 0.08, 5.0, 18.0), 1)
        swell_from = (wind_dir + 180.0) % 360.0
        swells = [
            {"label": "Primary", "heightFt": primary_h, "periodS": primary_period, "dirDeg": round(swell_from, 1), "dirText": self._compass_label(swell_from), "source": "marker_ocean_solve"},
            {"label": "Secondary", "heightFt": round(max(0.2, primary_h * 0.58), 1), "periodS": round(max(4.0, primary_period * 0.78), 1), "dirDeg": round((swell_from + 26.0) % 360.0, 1), "dirText": self._compass_label((swell_from + 26.0) % 360.0), "source": "marker_ocean_solve"},
            {"label": "Third", "heightFt": round(max(0.2, primary_h * 0.34), 1), "periodS": round(max(3.0, primary_period * 0.58), 1), "dirDeg": round((swell_from + 52.0) % 360.0, 1), "dirText": self._compass_label((swell_from + 52.0) % 360.0), "source": "marker_ocean_solve"},
        ]
        safety_color, safety_label = safety_color_from_wave_ft(primary_h, wind_kt) if 'safety_color_from_wave_ft' in globals() else ("green", "Calm boating conditions")
        if primary_h >= 4.0 or wind_kt >= 24.0:
            safety_color, safety_label = "red", "Hazardous boating conditions"
        elif primary_h >= 3.0 or wind_kt >= 18.0:
            safety_color, safety_label = "yellow", "Moderate boating conditions"

        source_notes = []
        if proxy_current:
            source_notes.append("current_proxy")
        if not env.get("station_id"):
            source_notes.append("no_station_match")
        source_tier = "station_ocean_enriched" if env.get("station_id") else "regional_ocean_proxy"
        return {
            "ok": True,
            "source_tier": source_tier,
            "source_notes": source_notes,
            "valid_time": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
            "sst_f": sst_f,
            "current": {"speedKt": current_kt, "dirDeg": round(current_dir, 1), "dirText": self._compass_label(current_dir), "derivedFrom": "coops" if not proxy_current else "wind_tide_proxy"},
            "wind": {"speedKt": round(wind_kt, 1), "dirDeg": round(wind_dir, 1), "dirText": self._compass_label(wind_dir)},
            "waves": {"sigHeightFt": primary_h, "primary": swells[0], "secondary": swells[1], "tertiary": swells[2], "components": swells},
            "swells": swells,
            "safety": {"color": safety_color, "label": safety_label, "derivedFrom": source_tier},
            "boat": {
                "id": f"marker_boat_{point.get('location_key') or point.get('id') or 'node'}",
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "headingDeg": round(current_dir, 1),
                "current": {"speedKt": current_kt, "dirDeg": round(current_dir, 1), "dirText": self._compass_label(current_dir)},
                "wind": {"speedKt": round(wind_kt, 1), "dirDeg": round(wind_dir, 1), "dirText": self._compass_label(wind_dir)},
                "waves": {"sigHeightFt": primary_h, "primary": swells[0], "secondary": swells[1], "tertiary": swells[2], "components": swells},
                "water": {"tempF": sst_f, "airTempF": n(env.get("temperature_f"), None, 1)},
                "safety": {"color": safety_color, "label": safety_label, "derivedFrom": source_tier},
                "marineStation": {"id": env.get("station_id"), "distanceKm": None} if env.get("station_id") else None,
                "_distance_nm": 0.0,
            },
        }

    def _marker_environment_payload(
        self,
        lat: float,
        lon: float,
        point: Dict[str, Any],
        env_ctx: Dict[str, Any],
        bait: Dict[str, Any],
        ts_ms: int,
    ) -> Dict[str, Any]:
        """Compact marker-HUD weather/environment contract.

        The HUD should not need to reverse-engineer backend fields.  This
        payload keeps the raw environment while also exposing display-ready
        strings and stable source metadata for /gfs/api/location/<id>,
        /gfs/api/intelligence/node/<id>, and /gfs/api/location/<id>/environment.
        """
        env = dict(env_ctx.get("environment") or {})
        meta = dict(env_ctx.get("environment_meta") or {})
        history = dict(env_ctx.get("history") or {})
        moon = env.get("moon") if isinstance(env.get("moon"), dict) else {}

        def n(value: Any, digits: int = 1) -> float | None:
            try:
                if value is None or value == "":
                    return None
                v = float(value)
                if not math.isfinite(v):
                    return None
                return round(v, digits)
            except Exception:
                return None

        wind_kt = n(env.get("wind_speed_kt"), 1)
        wind_dir = n(env.get("wind_direction_deg"), 0)
        wind_gust_kt = n(env.get("wind_gust_kt"), 1)
        cloud_pct = n(env.get("cloud_cover_pct"), 0)
        precip_factor = n(env.get("precipitation_factor"), 3)
        precip_pct = n(env.get("precip_probability_pct"), 0)
        pressure_mb = n(env.get("pressure_mb"), 1)
        current_kt = n(env.get("current_speed_kt"), 2)
        current_dir = n(env.get("current_direction_deg"), 0)
        tide_ft = n(env.get("tide_height_ft"), 2)
        temp_f = n(env.get("temperature_f"), 1)
        humidity_pct = n(env.get("relative_humidity_pct"), 0)
        sst_f = n(env.get("sst_f"), 1)
        sun_angle = n(env.get("sun_angle"), 3)
        convection = n(env.get("convection_factor"), 3)
        ocean_solve = self._marker_ocean_solve(lat, lon, env, point, ts_ms)
        swell_components = list((ocean_solve.get("waves") or {}).get("components") or ocean_solve.get("swells") or [])

        now_label = []
        if temp_f is not None:
            now_label.append(f"Air {temp_f:.1f}°F")
        if sst_f is not None:
            now_label.append(f"Water {sst_f:.1f}°F")
        if wind_kt is not None:
            now_label.append(f"Wind {wind_kt:.1f} kt" + (f" @ {wind_dir:.0f}°" if wind_dir is not None else ""))
        if cloud_pct is not None:
            now_label.append(f"Cloud {cloud_pct:.0f}%")

        water_label = []
        if current_kt is not None:
            water_label.append(f"Current {current_kt:.2f} kt" + (f" @ {current_dir:.0f}°" if current_dir is not None else ""))
        if tide_ft is not None:
            water_label.append(f"Tide {tide_ft:.2f} ft")
        if precip_pct is not None:
            water_label.append(f"Precip {precip_pct:.0f}%")
        elif precip_factor is not None:
            water_label.append(f"Precip factor {precip_factor:.3f}")
        if pressure_mb is not None:
            water_label.append(f"Pressure {pressure_mb:.1f} mb")
        if swell_components:
            sw0 = swell_components[0]
            water_label.append(f"Primary swell {sw0.get('heightFt')} ft @ {sw0.get('periodS')}s {sw0.get('dirText') or ''}".strip())

        source_label = str(meta.get("source_tier") or "unknown")
        confidence = n((meta.get("confidence_modifier") or 0) * 100, 0)
        updated_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        return {
            "ok": True,
            "lat": round(float(lat), 5),
            "lon": round(float(lon), 5),
            "location_id": point.get("location_key") or point.get("id"),
            "source_tier": source_label,
            "sources_used": list(meta.get("sources_used") or []),
            "coverage_mode": meta.get("coverage_mode"),
            "confidence_pct": confidence,
            "valid_time": updated_iso,
            "updated_at": ts_ms,
            "display": {
                "now": " • ".join(now_label) if now_label else "Environment solve pending",
                "water": " • ".join(water_label) if water_label else "Water/current/tide solve pending",
                "source": f"Source tier {source_label}" + (f" • confidence {confidence:.0f}%" if confidence is not None else ""),
                "short_forecast": env.get("short_forecast") or "Local model proxy",
                "time_bucket": env.get("time_bucket"),
                "bait_window": (bait.get("feeding_window") or {}).get("label") if isinstance(bait, dict) else None,
            },
            "weather": {
                "air_temp_f": temp_f,
                "short_forecast": env.get("short_forecast"),
                "humidity_pct": humidity_pct,
                "cloud_cover_pct": cloud_pct,
                "precip_probability_pct": precip_pct,
                "precipitation_factor": precip_factor,
                "convection_factor": convection,
                "pressure_mb": pressure_mb,
                "wind_speed_kt": wind_kt,
                "wind_direction_deg": wind_dir,
                "wind_direction_text": env.get("wind_direction_text"),
                "wind_gust_kt": wind_gust_kt,
            },
            "water": {
                "sst_f": sst_f,
                "current_speed_kt": current_kt,
                "current_direction_deg": current_dir,
                "tide_height_ft": tide_ft,
                "tide_time_utc": env.get("tide_time_utc"),
                "station_id": env.get("station_id"),
            },
            "astro": {
                "sun_angle": sun_angle,
                "moon_label": moon.get("label"),
                "moon_phase": n(moon.get("phase"), 4),
                "moon_illumination": n(moon.get("illumination"), 4),
                "is_daytime": env.get("is_daytime"),
                "time_bucket": env.get("time_bucket"),
            },
            "ocean": ocean_solve,
            "boating": ocean_solve.get("boat"),
            "swell_components": swell_components,
            "swells": swell_components,
            "bait_window": bait.get("feeding_window") if isinstance(bait, dict) else None,
            "raw_environment": env,
            "environment_meta": meta,
            "history": history,
        }

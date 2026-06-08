from __future__ import annotations
import os

import server.gfs_service as _svc
globals().update({k: v for k, v in vars(_svc).items() if not k.startswith("__")})


class TilesSceneMixin:
    def _tile_bounds_xyz(self, z: int, x: int, y: int) -> dict[str, float]:
        z = max(0, int(z))
        n = 2 ** z
        x = max(0, min(n - 1, int(x)))
        y = max(0, min(n - 1, int(y)))

        def tile2lon(tx: int, tz: int) -> float:
            return tx / (2 ** tz) * 360.0 - 180.0

        def tile2lat(ty: int, tz: int) -> float:
            val = math.pi * (1 - 2 * ty / (2 ** tz))
            return math.degrees(math.atan(math.sinh(val)))

        west = tile2lon(x, z)
        east = tile2lon(x + 1, z)
        north = tile2lat(y, z)
        south = tile2lat(y + 1, z)
        return {"west": west, "south": south, "east": east, "north": north}

    def _expand_bounds(self, bounds: dict[str, float], pad_deg: float = 0.18) -> dict[str, float]:
        return {
            "west": float(bounds.get("west", -180.0)) - pad_deg,
            "south": float(bounds.get("south", -80.0)) - pad_deg,
            "east": float(bounds.get("east", 180.0)) + pad_deg,
            "north": float(bounds.get("north", 80.0)) + pad_deg,
        }

    def _point_in_bounds(self, lat: float, lon: float, bounds: dict[str, float]) -> bool:
        return (
            float(bounds.get("south", -90.0)) <= float(lat) <= float(bounds.get("north", 90.0)
            ) and float(bounds.get("west", -180.0)) <= float(lon) <= float(bounds.get("east", 180.0))
        )

    def _feature_intersects_bounds(self, feat: dict[str, Any], bounds: dict[str, float]) -> bool:
        center = feat.get("center") or {}
        lat = center.get("lat")
        lon = center.get("lon")
        if lat is not None and lon is not None and self._point_in_bounds(safe_float(lat), safe_float(lon), bounds):
            return True
        # Try bounds center.
        bounds = feat.get("bounds") or {}
        if bounds:
            lat = bounds.get("lat_center")
            lon = bounds.get("lon_center")
            if lat is not None and lon is not None and self._point_in_bounds(safe_float(lat), safe_float(lon), bounds):
                return True
        # Try first footprint/path point if present.
        fp = feat.get("footprint")
        if isinstance(fp, list) and fp:
            p0 = fp[0] or {}
            lat = p0.get("lat")
            lon = p0.get("lng", p0.get("lon"))
            if lat is not None and lon is not None and self._point_in_bounds(safe_float(lat), safe_float(lon), bounds):
                return True
        return False

    def _feature_center(self, feat: dict[str, Any]) -> tuple[float | None, float | None]:
        center = feat.get("center") or {}
        lat = center.get("lat")
        lon = center.get("lon")
        if lat is None or lon is None:
            b = feat.get("bounds") or {}
            lat = b.get("lat_center", lat)
            lon = b.get("lon_center", lon)
        if lat is None or lon is None:
            fp = feat.get("footprint")
            if isinstance(fp, list) and fp:
                p0 = fp[0] or {}
                lat = p0.get("lat", lat)
                lon = p0.get("lng", p0.get("lon", lon))
        lat_f = safe_float(lat, None)
        lon_f = safe_float(lon, None)
        if lat_f is None or lon_f is None:
            return None, None
        return lat_f, lon_f

    def _feature_bbox(self, feat: dict[str, Any]) -> dict[str, float]:
        b = feat.get("bbox")
        if isinstance(b, dict):
            try:
                return {"west": float(b.get("west")), "south": float(b.get("south")), "east": float(b.get("east")), "north": float(b.get("north"))}
            except Exception:
                pass
        lat, lon = self._feature_center(feat)
        if lat is None or lon is None:
            return {"west": -181.0, "south": -91.0, "east": -181.0, "north": -91.0}
        return {"west": lon, "south": lat, "east": lon, "north": lat}

    def _grid_cells_for_bbox(self, bbox: dict[str, float], cell_deg: float = SPATIAL_GRID_DEG) -> list[str]:
        west = float(bbox.get("west", -180.0))
        east = float(bbox.get("east", 180.0))
        south = float(bbox.get("south", -90.0))
        north = float(bbox.get("north", 90.0))
        gx0 = int(math.floor((west + 180.0) / cell_deg))
        gx1 = int(math.floor((east + 180.0) / cell_deg))
        gy0 = int(math.floor((south + 90.0) / cell_deg))
        gy1 = int(math.floor((north + 90.0) / cell_deg))
        cells = []
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                cells.append(f"{gx}:{gy}")
        return cells

    def _dedup_key(self, layer: str, feat: dict[str, Any], meta: dict[str, Any]) -> str:
        if layer == "lightning":
            c = meta.get("centroid") or {}
            lat = round(safe_float(c.get("lat"), 0.0), 2)
            lon = round(safe_float(c.get("lon"), 0.0), 2)
            t = str(meta.get("time_bucket") or "na")
            intensity = round(safe_float(feat.get("estimated_energy") or feat.get("intensity") or feat.get("severity"), 0.0), 2)
            kind = str(feat.get("type") or feat.get("kind") or "strike")
            return f"ltg:{lat}:{lon}:{t}:{intensity}:{kind}"
        return str(meta.get("feature_id") or feat.get("id") or stable_hash_u32(json.dumps(feat, sort_keys=True, default=str)))

    def _feature_meta(self, layer: str, feat: dict[str, Any]) -> dict[str, Any]:
        lat, lon = self._feature_center(feat)
        bbox = self._feature_bbox(feat)
        fid = str(feat.get("id") or f"{layer}-{stable_hash_u32(json.dumps(feat, sort_keys=True, default=str))}")
        time_bucket = str(feat.get("time_bucket") or feat.get("ts_bucket") or feat.get("valid_time") or "na")
        altitude_band = str(feat.get("altitude_band") or feat.get("band") or "surface")
        meta = {
            "feature_id": fid,
            "layer": layer,
            "bbox": bbox,
            "centroid": {"lat": lat, "lon": lon},
            "time_bucket": time_bucket,
            "altitude_band": altitude_band,
        }
        meta["dedup_key"] = self._dedup_key(layer, feat, meta)
        return meta

    def _intersects_bbox(self, a: dict[str, float], b: dict[str, float]) -> bool:
        return not (a.get("east", -999) < b.get("west", 999) or a.get("west", 999) > b.get("east", -999) or a.get("north", -999) < b.get("south", 999) or a.get("south", 999) > b.get("north", -999))

    def _build_layer_feature_indexes(self, scene_payload: dict[str, Any]) -> None:
        scene = scene_payload.get("scene") if isinstance(scene_payload.get("scene"), dict) else {}
        layer_sources = {
            "clouds": scene_payload.get("items") or [],
            "precip": scene_payload.get("precip_columns") or scene.get("precip") or [],
            "lightning": scene_payload.get("lightning_events") or scene.get("lightning") or [],
            "wind": scene.get("wind") or ((scene_payload.get("balloons") or {}).get("items") if isinstance(scene_payload.get("balloons"), dict) else []) or [],
            "swell": scene.get("swell") or [],
            "sst": scene.get("sst") or [],
            "fish": (self.state.fish_points or self.load_fish()[0]) or [],
        }
        self.state.layer_feature_index = {}
        self.state.layer_feature_meta = {}
        self.state.layer_feature_store = {}
        for layer, feats in layer_sources.items():
            idx = {}
            metas = {}
            store = {}
            for feat in feats:
                if not isinstance(feat, dict):
                    continue
                meta = self._feature_meta(layer, feat)
                fid = meta["feature_id"]
                metas[fid] = meta
                store[fid] = feat
                for cell in self._grid_cells_for_bbox(meta["bbox"]):
                    idx.setdefault(cell, set()).add(fid)
            self.state.layer_feature_index[layer] = idx
            self.state.layer_feature_meta[layer] = metas
            self.state.layer_feature_store[layer] = store

    def _spatial_candidates(self, layer: str, bounds: dict[str, float]) -> tuple[list[dict[str, Any]], int]:
        idx = self.state.layer_feature_index.get(layer) or {}
        metas = self.state.layer_feature_meta.get(layer) or {}
        store = self.state.layer_feature_store.get(layer) or {}
        if not idx:
            return [], 0
        candidate_ids = set()
        for cell in self._grid_cells_for_bbox(bounds):
            candidate_ids.update(idx.get(cell) or set())
        out = []
        for fid in candidate_ids:
            meta = metas.get(fid) or {}
            if not self._intersects_bbox(meta.get("bbox") or {}, bounds):
                continue
            feat = store.get(fid)
            if feat is None:
                continue
            f = dict(feat)
            f.setdefault("feature_id", fid)
            f.setdefault("layer", layer)
            f.setdefault("bbox", meta.get("bbox"))
            f.setdefault("centroid", meta.get("centroid"))
            f.setdefault("time_bucket", meta.get("time_bucket"))
            f.setdefault("altitude_band", meta.get("altitude_band"))
            f.setdefault("dedup_key", meta.get("dedup_key"))
            c = f.get("centroid") or {}
            lat = safe_float(c.get("lat"), None)
            lon = safe_float(c.get("lon"), None)
            if lat is None or lon is None:
                log.debug("[gfs] skip malformed feature without finite centroid layer=%s fid=%s", layer, fid)
                continue
            out.append(f)
        return out, len(candidate_ids)

    def _record_tile_diag(self, key: str, diag: dict[str, Any]) -> None:
        self.state.tile_diagnostics[key] = diag
        if len(self.state.tile_diagnostics) > MAX_TILE_DIAGNOSTICS:
            for old_key in sorted(self.state.tile_diagnostics.keys())[: len(self.state.tile_diagnostics) - MAX_TILE_DIAGNOSTICS]:
                self.state.tile_diagnostics.pop(old_key, None)

    def _scene_cache_key(self, bbox: dict[str, float]) -> str:
        return f"scene:{round(safe_float(bbox.get('west'),-180.0),3)}:{round(safe_float(bbox.get('south'),-80.0),3)}:{round(safe_float(bbox.get('east'),180.0),3)}:{round(safe_float(bbox.get('north'),80.0),3)}"

    def _get_cached_scene_payload(self, bbox: dict[str, float]) -> tuple[dict[str, Any], dict[str, Any]]:
        now_ms = self._now_ms()
        ttl_ms = max(10_000, SCENE_REFRESH_TTL_SECONDS * 1000)
        key = self._scene_cache_key(bbox)
        row = self.state.tile_cache.get(key) or {}
        diag = {"scene_cache_key": key, "cache_hit": False, "cache_age_ms": None, "build_duration_ms": 0}
        if row and (now_ms - int(row.get("ts") or 0)) <= ttl_ms and isinstance(row.get("payload"), dict):
            diag["cache_hit"] = True
            diag["cache_age_ms"] = now_ms - int(row.get("ts") or 0)
            return row["payload"], diag

        if not self._scene_refresh_lock.acquire(blocking=False):
            if isinstance(row.get("payload"), dict):
                diag["cache_hit"] = True
                diag["cache_age_ms"] = now_ms - int(row.get("ts") or 0)
                return row["payload"], diag
            with self._scene_refresh_lock:
                pass
            row = self.state.tile_cache.get(key) or {}
            if isinstance(row.get("payload"), dict):
                diag["cache_hit"] = True
                diag["cache_age_ms"] = now_ms - int(row.get("ts") or 0)
                return row["payload"], diag

        started = time.perf_counter()
        try:
            payload = self.cloud_tiles_payload(bbox)
        except Exception as exc:
            log.exception("[gfs] cached scene generation failed")
            payload = self._degraded_scene_payload(bbox, str(exc))
        finally:
            self._scene_refresh_lock.release()

        diag["build_duration_ms"] = int((time.perf_counter() - started) * 1000)
        self.state.tile_cache[key] = {"ts": now_ms, "payload": payload}
        self.state.scene_cache = payload
        self.state.scene_cache_ts = now_ms
        try:
            self._build_layer_feature_indexes(payload)
        except Exception:
            log.exception("[gfs] layer feature index build failed")
        return payload, diag

    def get_scene_payload(self, bbox: dict[str, float] | None = None) -> dict[str, Any]:
        payload, _ = self._get_cached_scene_payload(self._normalize_bbox(bbox))
        return payload

    def layer_tile_payload(self, layer: str, z: int, x: int, y: int, pad_deg: float = 0.18, debug: bool = False) -> dict[str, Any]:
        layer_name = (layer or "").strip().lower()
        tile_bounds = self._tile_bounds_xyz(z, x, y)
        filter_bounds = self._expand_bounds(tile_bounds, pad_deg=pad_deg)
        tile_key = f"{layer_name}:{z}/{x}/{y}"
        scene_payload, cache_diag = self._get_cached_scene_payload(filter_bounds)
        if not self.state.layer_feature_index.get(layer_name):
            self._build_layer_feature_indexes(scene_payload)
        scene = scene_payload.get("scene") if isinstance(scene_payload.get("scene"), dict) else {}

        caps = {"clouds": 220, "precip": 240, "lightning": 160, "wind": 220, "swell": 160, "sst": 180, "fish": 260}
        if layer_name not in caps:
            return {
                "status": {"ok": False, "mode": "invalid", "errors": ["unknown_layer"], "warnings": [], "partial": False},
                "meta": {"layer": layer_name, "z": z, "x": x, "y": y, "bounds": tile_bounds, "schema_version": "atmo-tile-v1", "generated_at": self._now_ms()},
                "features": [],
                "summary": {"count": 0},
            }

        try:
            candidates, candidate_count = self._spatial_candidates(layer_name, filter_bounds)
            precise = [f for f in candidates if self._feature_intersects_bounds(f, filter_bounds)]
        except Exception as exc:
            log.exception("[gfs] tile derivation failed layer=%s tile=%s/%s/%s", layer_name, z, x, y)
            candidates, candidate_count, precise = [], 0, []
        precise = sorted(precise, key=lambda f: safe_float(f.get("visual_priority", f.get("importance", 0.0)), 0.0), reverse=True)

        dedup = []
        dedup_seen = set()
        dedup_suppressed = 0
        for feat in precise:
            dkey = str(feat.get("dedup_key") or feat.get("feature_id") or feat.get("id") or stable_hash_u32(json.dumps(feat, sort_keys=True, default=str)))
            if dkey in dedup_seen:
                dedup_suppressed += 1
                continue
            dedup_seen.add(dkey)
            dedup.append(feat)
        features = dedup[: caps[layer_name]]

        if layer_name in {"precip", "clouds", "sst"}:
            field_map = {"precip": "precip_rate", "clouds": "cloud_density", "sst": "temperature_k"}
            contour_geo = self._marching_squares_contours(field_map[layer_name], filter_bounds, z)
            for feat in contour_geo:
                features.append({
                    "id": f"contour-{stable_hash_u32(json.dumps(feat, sort_keys=True, default=str))}",
                    "type": "contour",
                    "geojson": feat,
                    "visual_priority": 0.4,
                })

        src_status = scene_payload.get("status") if isinstance(scene_payload.get("status"), dict) else {}
        src_meta = scene_payload.get("meta") if isinstance(scene_payload.get("meta"), dict) else {}
        diagnostics = {
            "tile_key": tile_key,
            "layer": layer_name,
            "request_bounds": tile_bounds,
            "filter_bounds": filter_bounds,
            "cache_hit": cache_diag.get("cache_hit", False),
            "cache_age_ms": cache_diag.get("cache_age_ms"),
            "build_duration_ms": cache_diag.get("build_duration_ms", 0),
            "candidate_count": candidate_count,
            "precise_count": len(precise),
            "emitted_count": len(features),
            "dedup_suppressed_count": dedup_suppressed,
            "decode_backend": self.state.decode_backend,
            "data_source_mode": self.state.data_source_mode,
            "cycle": self.state.model_cycle,
            "forecast_hour": self.state.model_forecast_hour,
            "valid_time": self.state.model_valid_time,
        }
        self._record_tile_diag(tile_key, diagnostics)

        out = {
            "status": {
                "ok": bool(src_status.get("ok", True)),
                "mode": str(src_status.get("mode") or scene_payload.get("payload_state") or "live"),
                "warnings": list(src_status.get("warnings") or []),
                "errors": list(src_status.get("errors") or []),
                "partial": bool(src_status.get("partial", False)),
                "generated_at": self._now_ms(),
                "request_bounds": tile_bounds,
                "data_source_mode": self.state.data_source_mode,
                "decode_backend": self.state.decode_backend,
            },
            "meta": {
                "layer": layer_name,
                "z": int(z),
                "x": int(x),
                "y": int(y),
                "bounds": tile_bounds,
                "filter_bounds": filter_bounds,
                "schema_version": "atmo-tile-v1",
                "generated_at": self._now_ms(),
                "analysis_time": src_meta.get("analysis_time") or scene_payload.get("cycle"),
                "valid_time": src_meta.get("valid_time") or scene_payload.get("valid_time"),
                "heuristic": bool(src_meta.get("heuristic_flags", {}).get("scene_features_estimated", scene_payload.get("heuristic", True))),
                "decode_backend": self.state.decode_backend,
                "data_source_mode": self.state.data_source_mode,
            },
            "features": features,
            "summary": {
                "count": len(features),
                "layer": layer_name,
                "tile": f"{z}/{x}/{y}",
                "candidate_count": candidate_count,
                "dedup_suppressed": dedup_suppressed,
            },
        }
        if debug:
            out["diagnostics"] = diagnostics
        return out

    def tile_diagnostics_payload(self, layer: str | None = None, tile: str | None = None) -> dict[str, Any]:
        layer_name = (layer or "").strip().lower()
        rows = []
        for key, diag in sorted(self.state.tile_diagnostics.items(), key=lambda kv: kv[0]):
            if layer_name and diag.get("layer") != layer_name:
                continue
            if tile and tile not in key:
                continue
            rows.append(diag)
        return {
            "status": {"ok": True, "generated_at": self._now_ms()},
            "meta": {
                "schema_version": "tile-diagnostics-v1",
                "decode_backend": self.state.decode_backend,
                "data_source_mode": self.state.data_source_mode,
                "cycle": self.state.model_cycle,
                "forecast_hour": self.state.model_forecast_hour,
            },
            "items": rows[-250:],
            "summary": {"count": len(rows[-250:])},
        }

    def _load_store(self) -> Dict[str, Any]:
        if not self.store_path.exists():
            return {"locations": {}}
        try:
            return json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception:
            return {"locations": {}}

    def _save_store(self, data: Dict[str, Any]) -> None:
        self.store_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _ensure_location_record(self, store: Dict[str, Any], location_key: str) -> Dict[str, Any]:
        locations = store.setdefault("locations", {})
        rec = locations.setdefault(
            location_key,
            {
                "location_key": location_key,
                "report_text": "",
                "report_updated_at": None,
                "live": {"active": False, "stream_url": "", "updated_at": None},
                "uploads": [],
            },
        )
        return rec

    def _location_media_payload(self, location_key: str) -> Dict[str, Any]:
        store = self._load_store()
        key_in = str(location_key or "").strip()
        if not key_in:
            return {"ok": False, "error": "missing location_key", "location_key": "", "uploads": [], "live": {}, "report_text": ""}

        fish_match = self._find_fish_point(key_in)
        key = str((fish_match or {}).get("location_key") or self._normalize_location_key(key_in))
        rec = self._ensure_location_record(store, key)
        now_ms = self._now_ms()
        if fish_match:
            intel = self._build_bait_intel(fish_match, now_ms)
        else:
            intel = self._heuristic_context(None, None, now_ms)
        csv_reports = list((fish_match or {}).get("all_reports") or (fish_match or {}).get("reports") or [])
        user_report = (rec.get("report_text") or "").strip()
        reports = [*csv_reports, *([user_report] if user_report else [])]
        return {
            "ok": True,
            "id": key,
            "location_id": key,
            "location_key": key,
            "csv_id": (fish_match or {}).get("csv_id"),
            "name": (fish_match or {}).get("name") or key,
            "label": (fish_match or {}).get("name") or key,
            "lat": (fish_match or {}).get("lat"),
            "lon": (fish_match or {}).get("lon"),
            "reports": reports,
            "all_reports": reports,
            "csv_reports": csv_reports,
            "last_report": reports[-1] if reports else "",
            "report_count": len(reports),
            "report_text": user_report,
            "report_updated_at": rec.get("report_updated_at"),
            "uploads": rec.get("uploads") or [],
            "live": rec.get("live") or {"active": False, "stream_url": "", "updated_at": None},
            **intel,
            "ts": self._now_ms(),
        }

    def location_payload(self, location_key: str) -> Dict[str, Any]:
        """Detailed marker payload for HUD opening."""
        return self._location_media_payload(location_key)

    def location_environment_payload(self, location_key: str) -> Dict[str, Any]:
        """Weather/environment-only payload for a marker HUD."""
        loc = self._find_fish_point(location_key)
        if not loc:
            return {"ok": False, "error": "location not found", "id": location_key, "marker_environment": None}
        media = self._location_media_payload(str(loc.get("location_key") or loc.get("id")))
        marker_env = media.get("marker_environment") or media.get("weather_environment") or (media.get("weather") or {}).get("marker_environment")
        return {
            "ok": bool(marker_env),
            "id": loc.get("id"),
            "location_id": loc.get("location_key"),
            "location_key": loc.get("location_key"),
            "name": loc.get("name"),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "marker_environment": marker_env,
            "weather_environment": marker_env,
            "environment": media.get("environment"),
            "environment_meta": media.get("environment_meta"),
            "weather": media.get("weather"),
            "ts": self._now_ms(),
        }

    def node_intelligence_payload(self, node_id: str) -> Dict[str, Any]:
        """Return a stable /intelligence/node/<id> contract for fish markers."""
        loc = self._find_fish_point(node_id)
        if not loc:
            return {"ok": False, "error": "location not found", "id": node_id, "profile": None}
        media = self._location_media_payload(str(loc.get("location_key") or loc.get("id")))
        extra_reports = list(media.get("reports") or [])
        profile = build_location_profile(loc, extra_reports)
        return {
            "ok": True,
            "id": loc.get("id"),
            "location_id": loc.get("location_key"),
            "location_key": loc.get("location_key"),
            "name": loc.get("name"),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "reports": extra_reports,
            "last_report": extra_reports[-1] if extra_reports else "",
            "profile": profile,
            "bait": media.get("bait"),
            "environment": media.get("environment"),
            "environment_meta": media.get("environment_meta"),
            "history": media.get("history"),
            "marker_environment": media.get("marker_environment") or media.get("weather_environment") or (media.get("weather") or {}).get("marker_environment"),
            "weather_environment": media.get("marker_environment") or media.get("weather_environment") or (media.get("weather") or {}).get("marker_environment"),
            "weather": media.get("weather"),
            "live": media.get("live"),
            "uploads": media.get("uploads"),
            "ts": self._now_ms(),
        }

    def health(self) -> Dict[str, Any]:
        points, _ = self.load_fish()
        return {
            "ok": True,
            "enabled": self.state.enabled,
            "source": self.state.source_name,
            "fish_count": len(points),
            "last_refresh_ts": self.state.last_refresh_ts,
            "last_error": self.state.last_error,
            "fish_csv": str(self._fish_csv_path()),
            "fish_csv_exists": self._fish_csv_path().exists(),
            "fish_csv_candidates": [str(p) for p in self._fish_csv_candidates()],
            "ingest": {
                "status": self.state.ingest_status,
                "last_attempt_ts": self.state.ingest_last_attempt_ts,
                "last_success_ts": self.state.ingest_last_success_ts,
                "error": self.state.ingest_error,
                "degraded_mode": self.state.degraded_mode,
                "using_last_known_good": self.state.using_last_known_good,
                "cycle": self.state.model_cycle,
                "forecast_hour": self.state.model_forecast_hour,
                "valid_time": self.state.model_valid_time,
                "analysis_time": self.state.model_analysis_time,
                "source_url": self.state.model_source_url,
                "cache_path": self.state.model_cache_path,
                "source_format": self.state.model_source_format,
                "fields_available": list(self.state.fields_available or []),
                "fields_missing": list(self.state.fields_missing or []),
                "decode_backend": self.state.decode_backend,
                "data_source_mode": self.state.data_source_mode,
            },
            "ts": self._now_ms(),
        }

    def config(self) -> Dict[str, Any]:
        return {
            "enabled": self.state.enabled,
            "api_base": "/gfs/api",
            "ws_base": "/gfs/ws",
            "cache_ttl_seconds": self.state.cache_ttl_seconds,
            "google_maps_api_key": os.getenv("GOOGLE_MAPS_API_KEY", "").strip() or os.getenv("MAPS_API_KEY", "").strip(),
            "ingest": {
                "fallback_cycle_depth": INGEST_FALLBACK_CYCLE_DEPTH,
                "preferred_forecast_hour": INGEST_PREFERRED_FORECAST_HOUR,
                "cache_min_bytes": INGEST_CACHE_MIN_BYTES,
                "source_format": "grib2",
            },
            "ts": self._now_ms(),
        }

    # Backward-compatible route/service contract helpers.
    def health_payload(self) -> Dict[str, Any]:
        payload = self.health()
        try:
            payload["gfs_cache"] = self.cache_status_payload().get("cache", {})
        except Exception as exc:
            payload["gfs_cache"] = {"enabled": True, "error": str(exc)}
        return payload


    # ------------------------------------------------------------------
    # Managed globe tile cache / center-first tile planning
    # ------------------------------------------------------------------
    def _tile_cache_root(self) -> Path:
        root = BASE_DIR / ".cache" / "gfs_tiles"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _wrap_lon(self, lon: float) -> float:
        try:
            x = float(lon)
        except Exception:
            return 0.0
        while x < -180.0:
            x += 360.0
        while x >= 180.0:
            x -= 360.0
        return x

    def _base_tile_grid(self) -> tuple[int, int]:
        cols = int(os.getenv("GFS_BASE_TILE_COLS", str(DEFAULT_VIEWPORT_GRID)) or str(DEFAULT_VIEWPORT_GRID))
        rows = int(os.getenv("GFS_BASE_TILE_ROWS", str(DEFAULT_VIEWPORT_GRID)) or str(DEFAULT_VIEWPORT_GRID))
        return max(1, cols), max(1, rows)

    def _tile_id_for_point(self, lat: float, lon: float, cols: int | None = None, rows: int | None = None) -> str:
        if cols is None or rows is None:
            cols, rows = self._base_tile_grid()
        lat_c = max(-89.999, min(89.999, float(lat)))
        lon_c = self._wrap_lon(float(lon))
        col = int(math.floor(((lon_c + 180.0) / 360.0) * cols))
        row = int(math.floor(((90.0 - lat_c) / 180.0) * rows))
        col = max(0, min(cols - 1, col))
        row = max(0, min(rows - 1, row))
        return f"z0_r{row}_c{col}"

    def _parse_tile_id(self, tile_id: str) -> tuple[int, int, int]:
        m = re.match(r"z(\d+)_r(\d+)_c(\d+)$", str(tile_id or ""))
        if not m:
            return 0, 0, 0
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    def _tile_bounds_by_id(self, tile_id: str) -> dict[str, float]:
        z, row, col = self._parse_tile_id(tile_id)
        base_cols, base_rows = self._base_tile_grid()
        scale = 2 ** max(0, z)
        cols = base_cols * scale
        rows = base_rows * scale
        col = max(0, min(cols - 1, int(col)))
        row = max(0, min(rows - 1, int(row)))
        lon_w = -180.0 + (360.0 / cols) * col
        lon_e = -180.0 + (360.0 / cols) * (col + 1)
        lat_n = 90.0 - (180.0 / rows) * row
        lat_s = 90.0 - (180.0 / rows) * (row + 1)
        return {"west": lon_w, "south": lat_s, "east": lon_e, "north": lat_n}

    def _bbox_center(self, bbox: dict[str, float]) -> tuple[float, float]:
        b = self._normalize_bbox(bbox)
        return ((b["south"] + b["north"]) / 2.0, self._wrap_lon((b["west"] + b["east"]) / 2.0))

    def _expand_bbox_factor(self, bbox: dict[str, float], factor: float | None = None, max_lon_span: float | None = None, max_lat_span: float | None = None) -> dict[str, float]:
        """Return a padded anti-clipping bbox around the viewport center."""
        b = self._normalize_bbox(bbox)
        clat, clon = self._bbox_center(b)
        factor_f = float(factor if factor is not None else os.getenv("GFS_VIEWPORT_PAD_FACTOR", "1.25"))
        max_lon = float(max_lon_span if max_lon_span is not None else os.getenv("GFS_MAX_WORK_LON_SPAN", "10.0"))
        max_lat = float(max_lat_span if max_lat_span is not None else os.getenv("GFS_MAX_WORK_LAT_SPAN", "10.0"))
        base_lon_span = max(0.25, abs(float(b["east"]) - float(b["west"])))
        base_lat_span = max(0.25, abs(float(b["north"]) - float(b["south"])))
        lon_span = min(max_lon, max(0.25, base_lon_span * factor_f))
        lat_span = min(max_lat, max(0.25, base_lat_span * factor_f))
        west = max(-180.0, clon - lon_span / 2.0)
        east = min(180.0, clon + lon_span / 2.0)
        south = max(-80.0, clat - lat_span / 2.0)
        north = min(80.0, clat + lat_span / 2.0)
        return {"west": west, "south": south, "east": east, "north": north}

    def _bounded_work_bbox(self, bbox: dict[str, float], max_lon_span: float = 10.0, max_lat_span: float = 10.0) -> dict[str, float]:
        """Padded/limited work bbox for cache/provider/polygon generation."""
        return self._expand_bbox_factor(bbox, factor=None, max_lon_span=max_lon_span, max_lat_span=max_lat_span)


    def _bbox_span_area(self, bbox: dict[str, float]) -> tuple[float, float, float]:
        b = self._normalize_bbox(bbox)
        width = abs(float(b["east"]) - float(b["west"]))
        if width > 180.0:
            width = 360.0 - width
        height = abs(float(b["north"]) - float(b["south"]))
        return width, height, max(0.0, width * height)

    def _scene_tier_for_bbox(self, bbox: dict[str, float]) -> str:
        width, height, area = self._bbox_span_area(bbox)
        span = max(width, height)
        if span <= 1.6 and area <= 2.6:
            return "harbor"
        if span <= 4.0 and area <= 14.0:
            return "coastal"
        if span <= 12.0 and area <= 90.0:
            return "regional"
        return "world"

    def _scene_budget(self, tier: str) -> dict[str, Any]:
        tier_key = str(tier or "regional").lower()
        table = {
            "harbor": {
                "target_cells": 90000, "provider_target_cells": 240000,
                "max_cloud_shells": 500, "max_cloud_particles": 5200,
                "max_boats": 18, "max_bait_polygons": 220,
                "fetch_padding": 1.4, "solve_dx_deg": 0.015, "render_dx_deg": 0.03,
            },
            "coastal": {
                "target_cells": 80000, "provider_target_cells": 200000,
                "max_cloud_shells": 500, "max_cloud_particles": 5200,
                "max_boats": 18, "max_bait_polygons": 180,
                "fetch_padding": 1.6, "solve_dx_deg": 0.03, "render_dx_deg": 0.06,
            },
            "regional": {
                "target_cells": 80000, "provider_target_cells": 200000,
                "max_cloud_shells": 500, "max_cloud_particles": 5200,
                "max_boats": 18, "max_bait_polygons": 150,
                "fetch_padding": 1.8, "solve_dx_deg": 0.06, "render_dx_deg": 0.10,
            },
            "world": {
                "target_cells": 64000, "provider_target_cells": 160000,
                "max_cloud_shells": 500, "max_cloud_particles": 5200,
                "max_boats": 18, "max_bait_polygons": 80,
                "fetch_padding": 2.0, "solve_dx_deg": 0.25, "render_dx_deg": 0.50,
            },
        }
        out = dict(table.get(tier_key) or table["regional"])
        out["tier"] = tier_key if tier_key in table else "regional"
        return out

    def _choose_decimation_for_shape(self, rows: int, cols: int, target_cells: int) -> int:
        try:
            total = max(1, int(rows) * int(cols))
            target = max(1, int(target_cells or 1))
            if total <= target:
                return 1
            return max(1, int(math.ceil(math.sqrt(total / float(target)))))
        except Exception:
            return 1

    def _estimate_provider_stride_for_bbox(self, bbox: dict[str, float], target_cells: int | None = None) -> int:
        # HYCOM/GFS provider spacing varies by product. Use a conservative 0.04°
        # estimate so large/tilted fetch bboxes are protected by target cell budgets
        # instead of fixed magic stride buckets. Small harbor bboxes stay native.
        b = self._normalize_bbox(bbox)
        width, height, area = self._bbox_span_area(b)
        if max(width, height) <= 1.6 and area <= 2.8:
            return 1
        native_dx = float(os.getenv("GFS_SCENE_PROVIDER_DX_DEG", "0.04") or "0.04")
        est_rows = max(1, int(math.ceil(height / native_dx)) + 1)
        est_cols = max(1, int(math.ceil(width / native_dx)) + 1)
        stride = self._choose_decimation_for_shape(est_rows, est_cols, int(target_cells or 12000))
        # HYCOM NCSS repeatedly timed out for 6–12 degree coastal boxes at stride 2/3.
        # Keep small harbors native, but use a safe floor for larger coastal bboxes.
        if area >= 30 or max(width, height) >= 5.0:
            stride = max(stride, int(os.getenv("GFS_HYCOM_TIMEOUT_SAFE_STRIDE", "1") or "5"))
        return min(int(os.getenv("GFS_HYCOM_MAX_PROVIDER_STRIDE", "2") or "10"), max(1, stride))

    def build_scene_plan(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, layer: str = "frame") -> dict[str, Any]:
        fetch_bbox = self._normalize_bbox(bbox)
        visible = self._normalize_bbox(visible_bbox or bbox)
        tier = self._scene_tier_for_bbox(visible)
        budget = self._scene_budget(tier)
        provider_stride = self._estimate_provider_stride_for_bbox(fetch_bbox, budget.get("provider_target_cells"))
        scene = {
            "schema": "lftr_scene_plan_v1",
            "layer": str(layer or "frame"),
            "tier": budget["tier"],
            "visible_bbox": visible,
            "fetch_bbox": fetch_bbox,
            "bbox_policy": "visible_bbox_draws__fetch_bbox_padded_for_tilt_cache_ncss",
            "provider_stride": provider_stride,
            "target_cells": budget["target_cells"],
            "provider_target_cells": budget["provider_target_cells"],
            "solve_dx_deg": budget["solve_dx_deg"],
            "render_dx_deg": budget["render_dx_deg"],
            "render_budget": {
                "max_cloud_shells": budget["max_cloud_shells"],
                "max_cloud_particles": budget["max_cloud_particles"],
                "max_boats": budget["max_boats"],
                "max_bait_polygons": budget["max_bait_polygons"],
            },
            "compat": {"legacy_lod_quality_stride_supported": True, "stride_is_budget_derived": True},
        }
        return scene

    def _attach_scene_plan(self, payload: dict[str, Any], scene: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        payload["scene_plan"] = scene
        payload.setdefault("scene_tier", scene.get("tier"))
        payload.setdefault("visible_bbox", scene.get("visible_bbox"))
        payload.setdefault("fetch_bbox", scene.get("fetch_bbox"))
        payload.setdefault("render_budget", scene.get("render_budget"))
        return payload

    def tile_plan_payload(self, bbox: dict[str, float] | None = None, max_tiles: int = 576) -> dict[str, Any]:
        """Return the universal 24x24 viewport tile contract.

        This replaces the older global z0 tile plan for browser/provider work.
        Every provider now receives the same tile_id and the same tile bbox for a
        given viewport, which keeps the cache/debug/render contract congruent.
        """
        requested = self._normalize_bbox(bbox)
        grid = int(os.getenv("GFS_VIEWPORT_TILE_GRID", str(DEFAULT_VIEWPORT_GRID)) or str(DEFAULT_VIEWPORT_GRID))
        tiles = split_viewport_tiles(requested, grid=grid)
        try:
            limit = max(1, min(int(max_tiles or (grid * grid)), grid * grid))
        except Exception:
            limit = grid * grid
        out_tiles: list[dict[str, Any]] = []
        for t in tiles[:limit]:
            out_tiles.append({
                "tile_id": t.tile_id,
                "z": 0,
                "row": t.row,
                "col": t.col,
                "bbox": t.bbox,
                "center": {"lat": round(t.center["lat"], 6), "lon": round(t.center["lon"], 6)},
                "priority": 0,
                "ring": 0,
                "intersects_viewport": True,
            })
        return {
            "ok": True,
            "schema": "lftr_viewport_tile_plan_v2_24x24",
            "strategy": "split_visible_viewport_into_congruent_24x24_tiles",
            "grid": {
                "cols": grid,
                "rows": grid,
                "count": grid * grid,
                "tile_degrees": {
                    "lon": (requested["east"] - requested["west"]) / float(grid),
                    "lat": (requested["north"] - requested["south"]) / float(grid),
                },
            },
            "requested_bbox": requested,
            "viewport_bbox": requested,
            "work_bbox": requested,
            "cache_bbox": requested,
            "bounds_policy": {"mode": "exact_viewport_no_extra_legacy_global_grid"},
            "tiles": out_tiles,
            "total_tiles": grid * grid,
            "returned_tiles": len(out_tiles),
            "ts": self._now_ms(),
        }

    def provider_tile_contract_payload(self, bbox: dict[str, float] | None = None, providers: list[str] | None = None, include_urls: bool = False, limit: int | None = None) -> dict[str, Any]:
        grid = int(os.getenv("GFS_VIEWPORT_TILE_GRID", str(DEFAULT_VIEWPORT_GRID)) or str(DEFAULT_VIEWPORT_GRID))
        if include_urls:
            return provider_jobs(bbox or self._normalize_bbox(None), providers=providers, grid=grid, limit=limit)
        return provider_tile_plan(bbox or self._normalize_bbox(None), providers=providers, grid=grid)

    def _tile_cache_path(self, tile_id: str, product: str = "intel") -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tile_id or "tile"))
        return self._tile_cache_root() / f"{safe}.{product}.json"

    def _json_safe_for_tile_cache(self, obj: Any) -> Any:
        """Make rich layer payloads safe for gzip JSON tile cache without dropping contracts."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, Path):
            return str(obj)
        if np is not None:
            try:
                if isinstance(obj, np.generic):
                    return obj.item()
                if isinstance(obj, np.ndarray):
                    # Tile caches must stay app-native and browser-loadable; never persist raw global arrays.
                    if obj.size > 4096:
                        return {"array_omitted": True, "shape": list(obj.shape), "dtype": str(obj.dtype)}
                    return obj.tolist()
            except Exception:
                pass
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if str(k).startswith("_"):
                    continue
                out[str(k)] = self._json_safe_for_tile_cache(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [self._json_safe_for_tile_cache(v) for v in obj]
        try:
            return json.loads(json.dumps(obj, default=str))
        except Exception:
            return str(obj)

    def _scene_tile_cache_path(self, tile_id: str, product: str = "scene") -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tile_id or "tile"))[:96]
        prod = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(product or "scene"))[:48]
        return self.scene_tile_cache_dir / f"{prod}_{safe}.json.gz"

    def _read_scene_tile_cache(self, tile_id: str, product: str = "scene", ttl_seconds: int = 300) -> dict[str, Any] | None:
        path = self._scene_tile_cache_path(tile_id, product)
        if not path.exists():
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if ttl_seconds >= 0 and age > ttl_seconds:
                return None
            import gzip
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                payload.setdefault("cache", {})
                payload["cache"].update({"hit": True, "age_seconds": round(age, 3), "path": str(path), "product": product})
                return payload
        except Exception as exc:
            log.debug("scene tile gzip cache read skipped tile=%s err=%s", tile_id, exc)
        return None

    def _write_scene_tile_cache(self, tile_id: str, payload: dict[str, Any], product: str = "scene") -> dict[str, Any]:
        path = self._scene_tile_cache_path(tile_id, product)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        tmp = path.with_name(path.name + ".tmp")
        try:
            import gzip
            safe_payload = self._json_safe_for_tile_cache(payload)
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
                json.dump(safe_payload, fh, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as exc:
            log.debug("scene tile gzip cache write skipped tile=%s err=%s", tile_id, exc)
        return payload

    def _source_quality_summary(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"available": False}
        source = str(payload.get("source") or payload.get("provider_source") or payload.get("live_source") or "")
        state = str(payload.get("payload_state") or payload.get("status") or payload.get("source_state") or "")
        txt = f"{source} {state} {payload.get('mode','')}".lower()
        return {
            "available": bool(payload),
            "source": source or None,
            "payload_state": state or None,
            "live_ncss_or_erddap": any(x in txt for x in ("ncss", "erddap", "gfs_nomads", "hycom", "coastwatch")),
            "mock": "mock" in txt or "synthetic" in txt,
            "proxy": "proxy" in txt or "fallback" in txt,
            "fallback_used": bool(payload.get("fallback_used") or ((payload.get("fallback") or {}).get("used") if isinstance(payload.get("fallback"), dict) else False)),
        }

    def _clip_markers_to_bbox(self, bbox: dict[str, float], limit: int = 260) -> list[dict[str, Any]]:
        try:
            points, _ = self.load_fish()
        except Exception:
            return []
        out = []
        for p in points or []:
            lat = safe_float(p.get("lat"), 9999)
            lon = safe_float(p.get("lon"), 9999)
            if bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lon <= bbox["east"]:
                out.append(p)
                if len(out) >= limit:
                    break
        return out

    def _inland_water_payload_for_bbox(self, bbox: dict[str, float] | None = None, *, source: str = "auto", geometry: str = "vector", lod: str = "auto", scene_tier: str | None = None, max_tiles: int = 24) -> dict[str, Any]:
        """Small local NHD/NHDPlus HR-style inland-water payload for a bbox/tile.

        This intentionally stays fast and local: it reads installed high-definition
        NHDPlus/3DHP/OSM-grade GeoJSON or json.gz tiles, then returns accepted
        polygons and flowlines. Coarse seed geometry is rejected.
        """
        if build_inland_water_payload is None:
            b = self._normalize_bbox(bbox)
            return {
                "ok": True,
                "status": "warming",
                "source": "inland_water_module_unavailable",
                "bbox": [b["west"], b["south"], b["east"], b["north"]],
                "polygons": [],
                "lines": [],
                "temperature_points": [],
                "count": 0,
                "payload_state": "module_unavailable",
            }
        return build_inland_water_payload(self.static_dir, self._normalize_bbox(bbox), source=source, geometry=geometry, lod=lod, scene_tier=scene_tier, max_tiles=max_tiles)

    def _empty_inland_water_shell(self, bbox: dict[str, float], reason: str = "cache_miss") -> dict[str, Any]:
        return {
            "ok": True,
            "status": "warming",
            "source": "inland_water_scene_tile_cache_warming",
            "payload_state": "warming",
            "bbox": [bbox["west"], bbox["south"], bbox["east"], bbox["north"]],
            "polygons": [],
            "lines": [],
            "temperature_points": [],
            "temperature_point_count": 0,
            "count": 0,
            "cache": {"hit": False, "product": "scene", "reason": reason},
            "contract": "lftr_inland_water_v1_tile_cache_shell",
        }

    def inland_water_tiles_payload(self, bbox: dict[str, float] | None = None, max_tiles: int = 24, visible_bbox: dict[str, float] | None = None, *, source: str = "auto", geometry: str = "vector", lod: str = "auto", scene_tier: str | None = None) -> dict[str, Any]:
        """Read local NHDPlus HR shoreline tile squares for the active viewport.

        Inland Waters is now independent from the generic scene-tile cache. The
        visible bbox selects installed NHDPlus HR json.gz tile squares; the route
        returns the union/full contents of those selected tiles. Missing manifests
        return immediately instead of waiting on background cache refresh or GFS providers.
        """
        norm = self._normalize_bbox(bbox)
        try:
            inland_limit = int(globals().get("INLAND_VIEW_TILE_LIMIT", 96) or 96)
            requested_limit = int(max_tiles or inland_limit)
            limit = max(1, min(requested_limit, inland_limit))
        except Exception:
            limit = int(globals().get("INLAND_VIEW_TILE_LIMIT", 96) or 96)
        direct = self._inland_water_payload_for_bbox(norm, source=source, geometry=geometry, lod=lod, scene_tier=scene_tier, max_tiles=limit)
        if isinstance(direct, dict):
            direct.setdefault("plan", {})
            direct["plan"].update({"requested_tile_squares": limit, "simple_tile_rule": f"active viewport bbox -> up to {limit} same-degree inland-water tiles", "selection_bbox": [norm["west"], norm["south"], norm["east"], norm["north"]], "visible_bbox": visible_bbox})
            direct["cache"] = {**(direct.get("cache") if isinstance(direct.get("cache"), dict) else {}), "mode": "local_nhdplus_full_tile_square_read", "selected_tiles": len(direct.get("selected_tiles") or [])}
            direct["tile_cache_contract"] = f"simple rule: visible/global viewport bbox -> up to {limit} active-LOD NHDPlus HR tile squares; disk cache 31 days; route view cache 120 seconds; no scene-cache/stale coarse merge"
        return direct


    def _bbox_has_probable_ocean_overlap(self, bbox: dict[str, float]) -> bool:
        b = self._normalize_bbox(bbox)
        west, east, south, north = b["west"], b["east"], b["south"], b["north"]
        # Coarse guardrail: skip HYCOM/boats for obvious inland southwest/desert/continental tiles.
        if east < -66 and west > -125 and south > 24 and north < 50:
            return False
        return True

    def _build_scene_tile_point_product(self, tile_id: str, bbox: dict[str, float], visible_bbox: dict[str, float] | None = None, allowed_layers: set[str] | None = None) -> dict[str, Any]:
        """Build the app-native per-tile point/cache product.

        This is the speed layer: NCSS/ERDDAP remain the source of truth, but the site
        can load this gzip JSON first. The product deliberately keeps the diverse
        payload contracts used by graphics layers: clouds, rain/precip columns,
        oceanPoints, baitAdvanced, boats, and fish markers.
        """
        started = time.time()
        bbox_norm = self._normalize_bbox(bbox)
        visible = self._normalize_bbox(visible_bbox or bbox_norm)
        scene = self.build_scene_plan(bbox_norm, visible, layer="scene-tile-cache")
        contracts: dict[str, Any] = {}
        errors: dict[str, str] = {}

        def _try(name: str, fn, *args):
            try:
                contracts[name] = fn(*args)
            except Exception as exc:
                level = log.info if isinstance(exc, FileNotFoundError) else log.warning
                level("[gfs tile cache] contract build failed tile=%s name=%s err=%s", tile_id, name, exc)
                contracts[name] = {"ok": False, "payload_state": "provider_failed", "source": f"{name}_unavailable", "error": str(exc), "bbox": bbox_norm}
                errors[name] = str(exc)

        requested_layers = {str(x).strip().lower() for x in (allowed_layers or set()) if str(x).strip()}
        all_layers = not requested_layers
        wants_clouds = all_layers or bool(requested_layers & {"clouds", "rain"})
        wants_ocean_points = all_layers or bool(requested_layers & {"currents", "current", "boats", "boater", "bait", "ocean", "oceanpoints"})
        wants_boats = all_layers or bool(requested_layers & {"boats", "boater"})
        wants_ocean_bait = all_layers or bool(requested_layers & {"bait", "ocean-bait", "baitadvanced"})
        wants_lightning = all_layers or "lightning" in requested_layers
        wants_inland = all_layers or bool(requested_layers & {"inland-water", "inlandwater", "inland"})

        if wants_clouds:
            _try("clouds", self._cloud_tiles_payload_heavy, bbox_norm, visible)
        else:
            contracts["clouds"] = {"ok": True, "payload_state": "not_requested", "source": "route_layer_policy", "items": [], "tiles": [], "cloud_regions": [], "precip_columns": [], "bbox": bbox_norm}

        if self._bbox_has_probable_ocean_overlap(bbox_norm) and (wants_ocean_points or wants_boats or wants_ocean_bait):
            if wants_ocean_points:
                _try("oceanPoints", self._ocean_points_payload_heavy, bbox_norm, "auto", visible)
            else:
                contracts["oceanPoints"] = {"ok": True, "payload_state": "not_requested", "source": "route_layer_policy", "points": [], "bbox": bbox_norm}
            if wants_boats:
                _try("boats", self._ocean_payload_heavy, bbox_norm, visible)
                try:
                    op = contracts.get("oceanPoints") if isinstance(contracts.get("oceanPoints"), dict) else {}
                    if isinstance(contracts.get("boats"), dict) and isinstance(op, dict):
                        contracts["boats"]["oceanPoints"] = op
                        contracts["boats"]["ocean_points"] = op.get("points") or contracts["boats"].get("ocean_points") or []
                        contracts["boats"]["ocean_point_count"] = len(op.get("points") or contracts["boats"].get("ocean_points") or [])
                except Exception:
                    pass
            else:
                contracts["boats"] = {"ok": True, "payload_state": "not_requested", "source": "route_layer_policy", "boats": [], "items": [], "bbox": bbox_norm}
            if wants_ocean_bait:
                _try("baitAdvanced", self._bait_advanced_payload_heavy, bbox_norm, visible)
                try:
                    op = contracts.get("oceanPoints") if isinstance(contracts.get("oceanPoints"), dict) else {}
                    if isinstance(contracts.get("baitAdvanced"), dict) and isinstance(op, dict):
                        contracts["baitAdvanced"]["sharedOceanPoints"] = op
                        contracts["baitAdvanced"]["shared_ocean_points"] = op.get("points") or []
                        contracts["baitAdvanced"]["shared_ocean_point_count"] = len(op.get("points") or [])
                        bait_obj = contracts["baitAdvanced"].get("bait")
                        if isinstance(bait_obj, dict):
                            bait_obj["shared_ocean_point_count"] = len(op.get("points") or [])
                            bait_obj.setdefault("meta", {})["shared_ocean_point_count"] = len(op.get("points") or [])
                            bait_obj.setdefault("meta", {})["shared_ocean_points_contract"] = "true_boat_shark_hud_ocean_points_attached_separately_from_dense_bait_rows"
                except Exception:
                    pass
            else:
                contracts["baitAdvanced"] = {"ok": True, "payload_state": "not_requested", "source": "route_layer_policy", "bait": {"polygons": [], "bait_score": []}, "bbox": bbox_norm}
        else:
            skip_reason = "no_ocean_overlap" if not self._bbox_has_probable_ocean_overlap(bbox_norm) else "not_requested"
            contracts["oceanPoints"] = {"ok": True, "payload_state": skip_reason, "source": f"hycom_skipped_{skip_reason}", "points": [], "bbox": bbox_norm}
            contracts["boats"] = {"ok": True, "payload_state": skip_reason, "source": f"boats_skipped_{skip_reason}", "boats": [], "items": [], "bbox": bbox_norm}
            contracts["baitAdvanced"] = {"ok": True, "payload_state": skip_reason, "source": f"ocean_bait_skipped_{skip_reason}", "bait": {"polygons": [], "bait_score": []}, "bbox": bbox_norm}
        if wants_lightning:
            _try("lightning", self.lightning_payload, bbox_norm, visible, 20)
        else:
            contracts["lightning"] = {"ok": True, "payload_state": "not_requested", "source": "route_layer_policy", "flashes": [], "regions": [], "bbox": bbox_norm}
        if wants_inland:
            _try("inlandWater", self._inland_water_payload_for_bbox, bbox_norm)
        else:
            contracts["inlandWater"] = self._empty_inland_water_shell(bbox_norm, "not_requested")
        markers = self._clip_markers_to_bbox(visible, limit=260)
        contracts["markers"] = markers

        clouds = contracts.get("clouds") if isinstance(contracts.get("clouds"), dict) else {}
        ocean_points = contracts.get("oceanPoints") if isinstance(contracts.get("oceanPoints"), dict) else {}
        bait = contracts.get("baitAdvanced") if isinstance(contracts.get("baitAdvanced"), dict) else {}
        boats = contracts.get("boats") if isinstance(contracts.get("boats"), dict) else {}
        lightning = contracts.get("lightning") if isinstance(contracts.get("lightning"), dict) else {}
        inland_water = contracts.get("inlandWater") if isinstance(contracts.get("inlandWater"), dict) else {}
        boat_items = boats.get("boats") or boats.get("items") or []
        bait_obj = bait.get("bait") if isinstance(bait.get("bait"), dict) else bait
        bait_polys = []
        if isinstance(bait_obj, dict):
            bait_polys = (bait_obj.get("polygons") or []) + (bait_obj.get("outer_polygons") or []) + (bait_obj.get("inner_polygons") or []) + (bait_obj.get("core_polygons") or [])

        quality = {name: self._source_quality_summary(val) for name, val in contracts.items() if isinstance(val, dict)}
        payload = {
            "ok": True,
            "schema": "lftr_scene_tile_point_cache_v1",
            "tile_id": tile_id,
            "bbox": bbox_norm,
            "visible_bbox": visible,
            "scene_plan": scene,
            "source": "app_native_scene_tile_cache",
            "cache_policy": "gzip_json_cache_first_large_diverse_contracts_live_refresh_second",
            "quality_policy": "live_ncss_erddap_or_explicit_empty_no_silent_mock_proxy",
            "contracts": contracts,
            # Compatibility mirrors for older frontend/debug tools.
            "clouds": clouds,
            "oceanPoints": ocean_points,
            "boats": boats,
            "baitAdvanced": bait,
            "lightning": lightning,
            "inlandWater": inland_water,
            "markers": markers,
            "quality": quality,
            "errors": errors,
            "summary": {
                "cloud_items": len(clouds.get("items") or clouds.get("tiles") or []),
                "cloud_regions": len(clouds.get("cloud_regions") or []),
                "precip_columns": len(clouds.get("precip_columns") or []),
                "ocean_points": len(ocean_points.get("points") or ocean_points.get("items") or []),
                "boat_count": len(boat_items),
                "bait_polygon_count": len(bait_polys),
                "inland_water_polygons": len(inland_water.get("polygons") or []),
                "inland_water_lines": len(inland_water.get("lines") or []),
                "inland_water_temp_points": len(inland_water.get("temperature_points") or []),
                "marker_count": len(markers),
            },
            "latency_ms": int((time.time() - started) * 1000),
            "updated_at": self._now_ms(),
        }
        return payload

    def _scene_tile_cache_placeholder(self, tile_id: str, bbox: dict[str, float], ttl_seconds: int = 300, reason: str = "cache_miss") -> dict[str, Any]:
        """Return a small JSON-safe tile shell without building live NCSS contracts.

        /gfs/api/tiles must be a fast cache read endpoint.  Missing tiles should not
        synchronously fetch/decode GFS/HYCOM/ERDDAP or the browser can hit nginx/
        worker timeouts.  The live build is handled by /gfs/api/cache/refresh.
        """
        return {
            "ok": True,
            "schema": "lftr_scene_tile_point_cache_v1_placeholder",
            "tile_id": tile_id,
            "bbox": bbox,
            "source": "scene_tile_cache_placeholder",
            "payload_state": "cache_miss",
            "mode": "cache_first_no_direct_provider_block",
            "cache": {"hit": False, "ttl_seconds": ttl_seconds, "product": "scene", "reason": reason},
            "contracts": {
                "clouds": {"ok": True, "source": "deferred_tile_cache", "payload_state": "warming", "items": [], "tiles": [], "cloud_regions": [], "precip_columns": []},
                "oceanPoints": {"ok": True, "source": "hycom_ocean_points_cache_warming", "payload_state": "warming", "points": []},
                "boats": {"ok": True, "source": "deferred_tile_cache", "payload_state": "warming", "boats": [], "items": []},
                "baitAdvanced": {"ok": True, "source": "cache_first", "mode": "cache_first_bait_warming", "payload_state": "warming", "bait": {"polygons": [], "bait_score": []}},
                "inlandWater": self._empty_inland_water_shell(bbox, reason),
                "markers": [],
            },
            "clouds": {"ok": True, "source": "deferred_tile_cache", "payload_state": "warming", "items": [], "tiles": [], "cloud_regions": [], "precip_columns": []},
            "oceanPoints": {"ok": True, "source": "hycom_ocean_points_cache_warming", "payload_state": "warming", "points": []},
            "boats": {"ok": True, "source": "deferred_tile_cache", "payload_state": "warming", "boats": [], "items": []},
            "baitAdvanced": {"ok": True, "source": "cache_first", "mode": "cache_first_bait_warming", "payload_state": "warming", "bait": {"polygons": [], "bait_score": []}},
            "inlandWater": self._empty_inland_water_shell(bbox, reason),
            "markers": [],
            "summary": {"cloud_items": 0, "cloud_regions": 0, "precip_columns": 0, "ocean_points": 0, "boat_count": 0, "bait_polygon_count": 0, "inland_water_polygons": 0, "inland_water_lines": 0, "inland_water_temp_points": 0, "marker_count": 0},
            "updated_at": self._now_ms(),
        }

    def tile_intel_payload(self, tile_id: str, ttl_seconds: int = 300, visible_bbox: dict[str, float] | None = None, allow_build: bool = True, allowed_layers: set[str] | None = None, bbox: dict[str, float] | None = None) -> dict[str, Any]:
        cached = self._read_scene_tile_cache(tile_id, "scene", ttl_seconds=ttl_seconds)
        if cached:
            return cached
        bbox = self._normalize_bbox(bbox) if bbox is not None else self._tile_bounds_by_id(tile_id)
        if not allow_build:
            return self._scene_tile_cache_placeholder(tile_id, bbox, ttl_seconds=ttl_seconds, reason="cache_miss_read_only")
        payload = self._build_scene_tile_point_product(tile_id, bbox, visible_bbox or bbox, allowed_layers=allowed_layers)
        payload["cache"] = {"hit": False, "ttl_seconds": ttl_seconds, "product": "scene"}
        return self._write_scene_tile_cache(tile_id, payload, "scene")

    def tiles_intel_payload(self, bbox: dict[str, float] | None = None, max_tiles: int = 512, visible_bbox: dict[str, float] | None = None) -> dict[str, Any]:
        """Return app-native scene-tile products cache-first for the current viewport.

        This keeps rich graphics contracts in the cache while still letting live NCSS/
        ERDDAP refresh happens behind the scenes via /cache/refresh.
        """
        try:
            limit = max(1, min(int(max_tiles or 576), 576))
        except Exception:
            limit = 576
        plan = self.tile_plan_payload(bbox, max_tiles=limit)
        selected = list(plan.get("tiles") or [])[:limit]
        tiles: list[dict[str, Any]] = []
        workers = max(1, min(int(os.getenv("GFS_TILE_READ_WORKERS", "6") or "16"), 24))

        def _read_one(item: dict[str, Any]) -> dict[str, Any]:
            try:
                # Read-only: never build live NCSS/ERDDAP contracts in the browser
                # tile request.  Missing tiles return a tiny warming shell while
                # /cache/refresh refreshes the gzip scene tile cache in the background.
                return self.tile_intel_payload(str(item.get("tile_id")), ttl_seconds=600, visible_bbox=visible_bbox, allow_build=False, bbox=item.get("bbox"))
            except Exception as exc:
                return {"ok": False, "tile_id": item.get("tile_id"), "bbox": item.get("bbox"), "error": str(exc), "payload_state": "read_failed"}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gfs-tile-read") as pool:
            futures = [pool.submit(_read_one, item) for item in selected]
            for fut in as_completed(futures):
                tiles.append(fut.result())
        tiles_by_id = {str(t.get("tile_id")): t for t in tiles if isinstance(t, dict)}
        ordered = [tiles_by_id.get(str(item.get("tile_id"))) for item in selected]
        ordered = [t for t in ordered if isinstance(t, dict)]
        return {
            "ok": True,
            "schema": "lftr_scene_tiles_point_cache_v2_cache_first_24x24",
            "cache_policy": "read_gzip_scene_tile_cache_only_no_direct_provider_block__live_refresh_via_cache_warm",
            "product_contract": "large_diverse_payload_contracts_preserved_per_tile",
            "plan": plan,
            "tiles": ordered,
            "count": len(ordered),
            "tile_read_workers": workers,
            "cache_hits": sum(1 for t in ordered if bool(((t.get("cache") or {}).get("hit")))),
            "cache_misses": sum(1 for t in ordered if str(t.get("payload_state") or "") == "cache_miss"),
            "ts": self._now_ms(),
        }

    def location_live_intel_payload(self, location_key: str) -> dict[str, Any]:
        """Small auto-refresh payload for the selected glass pane."""
        loc = self._find_fish_point(location_key)
        if not loc:
            return {"ok": False, "error": "location not found", "location_id": location_key, "ts": self._now_ms()}
        lat = safe_float(loc.get("lat"), 0.0)
        lon = safe_float(loc.get("lon"), 0.0)
        tile_id = self._tile_id_for_point(lat, lon)
        box = {"west": lon - 1.8, "south": lat - 1.8, "east": lon + 1.8, "north": lat + 1.8}
        tile = self.tile_intel_payload(tile_id, ttl_seconds=240)
        env = self.location_environment_payload(location_key)
        node = self.node_intelligence_payload(location_key)
        return {
            "ok": True,
            "schema": "lftr_selected_location_live_intel_v1",
            "location": loc,
            "tile_id": tile_id,
            "bbox": box,
            "tile_summary": tile.get("summary") or {},
            "environment": env,
            "node": node,
            "source_status": {
                "tile_cache_hit": bool((tile.get("cache") or {}).get("hit")),
                "tile_updated_at": tile.get("updated_at"),
                "environment_source": (env.get("marker_environment") or {}).get("source_tier") if isinstance(env, dict) else None,
                "auto_refresh": True,
            },
            "updated_at": self._now_ms(),
        }

    def config_payload(self) -> Dict[str, Any]:
        return self.config()

    def status_payload(self) -> Dict[str, Any]:
        return self.health()

    def diagnostics_payload(self) -> Dict[str, Any]:
        return self.tile_diagnostics_payload()

    def hazards_payload(self) -> Dict[str, Any]:
        scene_payload = self.get_scene_payload()
        scene = scene_payload.get("scene") if isinstance(scene_payload.get("scene"), dict) else {}
        return {
            "ok": True,
            "source": scene_payload.get("source", "unknown"),
            "payload_state": scene_payload.get("payload_state", "unknown"),
            "rain": scene_payload.get("precip_columns") or scene.get("precip") or [],
            "hail": scene_payload.get("hail_events") or scene.get("hail") or [],
            "lightning": scene_payload.get("lightning_events") or scene.get("lightning") or [],
            "ts": self._now_ms(),
        }

    def tile_layer_payload(self, layer: str, z: int, x: int, y: int, pad_deg: float = 0.18, debug: bool = False) -> Dict[str, Any]:
        return self.layer_tile_payload(layer=layer, z=z, x=x, y=y, pad_deg=pad_deg, debug=debug)

    def _extract_csv_reports(self, row: Dict[str, Any]) -> List[str]:
        """Return non-empty report_ columns in natural numeric order."""
        def sort_key(item: tuple[str, Any]) -> tuple[int, str]:
            key, _ = item
            m = re.search(r"report_(\d+)$", str(key or ""), re.I)
            return (int(m.group(1)) if m else 9999, str(key or ""))

        reports: List[str] = []
        for key, value in sorted(row.items(), key=sort_key):
            if not str(key or "").lower().startswith("report_"):
                continue
            text = str(value or "").strip()
            if text:
                reports.append(text)
        return reports

    def _find_fish_point(self, identifier: str) -> Dict[str, Any] | None:
        """Resolve a marker by canonical id, location_key, CSV id, or slug."""
        raw = str(identifier or "").strip()
        if not raw:
            return None
        norm = self._normalize_location_key(raw)
        points = list(self.state.fish_points or [])
        if not points:
            points, _ = self.load_fish()
        for point in points:
            candidates = {
                str(point.get("id") or ""),
                str(point.get("location_key") or ""),
                str(point.get("csv_id") or ""),
                self._normalize_location_key(str(point.get("name") or "")),
            }
            if raw in candidates or norm in {self._normalize_location_key(c) for c in candidates if c}:
                return point
        return None

    def load_fish(self) -> Tuple[List[Dict[str, Any]], str | None]:
        csv_path = self._fish_csv_path()
        if not csv_path.exists():
            candidates = ", ".join(str(p) for p in self._fish_csv_candidates())
            self.state.last_error = f"missing fish CSV: {csv_path}; searched: {candidates}"
            return [], self.state.last_error

        points: List[Dict[str, Any]] = []
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for i, row in enumerate(reader):
                    if not row:
                        continue

                    lat_raw = row.get("lat") or row.get("latitude") or row.get("Lat") or row.get("Latitude")
                    lon_raw = row.get("lon") or row.get("lng") or row.get("longitude") or row.get("Lon") or row.get("Longitude")
                    if lat_raw is None or lon_raw is None:
                        continue

                    try:
                        lat = float(str(lat_raw).strip())
                        lon = float(str(lon_raw).strip())
                    except Exception:
                        continue
                    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        continue

                    name = (row.get("name") or row.get("location") or row.get("label") or f"Location {i + 1}").strip()
                    raw_key = (row.get("location_key") or row.get("id") or row.get("locationId") or name or str(i + 1)).strip()
                    location_key = self._normalize_location_key(raw_key) or f"loc-{i + 1}"
                    csv_id = (row.get("id") or row.get("locationId") or str(i + 1)).strip()
                    reports = self._extract_csv_reports(row)
                    latest_report = reports[-1] if reports else ""
                    excluded = {
                        "lat", "latitude", "Lat", "Latitude",
                        "lon", "lng", "longitude", "Lon", "Longitude",
                        "name", "location", "label", "id", "locationId", "location_key",
                    }
                    point = {
                        # Canonical marker/API identifier.  The frontend calls
                        # /gfs/api/intelligence/node/<id> with this value.
                        "id": location_key,
                        "location_id": location_key,
                        "location_key": location_key,
                        "csv_id": csv_id,
                        "name": name,
                        "lat": lat,
                        "lon": lon,
                        "reports": reports,
                        "all_reports": reports,
                        "last_report": latest_report,
                        "report_count": len(reports),
                        "meta": {
                            k: v
                            for k, v in row.items()
                            if k and k not in excluded and not str(k).lower().startswith("report_")
                        },
                    }
                    point.update(self._build_bait_intel(point, self._now_ms()))
                    points.append(point)

            self.state.fish_points = points
            self.state.last_refresh_ts = self._now_ms()
            self.state.last_error = None
            return points, None
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"failed to parse fish CSV: {exc}"
            return [], self.state.last_error

    def fish_payload(self) -> Dict[str, Any]:
        points, err = self.load_fish()
        return {"ok": err is None, "error": err, "count": len(points), "items": points, "ts": self._now_ms()}

    def _point_in_request_bbox(self, point: Dict[str, Any], bbox: dict[str, float]) -> bool:
        try:
            lat = float(point.get("lat"))
            lon = float(point.get("lon"))
            return float(bbox.get("south", -90)) <= lat <= float(bbox.get("north", 90)) and float(bbox.get("west", -180)) <= lon <= float(bbox.get("east", 180))
        except Exception:
            return False

    def _regional_marker_frame_payload(self, bbox: dict[str, float], points: list[Dict[str, Any]], now_ms: int) -> Dict[str, Any]:
        selected = [p for p in points if self._point_in_request_bbox(p, bbox)] or points[:43]
        boats = []
        bait_score = []
        for pnt in selected[:80]:
            try:
                lat = float(pnt.get("lat")); lon = float(pnt.get("lon"))
            except Exception:
                continue
            intel = pnt if pnt.get("marker_environment") else {**pnt, **self._build_bait_intel(pnt, now_ms)}
            marker_env = intel.get("marker_environment") or {}
            ocean = marker_env.get("ocean") or {}
            boat = ocean.get("boat") or marker_env.get("boating")
            if isinstance(boat, dict):
                boats.append(boat)
            bait = intel.get("bait") or {}
            try:
                profile = build_location_profile(pnt, [])
            except Exception:
                profile = {}
            habitat_key = str(profile.get("habitat_key") or "").lower()
            ocean_like = habitat_key not in {"freshwater", "inland", "lake", "river"}
            bait_score.append({
                "lat": lat,
                "lon": lon,
                "probability": round(float(bait.get("presence_probability") or 0.35), 3),
                "preferred_depth_m": round(float((bait.get("school_depth_band_ft") or [25, 65])[0]) * 0.3048, 1),
                "depth_min_m": round(float((bait.get("school_depth_band_ft") or [25, 65])[0]) * 0.3048, 1),
                "depth_max_m": round(float((bait.get("school_depth_band_ft") or [25, 65])[-1]) * 0.3048, 1),
                "driver": "marker_history_ocean_proxy",
                "source": "fish_csv_marker_intelligence",
                "habitat_key": habitat_key or None,
                "waterbody": profile.get("waterbody"),
                "ocean_like": ocean_like,
                "water_mask_source": "marker_waterbody_profile_until_hycom_bait_grid",
            })
        avg_prob = sum(float(b.get("probability") or 0.0) for b in bait_score) / max(1, len(bait_score))
        return {
            "boats": {"ok": True, "boats": boats, "count": len(boats), "source": "marker_ocean_solve", "sparse_fallback": False},
            "baitAdvanced": {
                "ok": True,
                "schema": "bait_ocean_field_v1",
                "bait": {"status": "ok", "source": "marker_history_ocean_proxy", "meta": {"valid_cells": len(bait_score), "source": "fish_csv_marker_intelligence"}},
                "bait_score": bait_score,
                "front_lines": [],
                "convergence_polygons": [],
                "boil_probability_polygons": [],
                "confidence": {"overall": round(avg_prob, 3)},
                "valid_time": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
            },
        }


    def _split_cache_get(self, key: str, ttl_seconds: int) -> Any:
        if not hasattr(self, "_split_payload_cache"):
            self._split_payload_cache = {}
        row = self._split_payload_cache.get(key)
        if not row:
            return None
        if (time.time() - float(row.get("time", 0))) > ttl_seconds:
            return None
        return row.get("payload")

    def _split_cache_set(self, key: str, payload: Any) -> Any:
        if not hasattr(self, "_split_payload_cache"):
            self._split_payload_cache = {}
        self._split_payload_cache[key] = {"time": time.time(), "payload": payload}
        if str(key).startswith("scene_cache:"):
            self._split_scene_cache_janitor_locked(reason="write")
        return payload

    def _split_scene_cache_janitor_locked(self, reason: str = "manual") -> dict[str, Any]:
        """Keep scene cache small, cache-first, and quality-forward.

        Provider caches store raw/live data. Scene cache stores renderer-ready
        payloads. This method is now a thin wrapper around ``cache_policy`` so the
        old split/scene cache names obey one policy instead of drifting apart.
        """
        if not hasattr(self, "_split_payload_cache"):
            self._split_payload_cache = {}
        cache = self._split_payload_cache
        max_rows = max(16, int(SCENE_CACHE_MAX_MEMORY_ROWS))
        max_age = max(600, int(SCENE_CACHE_MAX_AGE_SECONDS))
        result = janitor_scene_rows(cache, max_rows=max_rows, max_age_seconds=max_age)
        removed = result.get("removed") or []
        return {
            "ok": True,
            "schema": "lftr_scene_cache_janitor_v2_single_policy",
            "reason": reason,
            "policy": "provider_cache_raw_scene_cache_render_ready_trim_stale_warming_and_low_quality_duplicates",
            "max_rows": max_rows,
            "removed_count": len(removed),
            "removed": removed[:25],
            "rows_remaining": result.get("rows_remaining", 0),
            "ts": self._now_ms(),
        }

    def _bbox_cache_key(self, prefix: str, bbox: dict[str, float]) -> str:
        b = self._normalize_bbox(bbox)
        rounded = [round(float(b[k]), 2) for k in ("west", "south", "east", "north")]
        return f"{prefix}:{rounded[0]},{rounded[1]},{rounded[2]},{rounded[3]}"

    def _bbox_center(self, bbox: dict[str, float]) -> tuple[float, float]:
        b = self._normalize_bbox(bbox)
        return ((float(b["south"]) + float(b["north"])) / 2.0, (float(b["west"]) + float(b["east"])) / 2.0)

    def _source_row(self, role: str, provider: str, function: str, url: str | None, *, engine: str = "", ttl_seconds: int = 0, status: str = "configured", details: str = "", variables: list[str] | None = None) -> Dict[str, Any]:
        return {
            "role": role,
            "provider": provider,
            "function": function,
            "engine": engine,
            "ttl_seconds": ttl_seconds,
            "status": status,
            "url": url,
            "variables": variables or [],
            "details": details,
        }


    def _resolution_truth_contract(self, bbox: dict[str, Any] | None = None, stride: int | None = None, source_resolution_deg: float | None = None, derived_resolution_deg: float | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        source_res = float(source_resolution_deg if source_resolution_deg is not None else 0.25)
        stride_val = max(1, int(stride or 1))
        contract = {
            "source_truth_first": True,
            "cache_first": True,
            "source_resolution_deg": source_res,
            "stride": stride_val,
            "viewport_fetch_bbox": bbox,
            "derived_render_geometry": True,
            "derived_resolution_deg": float(derived_resolution_deg if derived_resolution_deg is not None else max(source_res / stride_val, source_res / 8.0)),
            "policy": "use live source-resolution viewport fetches as truth, cache them, then derive smooth render geometry from that truth",
        }
        if isinstance(extra, dict):
            contract.update(extra)
        return contract

    def _attach_truth_contract(self, payload: dict[str, Any], *, bbox: dict[str, Any] | None = None, stride: int | None = None, source_resolution_deg: float | None = None, derived_resolution_deg: float | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        payload.setdefault("source_resolution_deg", float(source_resolution_deg if source_resolution_deg is not None else payload.get("source_resolution_deg") or 0.25))
        payload.setdefault("stride", max(1, int(stride or payload.get("stride") or payload.get("provider_stride") or ((payload.get("scene_plan") or {}).get("provider_stride") if isinstance(payload.get("scene_plan"), dict) else 1) or 1)))
        if derived_resolution_deg is not None and payload.get("derived_resolution_deg") is None:
            payload["derived_resolution_deg"] = float(derived_resolution_deg)
        payload["truth_contract"] = self._resolution_truth_contract(
            bbox=bbox or payload.get("bbox") or payload.get("bbox_object") or payload.get("requested_bbox"),
            stride=payload.get("stride"),
            source_resolution_deg=payload.get("source_resolution_deg"),
            derived_resolution_deg=payload.get("derived_resolution_deg"),
            extra=extra,
        )
        return payload

    def payload_debug_payload(self, bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        """Full layer/source contract status for the browser debug panel.

        This endpoint does not hide failures. It reports which payloads arrived,
        which variables were requested/selected, shapes, timings, drawable counts,
        and exact failure diagnostics so the next error can be followed from the UI.
        """
        b = self._normalize_bbox(bbox)
        started = time.time()
        out: dict[str, Any] = {"ok": True, "bbox": b, "ts": self._now_ms(), "layers": {}, "providers": {}, "latency_ms": 0}
        out["truth_contract"] = self._resolution_truth_contract(bbox=b, stride=1, source_resolution_deg=0.25, derived_resolution_deg=0.03125)
        try:
            w = self.generate_weather_payload(b)
            fields = w.get("fields") or {}
            gfs_stride = max(1, int(w.get("stride") or (((w.get("scene_plan") or {}).get("provider_stride")) if isinstance(w.get("scene_plan"), dict) else 1) or 1))
            out["providers"]["gfs_nomads"] = {
                "ok": w.get("source") == "gfs_nomads",
                "source": w.get("source"),
                "payload_state": w.get("payload_state"),
                "cycle": w.get("cycle"),
                "forecast_hour": w.get("forecast_hour"),
                "valid_time": w.get("valid_time"),
                "source_url": w.get("source_url"),
                "cache_path": w.get("cache_path"),
                "source_resolution_deg": float(w.get("source_resolution_deg") or 0.25),
                "stride": gfs_stride,
                "derived_resolution_deg": round(float(w.get("source_resolution_deg") or 0.25) / 8.0, 6),
                "fields_available": w.get("fields_available") or self.state.fields_available,
                "fields_missing": w.get("fields_missing") or self.state.fields_missing,
                "field_shapes": {k: (list(np.asarray(v).shape) if np is not None and v is not None else []) for k, v in fields.items() if k in {"wind_u", "wind_v", "wind_speed", "precip_rate", "cloud_density", "temp2m", "mslp"}},
            }
            out["layers"]["clouds"] = {
                "data_came": bool(w.get("tiles") or w.get("items")),
                "drawable_count": len(w.get("tiles") or w.get("items") or []),
                "rain_count": (w.get("rain") or {}).get("count", 0),
                "balloon_vectors": (w.get("balloons") or {}).get("count", 0),
                "has_uv_fields_for_jetstream": bool(fields.get("wind_u") and fields.get("wind_v")),
                "source_resolution_deg": float(w.get("source_resolution_deg") or 0.25),
                "stride": gfs_stride,
                "derived_resolution_deg": round(float(w.get("source_resolution_deg") or 0.25) / 8.0, 6),
            }
        except Exception as exc:
            out["providers"]["gfs_nomads"] = {"ok": False, "error": str(exc)}
        try:
            ocean = self.ocean_points_payload(b, "auto")
            meta = ocean.get("source_meta") or {}
            out["providers"]["hycom"] = {
                "ok": bool(ocean.get("ok")),
                "source": ocean.get("source"),
                "count": ocean.get("count"),
                "stride": ocean.get("stride"),
                "source_resolution_deg": 0.0,
                "derived_resolution_deg": None,
                "selected_attempt": meta.get("selected_attempt"),
                "selected_dataset": meta.get("selected_dataset"),
                "selected_vars": meta.get("selected_vars"),
                "selected_u_var": meta.get("selected_u_var"),
                "selected_v_var": meta.get("selected_v_var"),
                "hycom_slices": meta.get("hycom_slices"),
                "hycom_lon_diagnostics": meta.get("hycom_lon_diagnostics"),
                "grid_shape": meta.get("grid_shape") or (ocean.get("grid") or {}).get("grid_shape"),
                "real_subset": meta.get("real_subset"),
                "diagnostics": meta.get("diagnostics"),
                "debug_previews": meta.get("debug_previews"),
                "error": ocean.get("error"),
            }
            out["layers"]["ocean_points"] = {"data_came": bool(ocean.get("points")), "drawable_count": len(ocean.get("points") or []), "mask": ocean.get("mask"), "stride": ocean.get("stride")}
        except Exception as exc:
            out["providers"]["hycom"] = {"ok": False, "error": str(exc)}
        try:
            bait = self.bait_advanced_payload(b)
            bait_obj = bait.get("bait") or {}
            out["layers"]["bait"] = {
                "data_came": True,
                "status": bait_obj.get("status"),
                "source": bait_obj.get("source") or bait.get("source"),
                "polygon_count": len(bait_obj.get("polygons") or []),
                "outer_count": len(bait_obj.get("outer_polygons") or []),
                "inner_count": len(bait_obj.get("inner_polygons") or []),
                "stride": bait.get("stride") or ((bait.get("scene_plan") or {}).get("provider_stride") if isinstance(bait.get("scene_plan"), dict) else None),
                "source_resolution_deg": bait.get("source_resolution_deg") or 0.25,
                "derived_resolution_deg": bait.get("derived_resolution_deg"),
                "core_count": len(bait_obj.get("core_polygons") or []),
                "bait_score_rows": len(bait.get("bait_score") or []),
                "ocean_point_rows": len(bait.get("ocean_points") or []),
                "mode": bait.get("mode"),
            }
        except Exception as exc:
            out["layers"]["bait"] = {"data_came": False, "error": str(exc)}
        try:
            boats = self.boats_payload(b)
            out["layers"]["boats"] = {
                "data_came": True,
                "raw": boats.get("count"),
                "renderable_hint": boats.get("renderable_count_hint"),
                "fallback_rejected": boats.get("fallback_rejected_count_hint"),
                "source": boats.get("source"),
                "mode": boats.get("mode"),
                "contract": boats.get("render_contract"),
            }
        except Exception as exc:
            out["layers"]["boats"] = {"data_came": False, "error": str(exc)}
        try:
            cloud_truth = out.get("layers", {}).get("clouds", {})
            out["resolution_truth"] = self._resolution_truth_contract(
                bbox=b,
                stride=cloud_truth.get("stride") or 1,
                source_resolution_deg=cloud_truth.get("source_resolution_deg") or 0.25,
                derived_resolution_deg=cloud_truth.get("derived_resolution_deg") or 0.03125,
                extra={
                    "debug_payload": True,
                    "source_grid_shape": out.get("providers", {}).get("gfs_nomads", {}).get("field_shapes", {}).get("wind_u"),
                },
            )
        except Exception:
            out["resolution_truth"] = out.get("truth_contract")
        out["latency_ms"] = int((time.time() - started) * 1000)
        return out

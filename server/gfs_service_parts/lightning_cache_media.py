from __future__ import annotations
import os

import server.gfs_service as _svc
globals().update({k: v for k, v in vars(_svc).items() if not k.startswith("__")})


class LightningCacheMediaMixin:
    def _lightning_cache_path(self, bbox: dict[str, float] | None = None, product: str = "goes_glm") -> Path:
        b = self._normalize_bbox(bbox)
        sig = f"{b.get('west',0):.2f}_{b.get('south',0):.2f}_{b.get('east',0):.2f}_{b.get('north',0):.2f}"
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sig)[:96]
        path = DEFAULT_GFS_CACHE_DIR / "lightning"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{product}_{safe}.json.gz"

    def _read_lightning_cache(self, bbox: dict[str, float] | None = None, ttl_seconds: int = 90) -> dict[str, Any] | None:
        path = self._lightning_cache_path(bbox)
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
                payload["cache"].update({"hit": True, "age_seconds": round(age, 3), "path": str(path)})
                payload.setdefault("payload_state", "cache_hit")
                return payload
        except Exception as exc:
            log.debug("lightning cache read skipped err=%s", exc)
        return None

    def _write_lightning_cache(self, bbox: dict[str, float] | None, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._lightning_cache_path(bbox)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            import gzip
            safe_payload = self._json_safe_for_tile_cache(payload)
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
                json.dump(safe_payload, fh, separators=(",", ":"), ensure_ascii=False)
            tmp.replace(path)
        except Exception as exc:
            log.debug("lightning cache write skipped err=%s", exc)
        return payload

    def _goes_glm_s3_bases(self) -> list[tuple[str, str]]:
        # GOES-19 is current GOES-East; GOES-18 is current GOES-West.  These
        # buckets are public NOAA Open Data buckets.  Override with
        # GFS_GLM_SATELLITES="19,18" if needed.
        sats = os.getenv("GFS_GLM_SATELLITES", "19,18").replace("GOES", "").replace("goes", "")
        out: list[tuple[str, str]] = []
        for raw in re.split(r"[,;\s]+", sats):
            sat = raw.strip().lstrip("0")
            if not sat:
                continue
            if sat not in {"16", "17", "18", "19"}:
                continue
            out.append((f"GOES-{sat}", f"https://noaa-goes{sat}.s3.amazonaws.com"))
        return out or [("GOES-19", "https://noaa-goes19.s3.amazonaws.com"), ("GOES-18", "https://noaa-goes18.s3.amazonaws.com")]

    def _list_goes_glm_keys(self, base: str, when: datetime, limit: int = 12) -> list[str]:
        # NOAA S3 prefix example: GLM-L2-LCFA/2026/152/05/
        prefix = f"GLM-L2-LCFA/{when:%Y}/{when.timetuple().tm_yday:03d}/{when:%H}/"
        url = f"{base}/?list-type=2&prefix={prefix}"
        try:
            r = requests.get(url, timeout=(4, 12), headers={"User-Agent": DEFAULT_UA})
            if r.status_code != 200:
                return []
            import xml.etree.ElementTree as ET
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            root = ET.fromstring(r.text)
            keys = []
            for node in root.findall(".//s3:Key", ns):
                txt = (node.text or "").strip()
                if txt.endswith(".nc") and "GLM-L2-LCFA" in txt:
                    keys.append(txt)
            return sorted(keys)[-max(1, int(limit)):]
        except Exception as exc:
            log.debug("GOES GLM list skipped url=%s err=%s", url, exc)
            return []

    def _download_goes_glm_nc(self, base: str, key: str) -> Path | None:
        cache_dir = DEFAULT_GFS_CACHE_DIR / "lightning" / "glm_nc"
        cache_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[-160:]
        path = cache_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
        try:
            url = f"{base}/{key}"
            r = requests.get(url, timeout=(5, 30), headers={"User-Agent": DEFAULT_UA})
            if r.status_code != 200 or not r.content:
                return None
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(path)
            return path
        except Exception as exc:
            log.debug("GOES GLM download skipped key=%s err=%s", key, exc)
            return None

    def _read_goes_glm_flashes(self, path: Path, platform: str, bbox: dict[str, float], now: datetime) -> list[dict[str, Any]]:
        if xr is None:
            return []
        try:
            ds = xr.open_dataset(path, engine="netcdf4")
        except Exception:
            try:
                ds = xr.open_dataset(path)
            except Exception as exc:
                log.debug("GOES GLM nc open skipped path=%s err=%s", path, exc)
                return []
        try:
            lat_name = "flash_lat" if "flash_lat" in ds.variables else None
            lon_name = "flash_lon" if "flash_lon" in ds.variables else None
            if not lat_name or not lon_name:
                return []
            lats = ds[lat_name].values
            lons = ds[lon_name].values
            energies = ds["flash_energy"].values if "flash_energy" in ds.variables else [None] * len(lats)
            areas = ds["flash_area"].values if "flash_area" in ds.variables else [None] * len(lats)
            times = None
            for tname in ("flash_time_offset_of_first_event", "flash_time_offset_of_last_event", "flash_time_offset_of_first_event"):
                if tname in ds.variables:
                    times = ds[tname].values
                    break
            out = []
            west, south, east, north = [float(bbox[k]) for k in ("west", "south", "east", "north")]
            for i in range(min(len(lats), int(os.getenv("GFS_GLM_MAX_FLASHES_PER_FILE", "1200")))):
                try:
                    lat = float(lats[i]); lon = float(lons[i])
                    if not (south <= lat <= north and west <= lon <= east):
                        continue
                    energy = float(energies[i]) if energies is not None and energies[i] is not None else None
                    area = float(areas[i]) if areas is not None and areas[i] is not None else None
                    # GLM time variable is seconds relative to dataset epoch; xarray
                    # often decodes it to timedelta64. Keep a safe string if possible.
                    tstr = None; age = None
                    try:
                        tv = times[i] if times is not None else None
                        if tv is not None:
                            tstr = str(tv)
                    except Exception:
                        pass
                    out.append({
                        "lat": round(lat, 5), "lon": round(lon, 5),
                        "energy_j": energy, "area_m2": area,
                        "time": tstr, "valid_time": tstr,
                        "age_seconds": age if age is not None else 0,
                        "satellite": platform, "platform": platform,
                        "source": "goes_glm_l2_lcfa", "product": "GLM-L2-LCFA",
                    })
                except Exception:
                    continue
            return out
        finally:
            try: ds.close()
            except Exception: pass

    def _cluster_lightning_regions(self, flashes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for f in flashes:
            try:
                key = (int(float(f.get("lat")) * 2), int(float(f.get("lon")) * 2))  # ~0.5 degree cells
                buckets.setdefault(key, []).append(f)
            except Exception:
                continue
        regions = []
        for _key, items in buckets.items():
            if not items:
                continue
            lat = sum(float(x.get("lat")) for x in items) / len(items)
            lon = sum(float(x.get("lon")) for x in items) / len(items)
            energy = sum(float(x.get("energy_j") or 0.0) for x in items)
            regions.append({
                "center": {"lat": round(lat, 5), "lon": round(lon, 5)},
                "flash_count": len(items),
                "energy_j_sum": energy,
                "age_seconds": min(float(x.get("age_seconds") or 0.0) for x in items),
                "source": "goes_glm_l2_lcfa",
            })
        return sorted(regions, key=lambda r: int(r.get("flash_count") or 0), reverse=True)[:80]

    def _lightning_cache_shell(self, visible: dict[str, float], scene: dict[str, Any], minutes: int, reason: str = "queued_background_warm") -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return self._attach_scene_plan({
            "ok": True,
            "schema": "lftr_lightning_v1",
            "source": "goes_glm_l2_lcfa_cache_first",
            "payload_state": "warming",
            "mode": "stale_while_revalidate_no_direct_provider_block",
            "valid_window_minutes": minutes,
            "bbox": [visible["west"], visible["south"], visible["east"], visible["north"]],
            "bbox_used": [visible["west"], visible["south"], visible["east"], visible["north"]],
            "flashes": [],
            "items": [],
            "regions": [],
            "count": 0,
            "fallback_used": False,
            "mock": False,
            "proxy": False,
            "quality": {"live_goes_glm": False, "fallback_used": False, "mock": False, "proxy": False},
            "cache": {"hit": False, "mode": "queued_background_warm", "ttl_seconds": int(os.getenv("GFS_GLM_CACHE_TTL_SECONDS", "180") or "180"), "reason": reason},
            "diagnostics": {"note": "Lightning is cache-first; slow GOES/GLM provider work is running off the browser request path."},
            "generated_at": now.isoformat(),
        }, scene)

    def _schedule_lightning_warm(self, requested: dict[str, float], visible: dict[str, float], minutes: int, scene: dict[str, Any]) -> None:
        if os.getenv("GFS_DISABLE_GLM", "false").strip().lower() in {"1", "true", "yes", "on"}:
            return
        if not hasattr(self, "_lightning_warm_inflight"):
            self._lightning_warm_inflight = set()
        key = self._bbox_cache_key("lightning", visible) + f":{int(minutes or 20)}"
        if key in self._lightning_warm_inflight:
            return
        self._lightning_warm_inflight.add(key)

        def _run() -> None:
            try:
                self._lightning_payload_live(requested, visible, minutes)
            except Exception as exc:
                log.warning("[gfs lightning] deferred warm failed key=%s err=%s", key, exc)
            finally:
                try:
                    self._lightning_warm_inflight.discard(key)
                except Exception:
                    pass

        threading.Thread(target=_run, name="gfs-lightning-warm", daemon=True).start()

    def lightning_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, minutes: int = 20) -> dict[str, Any]:
        requested = self._normalize_bbox(bbox)
        visible = self._normalize_bbox(visible_bbox or requested)
        scene = self.build_scene_plan(requested, visible, layer="lightning")
        ttl = int(os.getenv("GFS_GLM_CACHE_TTL_SECONDS", "180") or "180")
        cached = self._read_lightning_cache(visible, ttl_seconds=ttl)
        if cached:
            cached["payload_state"] = "cache_hit_live_recent"
            cached["cache"] = {"hit": True, "mode": "fresh_lightning_cache", "ttl_seconds": ttl}
            cached["scene_plan"] = scene
            return cached
        stale = self._read_lightning_cache(visible, ttl_seconds=-1)
        self._schedule_lightning_warm(requested, visible, minutes, scene)
        if stale:
            stale["payload_state"] = "stale_while_revalidate"
            stale["cache"] = {"hit": True, "mode": "stale_while_revalidate", "ttl_seconds": ttl}
            stale["scene_plan"] = scene
            return stale
        return self._lightning_cache_shell(visible, scene, minutes)

    def _lightning_payload_live(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, minutes: int = 20) -> dict[str, Any]:
        requested = self._normalize_bbox(bbox)
        visible = self._normalize_bbox(visible_bbox or requested)
        scene = self.build_scene_plan(requested, visible, layer="lightning")
        ttl = int(os.getenv("GFS_GLM_CACHE_TTL_SECONDS", "60"))
        cached = self._read_lightning_cache(visible, ttl_seconds=ttl)
        if cached:
            cached["payload_state"] = "cache_hit_live_recent"
            cached["scene_plan"] = scene
            return cached
        if os.getenv("GFS_DISABLE_GLM", "false").strip().lower() in {"1", "true", "yes", "on"}:
            return self._attach_scene_plan({
                "ok": False, "schema": "lftr_lightning_v1", "source": "goes_glm_disabled",
                "payload_state": "disabled", "flashes": [], "items": [], "regions": [],
                "fallback_used": False, "mock": False, "proxy": False,
            }, scene)
        now = datetime.now(timezone.utc)
        flashes: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        hours = [now - timedelta(hours=h) for h in range(0, max(1, int(math.ceil(max(1, minutes) / 60)) + 2))]
        per_hour_keys = int(os.getenv("GFS_GLM_KEYS_PER_HOUR", "8"))
        for platform, base in self._goes_glm_s3_bases():
            before = len(flashes)
            for wh in hours:
                keys = self._list_goes_glm_keys(base, wh, limit=per_hour_keys)
                diagnostics.append({"platform": platform, "hour": wh.strftime("%Y-%j-%H"), "keys": len(keys)})
                for key in keys:
                    path = self._download_goes_glm_nc(base, key)
                    if not path:
                        continue
                    flashes.extend(self._read_goes_glm_flashes(path, platform, visible, now))
                    if len(flashes) >= int(os.getenv("GFS_GLM_MAX_FLASHES", "1500")):
                        break
                if len(flashes) >= int(os.getenv("GFS_GLM_MAX_FLASHES", "1500")):
                    break
            diagnostics[-1:]
            diagnostics.append({"platform": platform, "flashes_added": len(flashes) - before})
        # De-duplicate near-identical flashes.
        seen = set(); unique = []
        for f in flashes:
            key = (round(float(f.get("lat") or 0), 3), round(float(f.get("lon") or 0), 3), str(f.get("satellite")))
            if key in seen:
                continue
            seen.add(key); unique.append(f)
        unique = unique[: int(os.getenv("GFS_GLM_MAX_FLASHES", "1500"))]
        payload = {
            "ok": True,
            "schema": "lftr_lightning_v1",
            "source": "goes_glm_l2_lcfa",
            "payload_state": "live" if unique else "provider_empty",
            "valid_window_minutes": minutes,
            "bbox": [visible["west"], visible["south"], visible["east"], visible["north"]],
            "bbox_used": [visible["west"], visible["south"], visible["east"], visible["north"]],
            "flashes": unique,
            "items": unique,
            "regions": self._cluster_lightning_regions(unique),
            "count": len(unique),
            "fallback_used": False, "mock": False, "proxy": False,
            "quality": {"live_goes_glm": bool(unique), "fallback_used": False, "mock": False, "proxy": False},
            "diagnostics": {"providers": diagnostics, "satellites": [p for p, _ in self._goes_glm_s3_bases()], "note": "GOES GLM Level 2 LCFA flash_lat/flash_lon filtered to visible bbox"},
            "generated_at": now.isoformat(),
        }
        payload = self._attach_scene_plan(payload, scene)
        # Cache both positive and provider-empty GLM results.  Empty-but-fresh is
        # still valuable; without this, every browser refresh can re-trigger a
        # slow GOES/GLM scan that returns no flashes after tens of seconds.
        self._write_lightning_cache(visible, payload)
        return payload

    def note_viewport_priority(self, bbox: dict[str, float] | None = None, reason: str = "viewport") -> dict[str, Any]:
        """Remember a browser viewport as high priority without blocking."""
        b = self._normalize_bbox(bbox)
        b["noted_at"] = self._now_ms()
        b["reason"] = reason
        recent = [x for x in getattr(self, "_recent_viewports", []) if isinstance(x, dict)]
        sig = tuple(round(float(b[k]), 2) for k in ("west", "south", "east", "north"))
        filtered = []
        for old in recent:
            try:
                old_sig = tuple(round(float(old[k]), 2) for k in ("west", "south", "east", "north"))
                if old_sig == sig:
                    continue
            except Exception:
                pass
            filtered.append(old)
        self._recent_viewports = [b] + filtered[:11]
        return {"ok": True, "viewport_bbox": self._normalize_bbox(b), "cache_bbox": self._bounded_work_bbox(b), "reason": reason}

    def _default_boot_bboxes(self) -> list[dict[str, float]]:
        """Priority bboxes for no-browser always-on warming."""
        env = os.getenv("GFS_ALWAYS_ON_BOOT_BBOXES", "").strip()
        out: list[dict[str, float]] = []
        if env:
            for chunk in env.split(";"):
                try:
                    w, s, e, n = [float(x.strip()) for x in chunk.split(",")[:4]]
                    out.append({"west": w, "south": s, "east": e, "north": n})
                except Exception:
                    continue
        if out:
            return out[:12]
        try:
            points, _ = self.load_fish()
            pts = []
            for p in points[:80]:
                lat = safe_float(p.get("lat"), 999)
                lon = safe_float(p.get("lon"), 999)
                if -80 <= lat <= 80 and -180 <= lon <= 180:
                    pts.append((lat, lon))
            if pts:
                lats = [p[0] for p in pts]
                lons = [p[1] for p in pts]
                out.append({"west": max(-180, min(lons) - 1.0), "south": max(-80, min(lats) - 1.0), "east": min(180, max(lons) + 1.0), "north": min(80, max(lats) + 1.0)})
        except Exception:
            pass
        out.append({"west": -121.0, "south": 31.5, "east": -116.0, "north": 35.5})
        return out[:6]

    def start_always_on_cache(self) -> dict[str, Any]:
        """Start the server-resident 5-minute cache warmer daemon."""
        state = getattr(self, "_always_on_cache_state", {})
        if not state.get("enabled", True):
            return {"ok": True, "started": False, "reason": "disabled", "cache": state}
        if self._always_on_cache_started:
            return {"ok": True, "started": False, "reason": "already_started", "cache": state}
        self._always_on_cache_started = True
        state["running"] = True
        state["started_at"] = self._now_ms()
        def _loop() -> None:
            interval = max(60, int(state.get("interval_sec") or 300))
            max_tiles = max(1, min(int(os.getenv("GFS_ALWAYS_ON_MAX_TILES", "96") or "96"), 256))
            while True:
                try:
                    state["last_tick"] = self._now_ms()
                    candidates = list(getattr(self, "_recent_viewports", []) or [])[:4] + self._default_boot_bboxes()
                    scheduled = []
                    for i, bbox in enumerate(candidates[:6]):
                        # Always-on warming should keep the map responsive, not hammer every remote provider.
                        # Ocean/bait/boats warm only when the browser explicitly asks for those layers.
                        base_layers = ["clouds", "rain", "lightning"]
                        result = self.schedule_globe_cache_warm(bbox, max_tiles=max_tiles, reason="always_on_5min" if i == 0 else "always_on_background", layers=base_layers)
                        scheduled.append({"bbox": self._normalize_bbox(bbox), "scheduled": bool(result.get("scheduled")), "reason": result.get("reason")})
                        if result.get("scheduled"):
                            break
                    state["last_scheduled"] = scheduled
                    state["last_error"] = None
                except Exception as exc:
                    state["last_error"] = str(exc)
                    log.warning("[gfs/cache] always-on loop error: %s", exc)
                time.sleep(interval)
        t = threading.Thread(target=_loop, name="gfs-always-on-cache", daemon=True)
        self._always_on_cache_thread = t
        t.start()
        log.info("[gfs/cache] always-on cache daemon started interval=%ss pad_factor=%s", state.get("interval_sec"), state.get("pad_factor"))
        return {"ok": True, "started": True, "cache": state}

    def cache_status_payload(self) -> dict[str, Any]:
        root = self._tile_cache_root()
        files = list(root.glob("*.intel.json")) if root.exists() else []
        ages = []
        now = time.time()
        for p in files[:5000]:
            try:
                ages.append(max(0, int(now - p.stat().st_mtime)))
            except Exception:
                pass
        warm = self._cache_warm_status_payload()
        return {
            "ok": True,
            "schema": "lftr_cache_first_status_v1",
            "server_time": datetime.now(timezone.utc).isoformat(),
            "cache": {
                "enabled": True,
                "running": bool((getattr(self, "_always_on_cache_state", {}) or {}).get("running")),
                "cache_path": str(root),
                "tiles_total": len(files),
                "fresh_target_sec": int(os.getenv("GFS_CACHE_FRESH_TARGET_SEC", "240") or "240"),
                "newest_age_sec": min(ages) if ages else None,
                "oldest_age_sec_sample": max(ages) if ages else None,
                "clients_connected": "tracked_by_ws",
                "warm": warm,
                "always_on": getattr(self, "_always_on_cache_state", {}),
                "recent_viewports": list(getattr(self, "_recent_viewports", []) or [])[:6],
                "bounds_policy": {"mode": "padded_viewport", "factor": float(os.getenv("GFS_VIEWPORT_PAD_FACTOR", "1.25") or "1.25")},
            },
        }

    def contours_payload(self) -> Dict[str, Any]:
        return {"ok": True, "features": [], "ts": self._now_ms()}

    def overlay_payload(self) -> Dict[str, Any]:
        return {"ok": True, "layers": [], "ts": self._now_ms()}

    def legend_payload(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "legend": [
                {"name": "Low", "color": "#1f77b4"},
                {"name": "Medium", "color": "#ff7f0e"},
                {"name": "High", "color": "#d62728"},
            ],
            "ts": self._now_ms(),
        }

    def tile_png_bytes(self, z: int, x: int, y: int) -> bytes:
        _ = (z, x, y)
        return base64.b64decode(_TRANSPARENT_PNG_BASE64)

    def location_media(self, location_key: str) -> Dict[str, Any]:
        return self._location_media_payload(location_key)

    def upsert_report(self, location_key: str, report_text: str) -> Dict[str, Any]:
        key = self._normalize_location_key(location_key)
        if not key:
            return {"ok": False, "error": "missing location_key"}
        store = self._load_store()
        rec = self._ensure_location_record(store, key)
        rec["report_text"] = (report_text or "").strip()
        rec["report_updated_at"] = self._now_ms()
        self._save_store(store)
        return {"ok": True, **self._location_media_payload(key)}

    def upsert_live(self, location_key: str, active: bool, stream_url: str) -> Dict[str, Any]:
        key = self._normalize_location_key(location_key)
        if not key:
            return {"ok": False, "error": "missing location_key"}
        store = self._load_store()
        rec = self._ensure_location_record(store, key)
        rec["live"] = {
            "active": bool(active),
            "stream_url": (stream_url or "").strip(),
            "updated_at": self._now_ms(),
        }
        self._save_store(store)
        return {"ok": True, **self._location_media_payload(key)}

    def save_upload_video(self, location_key: str, filename: str, raw: bytes) -> Dict[str, Any]:
        key = self._normalize_location_key(location_key)
        if not key:
            return {"ok": False, "error": "missing location_key"}
        if not raw:
            return {"ok": False, "error": "empty upload"}

        safe_name = secure_filename(filename or "upload.mp4")
        ext = Path(safe_name).suffix.lower()
        if ext not in _ALLOWED_VIDEO_EXTS:
            return {"ok": False, "error": f"unsupported file type: {ext or 'none'}"}

        saved_name = f"{key}-{self._now_ms()}{ext}"
        out_path = self.fishvid_dir / saved_name
        out_path.write_bytes(raw)
        media_url = f"/static/fishvid/{saved_name}"

        store = self._load_store()
        rec = self._ensure_location_record(store, key)
        uploads = rec.setdefault("uploads", [])
        uploads.insert(
            0,
            {
                "url": media_url,
                "filename": saved_name,
                "mime": f"video/{ext.lstrip('.') if ext != '.mov' else 'quicktime'}",
                "uploaded_at": self._now_ms(),
            },
        )
        rec["uploads"] = uploads[:20]
        self._save_store(store)
        return {"ok": True, "location_key": key, "media_url": media_url, **self._location_media_payload(key)}



    # ------------------------------------------------------------------
    # LFTR scene-cache subscription spine
    # ------------------------------------------------------------------
    def _scene_cache_resolution_deg(self, bbox: dict[str, float] | None) -> float:
        """Return the cache-grid degree bucket for the current viewport span.

        This keeps the main scene cache simple: three-ish practical LOD buckets,
        quantized so camera jitter does not create endless duplicate cache rows.
        The resolver still prefers better/current cached payloads and low LOD only
        fills gaps or serves fast boot/global views.
        """
        b = self._normalize_bbox(bbox)
        span = max(abs(float(b.get("east", 0)) - float(b.get("west", 0))), abs(float(b.get("north", 0)) - float(b.get("south", 0))))
        if span <= 1.2:
            return 0.05
        if span <= 3.5:
            return 0.10
        if span <= 8.0:
            return 0.25
        if span <= 18.0:
            return 0.50
        return 1.00

    def _scene_cache_quantize_bbox(self, bbox: dict[str, float] | None, resolution_deg: float | None = None) -> dict[str, float]:
        b = self._normalize_bbox(bbox)
        r = float(resolution_deg or self._scene_cache_resolution_deg(b))
        if r <= 0:
            r = 0.25
        def q_floor(v: float) -> float:
            return math.floor(float(v) / r) * r
        def q_ceil(v: float) -> float:
            return math.ceil(float(v) / r) * r
        return {
            "west": round(q_floor(b["west"]), 4),
            "south": round(q_floor(b["south"]), 4),
            "east": round(q_ceil(b["east"]), 4),
            "north": round(q_ceil(b["north"]), 4),
        }

    def _scene_cache_bbox_contract(self, bbox: dict[str, float] | None, visible_bbox: dict[str, float] | None = None, layer: str | None = None, role: str = "scene-cache") -> dict[str, Any]:
        requested = self._normalize_bbox(bbox)
        visible = self._normalize_bbox(visible_bbox or requested)
        layer_key = str(layer or "").strip().lower().replace("-", "_")
        weather_layers = {"clouds", "rain", "lightning", "jetstream", "inland_water_temp", "inland_temp"}
        ocean_layers = {"bait", "boater", "boats", "boater_awareness", "shark_intel", "shark", "sharkintel"}
        static_layers = {"inland_water", "inland_waterways", "locations"}
        if layer_key in weather_layers:
            provider = requested
            provider_role = "weather_fetch_bbox"
        elif layer_key in ocean_layers or layer_key in static_layers:
            provider = visible
            provider_role = "visible_bbox"
        else:
            provider = requested
            provider_role = "requested_bbox"
        return {
            "version": "bbox_contract_v2_layer_specific",
            "role": role,
            "layer": layer_key or None,
            "policy": "visible_bbox_is_render_box_provider_bbox_is_layer_specific",
            "requested_bbox": requested,
            "fetch_bbox": requested,
            "visible_bbox": visible,
            "render_bbox": visible,
            "weather_fetch_bbox": requested,
            "ocean_fetch_bbox": visible,
            "shoreline_bbox": visible,
            "jetstream_bbox": visible,
            "jetstream_fetch_bbox": requested,
            "provider_bbox": provider,
            "provider_role": provider_role,
        }

    def _scene_cache_layer_key(self, layer: str, bbox: dict[str, float], visible_bbox: dict[str, float] | None = None) -> str:
        vis = self._normalize_bbox(visible_bbox or bbox)
        resolution = self._scene_cache_resolution_deg(vis)
        qvis = self._scene_cache_quantize_bbox(vis, resolution)
        # One simple scene-cache scheme: layer + quantized visible tile/bbox + LOD.
        # This avoids hoarding near-identical scene snapshots while still letting
        # high-detail viewport cache rows beat coarse/global rows.
        return "scene_cache:%s:lod%.2f:%s" % (str(layer).replace('-', '_'), resolution, self._bbox_key_fragment(qvis))

    def _scene_cache_first_paint_key(self, layer: str) -> str:
        # Tiny latest-good drawable for instant pill paint. It is not a full scene
        # snapshot and must never become the only cache source; it only bridges the
        # first animation frame while visible-bbox tiles warm in the background.
        return "scene_first_paint:%s" % str(layer).replace('-', '_')

    def _scene_cache_payload_version(self, layer: str, payload: dict[str, Any], quality_rank: int = 50) -> str:
        import hashlib
        now_hint = payload.get("resolved_time") or payload.get("valid_time") or payload.get("source_time") or payload.get("generated_at") or payload.get("ts") or self._now_ms()
        count_bits = [
            len(payload.get("items") or []) if isinstance(payload.get("items"), list) else 0,
            len(payload.get("features") or []) if isinstance(payload.get("features"), list) else 0,
            len(payload.get("precip_columns") or []) if isinstance(payload.get("precip_columns"), list) else 0,
            len(payload.get("flashes") or []) if isinstance(payload.get("flashes"), list) else 0,
            len(payload.get("boats") or []) if isinstance(payload.get("boats"), list) else 0,
            len(payload.get("polygons") or []) if isinstance(payload.get("polygons"), list) else 0,
            len((payload.get("bait") or {}).get("polygons") or []) if isinstance(payload.get("bait"), dict) else 0,
            len(payload.get("tempLabels") or []) if isinstance(payload.get("tempLabels"), list) else 0,
        ]
        base = "%s|%s|%s|%s|%s" % (layer, now_hint, quality_rank, payload.get("source"), ":".join(map(str, count_bits)))
        return "%s_q%s_%s" % (str(now_hint).replace(':', '').replace('-', '')[:18], int(quality_rank or 0), hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:10])

    def _scene_cache_empty_layer(self, layer: str, bbox: dict[str, float], reason: str = "warming") -> dict[str, Any]:
        now = self._now_ms()
        base = {
            "ok": True,
            "status": "warming",
            "payload_state": "warming",
            "source": "scene_cache_empty_%s" % layer,
            "bbox": bbox,
            "bbox_used": bbox,
            "cache": {"hit": False, "mode": reason, "ts": now},
            "ts": now,
        }
        key = str(layer).replace('-', '_')
        if key in {"clouds", "rain"}:
            base.update({"items": [], "features": [], "precip_columns": [], "scene": {}, "fields": {}})
        elif key == "lightning":
            base.update({"flashes": [], "items": [], "regions": []})
        elif key in {"boater", "boats", "boater_awareness"}:
            base.update({"boats": [], "items": [], "count": 0})
        elif key == "bait":
            base.update({"bait": {"status": "warming", "source": "scene_cache", "polygons": [], "outer_polygons": [], "inner_polygons": [], "core_polygons": []}, "bait_score": [], "polygons": []})
        elif key in {"shark_intel", "sharkintel", "shark"}:
            base.update({"schema": "lftr_shark_intel_v1", "contours": [], "polygons": [], "score_points": [], "species": {}, "target": {"primary_species": "leopard", "primary_size_in": [36, 42]}, "legal_caution": {"summary": "warming"}})
        elif key in {"inland_water", "inland_waterways"}:
            base.update({"polygons": [], "lines": [], "tempLabels": [], "temperature_points": [], "count": 0})
        elif key == "locations":
            base.update({"items": [], "locations": []})
        return base

    def _scene_cache_layer_has_renderable_content(self, layer: str, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        layer = str(layer or "").lower()
        if layer == "bait":
            bait = payload.get("bait") if isinstance(payload.get("bait"), dict) else {}
            ocean_points_obj = payload.get("oceanPoints")
            ocean_points_list = ocean_points_obj.get("points") if isinstance(ocean_points_obj, dict) else ocean_points_obj
            return bool(
                (isinstance(bait.get("polygons"), list) and len(bait.get("polygons")) > 0)
                or (isinstance(payload.get("bait_score"), list) and len(payload.get("bait_score")) > 0)
                or (isinstance(payload.get("ocean_points"), list) and len(payload.get("ocean_points")) > 0)
                or (isinstance(ocean_points_list, list) and len(ocean_points_list) > 0)
            )
        if layer == "boater":
            return bool(
                (isinstance(payload.get("boats"), list) and len(payload.get("boats")) > 0)
                or (isinstance(payload.get("points"), list) and len(payload.get("points")) > 0)
                or (isinstance(payload.get("ocean_points"), list) and len(payload.get("ocean_points")) > 0)
                or (isinstance(payload.get("oceanPoints"), list) and len(payload.get("oceanPoints")) > 0)
            )
        if layer in {"shark-intel", "shark_intel", "sharkintel", "shark"}:
            ocean_mask = payload.get("ocean_mask_source") if isinstance(payload.get("ocean_mask_source"), dict) else {}
            ocean_points_obj = payload.get("oceanPoints")
            ocean_points_list = ocean_points_obj.get("points") if isinstance(ocean_points_obj, dict) else ocean_points_obj
            return bool(
                (isinstance(payload.get("contours"), list) and len(payload.get("contours")) > 0)
                or (isinstance(payload.get("polygons"), list) and len(payload.get("polygons")) > 0)
                or (isinstance(payload.get("score_points"), list) and len(payload.get("score_points")) > 0)
                or int(ocean_mask.get("points") or 0) > 0
                or (isinstance(ocean_points_list, list) and len(ocean_points_list) > 0)
            )
        if layer == "inland_water_temp":
            return bool(
                (isinstance(payload.get("temperature_points"), list) and len(payload.get("temperature_points")) > 0)
                or (isinstance(payload.get("tempLabels"), list) and len(payload.get("tempLabels")) > 0)
            )
        if layer == "clouds":
            return bool(
                (isinstance(payload.get("features"), list) and len(payload.get("features")) > 0)
                or (isinstance(payload.get("items"), list) and len(payload.get("items")) > 0)
                or (isinstance(payload.get("cloud_layers"), list) and len(payload.get("cloud_layers")) > 0)
            )
        if layer == "rain":
            return bool(
                (isinstance(payload.get("rain"), list) and len(payload.get("rain")) > 0)
                or (isinstance(payload.get("precip_columns"), list) and len(payload.get("precip_columns")) > 0)
                or (isinstance(payload.get("features"), list) and len(payload.get("features")) > 0)
            )
        if layer == "lightning":
            return bool(
                (isinstance(payload.get("flashes"), list) and len(payload.get("flashes")) > 0)
                or (isinstance(payload.get("regions"), list) and len(payload.get("regions")) > 0)
                or (isinstance(payload.get("markers"), list) and len(payload.get("markers")) > 0)
            )
        return True

    def _scene_cache_layer_is_placeholder(self, layer: str, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return True
        status = str(payload.get("status") or payload.get("payload_state") or "").lower()
        source = str(payload.get("source") or "").lower()
        mode = str(payload.get("mode") or "").lower()
        ocean_backed = layer in {"bait", "boater", "boats", "boater_awareness", "shark-intel", "shark_intel", "sharkintel", "shark"}
        unavailable_tokens = (
            "live_required_unavailable",
            "ocean_live_required_unavailable",
            "bait_live_required_unavailable",
            "provider_empty",
            "waiting_for_sst_points",
            "hycom_ocean_points_empty",
            "quality_gate_failed",
            "large_bbox_cache_only_no_live_ncss",
        )
        if status in {"warming", "fetching_fresh", "unavailable", "provider_empty", "waiting_for_sst_points"} or "warming" in mode:
            return not self._scene_cache_layer_has_renderable_content(layer, payload)
        if source in {"deferred_tile_cache", "cache_first", "cache_first_queued"}:
            return not self._scene_cache_layer_has_renderable_content(layer, payload)
        if ocean_backed and any(tok in source or tok in status or tok in mode for tok in unavailable_tokens):
            return not self._scene_cache_layer_has_renderable_content(layer, payload)
        return False

    def _scene_cache_peek_layer(self, layer: str, bbox: dict[str, float], visible_bbox: dict[str, float] | None = None, *, max_age_sec: int = 86400, allow_first_paint: bool = True) -> dict[str, Any] | None:
        key = self._scene_cache_layer_key(layer, bbox, visible_bbox)
        payload = self._split_cache_peek(key)
        mode = "scene_cache"
        age = self._split_cache_age_seconds(key) if isinstance(payload, dict) else 999999999.0
        # If the exact viewport tile is missing, use the tiny latest-good
        # first-paint cache so pill clicks can animate immediately while a better
        # visible-bbox tile warms.
        if not isinstance(payload, dict) and allow_first_paint:
            fp_key = self._scene_cache_first_paint_key(layer)
            fp = self._split_cache_peek(fp_key)
            fp_age = self._split_cache_age_seconds(fp_key) if isinstance(fp, dict) else 999999999.0
            if isinstance(fp, dict) and fp_age <= SCENE_CACHE_FIRST_PAINT_MAX_AGE_SECONDS:
                payload = fp
                key = fp_key
                age = fp_age
                mode = "first_paint_cache"
        if not isinstance(payload, dict):
            return None
        if max_age_sec > 0 and age > max_age_sec:
            return None
        out = dict(payload)
        out.setdefault("cache", {})["hit"] = True
        is_placeholder = self._scene_cache_layer_is_placeholder(layer, out)
        out["cache"].update({"mode": mode, "age_sec": age, "key": key, "first_paint": mode == "first_paint_cache", "placeholder": bool(is_placeholder)})
        if is_placeholder:
            # A cache-first shell is not a drawable cache hit. Keep it visible in
            # diagnostics, but do not let fast reads/reporting pretend polygons are ready.
            out["cache"]["hit"] = False
        return out

    def _scene_cache_write_layer(self, layer: str, bbox: dict[str, float], payload: dict[str, Any], visible_bbox: dict[str, float] | None = None, *, quality_rank: int = 50) -> dict[str, Any]:
        key = self._scene_cache_layer_key(layer, bbox, visible_bbox)
        existing = self._split_cache_peek(key)
        existing_quality = 0
        try:
            existing_quality = int((existing or {}).get("cache_quality", {}).get("quality_rank") or (existing or {}).get("quality_rank") or 0)
        except Exception:
            existing_quality = 0
        incoming_quality = int(quality_rank or 0)
        # Never let a coarse/global refresh downgrade a sharper local/shore tile.
        if isinstance(existing, dict) and existing_quality > incoming_quality:
            out = dict(existing)
            out.setdefault("cache", {})["write_policy"] = "kept_existing_higher_quality"
            out["cache"].update({"existing_quality_rank": existing_quality, "incoming_quality_rank": incoming_quality, "key": key})
            return out
        incoming_renderable = self._scene_cache_layer_has_renderable_content(layer, payload if isinstance(payload, dict) else None)
        existing_renderable = self._scene_cache_layer_has_renderable_content(layer, existing if isinstance(existing, dict) else None)
        incoming_placeholder = self._scene_cache_layer_is_placeholder(layer, payload if isinstance(payload, dict) else None)
        if incoming_placeholder and not incoming_renderable and str(layer).lower() in {"bait", "boater", "boats", "boater_awareness", "shark-intel", "shark_intel", "sharkintel", "shark"}:
            # Ocean-backed layers must not promote points=0/provider-empty HYCOM rows as
            # latest-good scene cache. Return a diagnostic shell for this request only;
            # keep cache empty (or keep existing last-good below) until points/polygons exist.
            if not isinstance(existing, dict) or not existing_renderable:
                out = dict(payload or self._scene_cache_empty_layer(layer, bbox, "ocean_provider_warming_no_points"))
                out.setdefault("cache", {}).update({"hit": False, "write_policy": "rejected_empty_ocean_provider_not_promoted", "empty_write_rejected": True, "key": key})
                out["payload_state"] = out.get("payload_state") or "provider_empty_not_promoted"
                out["display_state"] = "waiting_for_first_ocean_points"
                return out
        if isinstance(existing, dict) and existing_renderable and not incoming_renderable:
            # Provider errors, empty HYCOM timeouts, and cache-first warming shells must
            # not erase a proven drawable scene-cache row. Preserve last-good until
            # a new drawable equal-or-better payload arrives.
            out = dict(existing)
            out.setdefault("cache", {})["write_policy"] = "kept_existing_renderable_over_empty"
            out["cache"].update({"incoming_quality_rank": incoming_quality, "key": key, "empty_write_rejected": True})
            return out
        now = self._now_ms()
        out = dict(payload or {})
        out.setdefault("ok", True)
        out.setdefault("bbox", bbox)
        out.setdefault("bbox_used", bbox)
        vis = self._normalize_bbox(visible_bbox or bbox)
        out.setdefault("visible_bbox", vis)
        out.setdefault("render_bbox", vis)
        out.setdefault("fetch_bbox", bbox)
        out.setdefault("bbox_contract", self._scene_cache_bbox_contract(bbox, vis, layer, role="scene-cache-layer-write"))
        version = self._scene_cache_payload_version(layer, out, incoming_quality)
        out["cache_quality"] = {
            "quality_rank": incoming_quality,
            "policy": "current_equal_or_better_replaces_lower_quality_never_downgrade_detail",
            "resolution_deg": self._scene_cache_resolution_deg(vis),
            "quantized_visible_bbox": self._scene_cache_quantize_bbox(vis),
            "updated_at": now,
            "version": version,
        }
        out["version"] = out.get("version") or version
        out.setdefault("cache", {})["hit"] = False
        out["cache"].update({"mode": "scene_cache_write", "key": key, "quality_rank": incoming_quality, "ts": now, "version": version})
        self._split_cache_set(key, out)
        # Per-layer first paint: overwrite only with drawable equal-or-better payloads.
        # Never store a warming shell as first-paint, because that makes later fast
        # reads look like cache hits while drawing zero polygons.
        fp_key = self._scene_cache_first_paint_key(layer)
        try:
            fp_existing = self._split_cache_peek(fp_key)
            fp_q = int((fp_existing or {}).get("cache_quality", {}).get("quality_rank") or 0) if isinstance(fp_existing, dict) else 0
            if incoming_renderable and (incoming_quality >= fp_q or not isinstance(fp_existing, dict)):
                fp = dict(out)
                fp.setdefault("cache", {}).update({"mode": "first_paint_write", "key": fp_key, "source_scene_key": key, "ts": now, "version": version})
                self._split_cache_set(fp_key, fp)
        except Exception:
            pass
        return out

    def _scene_cache_quality_rank(self, layer: str, bbox: dict[str, float], visible_bbox: dict[str, float] | None = None) -> int:
        b = self._normalize_bbox(visible_bbox or bbox)
        span = max(abs(float(b.get("east", 0)) - float(b.get("west", 0))), abs(float(b.get("north", 0)) - float(b.get("south", 0))))
        layer = str(layer).replace('-', '_')
        if span <= 1.2:
            q = 95
        elif span <= 3.5:
            q = 85
        elif span <= 8.0:
            q = 70
        elif span <= 18.0:
            q = 55
        else:
            q = 35
        if layer in {"inland_water", "inland_waterways", "bait", "shark_intel", "shark-intel"} and span <= 8.0:
            q += 5
        return min(100, q)

    def _scene_cache_build_layer(self, layer: str, bbox: dict[str, float], visible_bbox: dict[str, float] | None = None) -> dict[str, Any]:
        layer_key = str(layer or '').strip().lower().replace('-', '_')
        if layer_key == "locations":
            payload = self.fish_payload()
            items = payload.get("items") if isinstance(payload, dict) else []
            if isinstance(payload, dict) and isinstance(items, list):
                payload = dict(payload)
                payload.setdefault("locations", items)
            return payload
        requested_bbox = self._normalize_bbox(bbox)
        visible_provider_bbox = self._normalize_bbox(visible_bbox or requested_bbox)
        weather_provider_bbox = requested_bbox
        provider_bbox = visible_provider_bbox
        if layer_key in {"clouds", "rain", "lightning", "jetstream", "inland_water_temp", "inland_temp"}:
            provider_bbox = weather_provider_bbox
        bbox_contract = self._scene_cache_bbox_contract(requested_bbox, visible_provider_bbox, layer_key, role="scene-cache-build-layer")
        if layer_key == "clouds":
            # Clouds/rain may use the padded weather fetch bbox, but render/debug
            # still carries the exact visible_bbox so edge padding never leaks into
            # bait/boats/shoreline placement.
            out = self._cloud_tiles_payload_heavy(provider_bbox, visible_provider_bbox, force_live=GFS_CLOUDS_FORCE_LIVE_FETCH)
            if isinstance(out, dict):
                out.setdefault("bbox_contract", bbox_contract)
                out.setdefault("visible_bbox", visible_provider_bbox)
                out.setdefault("render_bbox", visible_provider_bbox)
                out.setdefault("fetch_bbox", provider_bbox)
            return out
        if layer_key == "rain":
            payload = self._cloud_tiles_payload_heavy(provider_bbox, visible_provider_bbox, force_live=GFS_CLOUDS_FORCE_LIVE_FETCH)
            if isinstance(payload, dict):
                out = dict(payload)
                out["source_layer"] = "rain_from_precip_only_scene_cache"
                out["cloud_items"] = 0
                out["items"] = []
                out["features"] = out.get("precip_columns") or out.get("features") or []
                out.setdefault("bbox_contract", bbox_contract)
                out.setdefault("visible_bbox", visible_provider_bbox)
                out.setdefault("render_bbox", visible_provider_bbox)
                out.setdefault("fetch_bbox", provider_bbox)
                return out
            return payload
        if layer_key == "lightning":
            return self.lightning_payload(bbox, visible_bbox, 20)
        if layer_key == "jetstream":
            # Jetstream balloons need real GFS u/v. Build from the visible viewport
            # in the background; do not use the huge tilted weather cache bbox.
            try:
                scene = self.get_scene_payload(provider_bbox)
                wind_items = []
                if isinstance(scene, dict):
                    wind_items = (((scene.get("scene") or {}).get("wind")) if isinstance(scene.get("scene"), dict) else []) or []
                jet_orbs = []
                for w in wind_items or []:
                    if not isinstance(w, dict):
                        continue
                    c = w.get("center") if isinstance(w.get("center"), dict) else {}
                    lat = safe_float(c.get("lat"), float("nan"))
                    lon = safe_float(c.get("lon"), float("nan"))
                    u = safe_float(w.get("vector_u"), 0.0)
                    v = safe_float(w.get("vector_v"), 0.0)
                    if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0):
                        continue
                    mph = safe_float(w.get("speed_mps"), math.hypot(u, v)) * 2.23694
                    jet_orbs.append({
                        "lat": round(lat, 5),
                        "lon": round(self._wrap_lon(lon), 5),
                        "u": u,
                        "v": v,
                        "mph": round(mph, 2),
                        "direction_deg": safe_float(w.get("direction_deg"), ((math.atan2(u, v) * 180.0 / math.pi) + 360.0) % 360.0),
                        "altitude_m": 3048.0,
                        "source": "gfs_uv_direction_scene_cache",
                        "source_level": w.get("altitude_band") or w.get("source_level") or "gfs_uv",
                    })
                return {
                    "ok": True,
                    "status": "ok" if jet_orbs else "warming",
                    "payload_state": "live_or_cached_gfs_uv" if jet_orbs else "warming",
                    "source": "gfs_uv_direction_scene_cache",
                    "items": jet_orbs,
                    "jet_orbs": jet_orbs,
                    "count": len(jet_orbs),
                    "bbox": provider_bbox,
                    "bbox_used": provider_bbox,
                    "visible_bbox": visible_provider_bbox,
                    "render_bbox": visible_provider_bbox,
                    "fetch_bbox": provider_bbox,
                    "bbox_contract": bbox_contract,
                    "bbox_policy": "jetstream_spawns_in_visible_bbox_samples_padded_weather_fetch_bbox",
                    "jetstream": {
                        "ok": bool(wind_items),
                        "source": "gfs_uv_direction_scene_cache",
                        "count": len(wind_items),
                        "fallback_used": False,
                        "mock": False,
                        "proxy": False,
                    },
                }
            except Exception as exc:
                out = self._scene_cache_empty_layer(layer_key, bbox, "jetstream_gfs_uv_pending")
                out.update({"source": "gfs_uv_direction_scene_cache_error", "error": str(exc), "jet_orbs": [], "items": [], "jetstream": {"ok": False, "source": "gfs_uv_direction_pending", "count": 0}})
                return out
        if layer_key in {"boater", "boats", "boater_awareness"}:
            scene = self.build_scene_plan(provider_bbox, provider_bbox, layer="boats")
            ocean = self._ocean_payload_heavy(provider_bbox, provider_bbox)
            boats = ocean.get("boats", []) or [] if isinstance(ocean, dict) else []
            source = str((ocean or {}).get("source") or "")
            mode = str((ocean or {}).get("mode") or "")
            is_fallback = ("fallback" in source.lower()) or ("marker_ocean_solve" in source.lower()) or ("fallback" in mode.lower()) or ("proxy" in mode.lower())
            ocean_points = (ocean or {}).get("oceanPoints", {}).get("points") if isinstance((ocean or {}).get("oceanPoints"), dict) else None
            ocean_points = ocean_points or (ocean or {}).get("ocean_points") or (ocean or {}).get("points") or (ocean or {}).get("current_points") or []
            return {
                "ok": bool((ocean or {}).get("ok")),
                "source": (ocean or {}).get("source"),
                "mode": (ocean or {}).get("mode"),
                "engine": (ocean or {}).get("engine"),
                "bbox": (ocean or {}).get("bbox") or provider_bbox,
                "scene_plan": scene,
                "visible_bbox": provider_bbox,
                "fetch_bbox": provider_bbox,
                "render_budget": scene.get("render_budget"),
                "valid_time": (ocean or {}).get("valid_time") or (ocean or {}).get("resolved_time"),
                "validTime": (ocean or {}).get("valid_time") or (ocean or {}).get("resolved_time"),
                "sourceTime": (ocean or {}).get("valid_time") or (ocean or {}).get("resolved_time"),
                "boats": boats[:int(os.getenv("GFS_BOAT_COUNT_MAX", "10") or "10")],
                "points": ocean_points,
                "ocean_points": ocean_points,
                "oceanAnalysisPoints": {"ok": bool(ocean_points), "source": "hycom_provider_ocean_analysis_points_embedded_in_boater_scene_cache", "points": ocean_points, "count": len(ocean_points), "contract": "large_finite_sst_current_data_field_not_visual_boat_count"},
                "ocean_analysis_point_count": len(ocean_points),
                "oceanPoints": {"ok": bool(ocean_points), "source": "hycom_provider_ocean_points_embedded_in_boater_scene_cache", "points": ocean_points, "count": len(ocean_points), "contract": "finite_sst_current_points_are_boat_squares_and_sea_mask"},
                "ocean_point_count": len(ocean_points),
                "current_points": ocean_points,
                "current_zone_points_count": len(ocean_points),
                "current_zone_grid": (ocean or {}).get("current_zone_grid"),
                "count": min(len(boats), int(os.getenv("GFS_BOAT_COUNT_MAX", "10") or "10")),
                "renderable_count_hint": (min(len(boats), int(os.getenv("GFS_BOAT_COUNT_MAX", "10") or "10")) if (not is_fallback and len(ocean_points) >= int(os.getenv("GFS_BOATER_MIN_OCEAN_POINTS", "48") or "48")) else 0),
                "fallback_rejected_count_hint": len(boats) if is_fallback else 0,
                "render_contract": "glb_boats_require_live_hycom_provider_sst_current_samples_and_ocean_points",
                "min_ocean_points_to_render": int(os.getenv("GFS_BOATER_MIN_OCEAN_POINTS", "48") or "96"),
                "rejection_counts": (((ocean or {}).get("grid") or {}).get("rejection_counts") or {}),
                "grid": (ocean or {}).get("grid"),
                "swell_components": (ocean or {}).get("swell_components", []),
                "source_meta": (ocean or {}).get("source_meta") or {},
                "sst_landmask": (((ocean or {}).get("source_meta") or {}).get("sst_landmask") or {}),
                "landmask_contract": (((ocean or {}).get("source_meta") or {}).get("landmask_contract") or "finite_sst_is_shared_water_gate_for_boater"),
                "diagnostics": (ocean or {}).get("diagnostics"),
                "cache": (ocean or {}).get("cache"),
                "quality_policy": (ocean or {}).get("quality_policy") or self._live_payload_policy(),
                "payload_state": (ocean or {}).get("payload_state") or ("live" if not is_fallback and boats else "provider_empty"),
                "fallback": (ocean or {}).get("fallback") or {"used": bool(is_fallback)},
                "bbox_contract": bbox_contract,
                "bbox_policy": "boats_current_use_visible_bbox_not_padded_weather_bbox",
                "ts": self._now_ms(),
            }
        if layer_key == "bait":
            scene = self.build_scene_plan(provider_bbox, provider_bbox, layer="bait")
            qb = self._quantize_bait_bbox(scene.get("fetch_bbox") or provider_bbox)
            out = self._bait_advanced_payload_heavy(qb, scene.get("visible_bbox"))
            if isinstance(out, dict):
                out.setdefault("bbox_contract", bbox_contract)
                out.setdefault("bbox_policy", "bait_uses_visible_bbox_not_padded_weather_bbox")
                out.setdefault("visible_bbox", visible_provider_bbox)
                out.setdefault("render_bbox", visible_provider_bbox)
            return out
        if layer_key in {"shark_intel", "shark-intel", "sharkintel", "shark"}:
            ocean_mask_payload = None
            try:
                scene = self.build_scene_plan(provider_bbox, provider_bbox, layer="shark-intel")
                ocean_mask_payload = self._ocean_points_payload_heavy(provider_bbox, "auto", scene.get("visible_bbox") or provider_bbox)
            except Exception as exc:
                ocean_mask_payload = {"ok": False, "source": "shark_intel_ocean_mask_fetch_failed", "error": str(exc), "points": []}
            out = shark_intel_payload(provider_bbox, provider_bbox, ocean_payload=ocean_mask_payload)
            if isinstance(out, dict):
                out.setdefault("bbox_contract", bbox_contract)
                out.setdefault("bbox_policy", "shark_intel_uses_visible_bbox_not_padded_weather_bbox")
            try:
                out["ocean_mask_source"] = {
                    "source": (ocean_mask_payload or {}).get("source"),
                    "ok": bool((ocean_mask_payload or {}).get("ok")),
                    "points": len((ocean_mask_payload or {}).get("points") or []),
                    "mask": (ocean_mask_payload or {}).get("mask"),
                    "grid": (ocean_mask_payload or {}).get("grid"),
                }
            except Exception:
                pass
            return out
        if layer_key in {"inland_water", "inland_waterways"}:
            return self.inland_water_tiles_payload(bbox, 96, visible_bbox, source="auto", geometry="vector", lod="auto", scene_tier=None)
        if layer_key in {"inland_water_temp", "inland_temp"}:
            return self.inland_water_temp_payload(bbox, visible_bbox)
        return self._scene_cache_empty_layer(layer_key, bbox, "unknown_layer")

    def _scene_cache_schedule_layer_refresh(self, layer: str, bbox: dict[str, float], visible_bbox: dict[str, float] | None = None) -> bool:
        key = self._scene_cache_layer_key(layer, bbox, visible_bbox)
        q = self._scene_cache_quality_rank(layer, bbox, visible_bbox)
        def _builder():
            payload = self._scene_cache_build_layer(layer, bbox, visible_bbox)
            return self._scene_cache_write_layer(layer, bbox, payload, visible_bbox, quality_rank=q)
        return self._schedule_split_warm(key, "scene-cache-%s" % layer, _builder)


    def scene_cache_fast_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, layers: list[str] | tuple[str, ...] | None = None, *, mode: str = "fast") -> dict[str, Any]:
        """Strict cache-only boot/read path.

        This must never call live providers, never schedule builds, and never run
        janitor work.  It exists so the browser can draw the last-good scene
        immediately while slower refresh workers update cache for later runs.
        """
        bbox_norm = self._normalize_bbox(bbox)
        visible_norm = self._normalize_bbox(visible_bbox or bbox_norm)
        wanted = [str(x).strip() for x in (layers or []) if str(x).strip()] or list(LIVE_SCENE_CACHE_DEFAULT_LAYERS)
        now = self._now_ms()
        out_layers: dict[str, Any] = {}
        meta_layers: dict[str, Any] = {}
        for raw in wanted:
            layer = str(raw).strip().lower()
            canonical = "boater" if layer in {"boats", "boater_awareness"} else ("inland-water" if layer in {"inland_water", "inland_waterways"} else ("shark-intel" if layer in {"shark_intel", "sharkintel", "shark"} else layer))
            cached = self._scene_cache_peek_layer(canonical, bbox_norm, visible_norm, max_age_sec=SCENE_CACHE_MAX_AGE_SECONDS, allow_first_paint=True)
            if cached is None:
                cached = self._scene_cache_empty_layer(canonical, bbox_norm, "fast_cache_miss_no_live_fetch")
                cached.setdefault("cache", {}).update({"hit": False, "mode": "fast_cache_miss_no_live_fetch", "fast": True, "refresh_scheduled": False})
            else:
                cached.setdefault("cache", {}).update({"fast": True, "refresh_scheduled": False})
            if canonical in STATIC_SCENE_CACHE_LAYERS:
                cached.setdefault("cache", {})["ttl_policy"] = "static_no_2min_reload"
            out_layers[canonical] = cached
            meta_layers[canonical] = {
                "subscribed": True,
                "cache_hit": bool(cached.get("cache", {}).get("hit")),
                "cache_mode": cached.get("cache", {}).get("mode"),
                "age_sec": cached.get("cache", {}).get("age_sec"),
                "refresh_scheduled": False,
                "quality": cached.get("cache_quality") or {"quality_rank": self._scene_cache_quality_rank(canonical, bbox_norm, visible_norm)},
                "ttl_policy": cached.get("cache", {}).get("ttl_policy") or ("static_no_2min_reload" if canonical in STATIC_SCENE_CACHE_LAYERS else "live_background_refresh_when_stale"),
                "status": cached.get("status") or cached.get("payload_state") or "ok",
                "source": cached.get("source"),
            }
        return {
            "ok": True,
            "schema": "lftr_scene_cache_subscription_v1",
            "source": "shared_scene_cache_subscription_spine_fast_cache_only",
            "policy": "boot_reads_last_good_cache_only_live_downloads_progressively_update_next_run_cache",
            "refresh_interval_ms": SCENE_CACHE_REFRESH_INTERVAL_MS,
            "bbox": bbox_norm,
            "visible_bbox": visible_norm,
            "render_bbox": visible_norm,
            "bbox_contract": self._scene_cache_bbox_contract(bbox_norm, visible_norm, role="scene-cache-fast-read"),
            "layers": out_layers,
            "cache": {
                "layers": meta_layers,
                "ts": now,
                "mode": mode,
                "read_only": True,
                "fast": True,
                "no_live_fetch": True,
                "no_build": True,
                "no_janitor": True,
                "ttl_policy": "2min_is_background_ttl_heartbeat_not_visible_cache_reload",
                "static_layers": sorted(STATIC_SCENE_CACHE_LAYERS),
                "first_paint_policy": "draw_last_good_cache_immediately_then_background_refresh_updates_changed_versions",
            },
            "ts": now,
        }

    def scene_cache_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, layers: list[str] | tuple[str, ...] | None = None, *, refresh: bool = True, mode: str = "read") -> dict[str, Any]:
        bbox_norm = self._normalize_bbox(bbox)
        visible_norm = self._normalize_bbox(visible_bbox or bbox_norm)
        wanted = [str(x).strip() for x in (layers or []) if str(x).strip()]
        if not wanted:
            wanted = list(BOOT_SCENE_CACHE_DEFAULT_LAYERS)
        mode = str(mode or "read").lower()
        read_only = mode in {"read", "first_paint", "fast", "cache"}
        janitor = self._split_scene_cache_janitor_locked(reason="scene_cache_payload")
        now = self._now_ms()
        out_layers: dict[str, Any] = {}
        meta_layers: dict[str, Any] = {}
        for raw in wanted:
            layer = str(raw).strip().lower()
            canonical = "boater" if layer in {"boats", "boater_awareness"} else ("inland-water" if layer in {"inland_water", "inland_waterways"} else ("shark-intel" if layer in {"shark_intel", "sharkintel", "shark"} else layer))
            cached = self._scene_cache_peek_layer(canonical, bbox_norm, visible_norm, max_age_sec=SCENE_CACHE_MAX_AGE_SECONDS, allow_first_paint=True)
            if cached is None:
                if canonical == "locations":
                    cached = self._scene_cache_build_layer(canonical, bbox_norm, visible_norm)
                    cached = self._scene_cache_write_layer(canonical, bbox_norm, cached, visible_norm, quality_rank=100)
                else:
                    cached = self._scene_cache_empty_layer(canonical, bbox_norm, "queued_background_warm")
            scheduled = False
            age = cached.get("cache", {}).get("age_sec") if isinstance(cached, dict) else None
            static_layer = canonical in STATIC_SCENE_CACHE_LAYERS
            # Static layers do not reload on the 2-minute TTL loop. They only warm
            # when missing/cold or when explicitly requested on boot/toggle. Live
            # companion sublayers, such as inland_water_temp, keep the TTL policy.
            empty_placeholder = self._scene_cache_layer_is_placeholder(canonical, cached)
            if (not read_only) and refresh and (empty_placeholder or cached.get("payload_state") == "warming" or age is None or ((not static_layer) and float(age or 999999) > SCENE_CACHE_STALE_SECONDS)):
                scheduled = self._scene_cache_schedule_layer_refresh(canonical, bbox_norm, visible_norm)
                cached.setdefault("cache", {})["refresh_scheduled"] = scheduled
                if empty_placeholder:
                    cached.setdefault("cache", {})["empty_placeholder_refresh"] = True
            if static_layer:
                cached.setdefault("cache", {})["ttl_policy"] = "static_no_2min_reload"
            out_layers[canonical] = cached
            meta_layers[canonical] = {
                "subscribed": True,
                "cache_hit": bool(cached.get("cache", {}).get("hit")),
                "cache_mode": cached.get("cache", {}).get("mode"),
                "age_sec": cached.get("cache", {}).get("age_sec"),
                "refresh_scheduled": bool(cached.get("cache", {}).get("refresh_scheduled", scheduled)),
                "quality": cached.get("cache_quality") or {"quality_rank": self._scene_cache_quality_rank(canonical, bbox_norm, visible_norm)},
                "ttl_policy": cached.get("cache", {}).get("ttl_policy") or ("static_no_2min_reload" if canonical in STATIC_SCENE_CACHE_LAYERS else "live_2min_refresh_when_stale"),
                "status": cached.get("status") or cached.get("payload_state") or "ok",
                "source": cached.get("source"),
            }
        return {
            "ok": True,
            "schema": "lftr_scene_cache_subscription_v1",
            "source": "shared_scene_cache_subscription_spine",
            "policy": "live_data_feeds_cache_cache_feeds_globe_pills_control_subscription_and_visibility",
            "refresh_interval_ms": SCENE_CACHE_REFRESH_INTERVAL_MS,
            "bbox": bbox_norm,
            "visible_bbox": visible_norm,
            "render_bbox": visible_norm,
            "bbox_contract": self._scene_cache_bbox_contract(bbox_norm, visible_norm, role="scene-cache-read"),
            "layers": out_layers,
            "cache": {
                "layers": meta_layers,
                "ts": now,
                "janitor": janitor,
                "mode": mode,
                "read_only": read_only,
                "scheme": "one_main_scene_cache_call_resolves_quantized_layer_tiles",
                "ttl_policy": "2min_live_layers_only_static_layers_local_or_cache_until_missing",
                "static_layers": sorted(STATIC_SCENE_CACHE_LAYERS),
                "lod_policy": "coarse_medium_high_only_low_fills_gaps_high_detail_wins",
                "highest_quality_policy": "current_equal_or_better_replaces_lower_quality_never_downgrade_detail",
                "first_paint_policy": "pill_click_reads_latest_good_layer_cache_immediately_live_refresh_runs_background",
                "visible_priority_policy": "visible_bbox_high_detail_first_nearby_buffer_second_global_coarse_fallback_third",
            },
            "ts": now,
        }

    def scene_cache_refresh_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, layers: list[str] | tuple[str, ...] | None = None, reason: str = "browser_2min_subscription_refresh") -> dict[str, Any]:
        bbox_norm = self._normalize_bbox(bbox)
        visible_norm = self._normalize_bbox(visible_bbox or bbox_norm)
        wanted = [str(x).strip() for x in (layers or []) if str(x).strip()] or list(LIVE_SCENE_CACHE_DEFAULT_LAYERS)
        jobs = {}
        reason_lc = str(reason or "").lower()
        movement = any(tok in reason_lc for tok in ("camera_move", "cache_pop", "viewport"))
        force_empty_repair = ("empty_placeholder_repair" in reason_lc) or ("force=1" in reason_lc) or ("force_refresh" in reason_lc)
        for layer in wanted:
            canonical = "boater" if layer in {"boats", "boater_awareness"} else ("inland-water" if layer in {"inland_water", "inland_waterways"} else ("shark-intel" if layer in {"shark_intel", "sharkintel", "shark"} else layer))
            if canonical in STATIC_SCENE_CACHE_LAYERS:
                jobs[canonical] = {"scheduled": False, "ttl_policy": "static_no_2min_reload", "note": "static layer is served from existing cache/local data and is not refreshed by the live refresh loop"}
                continue

            cached = self._scene_cache_peek_layer(canonical, bbox_norm, visible_norm, max_age_sec=SCENE_CACHE_MAX_AGE_SECONDS, allow_first_paint=False)
            age = cached.get("cache", {}).get("age_sec") if isinstance(cached, dict) else None
            empty_placeholder = self._scene_cache_layer_is_placeholder(canonical, cached)
            should_refresh = empty_placeholder or (cached is None) or (age is None) or (float(age or 999999) > SCENE_CACHE_STALE_SECONDS)

            # Prevent camera motion / overlapping visible bboxes from scheduling
            # near-identical heavy providers every few seconds. This is especially
            # important for GFS/cfgrib cloud/rain/jetstream and HYCOM/CoastWatch bait/boater.
            qkey = self._scene_cache_layer_key(canonical, bbox_norm, visible_norm)
            global_marker_key = f"scene-cache-refresh-marker:{canonical}"
            local_marker_key = f"scene-cache-refresh-marker:{qkey}"
            # Empty placeholders are not good cache. They may bypass the global
            # cooldown once so a first real drawable warm is not blocked by an early
            # empty shell, but they still keep a short local cooldown to prevent
            # repeated HYCOM/CoastWatch/GFS stampedes while the provider is slow.
            if empty_placeholder or cached is None:
                global_recent = None
                local_gap = 0 if force_empty_repair else max(10, min(30, SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS // 3))
            else:
                global_recent = self._split_cache_get(global_marker_key, max(1, SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS))
                local_gap = max(45, SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS * (2 if movement else 1))
            local_recent = None if force_empty_repair else self._split_cache_get(local_marker_key, max(1, local_gap))
            throttled = False if force_empty_repair else bool(global_recent or local_recent)
            scheduled = False
            if should_refresh and not throttled:
                try:
                    self._split_cache_set(global_marker_key, {"ts": self._now_ms(), "reason": reason, "layer": canonical})
                    self._split_cache_set(local_marker_key, {"ts": self._now_ms(), "reason": reason, "layer": canonical})
                except Exception:
                    pass
                scheduled = self._scene_cache_schedule_layer_refresh(canonical, bbox_norm, visible_norm)

            jobs[canonical] = {
                "scheduled": scheduled,
                "ttl_policy": "live_refresh_when_stale_with_layer_cooldown",
                "age_sec": age,
                "fresh_enough": not should_refresh,
                "empty_placeholder": bool(empty_placeholder),
                "priority": "visible_bbox_high_detail_first",
                "bbox_contract": self._scene_cache_bbox_contract(bbox_norm, visible_norm, canonical, role="scene-cache-refresh-job"),
                "throttled": bool(should_refresh and throttled),
                "min_gap_seconds": (0 if (force_empty_repair and empty_placeholder) else (max(10, min(30, SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS // 3)) if empty_placeholder else SCENE_CACHE_LAYER_REFRESH_MIN_GAP_SECONDS)),
                "force_empty_repair": bool(force_empty_repair and empty_placeholder),
                "movement_reason": movement,
            }
        return {
            "ok": True,
            "schema": "lftr_scene_cache_refresh_v2_throttled",
            "reason": reason,
            "mode": "background",
            "refresh_interval_ms": SCENE_CACHE_REFRESH_INTERVAL_MS,
            "bbox": bbox_norm,
            "visible_bbox": visible_norm,
            "render_bbox": visible_norm,
            "bbox_contract": self._scene_cache_bbox_contract(bbox_norm, visible_norm, role="scene-cache-refresh"),
            "priority_policy": "visible_bbox_high_detail_first_with_heavy_provider_cooldown",
            "jobs": jobs,
            "ts": self._now_ms(),
        }

    def scene_cache_janitor_payload(self, reason: str = "manual") -> dict[str, Any]:
        return self._split_scene_cache_janitor_locked(reason=reason)

    def _inland_scene_tier_for_bbox(self, bbox: dict[str, float] | None) -> str:
        b = self._normalize_bbox(bbox)
        try:
            width = abs(float(b.get("east", 0)) - float(b.get("west", 0)))
            height = abs(float(b.get("north", 0)) - float(b.get("south", 0)))
            span = max(width, height); area = max(0.0, width * height)
            if span <= 1.6 and area <= 2.6:
                return "harbor"
            if span <= 4.0 and area <= 14.0:
                return "coastal"
            if span <= 12.0 and area <= 90.0:
                return "regional"
        except Exception:
            pass
        return "world"

    def _inland_detail_allowed_for_bbox(self, bbox: dict[str, float] | None) -> bool:
        return self._inland_scene_tier_for_bbox(bbox) in {"harbor", "local", "coastal", "regional"}

    def inland_water_temp_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, max_points: int = 24) -> dict[str, Any]:
        # Separate NCSS temp companion sublayer. Geometry stays in /gfs/api/inland-water.
        # World zoom is overview-only: one largest closed lake temp per selected tile/viewport,
        # no bait seed rows. Regional/coastal/local/harbor may include bait seeds for contour rendering.
        from server.gfs.inland_water import inland_water_payload, inland_bait_payload, sample_surface_temperature, _centroid, _temperature_point, _enrich_inland_feature
        b = self._normalize_bbox(visible_bbox or bbox)
        tier = self._inland_scene_tier_for_bbox(b)
        detail_allowed = self._inland_detail_allowed_for_bbox(b)
        water = inland_water_payload(self.static_dir, b, source="auto", geometry=("vector" if detail_allowed else "coarse"), lod=("auto" if detail_allowed else "overview"), scene_tier=tier)
        features = list((water.get("polygons") or [])) + ([] if not detail_allowed else list(water.get("lines") or []))
        def _area(item: dict[str, Any]) -> float:
            try:
                return float(item.get("area_km2") or item.get("AREASQKM") or item.get("shape_area") or item.get("Shape_Area") or 0.0)
            except Exception:
                return 0.0
        if not detail_allowed:
            features = sorted(features, key=_area, reverse=True)[:max(1, min(8, int(max_points or 8)))]
            max_points = min(int(max_points or 8), len(features) or 1, 8)
        points = []
        checked = 0
        for item in features:
            c = _centroid(item.get("path") or [])
            if not c:
                continue
            lat, lon = float(c[0]), float(c[1])
            checked += 1
            temp = sample_surface_temperature(self, {"west": lon - 0.15, "south": lat - 0.15, "east": lon + 0.15, "north": lat + 0.15}, lat, lon, live=True)
            if temp.get("value_f") is not None:
                env = _enrich_inland_feature(item, self, lat, lon, temp.get("value_f"), temp.get("used") or temp.get("source"), "medium", live=True)
                points.append(_temperature_point({**item, **env}, lat, lon, temp.get("value_f"), temp.get("used") or temp.get("source"), "medium", len(points)))
            if len(points) >= max_points or checked >= max_points * (2 if detail_allowed else 1):
                break
        if detail_allowed:
            try:
                bait = inland_bait_payload(self.static_dir, self, b, live=False)
            except Exception as exc:
                bait = {"ok": False, "status": "error", "source": "inland_bait_companion_failed", "error": str(exc), "targets": [], "bait_score": [], "temperature_points": []}
        else:
            bait = {"ok": True, "status": "zoom_gated", "source": "inland_bait_gated_world_overview", "targets": [], "bait_score": [], "temperature_points": [], "policy": "bait contours render only at regional/coastal/local/harbor zoom"}
        return {
            "ok": True,
            "schema": "lftr_inland_water_temp_labels_v4_overview_gate",
            "source": "gfs_ncss_surface_temp_plus_inland_overview_companion",
            "bbox": b,
            "bbox_used": b,
            "scene_tier": tier,
            "overview_only": not detail_allowed,
            "inland_bait_render_allowed": detail_allowed,
            "temperature_points": points,
            "tempLabels": points,
            "bait": bait,
            "inland_bait": bait,
            "bait_score": bait.get("bait_score") if isinstance(bait, dict) else [],
            "bait_targets": bait.get("targets") if isinstance(bait, dict) else [],
            "bait_score_count": int(bait.get("bait_score_count") or len(bait.get("bait_score") or [])) if isinstance(bait, dict) else 0,
            "count": len(points),
            "water_feature_count": len((water.get("polygons") or [])) + len((water.get("lines") or [])),
            "overview_policy": "world zoom samples largest closed lake outlines/temp only; bait seeds gated",
            "contract": "lftr_inland_water_temp_v4_world_overview_then_zoom_bait",
            "ts": self._now_ms(),
        }


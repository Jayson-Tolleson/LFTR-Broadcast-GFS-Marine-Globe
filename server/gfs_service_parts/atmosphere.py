from __future__ import annotations
import os

import server.gfs_service as _svc
globals().update({k: v for k, v in vars(_svc).items() if not k.startswith("__")})


class AtmosphereMixin:
    def _heuristic_context(self, lat: float | None, lon: float | None, ts_ms: int) -> Dict[str, Any]:
        if lat is None or lon is None or not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
            fallback_point = {"lat": DEFAULT_WORLD_ENV_MARKER["lat"], "lon": DEFAULT_WORLD_ENV_MARKER["lon"], "location_key": "fallback", "meta": {}}
            intel = self._build_bait_intel(fallback_point, ts_ms)
            intel["bait"]["intensity"] = "low"
            intel["bait"]["confidence"] = min(intel["bait"].get("confidence", 42), 42)
            intel["environment_meta"]["source_tier"] = "heuristic_only"
            return intel

        point = {"lat": float(lat), "lon": float(lon), "location_key": f"pt-{self._rounded_env_key(float(lat), float(lon), 3)}", "meta": {}}
        return self._build_bait_intel(point, ts_ms)

    def _stable_noise(self, a: float, b: float, c: float) -> float:
        mix = math.sin(a * 12.9898 + b * 78.233 + c * 37.719)
        return mix - math.floor(mix)

    def _cloud_density_triplet(self, lat: float, lon: float, hour_bucket: int) -> Tuple[float, float, float, float, float]:
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        t = hour_bucket / 6.0

        macro = (
            0.5
            + 0.25 * math.sin(2.4 * lat_r + 0.8 * lon_r + t * 0.9)
            + 0.18 * math.cos(3.7 * lon_r - t * 0.7)
            + 0.12 * math.sin(5.2 * (lat_r + lon_r) + t * 0.35)
        )
        macro = max(0.0, min(1.0, macro))

        frontness = abs(math.sin(lat_r * 2.7 + lon_r * 0.35 + t * 0.45))
        gradient = abs(math.cos(lat_r * 3.9 - lon_r * 1.4 + t * 0.28))
        convective_proxy = max(0.0, min(1.0, 0.35 + 0.55 * frontness * gradient))

        humidity_surface = max(0.0, min(1.0, 0.45 + 0.35 * math.cos(lat_r - t * 0.08) + 0.2 * self._stable_noise(lat, lon, t)))
        humidity_mid = max(0.0, min(1.0, 0.4 + 0.42 * math.sin(lat_r * 1.6 + t * 0.1) + 0.18 * self._stable_noise(lat * 0.7, lon * 0.5, t)))
        humidity_high = max(0.0, min(1.0, 0.35 + 0.45 * math.cos(lon_r * 0.9 + t * 0.09) + 0.15 * self._stable_noise(lat * 0.6, lon * 0.3, t + 2)))

        low = max(0.0, min(1.0, macro * 0.7 + humidity_surface * 0.35 + convective_proxy * 0.2 - 0.12))
        mid = max(0.0, min(1.0, macro * 0.6 + humidity_mid * 0.45 + convective_proxy * 0.24 - 0.16))
        high = max(0.0, min(1.0, macro * 0.55 + humidity_high * 0.5 + frontness * 0.22 - 0.18))

        precipitation_factor = max(0.0, min(1.0, (low * 0.5 + mid * 0.7 + convective_proxy * 0.65) - 0.38))
        convection_factor = max(0.0, min(1.0, convective_proxy * 0.85 + precipitation_factor * 0.3))

        return low, mid, high, precipitation_factor, convection_factor

    def _classify_cloud_regime(
        self,
        low: float,
        mid: float,
        high: float,
        precip: float,
        convection: float,
        lat: float,
    ) -> str:
        if convection > 0.72 and precip > 0.55:
            return "deep_convection"
        if high > 0.62 and low < 0.35 and precip < 0.35:
            return "cirrus_sheet"
        if low > 0.66 and mid < 0.46 and high < 0.34 and abs(lat) <= 42:
            return "marine_stratocumulus"
        if (low + mid + high) / 3.0 > 0.52 and mid > 0.48:
            return "frontal_shield"
        return "cumulus_field"

    def _derive_band_architecture(
        self,
        regime: str,
        band: str,
        density: float,
        precip: float,
        convection: float,
        u: float,
        v: float,
    ) -> Dict[str, Any]:
        speed = math.hypot(float(u or 0.0), float(v or 0.0))
        regime_cfg = CLOUD_REGIMES[regime]

        if band == "low":
            base_alt = regime_cfg["base_altitude_m"]
            thickness = _lerp(700.0, 2200.0, density)
            if regime == "marine_stratocumulus":
                thickness *= 0.85
            lateral_scale = _lerp(70.0, regime_cfg["lateral_scale_km"], density)
        elif band == "mid":
            base_alt = max(2200.0, regime_cfg["base_altitude_m"] + 1900.0)
            thickness = _lerp(1000.0, 3200.0, density)
            lateral_scale = _lerp(60.0, regime_cfg["lateral_scale_km"] * 0.78, density)
        else:
            base_alt = max(6500.0, regime_cfg["base_altitude_m"] + 6200.0)
            thickness = _lerp(900.0, 2600.0, density)
            lateral_scale = _lerp(90.0, regime_cfg["lateral_scale_km"] * 1.05, density)

        if regime == "deep_convection":
            if band == "low":
                thickness *= _lerp(1.2, 1.7, convection)
            elif band == "mid":
                thickness *= _lerp(1.3, 1.9, convection)
            else:
                thickness *= _lerp(1.1, 1.6, convection)

        if regime == "cirrus_sheet" and band == "high":
            lateral_scale *= 1.35
            thickness *= 0.72

        top_alt = base_alt + thickness
        coverage = _clamp(density * 0.92 + precip * 0.18, 0.0, 1.0)

        return {
            "density": round(density, 4),
            "base_altitude_m": round(base_alt, 1),
            "top_altitude_m": round(top_alt, 1),
            "thickness_m": round(thickness, 1),
            "coverage": round(coverage, 4),
            "lateral_scale_km": round(lateral_scale, 1),
            "wind": {
                "u": round(float(u or 0.0), 3),
                "v": round(float(v or 0.0), 3),
                "speed_ms": round(speed, 3),
            },
        }

    def _derive_tile_cloud_architecture(
        self,
        tile_id: str,
        lat_center: float,
        lon_center: float,
        low: float,
        mid: float,
        high: float,
        precip: float,
        convection: float,
        wind_low: Dict[str, float],
        wind_mid: Dict[str, float],
        wind_high: Dict[str, float],
        seed: int,
    ) -> Dict[str, Any]:
        regime = self._classify_cloud_regime(low, mid, high, precip, convection, lat_center)
        cfg = CLOUD_REGIMES[regime]

        coverage = _clamp(low * 0.42 + mid * 0.33 + high * 0.25 + precip * 0.12, 0.0, 1.0)
        opacity = _clamp(0.18 + coverage * 0.64 + convection * 0.10, 0.0, 1.0)

        u_low = float((wind_low or {}).get("u", 0.0))
        v_low = float((wind_low or {}).get("v", 0.0))
        u_mid = float((wind_mid or {}).get("u", 0.0))
        v_mid = float((wind_mid or {}).get("v", 0.0))
        u_high = float((wind_high or {}).get("u", 0.0))
        v_high = float((wind_high or {}).get("v", 0.0))

        shear = math.hypot(u_high - u_low, v_high - v_low)
        wind_shear = _clamp(shear / 35.0, 0.0, 1.0)

        bands = {
            "low": self._derive_band_architecture(regime, "low", low, precip, convection, u_low, v_low),
            "mid": self._derive_band_architecture(regime, "mid", mid, precip, convection, u_mid, v_mid),
            "high": self._derive_band_architecture(regime, "high", high, precip, convection, u_high, v_high),
        }

        base_altitude_m = min(bands["low"]["base_altitude_m"], bands["mid"]["base_altitude_m"], bands["high"]["base_altitude_m"])
        top_altitude_m = max(bands["low"]["top_altitude_m"], bands["mid"]["top_altitude_m"], bands["high"]["top_altitude_m"])
        vertical_depth_m = max(0.0, top_altitude_m - base_altitude_m)

        mean_u = (u_low + u_mid + u_high) / 3.0
        mean_v = (v_low + v_mid + v_high) / 3.0
        anvil_dir_deg = (math.degrees(math.atan2(mean_u, mean_v)) + 360.0) % 360.0
        anvil_spread_km = 0.0
        if regime == "deep_convection":
            anvil_spread_km = _lerp(35.0, 140.0, _clamp(convection * 0.75 + wind_shear * 0.25, 0.0, 1.0))
        elif regime in ("frontal_shield", "cirrus_sheet"):
            anvil_spread_km = _lerp(20.0, 90.0, _clamp(high * 0.7 + wind_shear * 0.3, 0.0, 1.0))

        organization = _clamp(0.25 + coverage * 0.35 + precip * 0.15 + convection * 0.25, 0.0, 1.0)
        underside_darkness = _clamp(cfg["underside_darkness"] + precip * 0.16 + convection * 0.14, 0.0, 1.0)
        fringe_softness = _clamp(cfg["fringe_softness"] - convection * 0.10 + high * 0.06, 0.0, 1.0)

        return {
            "regime": regime,
            "coverage": round(coverage, 4),
            "opacity": round(opacity, 4),
            "base_altitude_m": round(base_altitude_m, 1),
            "top_altitude_m": round(top_altitude_m, 1),
            "vertical_depth_m": round(vertical_depth_m, 1),
            "underside_darkness": round(underside_darkness, 4),
            "fringe_softness": round(fringe_softness, 4),
            "organization": round(organization, 4),
            "wind_shear": round(wind_shear, 4),
            "tower_bias": round(_clamp(cfg["tower_bias"] + convection * 0.18, 0.0, 1.0), 4),
            "deck_bias": round(_clamp(cfg["deck_bias"] + low * 0.08 - convection * 0.10, 0.0, 1.0), 4),
            "wispy_bias": round(_clamp(cfg["wispy_bias"] + high * 0.10, 0.0, 1.0), 4),
            "anvil_dir_deg": round(anvil_dir_deg, 2),
            "anvil_spread_km": round(anvil_spread_km, 1),
            "bands": bands,
        }

    def _build_cloud_subcells(self, seed: int, regime: str, coverage: float, convection: float, precip: float) -> List[Dict[str, Any]]:
        rng = random.Random(seed)
        base_count = 4
        if coverage > 0.5:
            base_count += 2
        if convection > 0.55:
            base_count += 2
        if regime == "deep_convection":
            base_count += 2

        subcells = []
        for idx in range(base_count):
            role = "fringe"
            if regime == "deep_convection" and idx == 0:
                role = "tower"
            elif idx < 2:
                role = "core"
            elif regime in ("marine_stratocumulus", "frontal_shield") and idx >= base_count - 2:
                role = "deck"
            elif regime == "cirrus_sheet":
                role = "wispy"

            subcells.append(
                {
                    "id": f"sc-{idx}",
                    "dx": round(rng.uniform(-0.38, 0.38), 4),
                    "dy": round(rng.uniform(-0.38, 0.38), 4),
                    "weight": round(_clamp(rng.uniform(0.45, 1.0) * (0.7 + coverage * 0.3), 0.0, 1.0), 4),
                    "role": role,
                }
            )
        return subcells

    def _cloud_tile_payload(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float, hour_bucket: int) -> Dict[str, Any]:
        lat_center = (lat_min + lat_max) / 2.0
        lon_center = (lon_min + lon_max) / 2.0

        low, mid, high, precip, convection = self._cloud_density_triplet(lat_center, lon_center, hour_bucket)

        lat_r = math.radians(lat_center)
        lon_r = math.radians(lon_center)
        t = hour_bucket / 5.0

        wind_low_u = round(3.5 + 7.5 * math.sin(lat_r + t * 0.15), 3)
        wind_low_v = round(2.0 * math.cos(lon_r - t * 0.12), 3)

        wind_mid_u = round(6.0 + 9.5 * math.sin(lat_r * 0.8 - lon_r * 0.3 + t * 0.14), 3)
        wind_mid_v = round(2.6 * math.cos(lon_r * 0.85 + t * 0.09), 3)

        wind_high_u = round(11.0 + 17.0 * math.sin(lat_r * 0.55 + lon_r * 0.22 - t * 0.16), 3)
        wind_high_v = round(3.8 * math.cos(lon_r * 0.6 - t * 0.11), 3)

        importance = _clamp(
            low * 0.22
            + mid * 0.24
            + high * 0.18
            + precip * 0.18
            + convection * 0.18,
            0.0,
            1.0,
        )

        lat_idx = int((lat_min + 90) // 6)
        lon_idx = int((lon_min + 180) // 6)

        seed = int((abs(lat_center) * 1000 + abs(lon_center) * 100 + hour_bucket) % 10_000_000)
        wind = {
            "low": {"u": wind_low_u, "v": wind_low_v},
            "mid": {"u": wind_mid_u, "v": wind_mid_v},
            "high": {"u": wind_high_u, "v": wind_high_v},
        }
        arch = self._derive_tile_cloud_architecture(
            tile_id=f"gfs-{lat_idx:02d}-{lon_idx:02d}",
            lat_center=lat_center,
            lon_center=lon_center,
            low=low,
            mid=mid,
            high=high,
            precip=precip,
            convection=convection,
            wind_low=wind["low"],
            wind_mid=wind["mid"],
            wind_high=wind["high"],
            seed=seed,
        )

        tile_item = {
            "tile_id": f"gfs-{lat_idx:02d}-{lon_idx:02d}",
            "bounds": {
                "lat_min": round(lat_min, 4),
                "lat_max": round(lat_max, 4),
                "lon_min": round(lon_min, 4),
                "lon_max": round(lon_max, 4),
                "lat_center": round(lat_center, 4),
                "lon_center": round(lon_center, 4),
            },
            "low_density": round(low, 4),
            "mid_density": round(mid, 4),
            "high_density": round(high, 4),
            "precipitation_factor": round(precip, 4),
            "convection_factor": round(convection, 4),
            "altitude_low": 1200,
            "altitude_mid": 4200,
            "altitude_high": 9000,
            "wind": wind,
            "seed": seed,
            "importance": round(importance, 4),
            "updated_at": self._now_ms(),
            **arch,
            "subcells": self._build_cloud_subcells(seed, arch["regime"], arch["coverage"], convection, precip),
        }
        return enrich_cloud_tile_geometry(tile_item)

    def extract_precip_rate_mm_hr(self, datasets: dict[str, Any]) -> Any:
        """Extract precip rate (mm/hr) from available GFS variables."""
        if np is None:
            return None
        surf = datasets.get("surface")
        if surf is None:
            return None
        prate = self.safe_data_var(surf, ["prate", "PRATE", "tp", "unknown"])
        if prate is not None:
            arr = np.asarray(self.squeeze_forecast_array(prate).values, dtype=float) * 3600.0
            return np.clip(arr, 0.0, None)
        apcp = self.safe_data_var(surf, ["apcp", "APCP"])
        if apcp is not None:
            arr = np.asarray(self.squeeze_forecast_array(apcp).values, dtype=float)
            return np.clip(arr, 0.0, None)
        return None

    def compute_layer_rh(self, isobaric_ds: Any, top_hpa: int, bottom_hpa: int) -> Any:
        if np is None or isobaric_ds is None:
            return None

        rh = self.safe_data_var(isobaric_ds, ["r", "RH"])
        if rh is None or "isobaricInhPa" not in rh.dims:
            return None

        levels = np.asarray(isobaric_ds["isobaricInhPa"].values, dtype=float)
        sel = (levels <= bottom_hpa) & (levels >= top_hpa)
        if not np.any(sel):
            return None

        layer = rh.sel(isobaricInhPa=levels[sel])

        for dim in ["time", "step", "valid_time", "surface", "heightAboveGround"]:
            if hasattr(layer, "dims") and dim in layer.dims and layer.sizes.get(dim, 0) > 0:
                layer = layer.isel({dim: 0})

        vals = np.asarray(layer.values, dtype=float)

        if vals.ndim == 3:
            return np.nanmean(vals, axis=0)

        if vals.ndim == 2:
            return vals

        return None

    def estimate_cloud_base_top(self, isobaric_ds: Any, hgt_ds: Any = None) -> dict[str, Any]:
        if np is None or isobaric_ds is None:
            return {}

        rh = self.safe_data_var(isobaric_ds, ["r", "RH"])
        hgt = self.safe_data_var(isobaric_ds, ["gh", "HGT"])
        if rh is None or hgt is None:
            return {}

        rh_arr = self.squeeze_forecast_array(rh, preserve_dims=("isobaricInhPa",))
        hgt_arr = self.squeeze_forecast_array(hgt, preserve_dims=("isobaricInhPa",))
        if rh_arr is None or hgt_arr is None:
            return {}

        rhv = np.asarray(rh_arr.values, dtype=float)
        hgtv = np.asarray(hgt_arr.values, dtype=float)

        try:
            print(
                "[gfs] cloud base/top shapes:",
                f"rh={None if rhv is None else rhv.shape}",
                f"hgt={None if hgtv is None else hgtv.shape}",
            )
        except Exception:
            pass

        if rhv.ndim != 3 or hgtv.ndim != 3:
            return {}

        sat = rhv >= 80.0
        if not np.any(sat):
            return {}

        base_idx = np.argmax(sat, axis=0)
        top_idx = np.maximum(base_idx, rhv.shape[0] - 1 - np.argmax(np.flip(sat, axis=0), axis=0))

        base_m = np.take_along_axis(hgtv, np.expand_dims(base_idx, axis=0), axis=0)[0]
        top_m = np.take_along_axis(hgtv, np.expand_dims(top_idx, axis=0), axis=0)[0]

        return {
            "base_m": base_m,
            "top_m": top_m,
            "thickness_m": np.maximum(0.0, top_m - base_m),
        }

    def derive_cloud_layers(self, surface_ds: Any, agl_ds: Any, isobaric_ds: Any) -> dict[str, Any]:
        """Derive cloud layer occupancy from real GFS fields only.

        This intentionally does not invent default cloud cover.  If RH/TCDC/height
        fields are unavailable, callers receive an empty dict and the frontend can
        report `source_state=unavailable` instead of drawing mock clouds.
        """
        if np is None:
            return {}
        tcdc = None
        if surface_ds is not None:
            da = self.safe_data_var(surface_ds, ["tcc", "TCDC"])
            if da is not None:
                squeezed = self.squeeze_forecast_array(da)
                if squeezed is not None:
                    tcdc = np.asarray(squeezed.values, dtype=float)
        low = self.compute_layer_rh(isobaric_ds, 850, 1000)
        mid = self.compute_layer_rh(isobaric_ds, 600, 850)
        high = self.compute_layer_rh(isobaric_ds, 300, 600)

        for name, arr in [("low", low), ("mid", mid), ("high", high)]:
            if arr is not None and getattr(arr, "ndim", 0) != 2:
                if name == "low":
                    low = None
                elif name == "mid":
                    mid = None
                elif name == "high":
                    high = None
        if tcdc is not None and getattr(tcdc, "ndim", 0) != 2:
            tcdc = None

        try:
            print(
                "[gfs] layer shapes:",
                f"low={None if low is None else low.shape}",
                f"mid={None if mid is None else mid.shape}",
                f"high={None if high is None else high.shape}",
                f"tcdc={None if tcdc is None else tcdc.shape}",
            )
        except Exception:
            pass

        shape = None
        for arr in (low, mid, high, tcdc):
            if arr is not None:
                shape = tuple(np.asarray(arr).shape)
                break
        if not shape:
            log.warning("[gfs] no real cloud RH/TCDC fields available; cloud layer payload empty")
            return {}

        def occ(arr: Any) -> Any:
            if arr is None:
                return np.zeros(shape, dtype=float)
            return np.clip(np.asarray(arr, dtype=float) / 100.0, 0.0, 1.0)

        low_occ = occ(low)
        mid_occ = occ(mid)
        high_occ = occ(high)
        if tcdc is not None:
            tcdc_n = np.clip(np.asarray(tcdc, dtype=float) / 100.0, 0.0, 1.0)
            if tuple(tcdc_n.shape) == tuple(shape):
                if low is None and mid is None and high is None:
                    # TCDC is real, but it is a total field.  Split it conservatively so
                    # the visual shell exists without fabricating extra cover.
                    low_occ = np.clip(tcdc_n * 0.48, 0.0, 1.0)
                    mid_occ = np.clip(tcdc_n * 0.34, 0.0, 1.0)
                    high_occ = np.clip(tcdc_n * 0.18, 0.0, 1.0)
                else:
                    low_occ = np.clip(low_occ * 0.7 + tcdc_n * 0.3, 0.0, 1.0)
                    mid_occ = np.clip(mid_occ * 0.75 + tcdc_n * 0.25, 0.0, 1.0)
                    high_occ = np.clip(high_occ * 0.78 + tcdc_n * 0.22, 0.0, 1.0)
            else:
                log.warning("[gfs] skipping TCDC blend due to shape mismatch tcdc=%s target=%s", tuple(tcdc_n.shape), tuple(shape))
        alt = self.estimate_cloud_base_top(isobaric_ds)
        source_fields = [name for name, arr in (("rh_low", low), ("rh_mid", mid), ("rh_high", high), ("tcdc", tcdc)) if arr is not None]
        return {"low": low_occ, "mid": mid_occ, "high": high_occ, "alt": alt, "source_fields": source_fields}

    def wind_speed_dir_from_uv(self, u: float, v: float) -> tuple[float, float]:
        speed_mps = math.hypot(u, v)
        heading_deg = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
        return speed_mps, heading_deg

    def derive_balloon_vectors(self, datasets: dict[str, Any], target_ft: int = 10000) -> list[dict[str, Any]]:
        if np is None:
            return []
        iso = datasets.get("isobaricInhPa")
        if iso is None:
            return []
        u_da = self.safe_data_var(iso, ["u", "UGRD"])
        v_da = self.safe_data_var(iso, ["v", "VGRD"])
        if u_da is None or v_da is None or "isobaricInhPa" not in u_da.dims:
            return []
        levels = np.asarray(iso["isobaricInhPa"].values, dtype=float)
        preferred = [250.0, 300.0]
        level = None
        for cand in preferred:
            if cand in levels:
                level = cand
                break
        if level is None:
            level = float(levels[np.argmin(np.abs(levels - 275.0))])
        if not math.isfinite(level):
            level = 300.0
        u = np.asarray(self.squeeze_forecast_array(u_da.sel(isobaricInhPa=level)).values, dtype=float)
        v = np.asarray(self.squeeze_forecast_array(v_da.sel(isobaricInhPa=level)).values, dtype=float)
        lat2d, lon2d = self.ensure_lat_lon_2d(iso)
        if lat2d is None or lon2d is None:
            return []
        vectors: list[dict[str, Any]] = []
        step_y = max(1, u.shape[0] // 18)
        step_x = max(1, u.shape[1] // 36)
        for iy in range(0, u.shape[0], step_y):
            for ix in range(0, u.shape[1], step_x):
                speed_mps, heading_deg = self.wind_speed_dir_from_uv(float(u[iy, ix]), float(v[iy, ix]))
                altitude_m = 10500.0 if level <= 300.0 else 9800.0
                vectors.append({
                    "lat": float(lat2d[iy, ix]),
                    "lon": float(lon2d[iy, ix]),
                    "u": float(u[iy, ix]),
                    "v": float(v[iy, ix]),
                    "speed_mps": round(speed_mps, 3),
                    "speed_mph": round(speed_mps * 2.23694, 2),
                    "heading_deg": round(heading_deg, 2),
                    "altitude_m": round(altitude_m, 1),
                    "source_level": f"{int(level)} hPa",
                })
        return vectors

    def derive_hail_mask(self, datasets: dict[str, Any], precip_mm_hr: Any, cloud_layers: dict[str, Any], *, target_shape: tuple[int, int] | None = None, warned: set[str] | None = None) -> Any:
        if np is None or precip_mm_hr is None:
            return None
        precip_arr = np.asarray(precip_mm_hr, dtype=float)
        if target_shape is None:
            target_shape = tuple(precip_arr.shape)
        precip_arr = self._coerce_field_to_canonical_grid(precip_arr, target_shape, "precip_mm_hr", warned)

        surf = datasets.get("surface")
        cape = None
        if surf is not None:
            da = self.safe_data_var(surf, ["cape", "CAPE"])
            if da is not None:
                cape = np.asarray(self.squeeze_forecast_array(da).values, dtype=float)
        deep = cloud_layers.get("high") if isinstance(cloud_layers, dict) else None
        if cape is None:
            cape = np.zeros(target_shape, dtype=float)
        else:
            cape = self._coerce_field_to_canonical_grid(cape, target_shape, "cape", warned)
        if deep is None:
            deep = np.zeros(target_shape, dtype=float)
        else:
            deep = self._coerce_field_to_canonical_grid(deep, target_shape, "deep_cloud_mask", warned)

        if tuple(cape.shape) != tuple(precip_arr.shape) or tuple(deep.shape) != tuple(precip_arr.shape):
            raise ValueError(
                f"hail_mask_shape_mismatch cape={tuple(cape.shape)} precip={tuple(precip_arr.shape)} deep={tuple(deep.shape)}"
            )
        return (cape > 900.0) & (precip_arr > 3.0) & (deep > 0.55)

    def derive_lightning_mask(self, datasets: dict[str, Any], precip_mm_hr: Any, cloud_layers: dict[str, Any], *, target_shape: tuple[int, int] | None = None, warned: set[str] | None = None) -> Any:
        if np is None or precip_mm_hr is None:
            return None
        precip_arr = np.asarray(precip_mm_hr, dtype=float)
        if target_shape is None:
            target_shape = tuple(precip_arr.shape)
        precip_arr = self._coerce_field_to_canonical_grid(precip_arr, target_shape, "precip_mm_hr_lightning", warned)

        surf = datasets.get("surface")
        cape = np.zeros(target_shape, dtype=float)
        cin = np.zeros(target_shape, dtype=float)
        if surf is not None:
            d_cape = self.safe_data_var(surf, ["cape", "CAPE"])
            d_cin = self.safe_data_var(surf, ["cin", "CIN"])
            if d_cape is not None:
                cape = self._coerce_field_to_canonical_grid(np.asarray(self.squeeze_forecast_array(d_cape).values, dtype=float), target_shape, "cape_lightning", warned)
            if d_cin is not None:
                cin = self._coerce_field_to_canonical_grid(np.asarray(self.squeeze_forecast_array(d_cin).values, dtype=float), target_shape, "cin_lightning", warned)
        high = cloud_layers.get("high") if isinstance(cloud_layers, dict) else np.zeros(target_shape, dtype=float)
        high = self._coerce_field_to_canonical_grid(high, target_shape, "high_cloud_lightning", warned)
        return (cape > 650.0) & (cin > -160.0) & (precip_arr > 1.4) & (high > 0.5)

    def threshold_to_mask(self, array: Any, threshold: float) -> Any:
        if np is None or array is None:
            return None
        return np.asarray(array) >= threshold

    def connected_components_or_simple_cell_polygons(self, mask: Any, lat2d: Any, lon2d: Any) -> list[dict[str, Any]]:
        if np is None or mask is None or lat2d is None or lon2d is None:
            return []
        polys: list[dict[str, Any]] = []
        ys, xs = np.where(mask)
        for y, x in zip(ys.tolist(), xs.tolist()):
            dlat = 0.12
            dlon = 0.12
            ring = close_ring([
                {"lat": float(lat2d[y, x] - dlat), "lng": float(lon2d[y, x] - dlon)},
                {"lat": float(lat2d[y, x] - dlat), "lng": float(lon2d[y, x] + dlon)},
                {"lat": float(lat2d[y, x] + dlat), "lng": float(lon2d[y, x] + dlon)},
                {"lat": float(lat2d[y, x] + dlat), "lng": float(lon2d[y, x] - dlon)},
            ])
            polys.append({"points": ring})
            if len(polys) >= 3500:
                break
        return polys


    def derive_precip_columns_from_tiles(self, tiles: list[dict[str, Any]], max_items: int | None = None) -> list[dict[str, Any]]:
        """Build visualization-oriented precipitation columns from tile proxies.

        These are estimated scene hints (not direct observed column geometry).
        """
        out: list[dict[str, Any]] = []
        ranked = sorted(tiles or [], key=lambda t: float((t or {}).get("precip_rate") or ((t or {}).get("precipitation_factor", 0) * 45.0)), reverse=True)
        for t in ranked:
            precip_rate = float(t.get("precip_rate") or (float(t.get("precipitation_factor", 0.0)) * 45.0))
            if precip_rate < 1.2:
                continue
            bounds = t.get("bounds") or {}
            base_alt = float(t.get("base_altitude_m") or ((t.get("bands") or {}).get("low") or {}).get("base_altitude_m") or 1800.0)
            top_alt = float(t.get("top_altitude_m") or ((t.get("bands") or {}).get("mid") or {}).get("top_altitude_m") or 5200.0)
            virga_stop = 0.0
            if precip_rate < 3.0 and base_alt > 2200.0:
                virga_stop = min(base_alt - 120.0, max(250.0, base_alt * 0.18))
            out.append({
                "tile_id": t.get("tile_id"),
                "lat": float(bounds.get("lat_center") or 0.0),
                "lon": float(bounds.get("lon_center") or 0.0),
                "estimated_source_altitude_m": round(max(300.0, base_alt), 1),
                "estimated_top_altitude_m": round(max(base_alt, top_alt), 1),
                "estimated_surface_altitude_m": round(max(0.0, virga_stop), 1),
                "estimated_precip_rate_mm_hr": round(max(0.0, precip_rate), 3),
                "estimated_intensity": round(max(0.0, min(1.0, precip_rate / 45.0)), 4),
                "type_hint": "convective" if float(t.get("storm_energy") or t.get("convection_factor") or 0.0) > 0.62 else "rain",
                "wind_u": float(t.get("wind_u") or ((t.get("wind") or {}).get("mid") or {}).get("u") or 0.0),
                "wind_v": float(t.get("wind_v") or ((t.get("wind") or {}).get("mid") or {}).get("v") or 0.0),
                "estimated": True,
            })
            if max_items is not None and len(out) >= int(max_items):
                break
        return out

    def derive_lightning_events_from_tiles(self, tiles: list[dict[str, Any]], max_items: int = 140) -> list[dict[str, Any]]:
        """Build estimated lightning events from storm-strength proxies."""
        out: list[dict[str, Any]] = []
        ranked = sorted(tiles or [], key=lambda t: float(t.get("storm_energy") or t.get("convection_factor") or 0.0), reverse=True)
        for t in ranked:
            energy = float(t.get("storm_energy") or t.get("convection_factor") or 0.0)
            precip = float(t.get("precip_rate") or (float(t.get("precipitation_factor") or 0.0) * 45.0))
            if energy < 0.48 or precip < 4.0:
                continue
            bounds = t.get("bounds") or {}
            top_alt = float(t.get("top_altitude_m") or ((t.get("bands") or {}).get("high") or {}).get("top_altitude_m") or 9000.0)
            out.append({
                "tile_id": t.get("tile_id"),
                "lat": float(bounds.get("lat_center") or 0.0),
                "lon": float(bounds.get("lon_center") or 0.0),
                "estimated_flash_top_m": round(max(900.0, top_alt), 1),
                "estimated_flash_bottom_m": round(max(80.0, top_alt * (0.24 if energy > 0.72 else 0.42)), 1),
                "estimated_energy": round(max(0.0, min(1.0, energy)), 4),
                "severity_hint": "severe" if energy > 0.72 else "active",
                "estimated": True,
            })
            if len(out) >= max_items:
                break
        return out
    def serialize_cloud_payload(self, tiles: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        source = meta.get("source", "gfs_nomads")
        payload_state = meta.get("payload_state") or ("live" if source == "gfs_nomads" else "unavailable")
        bbox = meta.get("bbox") or meta.get("bbox_used") or meta.get("requested_bbox")
        diagnostics = dict(meta.get("diagnostics") or {})
        if bbox is not None:
            diagnostics.setdefault("requested_bbox", bbox)
            diagnostics.setdefault("fetch_bbox", bbox)
        diagnostics.setdefault("schema_contract", "tiles/items are authoritative; polygon_field_v1 is not used for cloud bodies")
        cloud_regions = meta.get("cloud_regions") or []
        if not cloud_regions and tiles:
            cloud_regions = [t.get("cloud_region") for t in tiles if isinstance(t, dict) and isinstance(t.get("cloud_region"), dict)]
        return {
            "ok": True,
            "schema": meta.get("schema") or "gfs_cloud_regions_v1",
            "source": source,
            "source_state": payload_state,
            "payload_state": payload_state,
            "heuristic": bool(meta.get("heuristic", source != "gfs_nomads")),
            "updated_at": self._now_ms(),
            "tiles": tiles,
            "items": tiles,
            "features": tiles,
            "cloud_regions": cloud_regions,
            "cloud_region_count": len(cloud_regions),
            "summary": {"tile_count": len(tiles), "cloud_region_count": len(cloud_regions), "cloud_shell_count": len(tiles), "real_data": source == "gfs_nomads"},
            "cycle": meta.get("cycle"),
            "forecast_hour": meta.get("forecast_hour"),
            "valid_time": meta.get("valid_time"),
            "bbox": bbox,
            "bbox_used": bbox,
            "requested_bbox": meta.get("requested_bbox") or bbox,
            "diagnostics": diagnostics,
            "fields_available": meta.get("fields_available") or [],
            "fields_missing": meta.get("fields_missing") or [],
        }

    def serialize_rain_payload(self, rain_polygons: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": rain_polygons, "count": len(rain_polygons)}

    def serialize_hail_payload(self, hail_polygons: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": hail_polygons, "count": len(hail_polygons)}

    def serialize_lightning_payload(self, lightning_polygons: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": lightning_polygons, "count": len(lightning_polygons)}

    def serialize_balloon_payload(self, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": vectors, "count": len(vectors)}

    def bbox_cache_key(self, bbox: dict[str, float]) -> str:
        return f"{bbox.get('west')}:{bbox.get('south')}:{bbox.get('east')}:{bbox.get('north')}"

    def payload_cache_key(self, cycle: str, forecast_hour: int, bbox: dict[str, float], grid_tag: str = "na") -> str:
        return f"gfs_payload:v3:{cycle}:{forecast_hour}:{grid_tag}:{self.bbox_cache_key(bbox)}"

    def write_cached_payload(self, key: str, payload: Any) -> None:
        if self.disk_cache is None:
            return
        self.disk_cache.set(key, payload, expire=DEFAULT_GFS_CACHE_TTL_SECONDS)

    def _tile_from_real_fields(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float, low: float, mid: float, high: float, precip: float, conv: float, u: float, v: float, seed_hint: str) -> dict[str, Any]:
        hour_bucket = int(time.time() // 3600)
        tile = self._cloud_tile_payload(lat_min, lat_max, lon_min, lon_max, hour_bucket)
        tile["low_density"] = round(float(low), 4)
        tile["mid_density"] = round(float(mid), 4)
        tile["high_density"] = round(float(high), 4)
        tile["precipitation_factor"] = round(float(precip), 4)
        tile["convection_factor"] = round(float(conv), 4)
        tile["seed"] = stable_hash_u32(seed_hint)
        for b in ("low", "mid", "high"):
            tile["bands"][b]["wind"]["u"] = round(float(u), 3)
            tile["bands"][b]["wind"]["v"] = round(float(v), 3)
        return enrich_cloud_tile_geometry(tile)

    def _grid_tag_from_shape(self, shape: tuple[int, ...] | None) -> str:
        if not shape or len(shape) < 2:
            return "na"
        return f"{int(shape[0])}x{int(shape[1])}"

    def _shape_of(self, arr: Any) -> tuple[int, ...] | None:
        if arr is None or np is None:
            return None
        try:
            a = np.asarray(arr)
            return tuple(a.shape)
        except Exception:
            return None

    def _coerce_to_shape(self, arr: Any, target_shape: tuple[int, ...], field_name: str) -> Any:
        if arr is None or np is None:
            return None
        try:
            a = np.asarray(arr, dtype=float)
        except Exception:
            log.warning("[gfs] dropping field=%s due to non-numeric data", field_name)
            return None
        if tuple(a.shape) == tuple(target_shape):
            return a
        log.warning("[gfs] dropping field=%s due to shape mismatch arr_shape=%s canonical=%s", field_name, tuple(a.shape), tuple(target_shape))
        return None

    def ensure_same_grid(self, field: Any, target_shape: tuple[int, int], field_name: str) -> Any:
        out = self._coerce_to_shape(field, target_shape, field_name)
        if out is None:
            got = self._shape_of(field)
            raise ValueError(f"live_grid_mismatch field={field_name} got={got} expected={target_shape}")
        return out

    def _resample_2d_to_shape(self, arr2d: Any, target_shape: tuple[int, int]) -> Any:
        src = np.asarray(arr2d, dtype=float)
        if src.ndim != 2:
            raise ValueError(f"expected_2d_field got_ndim={src.ndim}")
        th, tw = int(target_shape[0]), int(target_shape[1])
        if th <= 0 or tw <= 0:
            raise ValueError(f"invalid_target_shape={target_shape}")
        if tuple(src.shape) == (th, tw):
            return src
        y_idx = np.rint(np.linspace(0, src.shape[0] - 1, th)).astype(int)
        x_idx = np.rint(np.linspace(0, src.shape[1] - 1, tw)).astype(int)
        return src[np.ix_(y_idx, x_idx)]

    def _coerce_field_to_canonical_grid(self, field: Any, target_shape: tuple[int, int], field_name: str, warned: set[str] | None = None) -> Any:
        if np is None or field is None:
            return field
        arr = np.asarray(field, dtype=float)
        if arr.ndim != 2:
            raise ValueError(f"{field_name}_expected_2d got_shape={tuple(arr.shape)}")
        if tuple(arr.shape) == tuple(target_shape):
            return arr
        out = self._resample_2d_to_shape(arr, target_shape)
        warn_key = f"{field_name}:{tuple(arr.shape)}->{tuple(target_shape)}"
        if warned is None or warn_key not in warned:
            log.warning("[gfs] realigning field=%s from_shape=%s to canonical_shape=%s", field_name, tuple(arr.shape), tuple(target_shape))
            if warned is not None:
                warned.add(warn_key)
        return out

    def _derive_real_source_fields(self, groups: dict[str, Any]) -> dict[str, Any]:
        precip = self.extract_precip_rate_mm_hr(groups)
        hagl = groups.get("10m")
        if hagl is None:
            hagl = groups.get("2m")
        cloud_layers = self.derive_cloud_layers(groups.get("surface"), hagl, groups.get("isobaricInhPa"))
        vectors = self.derive_balloon_vectors(groups)
        if precip is None or not cloud_layers:
            raise RuntimeError("missing precip/cloud arrays")

        sample_ds = groups.get("surface")
        if sample_ds is None:
            sample_ds = groups.get("isobaricInhPa")
        if sample_ds is None:
            sample_ds = groups.get("10m")
        if sample_ds is None:
            sample_ds = groups.get("2m")
        lat2d, lon2d = self.ensure_lat_lon_2d(sample_ds)
        if lat2d is None or lon2d is None:
            raise RuntimeError("missing lat lon grid")

        canonical_shape = tuple(np.asarray(precip).shape)
        log.info("[gfs] canonical live grid selected shape=%s", canonical_shape)
        source_shapes = {
            "lat2d": tuple(np.asarray(lat2d).shape),
            "lon2d": tuple(np.asarray(lon2d).shape),
            "precip": tuple(np.asarray(precip).shape),
            "cloud_low": self._shape_of(cloud_layers.get("low")),
            "cloud_mid": self._shape_of(cloud_layers.get("mid")),
            "cloud_high": self._shape_of(cloud_layers.get("high")),
        }
        high = self._coerce_to_shape(cloud_layers.get("high"), canonical_shape, "cloud_high")
        low = self._coerce_to_shape(cloud_layers.get("low"), canonical_shape, "cloud_low")
        mid = self._coerce_to_shape(cloud_layers.get("mid"), canonical_shape, "cloud_mid")
        if high is None:
            high = np.zeros(canonical_shape, dtype=float)
        if low is None:
            low = np.zeros(canonical_shape, dtype=float)
        if mid is None:
            mid = np.zeros(canonical_shape, dtype=float)
        lat2d_live = self.ensure_same_grid(lat2d, canonical_shape, "hazard_lat2d")
        lon2d_live = self.ensure_same_grid(lon2d, canonical_shape, "hazard_lon2d")
        precip_live = self.ensure_same_grid(precip, canonical_shape, "hazard_precip_mm_hr")
        low_live = self.ensure_same_grid(low, canonical_shape, "hazard_cloud_low")
        mid_live = self.ensure_same_grid(mid, canonical_shape, "hazard_cloud_mid")
        high_live = self.ensure_same_grid(high, canonical_shape, "hazard_cloud_high")

        realigned_fields = [
            name for name, shape in source_shapes.items()
            if shape and len(shape) >= 2 and (int(shape[0]), int(shape[1])) != canonical_shape
        ]
        if realigned_fields:
            log.warning(
                "[gfs] hazard fields resampled once from source shapes=%s to canonical=%s",
                {k: v for k, v in source_shapes.items() if k in realigned_fields},
                canonical_shape,
            )

        conv = np.clip((precip_live / 30.0) * 0.6 + high_live * 0.4, 0.0, 1.0)
        humidity = self._extract_scalar_field(groups, [("isobaricInhPa", ["r", "RH"]), ("surface", ["r", "RH"])])
        wind_u = self._extract_scalar_field(groups, [("10m", ["u", "UGRD"]), ("isobaricInhPa", ["u", "UGRD"]), ("surface", ["u", "UGRD"])])
        wind_v = self._extract_scalar_field(groups, [("10m", ["v", "VGRD"]), ("isobaricInhPa", ["v", "VGRD"]), ("surface", ["v", "VGRD"])])
        wind_u = self.ensure_same_grid(wind_u, canonical_shape, "wind_u") if wind_u is not None else None
        wind_v = self.ensure_same_grid(wind_v, canonical_shape, "wind_v") if wind_v is not None else None
        wind_speed = np.sqrt(np.square(wind_u) + np.square(wind_v)) if wind_u is not None and wind_v is not None else (np.sqrt(np.square(vectors[0]["u"]) + np.square(vectors[0]["v"])) if vectors else np.zeros_like(precip))
        temp_k = self._extract_scalar_field(groups, [("surface", ["t", "TMP", "tmp"]), ("2m", ["t", "TMP", "tmp"])])
        pressure_pa = self._extract_scalar_field(groups, [("meanSea", ["prmsl", "PRMSL"]), ("surface", ["prmsl", "PRMSL"])])
        temp_k = self.ensure_same_grid(temp_k, canonical_shape, "temperature_k") if temp_k is not None else None
        pressure_pa = self.ensure_same_grid(pressure_pa, canonical_shape, "pressure_pa") if pressure_pa is not None else None

        lat2d = self._downsample_2d(lat2d_live, SCENE_DOWNSAMPLE_STRIDE)
        lon2d = self._downsample_2d(lon2d_live, SCENE_DOWNSAMPLE_STRIDE)
        precip = self._downsample_2d(precip_live, SCENE_DOWNSAMPLE_STRIDE)
        canonical_ds_shape = tuple(np.asarray(precip).shape)
        lat2d = self._coerce_to_shape(lat2d, canonical_ds_shape, "lat2d")
        lon2d = self._coerce_to_shape(lon2d, canonical_ds_shape, "lon2d")
        if lat2d is None or lon2d is None:
            raise RuntimeError(f"live_cloud_latlon_grid_invalid shape={canonical_ds_shape}; refusing synthetic world-grid cloud coordinates")
        low = self._downsample_2d(low_live, SCENE_DOWNSAMPLE_STRIDE)
        mid = self._downsample_2d(mid_live, SCENE_DOWNSAMPLE_STRIDE)
        high = self._downsample_2d(high_live, SCENE_DOWNSAMPLE_STRIDE)
        conv = self._downsample_2d(conv, SCENE_DOWNSAMPLE_STRIDE)
        humidity = self._downsample_2d(humidity, SCENE_DOWNSAMPLE_STRIDE) if humidity is not None else None
        wind_u = self._downsample_2d(wind_u, SCENE_DOWNSAMPLE_STRIDE) if wind_u is not None else None
        wind_v = self._downsample_2d(wind_v, SCENE_DOWNSAMPLE_STRIDE) if wind_v is not None else None
        wind_speed = self._downsample_2d(wind_speed, SCENE_DOWNSAMPLE_STRIDE) if wind_speed is not None else None
        temp_k = self._downsample_2d(temp_k, SCENE_DOWNSAMPLE_STRIDE) if temp_k is not None else None
        pressure_pa = self._downsample_2d(pressure_pa, SCENE_DOWNSAMPLE_STRIDE) if pressure_pa is not None else None

        cloud_layers_ds = {
            "low": low,
            "mid": mid,
            "high": high,
        }

        return {
            "canonical_shape": tuple(canonical_shape),
            "precip": precip,
            "cloud_layers": cloud_layers_ds,
            "vectors": vectors,
            "lat2d": lat2d,
            "lon2d": lon2d,
            "high": high,
            "low": low,
            "mid": mid,
            "conv": conv,
            "cloud_density": np.clip((low + mid + high) / 3.0, 0.0, 1.0),
            "precip_rate": precip,
            "wind_speed": wind_speed,
            "temperature_k": temp_k,
            "pressure_pa": pressure_pa,
            "humidity": humidity,
            "wind_u": wind_u,
            "wind_v": wind_v,
            "hazard_inputs": {
                "lat2d": lat2d_live,
                "lon2d": lon2d_live,
                "precip": precip_live,
                "cloud_layers": {
                    "low": low_live,
                    "mid": mid_live,
                    "high": high_live,
                },
                "source_shapes": source_shapes,
                "canonical_shape": tuple(canonical_shape),
                "resampled": bool(realigned_fields),
                "resampled_fields": list(realigned_fields),
                "created_at_ms": self._now_ms(),
            },
        }

    def _assert_same_shape(self, label: str, *arrays: Any) -> None:
        shapes: list[tuple[int, ...]] = []
        for arr in arrays:
            if arr is None:
                continue
            try:
                shape = tuple(np.asarray(arr).shape)
            except Exception:
                continue
            shapes.append(shape)
        unique = {s for s in shapes}
        if len(unique) > 1:
            raise ValueError(f"{label} shape mismatch: {shapes}")


    def _extract_scalar_field(self, groups: dict[str, Any], candidates: list[tuple[str, list[str]]]) -> Any:
        for group_name, names in candidates:
            ds = groups.get(group_name)
            arr = self.safe_data_var(ds, names)
            arr = self.squeeze_forecast_array(arr)
            if arr is None:
                continue
            if np is None:
                continue
            try:
                vals = np.asarray(arr.values, dtype=float)
                if vals.ndim == 2 and vals.size:
                    return vals
            except Exception:
                continue
        return None

    def _downsample_2d(self, arr: Any, stride: int) -> Any:
        if np is None or arr is None:
            return arr
        try:
            a = np.asarray(arr, dtype=float)
        except Exception:
            return arr
        if a.ndim != 2:
            return arr
        step = max(1, int(stride))
        return a[::step, ::step]

    def _json_grid(self, arr: Any, stride: int = 1, precision: int = 4) -> list[list[float]]:
        """Small JSON grid helper for frontend payload contracts/debug panels."""
        if np is None or arr is None:
            return []
        try:
            a = np.asarray(arr, dtype=float)
        except Exception:
            return []
        if a.ndim != 2 or not a.size:
            return []
        step = max(1, int(stride or 1))
        a = a[::step, ::step]
        a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
        return np.round(a.astype(float), int(precision)).tolist()

    def _store_scalar_fields(self, fields: dict[str, Any]) -> None:
        if np is None:
            return
        lat2d = fields.get("lat2d")
        lon2d = fields.get("lon2d")
        if lat2d is None or lon2d is None:
            return
        try:
            lat_arr = np.asarray(lat2d, dtype=float)
            lon_arr = np.asarray(lon2d, dtype=float)
        except Exception:
            return
        lat_arr = self._downsample_2d(lat_arr, SCALAR_DOWNSAMPLE_STRIDE)
        lon_arr = self._downsample_2d(lon_arr, SCALAR_DOWNSAMPLE_STRIDE)
        scalar_fields: dict[str, dict[str, Any]] = {}
        for key in ("cloud_density", "precip_rate", "temperature_k", "pressure_pa", "wind_u", "wind_v", "wind_speed", "humidity"):
            val = fields.get(key)
            if val is None:
                continue
            try:
                arr = np.asarray(val, dtype=float)
            except Exception:
                continue
            if arr.ndim != 2:
                continue
            arr = self._downsample_2d(arr, SCALAR_DOWNSAMPLE_STRIDE)
            scalar_fields[key] = {
                "lat": lat_arr.astype(np.float32).tolist(),
                "lon": lon_arr.astype(np.float32).tolist(),
                "values": arr.astype(np.float32).tolist(),
                "updated_at": self._now_ms(),
            }
        self.state.scalar_fields = scalar_fields

    def _lat_lon_to_tile(self, lat: float, lon: float, z: int) -> dict[str, int]:
        lat = max(-85.05112878, min(85.05112878, safe_float(lat, 0.0)))
        lon = max(-180.0, min(180.0, safe_float(lon, 0.0)))
        n = 2 ** max(0, int(z))
        x = int((lon + 180.0) / 360.0 * n)
        lat_rad = math.radians(lat)
        y = int((1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
        return {"x": max(0, min(n - 1, x)), "y": max(0, min(n - 1, y))}

    def _cloud_intensity_arrays(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Build normalized cloud intelligence fields from the live weather grid.

        This is the shared "sea of weather data" product for clouds/rain:
        low/mid/high cover, total cloud intensity, rain intensity, and tower
        signal are all kept on the native/canonical lat/lon grid. Rendering
        can simplify later; this function does not intentionally coarsen the
        science grid.
        """
        if np is None:
            return {}
        try:
            low = np.asarray(fields.get("low"), dtype=float)
            mid = np.asarray(fields.get("mid"), dtype=float)
            high = np.asarray(fields.get("high"), dtype=float)
            precip = np.asarray(fields.get("precip"), dtype=float)
            conv = np.asarray(fields.get("conv"), dtype=float)
            lat2d = np.asarray(fields.get("lat2d"), dtype=float)
            lon2d = np.asarray(fields.get("lon2d"), dtype=float)
        except Exception:
            return {}
        if low.ndim != 2 or lat2d.shape != low.shape or lon2d.shape != low.shape:
            return {}
        def norm_cloud(a: Any) -> Any:
            arr = np.nan_to_num(np.asarray(a, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
            # Some decoders return 0..1, others 0..100.
            if np.nanmax(arr) > 1.5:
                arr = arr / 100.0
            return np.clip(arr, 0.0, 1.0)
        low_n = norm_cloud(low)
        mid_n = norm_cloud(mid)
        high_n = norm_cloud(high)
        total = np.clip(np.maximum.reduce([low_n, mid_n, high_n]) * 0.62 + (low_n + mid_n + high_n) * 0.18, 0.0, 1.0)
        precip_n = np.clip(np.nan_to_num(precip, nan=0.0, posinf=0.0, neginf=0.0) / 45.0, 0.0, 1.0)
        conv_n = np.clip(np.nan_to_num(conv, nan=0.0, posinf=0.0, neginf=0.0), 0.0, 1.0)
        rain = np.clip((precip_n * 0.78) + (total * 0.22), 0.0, 1.0)
        tower = np.clip((high_n * 0.30) + (mid_n * 0.28) + (low_n * 0.18) + (precip_n * 0.34) + (conv_n * 0.45), 0.0, 1.0)
        return {"lat2d": lat2d, "lon2d": lon2d, "low": low_n, "mid": mid_n, "high": high_n, "total": total, "rain": rain, "tower": tower, "precip_norm": precip_n, "conv": conv_n}

    def _classify_cloud_region(self, low_v: float, mid_v: float, high_v: float, rain_v: float, tower_v: float) -> dict[str, Any]:
        """Classify a contour component into one of the visible cloud regimes."""
        if tower_v >= 0.72 and rain_v >= 0.18:
            return {"family": "vertical", "type": "cumulonimbus", "roles": ["base", "tower", "anvil", "rain_core"]}
        if tower_v >= 0.55:
            return {"family": "vertical", "type": "towering_cumulus", "roles": ["base", "tower", "turret"]}
        if rain_v >= 0.28 and (low_v + mid_v) >= 0.55:
            return {"family": "stratiform", "type": "nimbostratus", "roles": ["deep_rain_deck", "rain_core"]}
        if low_v >= 0.42 and mid_v >= 0.25:
            return {"family": "stratiform", "type": "stratocumulus", "roles": ["lumpy_deck", "fringe"]}
        if low_v >= 0.36:
            return {"family": "stratiform", "type": "stratus", "roles": ["low_sheet", "fringe"]}
        if mid_v >= 0.45 and high_v < 0.35:
            return {"family": "cumuliform", "type": "altocumulus", "roles": ["puff_field", "core"]}
        if mid_v >= 0.32:
            return {"family": "stratiform", "type": "altostratus", "roles": ["broad_deck", "fringe"]}
        if high_v >= 0.45 and low_v < 0.22 and mid_v < 0.30:
            return {"family": "cirriform", "type": "cirrus", "roles": ["wispy", "feather"]}
        if high_v >= 0.32:
            return {"family": "cirriform", "type": "cirrostratus", "roles": ["veil", "sheet"]}
        return {"family": "cumuliform", "type": "cumulus", "roles": ["base", "core", "dome"]}

    def _component_ring_from_cells(self, lat2d: Any, lon2d: Any, cells: list[tuple[int, int]], salt: int = 0) -> list[dict[str, float]]:
        """Create a stable organic footprint for a marching-squares component.

        This is a lightweight server-side contour footprint. It avoids drawing
        every grid cell by wrapping the component's point cloud with an organic
        ellipse/rounded hull; the exact field threshold and component cells are
        still derived from marching-square-style masks.
        """
        if not cells:
            return []
        lats = [float(lat2d[y, x]) for y, x in cells if np.isfinite(lat2d[y, x]) and np.isfinite(lon2d[y, x])]
        lons = [float(lon2d[y, x]) for y, x in cells if np.isfinite(lat2d[y, x]) and np.isfinite(lon2d[y, x])]
        if not lats or not lons:
            return []
        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)
        lat_c = (lat_min + lat_max) * 0.5
        lon_c = (lon_min + lon_max) * 0.5
        lat_r = max(0.025, (lat_max - lat_min) * 0.62)
        lon_r = max(0.025, (lon_max - lon_min) * 0.62)
        # Wider/larger components get more vertices, capped to keep payload fast.
        points = max(10, min(30, int(10 + math.sqrt(len(cells)) * 1.2)))
        ring: list[dict[str, float]] = []
        for i in range(points):
            ang = (2.0 * math.pi * i) / points
            n1 = (stable_unit_float(f"cloud-region:{salt}:{i}:a") - 0.5) * 0.26
            n2 = (stable_unit_float(f"cloud-region:{salt}:{i}:b") - 0.5) * 0.18
            radial = 1.0 + n1 + 0.08 * math.sin(ang * 3.0 + salt * 0.001)
            lat = lat_c + math.sin(ang) * lat_r * radial
            lon = lon_c + math.cos(ang) * lon_r * (1.0 + n2)
            ring.append({"lat": round(lat, 6), "lng": round(((lon + 180.0) % 360.0) - 180.0, 6)})
        if ring and ring[0] != ring[-1]:
            ring.append(dict(ring[0]))
        return ring

    def _component_walk(self, mask: Any, max_components: int = 220, max_cells_per_component: int = 3500) -> list[list[tuple[int, int]]]:
        """8-neighbor connected components without scipy."""
        if np is None:
            return []
        m = np.asarray(mask, dtype=bool)
        if m.ndim != 2 or m.size == 0:
            return []
        h, w = m.shape
        seen = np.zeros(m.shape, dtype=bool)
        comps: list[list[tuple[int, int]]] = []
        for sy in range(h):
            for sx in range(w):
                if not m[sy, sx] or seen[sy, sx]:
                    continue
                stack = [(sy, sx)]
                seen[sy, sx] = True
                cells: list[tuple[int, int]] = []
                while stack:
                    y, x = stack.pop()
                    cells.append((y, x))
                    if len(cells) >= max_cells_per_component:
                        continue
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            if dy == 0 and dx == 0:
                                continue
                            ny, nx = y + dy, x + dx
                            if ny < 0 or nx < 0 or ny >= h or nx >= w or seen[ny, nx] or not m[ny, nx]:
                                continue
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                if len(cells) >= 3:
                    comps.append(cells)
                    if len(comps) >= max_components:
                        return comps
        return comps

    def _derive_cloud_regions_from_fields(self, fields: dict[str, Any], cycle: str, forecast_hour: int, max_regions: int | None = None, bbox: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Derive cloud regions from the live cloud-intelligence grid.

        This is the cloud equivalent of bait marching-squares: the graphics are
        based on thresholded/connected cloud fields, not raw grid-cell placement.
        """
        if np is None:
            return []
        arrays = self._cloud_intensity_arrays(fields)
        if not arrays:
            return []
        lat2d = arrays["lat2d"]
        lon2d = arrays["lon2d"]
        total = arrays["total"]
        low = arrays["low"]
        mid = arrays["mid"]
        high = arrays["high"]
        rain = arrays["rain"]
        tower = arrays["tower"]
        vectors = fields.get("vectors") or []
        max_regions = int(max_regions or int(os.getenv("GFS_CLOUD_MAX_REGIONS", "260") or "260"))
        # Region solve must be scene/bbox-aware.  Previously we derived the top
        # global components first, then clipped to the tile/viewport bbox; for
        # many local bboxes that meant all 260 global regions were rejected and
        # clouds stayed stuck in warming.  Build a bbox mask before connected
        # components so the marching-squares candidates are local to the request.
        bbox_mask = None
        if isinstance(bbox, dict):
            try:
                bbox_mask = np.zeros(total.shape, dtype=bool)
                lat_arr = np.asarray(lat2d, dtype=float)
                lon_arr = ((np.asarray(lon2d, dtype=float) + 180.0) % 360.0) - 180.0
                south = min(float(bbox.get("south", -90.0)), float(bbox.get("north", 90.0)))
                north = max(float(bbox.get("south", -90.0)), float(bbox.get("north", 90.0)))
                west = ((float(bbox.get("west", -180.0)) + 180.0) % 360.0) - 180.0
                east = ((float(bbox.get("east", 180.0)) + 180.0) % 360.0) - 180.0
                # Light pad prevents edge-clipping of cloud regions that straddle
                # the visible/fetch boundary.
                lat_pad = max(0.25, (north - south) * 0.08)
                lon_pad = max(0.25, abs(east - west if west <= east else (east + 360.0 - west)) * 0.08)
                south -= lat_pad; north += lat_pad
                west_p = ((west - lon_pad + 180.0) % 360.0) - 180.0
                east_p = ((east + lon_pad + 180.0) % 360.0) - 180.0
                lat_ok = (lat_arr >= south) & (lat_arr <= north)
                if west_p <= east_p:
                    lon_ok = (lon_arr >= west_p) & (lon_arr <= east_p)
                else:
                    lon_ok = (lon_arr >= west_p) | (lon_arr <= east_p)
                bbox_mask = lat_ok & lon_ok
            except Exception:
                bbox_mask = None
        thresholds = [0.82, 0.65, 0.45, 0.28, 0.18]
        regions: list[dict[str, Any]] = []
        occupied = np.zeros(total.shape, dtype=bool)
        for level in thresholds:
            mask = (total >= level) & (~occupied)
            if bbox_mask is not None:
                mask = mask & bbox_mask
            components = self._component_walk(mask, max_components=max_regions * 2, max_cells_per_component=5000)
            # Large/high-confidence components first.
            components.sort(key=len, reverse=True)
            for idx, cells in enumerate(components):
                if len(regions) >= max_regions:
                    return regions
                if len(cells) < 4:
                    continue
                ys = np.asarray([c[0] for c in cells], dtype=int)
                xs = np.asarray([c[1] for c in cells], dtype=int)
                low_v = float(np.nanmean(low[ys, xs]))
                mid_v = float(np.nanmean(mid[ys, xs]))
                high_v = float(np.nanmean(high[ys, xs]))
                total_v = float(np.nanmean(total[ys, xs]))
                rain_v = float(np.nanmean(rain[ys, xs]))
                tower_v = float(np.nanmean(tower[ys, xs]))
                if max(low_v, mid_v, high_v, total_v) < 0.10:
                    continue
                salt = stable_hash_u32(f"{cycle}:{forecast_hour}:{level}:{idx}:{len(cells)}")
                ring = self._component_ring_from_cells(lat2d, lon2d, cells, salt)
                if len(ring) < 4:
                    continue
                lat_vals = [p["lat"] for p in ring[:-1]]
                lon_vals = [p["lng"] for p in ring[:-1]]
                lat_c = sum(lat_vals) / len(lat_vals)
                lon_c = sum(lon_vals) / len(lon_vals)
                classification = self._classify_cloud_region(low_v, mid_v, high_v, rain_v, tower_v)
                u = float(vectors[0].get("u", 0.0)) if vectors else 0.0
                v = float(vectors[0].get("v", 0.0)) if vectors else 0.0
                region = {
                    "id": f"cloud-region-{salt}",
                    "schema": "cloud_region_marching_squares_v1",
                    "method": "live_cloud_intensity_marching_squares_components",
                    "threshold": round(float(level), 3),
                    "cell_count": int(len(cells)),
                    "center": {"lat": round(lat_c, 6), "lon": round(lon_c, 6)},
                    "bbox": {"west": round(min(lon_vals), 6), "south": round(min(lat_vals), 6), "east": round(max(lon_vals), 6), "north": round(max(lat_vals), 6)},
                    "footprint": ring,
                    "cloud_type": classification["type"],
                    "family": classification["family"],
                    "roles": classification["roles"],
                    "intensity": {"low": round(low_v, 4), "mid": round(mid_v, 4), "high": round(high_v, 4), "total": round(total_v, 4), "rain": round(rain_v, 4), "tower": round(tower_v, 4)},
                    "wind": {"u": round(u, 3), "v": round(v, 3)},
                    "cycle": cycle,
                    "forecast_hour": forecast_hour,
                }
                regions.append(region)
                occupied[ys, xs] = True
        return regions

    def _cloud_region_to_tile(self, region: dict[str, Any], cycle: str, forecast_hour: int) -> dict[str, Any] | None:
        bbox = region.get("bbox") or {}
        try:
            lat_min = float(bbox.get("south")); lat_max = float(bbox.get("north")); lon_min = float(bbox.get("west")); lon_max = float(bbox.get("east"))
        except Exception:
            return None
        inten = region.get("intensity") or {}
        wind = region.get("wind") or {}
        tile = self._tile_from_real_fields(
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            float(inten.get("low", 0.0)),
            float(inten.get("mid", 0.0)),
            float(inten.get("high", 0.0)),
            float(inten.get("rain", 0.0)),
            float(inten.get("tower", 0.0)),
            float(wind.get("u", 0.0)),
            float(wind.get("v", 0.0)),
            str(region.get("id") or f"{cycle}:{forecast_hour}:cloud-region"),
        )
        tile["id"] = region.get("id")
        tile["tile_id"] = region.get("id")
        tile["source_method"] = "cloud_region_marching_squares_v1"
        tile["cloud_region_id"] = region.get("id")
        tile["cloud_region"] = region
        tile["region_footprint"] = region.get("footprint") or []
        tile["coverage"] = round(float((region.get("intensity") or {}).get("total", 0.0)) * 100.0, 2)
        tile["cloud_type"] = region.get("cloud_type")
        tile["regime"] = region.get("family") or tile.get("regime")
        # Override server footprints to use the region footprint, so the shell is a region contour instead of a grid rectangle.
        for band_name, band in (tile.get("bands") or {}).items():
            if not isinstance(band, dict):
                continue
            fps = band.get("footprints")
            if isinstance(fps, list) and fps:
                fps[0]["points"] = region.get("footprint") or fps[0].get("points") or []
                fps[0]["source"] = "cloud_region_marching_squares_v1"
        return tile

    def _marching_squares_contours(self, field_name: str, bounds: dict[str, float], z: int) -> list[dict[str, Any]]:
        if np is None:
            return []
        sf = (self.state.scalar_fields or {}).get(field_name)
        if not sf:
            return []
        try:
            lat = np.asarray(sf.get("lat"), dtype=float)
            lon = np.asarray(sf.get("lon"), dtype=float)
            vals = np.asarray(sf.get("values"), dtype=float)
        except Exception:
            return []
        if vals.ndim != 2 or vals.shape[0] < 2 or vals.shape[1] < 2:
            return []
        detail_stride = 4 if z < 3 else 2 if z < 6 else 1
        levels = {
            "precip_rate": [0.1, 1.0, 4.0, 12.0],
            "cloud_density": [0.2, 0.4, 0.65, 0.85],
            "pressure_pa": [99500.0, 100500.0, 101300.0],
            "temperature_k": [273.15, 283.15, 293.15, 303.15],
        }.get(field_name, [0.3, 0.6])
        south, north = bounds.get("south", -90.0), bounds.get("north", 90.0)
        west, east = bounds.get("west", -180.0), bounds.get("east", 180.0)
        out = []
        max_features = 60 if z < 3 else 140 if z < 6 else 260
        for lvl in levels:
            for y in range(0, vals.shape[0] - 1, detail_stride):
                for x in range(0, vals.shape[1] - 1, detail_stride):
                    v00 = safe_float(vals[y, x], 0.0)
                    v10 = safe_float(vals[y, x + 1], 0.0)
                    v01 = safe_float(vals[y + 1, x], 0.0)
                    v11 = safe_float(vals[y + 1, x + 1], 0.0)
                    mask = (1 if v00 >= lvl else 0) | (2 if v10 >= lvl else 0) | (4 if v11 >= lvl else 0) | (8 if v01 >= lvl else 0)
                    if mask in (0, 15):
                        continue
                    lat0 = safe_float(lat[y, x], None); lat1 = safe_float(lat[y + 1, x + 1], None)
                    lon0 = safe_float(lon[y, x], None); lon1 = safe_float(lon[y + 1, x + 1], None)
                    if None in (lat0, lat1, lon0, lon1):
                        continue
                    c_lat = (lat0 + lat1) * 0.5
                    c_lon = (lon0 + lon1) * 0.5
                    if not (south <= c_lat <= north and west <= c_lon <= east):
                        continue
                    poly = [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]
                    out.append({
                        "type": "Feature",
                        "properties": {"field": field_name, "level": lvl, "mask": mask, "lod_z": z},
                        "geometry": {"type": "Polygon", "coordinates": [poly]},
                    })
                    if len(out) >= max_features:
                        return out
        return out

    def _derive_real_cloud_tiles(self, fields: dict[str, Any], cycle: str, forecast_hour: int, bbox: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Create cloud shell tiles from marching-squares cloud regions first.

        Fallback to the legacy block/tile method only if the region solver cannot
        derive valid live regions. The preferred path removes the visible grid by
        producing contour components from the cloud-intelligence field.
        """
        if np is not None:
            try:
                regions = self._derive_cloud_regions_from_fields(fields, cycle, forecast_hour, bbox=bbox)
                tiles = [self._cloud_region_to_tile(r, cycle, forecast_hour) for r in regions]
                tiles = [t for t in tiles if isinstance(t, dict)]
                if tiles:
                    for t in tiles:
                        t["schema"] = "cloud_shell_tile_from_region_v1"
                        t["method"] = "cloud_region_marching_squares_v1"
                    # Attach for serializers without requiring a second solve.
                    fields["cloud_regions"] = regions
                    fields["cloud_region_method"] = "live_cloud_intensity_marching_squares_components"
                    return tiles
            except Exception:
                log.exception("[gfs clouds] cloud region marching-squares solve failed; falling back to legacy cloud tiles")

        precip = fields["precip"]
        lat2d = fields["lat2d"]
        lon2d = fields["lon2d"]
        low = fields["low"]
        mid = fields["mid"]
        high = fields["high"]
        conv = fields["conv"]
        vectors = fields["vectors"]

        tiles: list[dict[str, Any]] = []
        y_step = max(1, precip.shape[0] // 28)
        x_step = max(1, precip.shape[1] // 56)
        for y in range(0, precip.shape[0] - y_step, y_step):
            for x in range(0, precip.shape[1] - x_step, x_step):
                lat_min = float(np.min(lat2d[y:y+y_step, x:x+x_step]))
                lat_max = float(np.max(lat2d[y:y+y_step, x:x+x_step]))
                lon_min = float(np.min(lon2d[y:y+y_step, x:x+x_step]))
                lon_max = float(np.max(lon2d[y:y+y_step, x:x+x_step]))
                low_v = float(np.nanmean(low[y:y+y_step, x:x+x_step]))
                mid_v = float(np.nanmean(mid[y:y+y_step, x:x+x_step]))
                high_v = float(np.nanmean(high[y:y+y_step, x:x+x_step]))
                precip_v = float(np.nanmean(np.clip(precip[y:y+y_step, x:x+x_step] / 45.0, 0.0, 1.0)))
                conv_v = float(np.nanmean(conv[y:y+y_step, x:x+x_step]))
                if max(low_v, mid_v, high_v) < 0.06:
                    continue
                u = vectors[0]["u"] if vectors else 0.0
                v = vectors[0]["v"] if vectors else 0.0
                tile = self._tile_from_real_fields(
                    lat_min,
                    lat_max,
                    lon_min,
                    lon_max,
                    low_v,
                    mid_v,
                    high_v,
                    precip_v,
                    conv_v,
                    u,
                    v,
                    f"{cycle}:{forecast_hour}:{y}:{x}",
                )
                tile["method"] = "legacy_block_tile_cloud_shells"
                tiles.append(tile)
        return tiles

    def _select_hazard_canonical_shape(self, fields: dict[str, Any], cloud_layers: dict[str, Any], precip_shape: tuple[int, int]) -> tuple[int, int]:
        from_fields = fields.get("canonical_shape")
        lat_shape = self._shape_of(fields.get("lat2d"))
        low_shape = self._shape_of(cloud_layers.get("low") if isinstance(cloud_layers, dict) else None)

        preferred = None
        if from_fields and len(from_fields) >= 2:
            preferred = (int(from_fields[0]), int(from_fields[1]))
        elif lat_shape and len(lat_shape) >= 2:
            preferred = (int(lat_shape[0]), int(lat_shape[1]))
        elif low_shape and len(low_shape) >= 2:
            preferred = (int(low_shape[0]), int(low_shape[1]))
        else:
            preferred = tuple(int(x) for x in precip_shape)

        candidate_shapes = [
            tuple(int(x) for x in sh[:2])
            for sh in (from_fields, lat_shape, low_shape, precip_shape)
            if sh is not None and len(sh) >= 2
        ]
        max_shape = max(candidate_shapes, key=lambda x: x[0] * x[1]) if candidate_shapes else preferred
        if preferred[0] * preferred[1] < max_shape[0] * max_shape[1]:
            raise ValueError(
                f"hazard_canonical_shape_regression chosen={preferred} max_available={max_shape}"
            )
        return preferred

    def _derive_real_hazard_payloads(self, groups: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        warned: set[str] = set()
        hazard_inputs = fields.get("hazard_inputs") if isinstance(fields.get("hazard_inputs"), dict) else {}
        precip = np.asarray(hazard_inputs.get("precip", fields["precip"]), dtype=float)
        cloud_layers = hazard_inputs.get("cloud_layers") if isinstance(hazard_inputs.get("cloud_layers"), dict) else (fields["cloud_layers"] if isinstance(fields.get("cloud_layers"), dict) else {})
        canonical_shape = self._select_hazard_canonical_shape(
            {**fields, "canonical_shape": hazard_inputs.get("canonical_shape", fields.get("canonical_shape")), "lat2d": hazard_inputs.get("lat2d", fields.get("lat2d"))},
            cloud_layers,
            tuple(precip.shape),
        )
        log.info("[gfs] hazard canonical target shape=%s", canonical_shape)
        if hazard_inputs:
            log.info(
                "[gfs] hazard inputs canonicalized cycle_ready shape=%s resampled=%s fields=%s",
                canonical_shape,
                bool(hazard_inputs.get("resampled")),
                ",".join(hazard_inputs.get("resampled_fields") or []) or "none",
            )

        lat2d = self.ensure_same_grid(hazard_inputs.get("lat2d", fields["lat2d"]), canonical_shape, "hazard_lat2d")
        lon2d = self.ensure_same_grid(hazard_inputs.get("lon2d", fields["lon2d"]), canonical_shape, "hazard_lon2d")
        precip = self.ensure_same_grid(precip, canonical_shape, "hazard_precip_mm_hr")
        cloud_layers = {
            "low": self.ensure_same_grid(cloud_layers.get("low", np.zeros(canonical_shape, dtype=float)), canonical_shape, "hazard_cloud_low"),
            "mid": self.ensure_same_grid(cloud_layers.get("mid", np.zeros(canonical_shape, dtype=float)), canonical_shape, "hazard_cloud_mid"),
            "high": self.ensure_same_grid(cloud_layers.get("high", np.zeros(canonical_shape, dtype=float)), canonical_shape, "hazard_cloud_high"),
        }

        self._assert_same_shape(
            "hazard_fields",
            precip,
            cloud_layers.get("low"),
            cloud_layers.get("mid"),
            cloud_layers.get("high"),
            lat2d,
            lon2d,
        )
        rain_mask = self.threshold_to_mask(precip, 0.5)
        hail_mask = self.derive_hail_mask(groups, precip, cloud_layers, target_shape=canonical_shape, warned=warned)
        lightning_mask = self.derive_lightning_mask(groups, precip, cloud_layers, target_shape=canonical_shape, warned=warned)
        hail_mask = self._coerce_field_to_canonical_grid(hail_mask, canonical_shape, "hail_mask", warned)
        lightning_mask = self._coerce_field_to_canonical_grid(lightning_mask, canonical_shape, "lightning_mask", warned)
        rain_mask = self._coerce_field_to_canonical_grid(rain_mask, canonical_shape, "rain_mask", warned)
        rain_polys = self.connected_components_or_simple_cell_polygons(rain_mask, lat2d, lon2d)
        hail_polys = self.connected_components_or_simple_cell_polygons(hail_mask, lat2d, lon2d)
        lightning_polys = self.connected_components_or_simple_cell_polygons(lightning_mask, lat2d, lon2d)
        return {
            "rain": self.serialize_rain_payload(rain_polys),
            "hail": self.serialize_hail_payload(hail_polys),
            "lightning": self.serialize_lightning_payload(lightning_polys),
        }

    def generate_real_gfs_payload(self, bbox: dict[str, float] | None = None, *, force_live: bool = False) -> dict[str, Any]:
        bbox = self._normalize_bbox(bbox)
        ingest = self.ingest_latest_model_fields(bbox, force_live=force_live)
        fetch = ingest["fetch"]
        groups = ingest["groups"]
        mode = ingest.get("mode", "live")

        try:
            fields = self._derive_real_source_fields(groups)
            self._store_scalar_fields(fields)
            raw_tiles = self._derive_real_cloud_tiles(fields, fetch.cycle, fetch.forecast_hour, bbox=bbox)
            tiles, cloud_diag = self._clip_cloud_tiles_to_bbox(raw_tiles, bbox)
            if raw_tiles and not tiles and not cloud_diag.get("polar_seam_fallback"):
                log.warning("[gfs clouds] live tiles all rejected by bbox contract bbox=%s diag=%s", bbox, cloud_diag)
            hazards = self._derive_real_hazard_payloads(groups, fields)
            grid_shape = self._shape_of(fields.get("precip"))
            grid_tag = self._grid_tag_from_shape(grid_shape)
            payload = {
                "source": "gfs_nomads",
                "grid_shape": list(grid_shape) if grid_shape else None,
                "grid_tag": grid_tag,
                # /api/gfs/scene and the jetstream layer require these exact
                # field keys as plain 2-D JSON arrays. Keep the same successful
                # NOMADS/cfgrib source, but expose the decoded U/V payload in
                # the frontend contract instead of falling into pseudo wind.
                "fields": {
                    "wind_u": self._json_grid(fields.get("wind_u"), precision=4),
                    "wind_v": self._json_grid(fields.get("wind_v"), precision=4),
                    "wind_speed": self._json_grid(fields.get("wind_speed"), precision=4),
                    "precip_rate": self._json_grid(fields.get("precip_rate"), precision=4),
                    "cloud_density": self._json_grid(fields.get("cloud_density"), precision=4),
                    "temp2m": self._json_grid(fields.get("temperature_k"), precision=3),
                    "mslp": self._json_grid(fields.get("pressure_pa"), precision=2),
                },
                "field_shapes": {
                    k: list(np.asarray(v).shape) for k, v in {
                        "wind_u": fields.get("wind_u"),
                        "wind_v": fields.get("wind_v"),
                        "precip_rate": fields.get("precip_rate"),
                        "cloud_density": fields.get("cloud_density"),
                    }.items() if v is not None
                },
                "cycle": fetch.cycle,
                "forecast_hour": fetch.forecast_hour,
                "valid_time": fetch.valid_time,
                "bbox": bbox,
                "bbox_used": bbox,
                "requested_bbox": bbox,
                "diagnostics": {
                    **cloud_diag,
                    "requested_bbox": bbox,
                    "fetch_bbox": bbox,
                    "grid_shape": list(grid_shape) if grid_shape else None,
                    "source_url": fetch.url,
                    "cloud_region_method": fields.get("cloud_region_method") or "legacy_block_tiles",
                    "cloud_region_count": len(fields.get("cloud_regions") or []),
                },
                "schema": "gfs_cloud_regions_v1",
                "cloud_regions": fields.get("cloud_regions") or [],
                "cloud_region_count": len(fields.get("cloud_regions") or []),
                "tiles": tiles,
                "rain": hazards["rain"],
                "hail": hazards["hail"],
                "lightning": hazards["lightning"],
                "balloons": self.serialize_balloon_payload(fields["vectors"]),
                "heuristic": False,
                "quality_note": "Cloud/precip overlays are derived from real NOMADS GFS GRIB2 fields; geometry remains visualization-oriented.",
                "source_format": "grib2",
                "source_url": fetch.url,
                "cache_path": str(fetch.path) if fetch.path else None,
                "fields_available": list(self.state.fields_available or []),
                "fields_missing": list(self.state.fields_missing or []),
                "using_last_known_good": mode == "last_known_good",
                "degraded_mode": mode != "live",
                "decode_backend": self.state.decode_backend,
                "data_source_mode": self.state.data_source_mode,
            }
            return self._annotate_weather_payload(
                payload,
                bbox=bbox,
                source="gfs_nomads",
                payload_state="live" if mode == "live" else "cached",
                heuristic=False,
                quality_note=payload["quality_note"] if mode == "live" else "Serving last-known-good GRIB2 decode after live fetch/decode failure.",
                confidence="high" if mode == "live" else "medium",
            )
        finally:
            # Decoded GRIB groups may be owned by the process-level snapshot
            # cache.  Do not close those xarray objects after every request;
            # that was the cfgrib reopen storm visible in journalctl.  They are
            # closed only when a newer GRIB file replaces the snapshot.
            if not bool(ingest.get("cache_owned")):
                self._release_groups(groups)
                groups = None
                gc.collect()

    def read_most_recent_cached_real_payload(self, max_age_seconds: int = 5400) -> Any:
        if self.disk_cache is None:
            return None
        now_ts = time.time()
        newest = None
        newest_ts = 0.0
        try:
            for k in self.disk_cache.iterkeys():
                if not str(k).startswith('gfs_payload:v'):
                    continue
                row = self.disk_cache.get(k)
                if not isinstance(row, dict):
                    continue
                if row.get('source') != 'gfs_nomads':
                    continue
                ts = float(row.get('updated_at', 0)) / 1000.0 if row.get('updated_at') else 0.0
                if ts <= 0:
                    continue
                if now_ts - ts > max_age_seconds:
                    continue
                if ts > newest_ts:
                    newest_ts = ts
                    newest = row
        except Exception:
            return None
        return newest

    def _weather_unavailable_payload(self, bbox: dict[str, float], reason: str) -> dict[str, Any]:
        now = self._utc_now()
        return self._attach_truth_contract({
            "ok": True,
            "status": "warming",
            "payload_state": "provider_unavailable",
            "source": "gfs_nomads_unavailable_cache_warming",
            "bbox": bbox,
            "bbox_used": bbox,
            "requested_bbox": bbox,
            "generated_at": now,
            "resolved_time": now,
            "cycle": "unavailable",
            "forecast_hour": 0,
            "grid_tag": "provider_unavailable",
            "items": [],
            "features": [],
            "cloud_items": 0,
            "clouds": {"status": "warming", "source": "gfs_nomads_unavailable", "features": [], "cloud_items": 0},
            "rain": {"status": "warming", "source": "gfs_nomads_unavailable", "precip_columns": [], "features": []},
            "fields": {},
            "error": str(reason),
            "quality_note": "Live NOMADS/GRIB was unavailable; returning a non-fatal warming shell so visual routes stay alive.",
            "confidence": "none",
        }, bbox=bbox)

    def _generate_weather_payload_uncached(self, bbox: dict[str, float] | None = None, *, force_live: bool = False) -> dict[str, Any]:
        bbox = self._normalize_bbox(bbox)
        try:
            payload = self.generate_real_gfs_payload(bbox, force_live=force_live)
            # Forecast hour must stay numeric. Some derived cloud region IDs look like
            # "gfs-19-06:494709:frontal_shield"; if one bubbles into metadata, do
            # not let int(...) abort the whole NOMADS/cloud path.
            raw_fhr = payload.get("forecast_hour", 0)
            try:
                fhr = int(raw_fhr)
            except Exception:
                log.warning("[gfs] non-numeric forecast_hour ignored value=%r", raw_fhr)
                fhr = 0
                payload["forecast_hour"] = 0
                payload.setdefault("warnings", []).append("non_numeric_forecast_hour_guarded")
            key = self.payload_cache_key(payload.get("cycle", "na"), fhr, bbox or {}, str(payload.get("grid_tag") or "na"))
            self.write_cached_payload(key, payload)
            return payload
        except Exception as exc:
            log.warning("[gfs] live real payload generation failed err=%s", exc)
            cached = self.read_most_recent_cached_real_payload(max_age_seconds=5400)
            if isinstance(cached, dict):
                log.warning("[gfs] using cached real payload due to live failure grid_tag=%s", cached.get("grid_tag"))
                cached_payload = self._annotate_weather_payload(
                    dict(cached),
                    bbox=bbox,
                    source="gfs_nomads",
                    payload_state="cached",
                    heuristic=False,
                    quality_note="Serving recent cached NOMADS-derived payload because live fetch failed.",
                    confidence="medium",
                )
                return cached_payload
            return self._weather_unavailable_payload(bbox, f"live_only_mode_failed reason={exc}")

    def generate_weather_payload(self, bbox: dict[str, float] | None = None, *, force_live: bool = False) -> dict[str, Any]:
        refresh_bbox = self._normalize_bbox(bbox)
        cache_key = self._bbox_key_fragment(refresh_bbox)
        ttl_ms = max(10_000, WEATHER_REFRESH_TTL_SECONDS * 1000)
        now_ms = self._now_ms()
        row = self._weather_payload_cache or {}
        cached_payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
        cached_ts = int(row.get("ts") or 0)
        cached_key = str(row.get("key") or "")
        if (not force_live) and cached_payload and cached_key == cache_key and (now_ms - cached_ts) <= ttl_ms:
            return cached_payload

        with self._weather_refresh_lock:
            row = self._weather_payload_cache or {}
            cached_payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
            cached_ts = int(row.get("ts") or 0)
            cached_key = str(row.get("key") or "")
            now_ms = self._now_ms()
            if (not force_live) and cached_payload and cached_key == cache_key and (now_ms - cached_ts) <= ttl_ms:
                return cached_payload
            try:
                payload = self._generate_weather_payload_uncached(refresh_bbox, force_live=force_live)
                payload["bbox"] = refresh_bbox
                payload["bbox_used"] = refresh_bbox
                payload["requested_bbox"] = refresh_bbox
                self._weather_payload_cache = {"ts": self._now_ms(), "key": cache_key, "payload": payload}
                return self._attach_truth_contract(payload, bbox=refresh_bbox)
            except Exception as exc:
                if cached_payload and cached_key == cache_key:
                    return cached_payload
                return self._weather_unavailable_payload(refresh_bbox, str(exc))

    def validate_bbox_real_fields(self, bbox: dict[str, float] | None = None) -> dict[str, Any]:
        """Manual check that precip/cloud/wind products are non-empty."""
        payload = self.generate_weather_payload(bbox)
        fields = payload.get("fields") or {}
        def shape(x):
            try:
                if isinstance(x, dict) and "values" in x:
                    x = x.get("values")
                return list(np.asarray(x).shape) if np is not None and x is not None else []
            except Exception:
                return []
        return {
            "ok": True,
            "source": payload.get("source"),
            "payload_state": payload.get("payload_state"),
            "source_url": payload.get("source_url"),
            "cycle": payload.get("cycle"),
            "forecast_hour": payload.get("forecast_hour"),
            "valid_time": payload.get("valid_time"),
            "has_precip": bool((payload.get("rain") or {}).get("count", 0) > 0),
            "has_cloud": bool(len(payload.get("tiles") or payload.get("items") or []) > 0),
            "has_balloon_vectors": bool((payload.get("balloons") or {}).get("count", 0) > 0),
            "has_field_wind_u": bool(fields.get("wind_u")),
            "has_field_wind_v": bool(fields.get("wind_v")),
            "field_shapes": {k: shape(fields.get(k)) for k in ["wind_u", "wind_v", "wind_speed", "precip_rate", "cloud_density", "temp2m", "mslp"]},
            "cloud_tiles": len(payload.get("tiles") or payload.get("items") or []),
            "rain_count": (payload.get("rain") or {}).get("count", 0),
            "fields_available": payload.get("fields_available") or self.state.fields_available,
            "fields_missing": payload.get("fields_missing") or self.state.fields_missing,
        }

    def _cloud_feature_from_tile(self, tile: dict[str, Any]) -> dict[str, Any]:
        bounds = tile.get("bounds") or {}
        low = (tile.get("bands") or {}).get("low") or {}
        mid = (tile.get("bands") or {}).get("mid") or {}
        high = (tile.get("bands") or {}).get("high") or {}
        return {
            "id": str(tile.get("tile_id") or f"cloud-{stable_hash_u32(str(bounds))}"),
            "center": {"lat": float(bounds.get("lat_center") or 0.0), "lon": float(bounds.get("lon_center") or 0.0)},
            "footprint": (low.get("footprints") or [{"points": []}])[0].get("points", []),
            "estimated_cloud_base_m": float(tile.get("estimated_cloud_base_m") or tile.get("base_altitude_m") or low.get("base_altitude_m") or 1000.0),
            "estimated_cloud_top_m": float(tile.get("estimated_cloud_top_m") or tile.get("top_altitude_m") or high.get("top_altitude_m") or 8000.0),
            "estimated_thickness_m": float(tile.get("estimated_cloud_thickness_m") or tile.get("vertical_depth_m") or 2200.0),
            "estimated_density": float(tile.get("estimated_density") or tile.get("density") or 0.0),
            "opacity_hint": round(_clamp(0.14 + float(tile.get("density") or 0.0) * 0.54, 0.08, 0.8), 4),
            "convective_strength": float(tile.get("storm_energy") or tile.get("convection_factor") or 0.0),
            "cloud_type_hint": str(tile.get("regime") or "cumulus_field"),
            "layer_hints": [
                {
                    "band": "low",
                    "base_m": float(low.get("base_altitude_m") or 0.0),
                    "top_m": float(low.get("top_altitude_m") or 0.0),
                    "density": float(low.get("density") or tile.get("low_density") or 0.0),
                    "spread_km": float(low.get("lateral_scale_km") or 0.0),
                },
                {
                    "band": "mid",
                    "base_m": float(mid.get("base_altitude_m") or 0.0),
                    "top_m": float(mid.get("top_altitude_m") or 0.0),
                    "density": float(mid.get("density") or tile.get("mid_density") or 0.0),
                    "spread_km": float(mid.get("lateral_scale_km") or 0.0),
                },
                {
                    "band": "high",
                    "base_m": float(high.get("base_altitude_m") or 0.0),
                    "top_m": float(high.get("top_altitude_m") or 0.0),
                    "density": float(high.get("density") or tile.get("high_density") or 0.0),
                    "spread_km": float(high.get("lateral_scale_km") or 0.0),
                },
            ],
            "visual_priority": float(tile.get("importance") or 0.0),
            "noise_seed": int(tile.get("seed") or stable_hash_u32(str(tile.get("tile_id") or "cloud"))),
            "heuristic": True,
            "source_confidence": "estimated",
            "source_fields": {
                "proxy_precip_rate": float(tile.get("precip_rate") or 0.0),
                "proxy_convection": float(tile.get("convection_factor") or tile.get("storm_energy") or 0.0),
                "proxy_low_density": float(tile.get("low_density") or 0.0),
                "proxy_mid_density": float(tile.get("mid_density") or 0.0),
                "proxy_high_density": float(tile.get("high_density") or 0.0),
            },
        }

    def _precip_feature_from_column(self, col: dict[str, Any]) -> dict[str, Any]:
        rate = float(col.get("estimated_precip_rate_mm_hr") or 0.0)
        intensity = _clamp(rate / 45.0, 0.0, 1.0)
        if intensity >= 0.9:
            bucket = "black"
            cls = "extreme"
        elif intensity >= 0.75:
            bucket = "red"
            cls = "very_heavy"
        elif intensity >= 0.6:
            bucket = "orange"
            cls = "heavy"
        elif intensity >= 0.45:
            bucket = "yellow"
            cls = "moderate"
        elif intensity >= 0.3:
            bucket = "green"
            cls = "light_moderate"
        elif intensity >= 0.16:
            bucket = "blue"
            cls = "light"
        else:
            bucket = "white"
            cls = "very_light"
        lat = float(col.get("lat") or 0.0)
        lon = float(col.get("lon") or 0.0)
        spread = 0.08 + intensity * 0.18
        footprint = close_ring([
            {"lat": lat - spread, "lng": lon - spread},
            {"lat": lat - spread, "lng": lon + spread},
            {"lat": lat + spread, "lng": lon + spread},
            {"lat": lat + spread, "lng": lon - spread},
        ])
        return {
            "id": f"precip-{col.get('tile_id') or stable_hash_u32(str(col))}",
            "center": {"lat": lat, "lon": lon},
            "footprint": footprint,
            "precip_type_hint": str(col.get("type_hint") or "rain"),
            "intensity_value": round(rate, 3),
            "intensity_class": cls,
            "palette_bucket": bucket,
            "source_altitude_m": float(col.get("estimated_source_altitude_m") or 0.0),
            "target_altitude_m": float(col.get("estimated_surface_altitude_m") or 0.0),
            "linked_cloud_id": str(col.get("tile_id") or ""),
            "storm_strength_hint": round(intensity, 4),
            "visual_priority": round(_clamp(intensity * 0.78 + (1 if cls in {"heavy", "very_heavy", "extreme"} else 0) * 0.18, 0.0, 1.0), 4),
            "heuristic": True,
            "source_fields": {"proxy_precip_rate": rate, "proxy_wind_u": float(col.get("wind_u") or 0.0), "proxy_wind_v": float(col.get("wind_v") or 0.0)},
        }

    def _lightning_feature_from_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        energy = float(ev.get("estimated_energy") or 0.0)
        return {
            "id": f"ltg-{ev.get('tile_id') or stable_hash_u32(str(ev))}",
            "center": {"lat": float(ev.get("lat") or 0.0), "lon": float(ev.get("lon") or 0.0)},
            "path": [],
            "linked_cloud_id": str(ev.get("tile_id") or ""),
            "severity_hint": str(ev.get("severity_hint") or ("strong" if energy > 0.7 else "active")),
            "start_altitude_m": float(ev.get("estimated_flash_top_m") or 0.0),
            "end_altitude_m": float(ev.get("estimated_flash_bottom_m") or 0.0),
            "flash_hint": True,
            "visual_priority": round(_clamp(energy, 0.0, 1.0), 4),
            "heuristic": True,
            "source_fields": {"inferred_lightning_risk": energy},
        }

    def _wind_feature_from_vector(self, v: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"wind-{stable_hash_u32(str(v.get('lat'))+':'+str(v.get('lon'))+':'+str(v.get('source_level')))}",
            "center": {"lat": float(v.get("lat") or 0.0), "lon": float(v.get("lon") or 0.0)},
            "vector_u": float(v.get("u") or 0.0),
            "vector_v": float(v.get("v") or 0.0),
            "speed_mps": float(v.get("speed_mps") or 0.0),
            "direction_deg": float(v.get("heading_deg") or 0.0),
            "altitude_band": str(v.get("source_level") or "unknown"),
            "feature_type": "jetstream_hint",
            "visual_priority": round(_clamp(float(v.get("speed_mps") or 0.0) / 45.0, 0.0, 1.0), 4),
            "heuristic": True,
        }

    def derive_swell_features_from_wind(self, wind_features: list[dict[str, Any]], max_items: int = 120) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for w in sorted(wind_features or [], key=lambda x: float(x.get("speed_mps") or 0.0), reverse=True):
            speed = float(w.get("speed_mps") or 0.0)
            if speed < 4.0:
                continue
            out.append({
                "id": f"swell-{w.get('id')}",
                "center": dict(w.get("center") or {"lat": 0.0, "lon": 0.0}),
                "direction_deg": float(w.get("direction_deg") or 0.0),
                "height_m": round(_clamp(0.3 + speed * 0.09, 0.2, 7.0), 3),
                "period_s": round(_clamp(4.0 + speed * 0.35, 4.0, 18.0), 3),
                "sample_area_km": round(_clamp(20 + speed * 3.8, 20, 220), 2),
                "intensity_class": "high" if speed > 17 else "moderate" if speed > 10 else "low",
                "visual_priority": round(_clamp(speed / 35.0, 0.0, 1.0), 4),
                "heuristic": True,
            })
            if len(out) >= max_items:
                break
        return out

    def _fish_scene_feature(self, fish: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(fish.get("location_key") or fish.get("id") or "fish"),
            "lat": float(fish.get("lat") or 0.0),
            "lon": float(fish.get("lon") or 0.0),
            "label": str(fish.get("name") or fish.get("location_key") or "Fish"),
            "activity_hint": (fish.get("bait") or {}).get("intensity") or "unknown",
            "source": "fish_csv",
            "meta": fish.get("meta") or {},
        }

    def build_scene_payload(self, weather: dict[str, Any], bbox: dict[str, float]) -> dict[str, Any]:
        warnings: list[str] = []
        errors: list[str] = []
        clouds: list[dict[str, Any]] = []
        precip: list[dict[str, Any]] = []
        lightning: list[dict[str, Any]] = []
        wind: list[dict[str, Any]] = []
        swell: list[dict[str, Any]] = []
        fish_scene: list[dict[str, Any]] = []

        try:
            cloud_tiles = weather.get("tiles") or weather.get("items") or []
            clouds = [self._cloud_feature_from_tile(t) for t in cloud_tiles[:800] if isinstance(t, dict)]
        except Exception as exc:
            warnings.append("cloud_feature_derivation_failed")
            errors.append(str(exc))

        try:
            raw_precip = weather.get("precip_columns") or self.derive_precip_columns_from_tiles(weather.get("tiles") or weather.get("items") or [], max_items=None)
            precip = [self._precip_feature_from_column(c) for c in raw_precip if isinstance(c, dict)]
        except Exception as exc:
            warnings.append("precip_feature_derivation_failed")
            errors.append(str(exc))

        try:
            raw_ltg = weather.get("lightning_events") or self.derive_lightning_events_from_tiles(weather.get("tiles") or weather.get("items") or [], max_items=140)
            lightning = [self._lightning_feature_from_event(e) for e in raw_ltg if isinstance(e, dict)]
        except Exception as exc:
            warnings.append("lightning_feature_derivation_failed")
            errors.append(str(exc))

        try:
            raw_wind = (weather.get("balloons") or {}).get("items") if isinstance(weather.get("balloons"), dict) else []
            wind = [self._wind_feature_from_vector(v) for v in (raw_wind or []) if isinstance(v, dict)]
        except Exception as exc:
            warnings.append("wind_feature_derivation_failed")
            errors.append(str(exc))

        try:
            swell = self.derive_swell_features_from_wind(wind, max_items=120)
        except Exception as exc:
            warnings.append("swell_feature_derivation_failed")
            errors.append(str(exc))

        fish_error = None
        try:
            fish_points, fish_error = self.load_fish()
            fish_scene = [self._fish_scene_feature(f) for f in fish_points[:1500] if isinstance(f, dict)]
        except Exception as exc:
            fish_error = str(exc)
            warnings.append("fish_feature_derivation_failed")
            errors.append(str(exc))

        if fish_error:
            warnings.append("fish_source_unavailable")

        heuristic_flag = bool(weather.get("heuristic", True) or weather.get("payload_state") != "live")
        status = {
            "ok": len(errors) == 0,
            "mode": str(weather.get("payload_state") or "synthetic"),
            "warnings": warnings,
            "errors": errors,
            "upstream_available": weather.get("source") == "gfs_nomads",
            "partial": bool(warnings),
            "generated_at": self._now_ms(),
            "request_bounds": bbox,
            "fallback_active": str(weather.get("payload_state") or "synthetic") != "live",
            "heuristic_dominant": heuristic_flag,
            "decode_backend": weather.get("decode_backend") or self.state.decode_backend,
            "data_source_mode": weather.get("data_source_mode") or self.state.data_source_mode,
        }
        meta = {
            "schema_version": "atmo-scene-v1",
            "source_name": str(weather.get("source") or "fallback_proxy"),
            "source_type": "model" if weather.get("source") == "gfs_nomads" else "heuristic",
            "analysis_time": weather.get("cycle"),
            "valid_time": weather.get("valid_time"),
            "generated_at": self._now_ms(),
            "bounds": bbox,
            "units": {
                "altitude": "m",
                "wind_speed": "mps",
                "direction": "deg",
                "precip_rate": "mm_hr",
                "swell_height": "m",
                "swell_period": "s",
            },
            "heuristic_flags": {
                "scene_features_estimated": True,
                "fallback_payload": str(weather.get("payload_state") or "synthetic") != "live",
                "cloud_geometry_derived": True,
                "precip_columns_derived": True,
                "lightning_events_inferred": True,
            },
            "quality_note": weather.get("quality_note"),
            "confidence": weather.get("confidence"),
            "bbox_used": weather.get("bbox_used") or bbox,
            "source_format": weather.get("source_format") or self.state.model_source_format,
            "source_url": weather.get("source_url") or self.state.model_source_url,
            "cache_path": weather.get("cache_path") or self.state.model_cache_path,
            "fields_available": list(weather.get("fields_available") or self.state.fields_available or []),
            "fields_missing": list(weather.get("fields_missing") or self.state.fields_missing or []),
            "using_last_known_good": bool(weather.get("using_last_known_good", self.state.using_last_known_good)),
            "degraded_mode": bool(weather.get("degraded_mode", self.state.degraded_mode)),
            "decode_backend": weather.get("decode_backend") or self.state.decode_backend,
            "data_source_mode": weather.get("data_source_mode") or self.state.data_source_mode,
        }
        summary = {
            "cloud_count": len(clouds),
            "precip_count": len(precip),
            "lightning_count": len(lightning),
            "wind_count": len(wind),
            "swell_count": len(swell),
            "fish_count": len(fish_scene),
            "dominant_weather_mode": "convective" if any(float(c.get("convective_strength") or 0) > 0.65 for c in clouds[:120]) else "layered",
            "strongest_storm_class": "severe" if any(float(c.get("convective_strength") or 0) > 0.78 for c in clouds[:120]) else "moderate",
            "notes": warnings,
        }
        return {
            "status": status,
            "meta": meta,
            "scene": {
                "clouds": clouds,
                "precip": precip,
                "lightning": lightning,
                "wind": wind,
                "swell": swell,
                "fish": fish_scene,
            },
            "summary": summary,
        }
    def _degraded_scene_payload(self, bbox: dict[str, float], reason: str) -> dict[str, Any]:
        now = self._now_ms()
        return {
            "ok": False,
            "status": {
                "ok": False,
                "mode": "degraded",
                "degraded": True,
                "warnings": ["scene_generation_degraded"],
                "errors": [str(reason)],
                "partial": True,
                "generated_at": now,
                "request_bounds": bbox,
            },
            "meta": {
                "schema_version": "atmo-scene-v1",
                "generated_at": now,
                "bounds": bbox,
                "degraded": True,
                "decode_backend": self.state.decode_backend,
                "data_source_mode": self.state.data_source_mode,
            },
            "scene": {"clouds": [], "precip": [], "lightning": [], "wind": [], "swell": [], "fish": []},
            "summary": {"cloud_count": 0, "precip_count": 0, "lightning_count": 0, "wind_count": 0, "swell_count": 0, "fish_count": 0, "notes": ["degraded_response"]},
            "items": [],
            "precip_columns": [],
            "lightning_events": [],
            "source": "unavailable",
            "source_state": "unavailable",
            "payload_state": "unavailable",
            "heuristic": False,
            "quality_note": "No real cloud source data is currently available; no synthetic cloud geometry is drawn.",
            "confidence": "none",
            "bbox_used": bbox,
        }

    def cloud_tiles_payload(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None) -> Dict[str, Any]:
        bbox_norm = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(bbox_norm, visible_bbox, layer="clouds")
        key = self._bbox_cache_key(f"clouds-{scene.get('tier')}", bbox_norm)
        def _shell():
            return {
                "ok": True,
                "source": "live_first_clouds",
                "source_state": "fetching_fresh",
                "payload_state": "fetching_fresh",
                "bbox": [bbox_norm["west"], bbox_norm["south"], bbox_norm["east"], bbox_norm["north"]],
                "bbox_object": bbox_norm,
                "scene_plan": scene,
                "visible_bbox": scene.get("visible_bbox"),
                "fetch_bbox": scene.get("fetch_bbox"),
                "render_budget": scene.get("render_budget"),
                "items": [],
                "features": [],
                "precip_columns": [],
                "polygon_field_v1": [],
                "summary": {"cloudItems": 0, "precipColumns": 0, "liveFirst": True},
                "debug": {"cloud_cache_policy": "No 15-60 minute source cache. Retain last-known-good only for instant display while fresh GFS fetch runs."},
                "quality_note": "Live-first cloud payload queued; retained clouds may display only until fresh GFS arrives.",
            }
        return self._live_first_retained_split_payload(
            key=key,
            label="clouds",
            dedupe_seconds=CLOUD_LIVE_DEDUPE_SECONDS,
            retained_max_age_seconds=CLOUD_RETAINED_MAX_AGE_SECONDS,
            builder=lambda: self._cloud_tiles_payload_heavy(bbox_norm, scene.get("visible_bbox"), force_live=GFS_CLOUDS_FORCE_LIVE_FETCH),
            shell_factory=_shell,
        )

    def _cloud_tiles_payload_heavy(self, bbox: dict[str, float] | None = None, visible_bbox: dict[str, float] | None = None, *, force_live: bool = False) -> Dict[str, Any]:
        bbox_norm = self._normalize_bbox(bbox)
        scene = self.build_scene_plan(bbox_norm, visible_bbox, layer="clouds")
        try:
            weather = self.generate_weather_payload(bbox_norm, force_live=force_live)
        except Exception as exc:
            log.exception("[gfs] scene weather generation failed")
            return self._attach_scene_plan(self._degraded_scene_payload(bbox_norm, str(exc)), scene)

        try:
            if weather.get("source") == "gfs_nomads":
                payload = self.serialize_cloud_payload(weather.get("tiles", []), weather)
                payload["payload_state"] = weather.get("payload_state", "live")
                payload["heuristic"] = bool(weather.get("heuristic", False))
                payload["quality_note"] = weather.get("quality_note") or "Real NOMADS field ingestion with visualization-derived geometry."
                payload["confidence"] = weather.get("confidence") or "high"
                payload["rain"] = weather.get("rain", {"items": [], "count": 0})
                payload["hail"] = weather.get("hail", {"items": [], "count": 0})
                payload["lightning"] = weather.get("lightning", {"items": [], "count": 0})
                payload["balloons"] = weather.get("balloons", {"items": [], "count": 0})
                payload["precip_columns"] = self.derive_precip_columns_from_tiles(payload.get("items", []), max_items=None)
                payload["lightning_events"] = self.derive_lightning_events_from_tiles(payload.get("items", []), max_items=140)
                payload["note"] = "Primary source is NOAA NOMADS GFS 0.25 via GRIB subset decode."
                payload["cycle"] = weather.get("cycle")
                payload["forecast_hour"] = weather.get("forecast_hour")
                payload["valid_time"] = weather.get("valid_time")
                payload["time_selection"] = "present_or_nearest_latest_available"
                payload["cloud_shell_refresh_seconds"] = CLOUD_LIVE_DEDUPE_SECONDS
                payload["bbox_used"] = weather.get("bbox_used")
            else:
                payload = self._annotate_weather_payload(
                    weather,
                    bbox=bbox_norm,
                    source=str(weather.get("source") or "unavailable"),
                    payload_state=str(weather.get("payload_state") or "unavailable"),
                    heuristic=bool(weather.get("heuristic", False)),
                    quality_note=str(weather.get("quality_note") or "No real cloud source data is currently available."),
                    confidence=str(weather.get("confidence") or "none"),
                )
                payload.setdefault("precip_columns", self.derive_precip_columns_from_tiles(payload.get("items", []), max_items=None))
                payload.setdefault("lightning_events", self.derive_lightning_events_from_tiles(payload.get("items", []), max_items=140))

            payload["schema"] = "gfs_cloud_regions_v1"
            payload.setdefault("debug", {})["cloud_cache_policy"] = "live_first; retained last-known-good is display-only; no long fresh source TTL"
            payload.setdefault("cache_policy", {})["clouds"] = {
                "mode": "live_first_retained_display",
                "freshness": "attempt_fresh_gfs_only_after_global_cooldown_or_explicit_force",
                "dedupe_seconds": CLOUD_LIVE_DEDUPE_SECONDS,
                "retained_display_max_age_seconds": CLOUD_RETAINED_MAX_AGE_SECONDS,
                "force_live_fetch": bool(GFS_CLOUDS_FORCE_LIVE_FETCH),
            }
            payload.setdefault("time_selection", "present_or_nearest_latest_available")
            payload.setdefault("cloud_shell_refresh_seconds", CLOUD_LIVE_DEDUPE_SECONDS)
            payload["bbox"] = bbox_norm
            payload["bbox_used"] = bbox_norm
            payload["requested_bbox"] = bbox_norm
            payload.setdefault("tiles", payload.get("items") or [])
            if not payload.get("cloud_regions"):
                payload["cloud_regions"] = [t.get("cloud_region") for t in (payload.get("tiles") or []) if isinstance(t, dict) and isinstance(t.get("cloud_region"), dict)]
            payload["cloud_region_count"] = len(payload.get("cloud_regions") or [])
            payload.setdefault("diagnostics", {})
            if isinstance(payload.get("diagnostics"), dict):
                payload["diagnostics"].setdefault("requested_bbox", bbox_norm)
                payload["diagnostics"].setdefault("fetch_bbox", bbox_norm)
                payload["diagnostics"].setdefault("cloud_region_method", "live_cloud_intensity_marching_squares_components")
                payload["diagnostics"].setdefault("cloud_region_count", payload.get("cloud_region_count", 0))
            scene_payload = self.build_scene_payload(payload, bbox_norm)
            payload["status"] = scene_payload.get("status", {})
            payload["meta"] = scene_payload.get("meta", {})
            payload["scene"] = scene_payload.get("scene", {})
            payload["summary"] = scene_payload.get("summary", payload.get("summary") or {})
            payload["ok"] = bool(payload.get("ok", True) and payload["status"].get("ok", True))
            return self._attach_truth_contract(payload, bbox=bbox_norm, stride=(weather.get("scene_plan") or {}).get("provider_stride") if isinstance(weather.get("scene_plan"), dict) else weather.get("stride"), source_resolution_deg=weather.get("source_resolution_deg") or 0.25, derived_resolution_deg=0.03125, extra={"cloud_shells_smoothed": True, "neon_polygon_edges": True})
        except Exception as exc:
            log.exception("[gfs] scene payload assembly failed")
            return self._attach_truth_contract(self._degraded_scene_payload(bbox_norm, str(exc)), bbox=bbox_norm)

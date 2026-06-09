from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def clamp_lat(lat: float) -> float:
    return max(-89.9, min(89.9, float(lat)))


def normalize_lon(lon: float) -> float:
    v = float(lon)
    while v < -180.0:
        v += 360.0
    while v >= 180.0:
        v -= 360.0
    return v


def solar_declination(day_of_year: int) -> float:
    """Approximate solar declination in radians.

    This dependency-free approximation is intentionally good-enough for visual
    day/twilight/night rendering and can later be replaced with an astronomy
    package without changing the /gfs/api/sky contract.
    """
    return math.radians(23.44) * math.sin(math.radians((360.0 / 365.0) * (int(day_of_year) - 81)))


def sun_elevation(lat: float, lon: float, now_utc: datetime | None = None) -> float:
    """Return approximate sun elevation angle in degrees for lat/lon at UTC time."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    lat_f = clamp_lat(float(lat))
    lon_f = normalize_lon(float(lon))
    lat_rad = math.radians(lat_f)
    decl = solar_declination(now.timetuple().tm_yday)

    utc_hour = now.hour + (now.minute / 60.0) + (now.second / 3600.0) + (now.microsecond / 3_600_000_000.0)
    local_solar_time = (utc_hour + (lon_f / 15.0)) % 24.0
    hour_angle = math.radians(15.0 * (local_solar_time - 12.0))

    sine_elev = (
        math.sin(lat_rad) * math.sin(decl)
        + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle)
    )
    sine_elev = max(-1.0, min(1.0, sine_elev))
    return math.degrees(math.asin(sine_elev))


def sky_mode(elevation_deg: float) -> str:
    elev = float(elevation_deg)
    if elev > 6.0:
        return "day"
    if elev > 0.0:
        return "golden"
    if elev > -6.0:
        return "civil"
    if elev > -12.0:
        return "nautical"
    if elev > -18.0:
        return "astronomical"
    return "night"


def visual_settings(mode: str, elevation_deg: float) -> dict[str, Any]:
    m = str(mode or "day")
    if m == "day":
        return {"stars": False, "atmosphere_opacity": 1.0, "cloud_opacity": 1.0, "horizon_glow": 0.18}
    if m == "golden":
        return {"stars": False, "atmosphere_opacity": 0.88, "cloud_opacity": 0.92, "horizon_glow": 0.32}
    if m == "civil":
        return {"stars": False, "atmosphere_opacity": 0.72, "cloud_opacity": 0.82, "horizon_glow": 0.26}
    if m == "nautical":
        return {"stars": True, "atmosphere_opacity": 0.50, "cloud_opacity": 0.62, "horizon_glow": 0.18}
    if m == "astronomical":
        return {"stars": True, "atmosphere_opacity": 0.34, "cloud_opacity": 0.46, "horizon_glow": 0.11}
    return {"stars": True, "atmosphere_opacity": 0.22, "cloud_opacity": 0.35, "horizon_glow": 0.07}


def sky_payload(lat: float, lon: float, now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    lat_f = clamp_lat(float(lat))
    lon_f = normalize_lon(float(lon))
    elev = sun_elevation(lat_f, lon_f, now)
    mode = sky_mode(elev)
    payload = {
        "ok": True,
        "schema": "lftr_gfs_sky_v1",
        "server_time_utc": now.isoformat().replace("+00:00", "Z"),
        "lat": lat_f,
        "lon": lon_f,
        "sun_elevation_deg": round(elev, 3),
        "mode": mode,
    }
    payload.update(visual_settings(mode, elev))
    return payload

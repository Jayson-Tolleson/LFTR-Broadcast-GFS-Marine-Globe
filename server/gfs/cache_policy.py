"""Small cache policy helpers for GFS scene/provider caches.

Goal: one readable policy for the many historical cache names. Provider caches
feed raw data; scene caches feed renderers. Anything else should be treated as a
compatibility shim and aggressively janitored.
"""
from __future__ import annotations

import time
from typing import Any


def payload_quality_rank(payload: Any) -> int:
    try:
        if isinstance(payload, dict):
            return int((payload.get("cache_quality") or {}).get("quality_rank") or payload.get("quality_rank") or 0)
    except Exception:
        return 0
    return 0


def payload_state(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    return payload.get("status"), payload.get("payload_state")


def janitor_scene_rows(cache: dict[str, dict[str, Any]], *, max_rows: int, max_age_seconds: int, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    rows: list[tuple[int, float, str]] = []
    removed: list[dict[str, Any]] = []
    for key, row in list(cache.items()):
        if not str(key).startswith("scene_cache:") or not isinstance(row, dict):
            continue
        payload = row.get("payload")
        age = max(0.0, now - float(row.get("time", 0) or 0))
        quality = payload_quality_rank(payload)
        status, state = payload_state(payload)
        if age > max_age_seconds or (age > 900 and (status == "warming" or state == "warming")):
            cache.pop(key, None)
            removed.append({"key": key, "reason": "stale_or_warming", "age_sec": int(age), "quality_rank": quality})
            continue
        rows.append((quality, age, key))
    if len(rows) > max_rows:
        rows.sort(key=lambda item: (item[0], -item[1]))
        for quality, age, key in rows[: max(0, len(rows) - max_rows)]:
            cache.pop(key, None)
            removed.append({"key": key, "reason": "max_rows_low_quality_trim", "age_sec": int(age), "quality_rank": quality})
    return {
        "removed": removed,
        "rows_remaining": len([k for k in cache.keys() if str(k).startswith("scene_cache:")]),
    }

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GFSState:
    enabled: bool = True
    source_name: str = "gfs"
    last_refresh_ts: int | None = None
    last_error: str | None = None
    fish_points: list[dict[str, Any]] = field(default_factory=list)
    cache_ttl_seconds: int = 1800

    ingest_status: str = "not_started"
    ingest_last_attempt_ts: int | None = None
    ingest_last_success_ts: int | None = None
    ingest_error: str | None = None
    degraded_mode: bool = False
    using_last_known_good: bool = False
    last_good_model_state: dict[str, Any] | None = None

    model_cycle: str | None = None
    model_forecast_hour: int | None = None
    model_valid_time: str | None = None
    model_analysis_time: str | None = None
    model_source_url: str | None = None
    model_cache_path: str | None = None
    model_source_format: str | None = None
    fields_available: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    decode_backend: str | None = None
    data_source_mode: str = "primary"

    scalar_fields: dict[str, Any] = field(default_factory=dict)
    scene_cache: dict[str, Any] | None = None
    scene_cache_ts: int = 0
    tile_cache: dict[str, Any] = field(default_factory=dict)
    tile_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    layer_feature_store: dict[str, Any] = field(default_factory=dict)
    layer_feature_index: dict[str, Any] = field(default_factory=dict)
    layer_feature_meta: dict[str, Any] = field(default_factory=dict)

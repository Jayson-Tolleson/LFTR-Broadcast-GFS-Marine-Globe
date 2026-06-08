from __future__ import annotations

import os
import logging
from dataclasses import dataclass


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Settings:
    debug: bool
    socket_path: str
    static_dir: str
    default_room: str
    max_room_len: int
    max_upload_bytes: int

    ai_enabled: bool
    ai_fallback_text: str

    turn_url: str
    turn_urls: str
    turns_url: str
    turn_username: str
    turn_password: str
    domain: str
    public_ip: str

    google_maps_api_key: str



def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}



def load_settings() -> Settings:
    google_maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not google_maps_api_key:
        logger.warning("GOOGLE_MAPS_API_KEY is empty; Google 3D map will not load.")

    return Settings(
        debug=_env_bool("DEBUG", False),
        socket_path=os.getenv("SOCKET_PATH", "/socket.io"),
        static_dir=os.getenv("STATIC_DIR", "static"),
        default_room=os.getenv("DEFAULT_ROOM", "default"),
        max_room_len=int(os.getenv("MAX_ROOM_LEN", "48")),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))),
        ai_enabled=_env_bool("AI_ENABLED", False),
        ai_fallback_text=os.getenv("AI_FALLBACK_TEXT", "AI is unavailable right now."),
        turn_url=os.getenv("TURN_URL", ""),
        turn_urls=os.getenv("TURN_URLS", ""),
        turns_url=os.getenv("TURNS_URL", ""),
        turn_username=os.getenv("TURN_USERNAME", ""),
        turn_password=os.getenv("TURN_PASSWORD", ""),
        domain=os.getenv("DOMAIN", ""),
        public_ip=os.getenv("PUBLIC_IP", ""),
        google_maps_api_key=google_maps_api_key,
    )

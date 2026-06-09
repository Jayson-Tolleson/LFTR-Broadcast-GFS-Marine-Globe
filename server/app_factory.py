from __future__ import annotations

import sitecustomize  # noqa: F401 - LFTR runtime compatibility guards

import logging
from pathlib import Path

from quart import Quart

from server.config import load_settings
from server.gfs.config import load_gfs_config
from server.gfs.engine import GfsEngine
from server.gfs.media import LocationMediaStore
from server.gfs_service import GFSService
from server.routes import register_routes
from server.rtc import RTCManager
from server.state import AppState


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = STATIC_DIR


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def _validate_layout() -> None:
    if not STATIC_DIR.exists():
        raise RuntimeError(f"static directory missing at startup: {STATIC_DIR}")
    if not STATIC_DIR.is_dir():
        raise RuntimeError(f"static path is not a directory at startup: {STATIC_DIR}")
    if not TEMPLATES_DIR.exists() or not TEMPLATES_DIR.is_dir():
        raise RuntimeError(f"templates directory missing at startup: {TEMPLATES_DIR}")


def create_quart_app() -> Quart:
    settings = load_settings()
    _configure_logging(settings.debug)
    _validate_layout()

    app = Quart(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    state = AppState(default_room=settings.default_room)
    rtc = RTCManager(state)
    app.extensions["gfs_engine"] = GfsEngine(load_gfs_config(debug_enabled=settings.debug))
    app.extensions["gfs_media_store"] = LocationMediaStore(data_dir=STATIC_DIR / "data", media_dir=STATIC_DIR / "fishvid")
    # Start /gfs at process boot so the server-side cache stays hot even with no browsers connected.
    gfs_service = GFSService(str(STATIC_DIR))
    app.extensions["gfs_service"] = gfs_service
    try:
        gfs_service.start_always_on_cache()
    except Exception:
        logging.getLogger("server.startup").exception("failed to start always-on gfs cache")

    register_routes(app, state, settings, rtc)

    app.settings_obj = settings
    app.state_obj = state
    app.rtc_manager = rtc

    logging.getLogger("server.startup").info(
        "startup ready framework=quart static=%s templates=%s routes=/,/broadcast,/watch,/gfs ws=/ws/watch,/ws/broadcast,/ws/chat,/ws/gfs",
        STATIC_DIR,
        TEMPLATES_DIR,
    )
    return app


def create_asgi_app() -> Quart:
    return create_quart_app()


def create_app() -> Quart:
    return create_quart_app()

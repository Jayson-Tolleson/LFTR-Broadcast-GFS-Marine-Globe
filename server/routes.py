from __future__ import annotations

from pathlib import Path

from quart import Quart

from server.api import api_bp
from server.broadcast.routes import register_broadcast_routes
from server.config import Settings
from server.gfs import create_gfs_blueprint
from server.routes_core import register_core_routes, _static_file as _core_static_file, build_ice_servers
from server.rtc import RTCManager
from server.state import AppState


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_file(path_name: str):
    return _core_static_file(path_name, STATIC_DIR)


def register_routes(app: Quart, state: AppState, settings: Settings, rtc: RTCManager) -> None:
    register_core_routes(app, settings, STATIC_DIR)
    register_broadcast_routes(app, state, rtc)
    app.register_blueprint(create_gfs_blueprint(STATIC_DIR))
    app.register_blueprint(api_bp)


__all__ = ["register_routes", "_static_file", "build_ice_servers", "STATIC_DIR"]

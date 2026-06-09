from __future__ import annotations

__all__ = ["GfsEngine", "create_gfs_blueprint"]


def __getattr__(name: str):
    if name == "GfsEngine":
        from server.gfs.engine import GfsEngine
        return GfsEngine
    if name == "create_gfs_blueprint":
        from server.gfs.routes import create_gfs_blueprint
        return create_gfs_blueprint
    raise AttributeError(name)

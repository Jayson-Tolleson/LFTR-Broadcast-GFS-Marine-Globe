from __future__ import annotations

from pathlib import Path


class LocationMediaStore:
    """Small compatibility holder used by app_factory.

    The main GFSService still owns the actual media/report store; this object
    keeps startup from failing when newer app_factory code registers extensions.
    """

    def __init__(self, data_dir: Path, media_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.media_dir = Path(media_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

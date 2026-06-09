from __future__ import annotations

from typing import Any


class GfsError(Exception):
    status_code = 500
    code = "gfs_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = int(status_code)
        if code is not None:
            self.code = code
        self.message = message
        self.extra = extra

    def to_json(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


class InvalidBBoxError(GfsError):
    status_code = 400
    code = "invalid_bbox"


class ProviderUnavailableError(GfsError):
    status_code = 503
    code = "provider_unavailable"

    def __init__(self, message: str, *, provider: str | None = None, **extra: Any) -> None:
        if provider:
            extra["provider"] = provider
        super().__init__(message, **extra)

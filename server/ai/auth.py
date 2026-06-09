from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _existing_file(path_value: str) -> str:
    candidate = (path_value or "").strip()
    if not candidate:
        return ""
    path = Path(candidate)
    return str(path) if path.is_file() else ""


def is_explicit_key_available() -> bool:
    return bool(_existing_file(os.getenv("GCP_KEY", "")) or _existing_file(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")))


def is_adc_available() -> bool:
    try:
        import google.auth

        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return True
    except Exception:
        return False


def maybe_apply_google_credentials_env() -> str | None:
    explicit = _existing_file(os.getenv("GCP_KEY", "")) or _existing_file(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""))
    if explicit:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = explicit
        return explicit

    # Never poison env with non-existent path
    current = _existing_file(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""))
    if current:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = current
        return current
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    return None


def get_effective_google_project() -> str:
    for key in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "GOOGLE_PROJECT_ID"):
        value = (os.getenv(key, "") or "").strip()
        if value:
            return value
    try:
        import google.auth

        _, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return (project or "").strip()
    except Exception:
        return ""


def resolve_gcp_auth_mode() -> str:
    explicit = is_explicit_key_available()
    adc = is_adc_available()
    if explicit:
        return "explicit_key_ok"
    if adc:
        return "adc_ok"

    bad_paths = [os.getenv("GCP_KEY", "").strip(), os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()]
    if any(p for p in bad_paths if p) and not explicit:
        return "auth_invalid"
    return "auth_missing"


def auth_status_payload() -> dict[str, Any]:
    maybe_apply_google_credentials_env()
    mode = resolve_gcp_auth_mode()
    return {
        "auth_mode": mode,
        "adc_ok": mode == "adc_ok",
        "explicit_key_ok": mode == "explicit_key_ok",
        "auth_missing": mode == "auth_missing",
        "auth_invalid": mode == "auth_invalid",
        "project": get_effective_google_project(),
        "credentials_path": os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip() or None,
    }

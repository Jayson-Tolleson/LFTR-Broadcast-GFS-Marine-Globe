from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Sequence

from .gemini import generate_ai_reply, provider_name
from .speech import synthesize_voice, transcribe_pcm16_chunk
from .auth import auth_status_payload, resolve_gcp_auth_mode

log = logging.getLogger("server.ai.pkg")
_WARNED_STT_UNAVAILABLE = False


def _json_response(payload: Dict[str, Any], status: int = 200) -> Any:
    try:
        from quart import Response

        return Response(json.dumps(payload), status=status, content_type="application/json")
    except Exception:
        return payload


async def handle_chat(payload: Dict[str, Any], fallback_text: str) -> Dict[str, Any]:
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return {"ok": False, "provider": "none", "error": "missing_text", "message": "Please provide text.", "data": {"reply": "", "command": None}}
    try:
        response = await asyncio.to_thread(generate_ai_reply, text)
        reply = str((response or {}).get("text") or "").strip() or fallback_text
        return {"ok": True, "provider": provider_name(), "error": None, "message": "ok", "data": {"reply": reply, "command": None}}
    except Exception as exc:
        log.exception("ai chat provider request failed")
        return {"ok": False, "provider": provider_name(), "error": "provider_request_failed", "message": str(exc), "data": {"reply": fallback_text, "command": None}}


async def handle_tts(payload: Dict[str, Any]) -> Any:
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return _json_response({"ok": False, "provider": "none", "error": "missing_text", "message": "text is required", "data": None}, 400)

    try:
        voice_url = await asyncio.to_thread(synthesize_voice, text)
        return _json_response({"ok": True, "provider": provider_name(), "error": None, "message": "ok", "data": {"voice": voice_url}})
    except Exception as exc:
        log.exception("tts provider request failed")
        return _json_response({"ok": False, "provider": provider_name(), "error": "provider_request_failed", "message": str(exc), "data": None}, 500)


async def handle_websearch(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = str((payload or {}).get("query") or "").strip()
    if not query:
        return {"ok": False, "provider": "none", "error": "missing_query", "message": "query is required", "data": {"query": "", "results": []}}

    serpapi_key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not serpapi_key:
        return {"ok": False, "provider": "none", "error": "provider_unconfigured", "message": "SERPAPI_API_KEY missing", "data": {"query": query, "results": []}}

    def _fetch() -> Dict[str, Any]:
        q = urllib.parse.urlencode({"q": query, "api_key": serpapi_key, "engine": "google", "num": 5})
        with urllib.request.urlopen(f"https://serpapi.com/search.json?{q}", timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        organic = body.get("organic_results") or []
        rows = [{"title": r.get("title") or "", "url": r.get("link") or "", "snippet": r.get("snippet") or ""} for r in organic[:5]]
        return {"ok": True, "provider": "serpapi", "error": None, "message": "ok", "data": {"query": query, "results": rows}}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        log.exception("websearch provider request failed")
        return {"ok": False, "provider": "serpapi", "error": "provider_request_failed", "message": str(exc), "data": {"query": query, "results": []}}



def stt_available() -> bool:
    try:
        from google.cloud import speech  # noqa: F401
        return resolve_gcp_auth_mode() in {"adc_ok", "explicit_key_ok"}
    except Exception:
        return False


def ai_status() -> dict[str, Any]:
    status = auth_status_payload()
    return {
        **status,
        "stt_ready": stt_available(),
    }


async def transcribe_track(chunks: Sequence[bytes], *, sample_rate_hz: int, channels: int, encoding: str = "linear16") -> str:
    global _WARNED_STT_UNAVAILABLE
    if not chunks:
        return ""

    chunk = b"".join([c for c in chunks if c])
    if not chunk:
        return ""

    try:
        transcript = await asyncio.to_thread(
            transcribe_pcm16_chunk,
            chunk,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        return str(transcript or "").strip()
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        if not _WARNED_STT_UNAVAILABLE:
            _WARNED_STT_UNAVAILABLE = True
            log.warning("STT backend unavailable; transcribe_track will return empty transcript err=%s", exc.__class__.__name__)
        log.debug("transcribe_track failed", exc_info=True)
        return ""



__all__ = [
    "generate_ai_reply",
    "provider_name",
    "synthesize_voice",
    "handle_chat",
    "handle_tts",
    "handle_websearch",
    "transcribe_track",
    "stt_available",
    "ai_status",
]

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4
from collections import defaultdict
from dataclasses import asdict
from typing import Any
import shutil

from quart import jsonify, request, websocket

from server import ai
from server.audio.validation import AudioValidationError, decode_audio_chunk_payload
from server.media.upload import save_upload
from server.state import AppState
from server.rtc import StreamOfflineError
from server.utils import now_ms


log = logging.getLogger("server.broadcast.routes")
STT_SOURCE_LITERAL = {"source": "stt"}
WATCH_OFFER_TIMEOUT_S = 12.0
STT_FAILURE_BACKOFF_S = 20.0
STT_FAILURE_THRESHOLD = 3
WATCH_RETRY_BACKOFF_S = 1.5
RECORDINGS_DIR = Path(__file__).resolve().parents[2] / "uploads" / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


class RoomRegistry:
    def __init__(self) -> None:
        self.rooms: dict[str, dict[str, dict[Any, str]]] = defaultdict(lambda: {"chat": {}, "broadcast": {}, "watch": {}})

    def register(self, room: str, kind: str, ws: Any, client_id: str) -> None:
        self.rooms[room][kind][ws] = client_id

    def unregister(self, room: str, kind: str, ws: Any) -> str | None:
        bucket = self.rooms.get(room, {}).get(kind, {})
        cid = bucket.pop(ws, None)
        if room in self.rooms and not any(self.rooms[room][k] for k in ("chat", "broadcast", "watch")):
            self.rooms.pop(room, None)
        return cid

    def viewer_count(self, room: str) -> int:
        return len(self.rooms.get(room, {}).get("watch", {}))

    async def send_ws(self, ws: Any, message: dict[str, Any]) -> None:
        await ws.send_json(message)

    async def broadcast_room(self, room: str, message: dict[str, Any], kinds: tuple[str, ...] = ("chat", "broadcast", "watch")) -> None:
        dead: list[tuple[str, Any]] = []
        room_obj = self.rooms.get(room, {})
        for kind in kinds:
            for ws in list(room_obj.get(kind, {}).keys()):
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append((kind, ws))
        for kind, ws in dead:
            self.unregister(room, kind, ws)


registry = RoomRegistry()


async def _set_ai_status(state: AppState, room_id: str, status: str) -> None:
    room = state.ensure_room(room_id)
    room.settings.ai_status = "active" if status == "active" else "idle"
    await registry.broadcast_room(room_id, {"type": "ai_status", "room": room_id, "status": room.settings.ai_status, "ts": now_ms()})


def _normalize_room_client(payload: dict[str, Any], default_room: str, role: str) -> tuple[str, str]:
    room = str(payload.get("room") or payload.get("payload", {}).get("room") or default_room or "default")
    client_id = str(payload.get("clientId") or payload.get("payload", {}).get("clientId") or f"{role}:{id(payload)}")
    return room, client_id


def _stage_payload(room_id: str, room) -> dict[str, Any]:
    media = room.media
    mode = "live" if media.live_active else ("upload" if media.latest_upload_url else "none")
    return {
        "room": room_id,
        "mode": mode,
        "liveActive": bool(media.live_active),
        "latestUploadUrl": media.latest_upload_url,
        "latestUploadMime": media.latest_upload_mime,
        "latestUploadAt": media.latest_upload_at,
        "locationId": media.location_id,
        "label": media.label,
        "ts": now_ms(),
    }


async def _broadcast_presence(state: AppState, room_id: str) -> None:
    room = state.ensure_room(room_id)
    room.runtime.viewer_count = registry.viewer_count(room_id)
    room.runtime.broadcaster_present = room.broadcaster_sid is not None
    room.runtime.broadcast_connected = room.broadcaster_sid is not None
    room.runtime.chat_connected = bool(registry.rooms.get(room_id, {}).get("chat"))
    room.runtime.watch_connected = room.runtime.viewer_count > 0

    await registry.broadcast_room(
        room_id,
        {
            "type": "presence",
            "room": room_id,
            "broadcaster_present": room.runtime.broadcaster_present,
            "viewer_count": room.runtime.viewer_count,
            "ts": now_ms(),
        },
    )
    await registry.broadcast_room(room_id, {"type": "viewer_count", "room": room_id, "count": room.runtime.viewer_count, "ts": now_ms()})
    snapshot = state.room_state_payload(room_id)
    await registry.broadcast_room(room_id, {"type": "state_sync", "room": room_id, "state": snapshot, "ts": now_ms()})
    await registry.broadcast_room(room_id, {"type": "state_update", "room": room_id, "state": snapshot, "ts": now_ms()})


def _rtc_room_live(rtc, room_id: str, room) -> bool:
    rtc_live = bool(rtc is not None and rtc.has_live_source(room_id))
    if room.media.live_active != rtc_live:
        room.media.live_active = rtc_live
        room.media.mode = "live" if rtc_live else ("upload" if room.media.latest_upload_url else "none")
    return rtc_live


async def _chat_emit(room_id: str, payload: dict[str, Any]) -> None:
    await registry.broadcast_room(room_id, {"type": "chat", "room": room_id, **payload, "ts": now_ms()})


AI_REPLY_DEDUPE_MS = 6000
_RECENT_AI_REPLY_KEYS: dict[str, int] = {}


def _ai_reply_key(room_id: str, client_id: str, text: str) -> str:
    return f"{room_id}:{client_id}:{hash(text.strip().lower())}"


async def _maybe_emit_ai_reply(state: AppState, room_id: str, client_id: str, text: str, *, source: str | None = None, stt_final: bool = False) -> None:
    room = state.ensure_room(room_id)
    if not room.settings.ai_enabled:
        return
    # STT chunks are posted to chat, but chunked interim transcripts should not
    # trigger repeated AI answers. Only an explicitly final STT transcript may reply.
    if source == "stt" and not stt_final:
        return
    clean = str(text or "").strip()
    if not clean:
        return
    now = now_ms()
    key = _ai_reply_key(room_id, client_id, clean)
    last = _RECENT_AI_REPLY_KEYS.get(key, 0)
    if last and now - last < AI_REPLY_DEDUPE_MS:
        return
    _RECENT_AI_REPLY_KEYS[key] = now
    # keep dict bounded
    for k, ts in list(_RECENT_AI_REPLY_KEYS.items())[:64]:
        if now - ts > 60000:
            _RECENT_AI_REPLY_KEYS.pop(k, None)
    await _set_ai_status(state, room_id, "active")
    try:
        result = await ai.handle_chat({"text": clean, "room": room_id, "clientId": client_id}, "AI is unavailable right now.")
        data = result.get("data") if isinstance(result, dict) else {}
        reply = str((data or {}).get("reply") or "").strip()
        if not reply:
            return
        payload: dict[str, Any] = {
            "user": "assistant",
            "clientId": "room-ai",
            "source": "ai",
            "text": reply,
            "replyToClientId": client_id,
        }
        if room.settings.tts_enabled or room.settings.hear_ai_voice:
            try:
                voice = await asyncio.to_thread(ai.synthesize_voice, reply)
                if voice:
                    payload["voice"] = voice
                    payload["voiceUrl"] = voice
            except Exception as exc:
                log.warning("room ai tts failed room=%s err=%s", room_id, exc.__class__.__name__)
        await _chat_emit(room_id, payload)
    except Exception as exc:
        log.warning("room ai reply failed room=%s err=%s", room_id, exc.__class__.__name__)
    finally:
        await _set_ai_status(state, room_id, "idle")


def _find_ws_by_client(room_id: str, kind: str, client_id: str) -> Any | None:
    bucket = registry.rooms.get(room_id, {}).get(kind, {})
    for ws, cid in bucket.items():
        if cid == client_id:
            return ws
    return None


async def _route_to_viewer(room_id: str, viewer_id: str, payload: dict[str, Any]) -> bool:
    ws = _find_ws_by_client(room_id, "watch", viewer_id)
    if ws is None:
        log.warning("signal route miss room=%s kind=watch viewer=%s type=%s", room_id, viewer_id, payload.get("type"))
        return False
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        log.warning("signal route failed room=%s kind=watch viewer=%s type=%s", room_id, viewer_id, payload.get("type"), exc_info=True)
        registry.unregister(room_id, "watch", ws)
        return False


async def _route_to_broadcaster(room_id: str, payload: dict[str, Any]) -> bool:
    bucket = registry.rooms.get(room_id, {}).get("broadcast", {})
    ws = next(iter(bucket.keys()), None)
    if ws is None:
        log.debug("signal route miss room=%s kind=broadcast type=%s", room_id, payload.get("type"))
        return False
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        log.warning("signal route failed room=%s kind=broadcast type=%s", room_id, payload.get("type"), exc_info=True)
        registry.unregister(room_id, "broadcast", ws)
        return False


async def _handle_chat_text(state: AppState, room_id: str, client_id: str, role: str, text: str, source: str | None = None, *, stt_final: bool = False) -> None:
    # Emit the human/STT transcript first. Then optionally let room AI respond once.
    base_payload = {"user": role, "clientId": client_id, "text": text}
    if source:
        base_payload["source"] = source
    if source == "stt":
        base_payload["sttFinal"] = bool(stt_final)
    await _chat_emit(room_id, base_payload)
    if role != "assistant":
        await _maybe_emit_ai_reply(state, room_id, client_id, text, source=source, stt_final=stt_final)


def _merge_room_state(room, update: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "ai_enabled": bool(update.get("ai_enabled", room.settings.ai_enabled)),
        "stt_enabled": bool(update.get("stt_enabled", room.settings.stt_enabled)),
        "tts_enabled": bool(update.get("tts_enabled", room.settings.tts_enabled)),
        "hear_ai_voice": bool(update.get("hear_ai_voice", room.settings.hear_ai_voice)),
        "mic_enabled": bool(update.get("mic_enabled", room.settings.mic_enabled)),
        "camera_enabled": bool(update.get("camera_enabled", room.settings.camera_enabled)),
        "screen_enabled": bool(update.get("screen_enabled", room.settings.screen_enabled)),
        "noise_cancel_enabled": bool(update.get("noise_cancel_enabled", room.settings.noise_cancel_enabled)),
        "record_enabled": bool(update.get("record_enabled", room.settings.record_enabled)),
        "rtmp_enabled": bool(update.get("rtmp_enabled", room.settings.rtmp_enabled)),
        "rtmp_url": str(update.get("rtmp_url", room.settings.rtmp_url or "") or ""),
    }
    for k, v in normalized.items():
        setattr(room.settings, k, v)
    return normalized


def register_broadcast_routes(app, state: AppState | None = None, rtc=None) -> None:
    state = state or AppState(default_room="default")

    async def _emit_room_from_rtc(room_id: str, message: dict[str, Any]) -> None:
        await registry.broadcast_room(room_id, {"room": room_id, **message})

    state.ws_emit_room = _emit_room_from_rtc

    @app.websocket('/ws/chat')
    async def ws_chat():
        ws = websocket._get_current_object()
        room_id = state.default_room
        client_id = f"chat:{id(ws)}"
        role = "participant"
        joined = False
        stt_failures = 0
        stt_backoff_until = 0.0
        stt_contract_logged = False
        log.info("chat socket connected room=%s client=%s", room_id, client_id)
        try:
            while True:
                try:
                    payload = await websocket.receive_json()
                except Exception as exc:
                    log.info("/ws/chat receive closed room=%s client=%s reason=%s", room_id, client_id, exc.__class__.__name__)
                    break
                kind = payload.get("type", "chat")
                data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
                if kind == "join":
                    room_id, client_id = _normalize_room_client(data, state.default_room, "chat")
                    role = str(data.get("role") or "participant")
                    registry.register(room_id, "chat", ws, client_id)
                    joined = True
                    await ws.send_json({"type": "connected", "room": room_id, "clientId": client_id, "role": role, "ts": now_ms()})
                    await ws.send_json({"type": "state_sync", "room": room_id, "state": state.room_state_payload(room_id), "ts": now_ms()})
                    await ws.send_json({"type": "stage_state", "payload": _stage_payload(room_id, state.ensure_room(room_id))})
                    await _broadcast_presence(state, room_id)
                    continue

                if not joined:
                    room_id, client_id = _normalize_room_client(data, state.default_room, "chat")
                    registry.register(room_id, "chat", ws, client_id)
                    joined = True

                room = state.ensure_room(room_id)
                if kind == "ping":
                    await ws.send_json({"type": "pong", "room": room_id, "ts": now_ms()})
                elif kind == "chat":
                    text = str(data.get("text") or "").strip()
                    if text:
                        await _handle_chat_text(state, room_id, client_id, role, text)
                elif kind == "toggle_state":
                    _merge_room_state(room, data.get("state") or {})
                    await _broadcast_presence(state, room_id)
                elif kind == "web_search":
                    query = str(data.get("query") or "").strip()
                    if query and room.settings.web_search_enabled:
                        res = await ai.handle_websearch({"query": query})
                        await registry.broadcast_room(room_id, {"type": "web_search_result", "room": room_id, "query": query, "result": res, "ts": now_ms()})
                elif kind == "attachment_uploaded":
                    attachment = data.get("attachment") or {}
                    await registry.broadcast_room(room_id, {"type": "attachment", "room": room_id, "user": role, "clientId": client_id, "attachment": attachment, "ts": now_ms()})
                elif kind == "audio_chunk":
                    if not room.settings.stt_enabled:
                        continue
                    now_loop = asyncio.get_running_loop().time()
                    if now_loop < stt_backoff_until:
                        continue

                    try:
                        parsed = decode_audio_chunk_payload(data)
                        if not stt_contract_logged:
                            stt_contract_logged = True
                            log.info(
                                "stt contract accepted room=%s client=%s encoding=%s sample_rate=%s channels=%s chunk_ms=%s",
                                room_id,
                                client_id,
                                parsed.encoding,
                                parsed.sample_rate_hz,
                                parsed.channels,
                                data.get("durationMs") or data.get("duration_ms"),
                            )
                    except AudioValidationError as exc:
                        stt_failures += 1
                        log.warning("stt chunk rejected room=%s client=%s reason=%s", room_id, client_id, str(exc))
                        await ws.send_json({"type": "stt_status", "room": room_id, "ok": False, "reason": str(exc), "failures": stt_failures, "ts": now_ms()})
                        if stt_failures >= STT_FAILURE_THRESHOLD:
                            stt_backoff_until = now_loop + STT_FAILURE_BACKOFF_S
                            log.warning("stt backoff activated room=%s client=%s backoff_s=%s", room_id, client_id, STT_FAILURE_BACKOFF_S)
                        continue

                    transcribe_fn = getattr(ai, "transcribe_track", None)
                    if not callable(transcribe_fn):
                        log.warning("transcribe_track missing room=%s client=%s", room_id, client_id)
                        continue

                    try:
                        text = str(
                            await transcribe_fn(
                                [parsed.audio_bytes],
                                sample_rate_hz=parsed.sample_rate_hz,
                                channels=parsed.channels,
                                encoding=parsed.encoding,
                            )
                            or ""
                        ).strip()
                        stt_failures = 0
                    except ValueError as exc:
                        stt_failures = STT_FAILURE_THRESHOLD
                        stt_backoff_until = now_loop + STT_FAILURE_BACKOFF_S
                        log.warning("stt non-retryable room=%s client=%s reason=%s backoff_s=%s", room_id, client_id, str(exc), STT_FAILURE_BACKOFF_S)
                        continue
                    except Exception as exc:
                        stt_failures += 1
                        log.warning("stt failed room=%s client=%s err=%s", room_id, client_id, exc.__class__.__name__)
                        if stt_failures >= STT_FAILURE_THRESHOLD:
                            stt_backoff_until = now_loop + STT_FAILURE_BACKOFF_S
                            log.warning("stt backoff activated room=%s client=%s backoff_s=%s", room_id, client_id, STT_FAILURE_BACKOFF_S)
                        continue

                    if text:
                        await ws.send_json({"type": "stt_status", "room": room_id, "ok": True, "transcribed": True, "chars": len(text), "ts": now_ms()})
                        await _handle_chat_text(state, room_id, client_id, role, text, source="stt", stt_final=bool(data.get("final") or data.get("isFinal")))
                    else:
                        await ws.send_json({"type": "stt_status", "room": room_id, "ok": True, "transcribed": False, "reason": "empty_transcript", "ts": now_ms()})
        finally:
            if joined:
                registry.unregister(room_id, "chat", ws)
                await _broadcast_presence(state, room_id)

    @app.websocket('/ws/broadcast')
    async def ws_broadcast():
        ws = websocket._get_current_object()
        room_id = state.default_room
        client_id = f"broadcaster:{id(ws)}"
        joined = False
        log.info("broadcast socket connected room=%s client=%s", room_id, client_id)
        try:
            while True:
                try:
                    payload = await websocket.receive_json()
                except Exception as exc:
                    log.info("/ws/broadcast receive closed room=%s client=%s reason=%s", room_id, client_id, exc.__class__.__name__)
                    break
                kind = payload.get("type", "ping")
                data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
                if kind == "join":
                    room_id, client_id = _normalize_room_client(data, state.default_room, "broadcaster")
                    registry.register(room_id, "broadcast", ws, client_id)
                    joined = True
                    room = state.ensure_room(room_id)
                    room.broadcaster_sid = client_id
                    room.runtime.broadcast_connected = True
                    await ws.send_json({"type": "state_sync", "room": room_id, "state": state.room_state_payload(room_id), "ts": now_ms()})
                    await ws.send_json({"type": "connectivity", "room": room_id, "connected": True, "ts": now_ms()})
                    await ws.send_json({"type": "stage_state", "payload": _stage_payload(room_id, room)})
                    await _broadcast_presence(state, room_id)
                    continue

                if not joined:
                    room_id, client_id = _normalize_room_client(data, state.default_room, "broadcaster")
                    registry.register(room_id, "broadcast", ws, client_id)
                    joined = True
                    room = state.ensure_room(room_id)
                    room.broadcaster_sid = client_id

                room = state.ensure_room(room_id)
                if kind == "ping":
                    await ws.send_json({"type": "pong", "room": room_id, "ts": now_ms()})
                elif kind == "webrtc_offer":
                    sdp = data.get("sdp")
                    sdp_type = data.get("type") or "offer"
                    if sdp:
                        log.info("broadcaster offer received room=%s client=%s sdp_type=%s", room_id, client_id, sdp_type)
                        answer = await rtc.start_broadcaster_from_offer(room_id, client_id, sdp, sdp_type) if rtc is not None else {"sdp": None, "type": "answer"}
                        log.info("broadcaster answer sent room=%s client=%s answer_type=%s", room_id, client_id, answer.get("type", "answer"))
                        _rtc_room_live(rtc, room_id, room)
                        await ws.send_json({"type": "webrtc_answer", "room": room_id, "clientId": client_id, "sdp": answer.get("sdp"), "answerType": answer.get("type", "answer"), "ts": now_ms()})
                        await registry.broadcast_room(room_id, {"type": "stage_state", "payload": _stage_payload(room_id, room)})
                        await _broadcast_presence(state, room_id)
                elif kind in {"webrtc_ice", "watch_ice"}:
                    if rtc is not None:
                        cand = rtc.parse_ice(data or {})
                        await rtc.add_broadcaster_ice_candidate(room_id, client_id, cand)
                elif kind == "offer":
                    viewer_id = str(data.get("viewerId") or "").strip()
                    sdp = data.get("sdp")
                    sdp_type = data.get("type") or "offer"
                    if viewer_id and sdp:
                        ok = await _route_to_viewer(
                            room_id,
                            viewer_id,
                            {"type": "offer", "room": room_id, "viewerId": viewer_id, "payload": {"sdp": sdp, "type": sdp_type}, "ts": now_ms()},
                        )
                        log.info("signal offer route room=%s viewer=%s ok=%s", room_id, viewer_id, ok)
                elif kind == "ice-candidate":
                    viewer_id = str(data.get("viewerId") or "").strip()
                    cand = data.get("candidate")
                    if viewer_id and cand:
                        ok = await _route_to_viewer(
                            room_id,
                            viewer_id,
                            {"type": "ice-candidate", "room": room_id, "viewerId": viewer_id, "candidate": cand, "ts": now_ms()},
                        )
                        log.debug("signal ice route room=%s viewer=%s ok=%s", room_id, viewer_id, ok)
                elif kind == "media_ready":
                    room.media.live_active = True
                    room.media.mode = "live"
                    await registry.broadcast_room(room_id, {"type": "broadcaster-start", "room": room_id, "ts": now_ms()}, kinds=("watch",))
                    await _broadcast_presence(state, room_id)
                elif kind == "toggle_state":
                    _merge_room_state(room, data.get("state") or {})
                    await _broadcast_presence(state, room_id)
                elif kind == "set_media_mode":
                    room.settings.camera_enabled = bool(data.get("camera", room.settings.camera_enabled))
                    room.settings.screen_enabled = bool(data.get("screen", room.settings.screen_enabled))
                    room.settings.mic_enabled = bool(data.get("mic", room.settings.mic_enabled))
                    await _broadcast_presence(state, room_id)
                elif kind == "chat":
                    text = str(data.get("text") or "").strip()
                    if text:
                        await _handle_chat_text(state, room_id, client_id, "broadcaster", text)
                elif kind == "attachment":
                    await registry.broadcast_room(room_id, {"type": "attachment", "room": room_id, "user": "broadcaster", "clientId": client_id, "attachment": data.get("attachment") or {}, "ts": now_ms()})
        finally:
            if joined:
                registry.unregister(room_id, "broadcast", ws)
                room = state.ensure_room(room_id)
                if room.broadcaster_sid == client_id:
                    room.broadcaster_sid = None
                if rtc is not None:
                    try:
                        await rtc.stop_broadcaster(room_id, client_id)
                    except Exception:
                        log.exception("broadcaster cleanup failed")
                room.media.live_active = False
                room.media.mode = "upload" if room.media.latest_upload_url else "none"
                log.info("broadcaster stop room=%s client=%s", room_id, client_id)
                await registry.broadcast_room(room_id, {"type": "broadcaster-stop", "room": room_id, "ts": now_ms()}, kinds=("watch",))
                await _broadcast_presence(state, room_id)

    @app.websocket('/ws/watch')
    async def ws_watch():
        ws = websocket._get_current_object()
        room_id = state.default_room
        client_id = f"watch:{id(ws)}"
        joined = True
        offer_outstanding = False
        offer_started_at = 0.0
        watcher_state = "disconnected"
        next_request_allowed_at = 0.0
        log.info("watch socket connected room=%s client=%s", room_id, client_id)

        def _set_state(next_state: str, reason: str) -> None:
            nonlocal watcher_state
            if watcher_state != next_state:
                log.info("watch state transition room=%s client=%s %s->%s reason=%s", room_id, client_id, watcher_state, next_state, reason)
                watcher_state = next_state

        _set_state("socket_connected", "ws_open")

        async def _send_waiting_no_broadcaster(active_room_id: str, active_client_id: str) -> None:
            room = state.ensure_room(active_room_id)
            log.debug("watch waiting room=%s client=%s reason=no_broadcaster", active_room_id, active_client_id)
            await ws.send_json({"type": "presence", "room": active_room_id, "broadcaster_present": False, "stream_live": False, "viewer_count": len(room.viewers), "ts": now_ms()})
            await ws.send_json({"type": "waiting", "room": active_room_id, "message": "no_broadcaster", "ts": now_ms()})

        async def _send_waiting_stream_offline(active_room_id: str, active_client_id: str) -> None:
            room = state.ensure_room(active_room_id)
            log.debug("watch waiting room=%s client=%s reason=stream_not_live", active_room_id, active_client_id)
            await ws.send_json({"type": "presence", "room": active_room_id, "broadcaster_present": room.broadcaster_sid is not None, "stream_live": False, "viewer_count": len(room.viewers), "ts": now_ms()})
            await ws.send_json({"ok": False, "type": "waiting", "room": active_room_id, "message": "stream_offline", "ts": now_ms()})

        def _room_has_live_source(active_room_id: str) -> bool:
            room = state.ensure_room(active_room_id)
            live = _rtc_room_live(rtc, active_room_id, room)
            log.debug("watch live-check room=%s client=%s broadcaster_present=%s rtc_live=%s", active_room_id, client_id, bool(room.broadcaster_sid), live)
            return live

        async def _send_offer(active_room_id: str, active_client_id: str) -> None:
            nonlocal offer_outstanding, offer_started_at
            offer = await rtc.start_viewer_offer(active_room_id, active_client_id)
            await ws.send_json({"type": "watch_offer", "room": active_room_id, "payload": offer, "ts": now_ms()})
            offer_outstanding = True
            offer_started_at = asyncio.get_running_loop().time()
            _set_state("offer_pending", "offer_sent")
            log.info("watch offer sent room=%s client=%s", active_room_id, active_client_id)

        registry.register(room_id, "watch", ws, client_id)
        state.ensure_room(room_id).viewers[client_id] = True
        await ws.send_json({"type": "connected", "room": room_id, "clientId": client_id, "role": "viewer", "ts": now_ms()})
        await ws.send_json({"type": "state_sync", "room": room_id, "state": state.room_state_payload(room_id), "ts": now_ms()})
        await _broadcast_presence(state, room_id)
        _set_state("joined", "auto_join")
        room = state.ensure_room(room_id)
        if room.broadcaster_sid is None:
            _set_state("waiting_for_broadcaster", "auto_no_broadcaster")
            await _send_waiting_no_broadcaster(room_id, client_id)
        elif await _route_to_broadcaster(room_id, {"type": "viewer_joined", "room": room_id, "viewerId": client_id, "ts": now_ms()}):
            _set_state("request_pending", "auto_viewer_joined")
        elif rtc is not None and _room_has_live_source(room_id):
            _set_state("request_pending", "auto_request_stream")
            try:
                await _send_offer(room_id, client_id)
            except StreamOfflineError:
                _set_state("waiting_for_broadcaster", "auto_stream_offline")
                await _send_waiting_stream_offline(room_id, client_id)
            except Exception:
                log.exception("watch auto-offer creation failed room=%s client=%s", room_id, client_id)
        else:
            _set_state("waiting_for_broadcaster", "auto_stream_pending")
            await _send_waiting_stream_offline(room_id, client_id)

        try:
            while True:
                try:
                    payload = await websocket.receive_json()
                except Exception as exc:
                    log.info("/ws/watch receive closed room=%s client=%s reason=%s", room_id, client_id, exc.__class__.__name__)
                    break

                kind = payload.get("type", "join")
                data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload

                now_loop = asyncio.get_running_loop().time()
                if offer_outstanding and (now_loop - offer_started_at) > WATCH_OFFER_TIMEOUT_S:
                    log.warning("watch offer timeout room=%s client=%s timeout_s=%s", room_id, client_id, WATCH_OFFER_TIMEOUT_S)
                    offer_outstanding = False
                    offer_started_at = 0.0
                    next_request_allowed_at = now_loop + WATCH_RETRY_BACKOFF_S
                    _set_state("waiting_for_broadcaster", "offer_timeout")

                if kind in {"join", "watch_join"}:
                    next_room_id, next_client_id = _normalize_room_client(data, state.default_room, "viewer")
                    if joined and next_room_id == room_id:
                        if next_client_id != client_id:
                            state.ensure_room(room_id).viewers.pop(client_id, None)
                            registry.unregister(room_id, "watch", ws)
                            client_id = next_client_id
                            registry.register(room_id, "watch", ws, client_id)
                            state.ensure_room(room_id).viewers[client_id] = True
                            log.info("watch join client_id updated room=%s client=%s", room_id, client_id)
                        else:
                            log.info("watch duplicate join ignored room=%s client=%s", room_id, client_id)
                        await ws.send_json({"type": "state_sync", "room": room_id, "state": state.room_state_payload(room_id), "ts": now_ms()})
                        continue
                    if joined:
                        registry.unregister(room_id, "watch", ws)
                        state.ensure_room(room_id).viewers.pop(client_id, None)

                    room_id, client_id = next_room_id, next_client_id
                    log.info("watcher joined room=%s client=%s", room_id, client_id)
                    registry.register(room_id, "watch", ws, client_id)
                    joined = True
                    _set_state("joined", "join")
                    state.ensure_room(room_id).viewers[client_id] = True
                    await ws.send_json({"type": "state_sync", "room": room_id, "state": state.room_state_payload(room_id), "ts": now_ms()})
                    await _broadcast_presence(state, room_id)
                    room = state.ensure_room(room_id)
                    if room.broadcaster_sid is None:
                        _set_state("waiting_for_broadcaster", "no_broadcaster")
                        await _send_waiting_no_broadcaster(room_id, client_id)
                    else:
                        if rtc is None:
                            _set_state("waiting_for_broadcaster", "rtc_unavailable")
                            await ws.send_json({"type": "error", "room": room_id, "message": "rtc_unavailable", "ts": now_ms()})
                        elif not _room_has_live_source(room_id):
                            _set_state("waiting_for_broadcaster", "stream_offline")
                            await _send_waiting_stream_offline(room_id, client_id)
                        elif not offer_outstanding:
                            log.info("watch signaling started room=%s client=%s", room_id, client_id)
                            try:
                                _set_state("request_pending", "join_request_stream")
                                await _send_offer(room_id, client_id)
                            except StreamOfflineError:
                                offer_outstanding = False
                                offer_started_at = 0.0
                                await _send_waiting_stream_offline(room_id, client_id)
                            except Exception:
                                log.exception("watch offer creation failed room=%s client=%s", room_id, client_id)
                                await ws.send_json({"ok": False, "type": "error", "room": room_id, "message": "watch_offer_failed", "ts": now_ms()})
                    await ws.send_json({"type": "stage_state", "payload": _stage_payload(room_id, state.ensure_room(room_id))})
                    continue

                if kind == "ping":
                    await ws.send_json({"type": "pong", "room": room_id, "ts": now_ms()})
                elif kind == "request_stream":
                    if now_loop < next_request_allowed_at:
                        log.debug("watch request_stream ignored room=%s client=%s reason=backoff until=%s", room_id, client_id, round(next_request_allowed_at - now_loop, 3))
                        continue
                    if offer_outstanding:
                        log.debug("watch request_stream ignored room=%s client=%s reason=offer_outstanding", room_id, client_id)
                        continue
                    if watcher_state == "peer_active":
                        log.debug("watch request_stream ignored room=%s client=%s reason=peer_active", room_id, client_id)
                        continue
                    log.info("watch request_stream room=%s client=%s", room_id, client_id)
                    _set_state("request_pending", "request_stream")
                    room = state.ensure_room(room_id)
                    if room.broadcaster_sid is None:
                        offer_outstanding = False
                        offer_started_at = 0.0
                        _set_state("waiting_for_broadcaster", "no_broadcaster")
                        next_request_allowed_at = now_loop + WATCH_RETRY_BACKOFF_S
                        await _send_waiting_no_broadcaster(room_id, client_id)
                        continue
                    if await _route_to_broadcaster(room_id, {"type": "viewer_joined", "room": room_id, "viewerId": client_id, "ts": now_ms()}):
                        log.info("watcher resumed room=%s client=%s via viewer_joined", room_id, client_id)
                        continue
                    if rtc is None:
                        _set_state("waiting_for_broadcaster", "rtc_unavailable")
                        await ws.send_json({"type": "error", "room": room_id, "message": "rtc_unavailable", "ts": now_ms()})
                    elif not _room_has_live_source(room_id):
                        offer_outstanding = False
                        offer_started_at = 0.0
                        _set_state("waiting_for_broadcaster", "stream_offline")
                        next_request_allowed_at = now_loop + WATCH_RETRY_BACKOFF_S
                        await _send_waiting_stream_offline(room_id, client_id)
                    else:
                        try:
                            await _send_offer(room_id, client_id)
                        except StreamOfflineError:
                            offer_outstanding = False
                            offer_started_at = 0.0
                            await _send_waiting_stream_offline(room_id, client_id)
                        except Exception:
                            log.exception("watch request_stream offer failed room=%s client=%s", room_id, client_id)
                            await ws.send_json({"ok": False, "type": "error", "room": room_id, "message": "watch_offer_failed", "ts": now_ms()})
                            offer_outstanding = False
                            offer_started_at = 0.0
                elif kind in {"watch_answer", "webrtc_answer"}:
                    sdp = data.get("sdp")
                    sdp_type = data.get("type") or "answer"
                    if sdp:
                        offer_outstanding = False
                        offer_started_at = 0.0
                        _set_state("peer_active", "answer_received")
                        log.info("watch answer received room=%s client=%s", room_id, client_id)
                        if rtc is not None:
                            await rtc.set_viewer_answer(room_id, client_id, sdp, sdp_type)
                elif kind == "answer":
                    sdp = data.get("sdp")
                    sdp_type = data.get("type") or "answer"
                    if sdp:
                        offer_outstanding = False
                        offer_started_at = 0.0
                        _set_state("peer_active", "answer_routed")
                        await _route_to_broadcaster(
                            room_id,
                            {"type": "answer", "room": room_id, "viewerId": client_id, "payload": {"sdp": sdp, "type": sdp_type}, "ts": now_ms()},
                        )
                elif kind in {"webrtc_ice", "watch_ice"}:
                    if rtc is not None:
                        cand = rtc.parse_ice(data or {})
                        log.debug("watch ice queued room=%s client=%s", room_id, client_id)
                        await rtc.add_viewer_ice_candidate(room_id, client_id, cand)
                elif kind == "ice-candidate":
                    cand = data.get("candidate")
                    if cand:
                        await _route_to_broadcaster(
                            room_id,
                            {"type": "ice-candidate", "room": room_id, "viewerId": client_id, "candidate": cand, "ts": now_ms()},
                        )
                elif kind == "chat":
                    text = str(data.get("text") or "").strip()
                    if text:
                        await _handle_chat_text(state, room_id, client_id, "viewer", text)
                elif kind == "web_search":
                    query = str(data.get("query") or "").strip()
                    room = state.ensure_room(room_id)
                    if query and room.settings.web_search_enabled:
                        res = await ai.handle_websearch({"query": query})
                        await registry.broadcast_room(room_id, {"type": "web_search_result", "room": room_id, "query": query, "result": res, "ts": now_ms()})
                elif kind in {"attachment", "attachment_uploaded"}:
                    attachment = data.get("attachment") or {}
                    await registry.broadcast_room(room_id, {"type": "attachment", "room": room_id, "user": "viewer", "clientId": client_id, "attachment": attachment, "ts": now_ms()})
        finally:
            if joined:
                _set_state("cleanup_pending", "disconnect")
                registry.unregister(room_id, "watch", ws)
                state.ensure_room(room_id).viewers.pop(client_id, None)
                await _route_to_broadcaster(room_id, {"type": "viewer_left", "room": room_id, "viewerId": client_id, "ts": now_ms()})
                if rtc is not None:
                    try:
                        await rtc.stop_viewer(room_id, client_id)
                        log.info("watch peer cleanup room=%s client=%s", room_id, client_id)
                    except Exception:
                        log.exception("watch cleanup failed")
                await _broadcast_presence(state, room_id)
            _set_state("disconnected", "ws_close")
            log.info("watch socket disconnected room=%s client=%s", room_id, client_id)

    @app.post('/api/broadcast/recording')
    async def api_broadcast_recording():
        files = await request.files
        file_storage = files.get('file')
        room_id = (request.args.get('room') or state.default_room or 'default').strip() or 'default'
        if not file_storage:
            return jsonify({'ok': False, 'error': 'file required'}), 400
        token = uuid4().hex[:10]
        src = RECORDINGS_DIR / f"{room_id}_{token}.webm"
        out = RECORDINGS_DIR / f"{room_id}_{token}.mp4"
        await file_storage.save(src)
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            return jsonify({'ok': False, 'error': 'ffmpeg unavailable'}), 500
        cmd = [ffmpeg, '-y', '-i', str(src), '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-c:a', 'aac', '-b:a', '128k', str(out)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning('recording transcode failed room=%s err=%s', room_id, (stderr or b'')[:400].decode(errors='ignore'))
            return jsonify({'ok': False, 'error': 'ffmpeg transcode failed'}), 500
        room = state.ensure_room(room_id)
        room.media.latest_upload_url = f"/uploads/recordings/{out.name}"
        room.media.latest_upload_mime = 'video/mp4'
        room.media.latest_upload_at = now_ms()
        room.media.mode = 'upload'
        await registry.broadcast_room(room_id, {'type': 'stage_state', 'payload': _stage_payload(room_id, room), 'ts': now_ms()})
        return jsonify({'ok': True, 'url': room.media.latest_upload_url})

    @app.post('/api/broadcast/rtmp')
    async def api_broadcast_rtmp():
        body = await request.get_json(force=True, silent=True) or {}
        room_id = str(body.get('room') or state.default_room or 'default')
        enabled = bool(body.get('enabled', False))
        stream_key = str(body.get('stream_key') or '').strip()
        base_url = str(body.get('rtmp_url') or 'rtmp://a.rtmp.youtube.com/live2').strip()
        room = state.ensure_room(room_id)
        room.settings.rtmp_enabled = enabled
        room.settings.rtmp_url = f"{base_url}/{stream_key}" if (enabled and stream_key) else ''
        await registry.broadcast_room(room_id, {'type': 'state_update', 'room': room_id, 'state': state.room_state_payload(room_id), 'ts': now_ms()})
        return jsonify({'ok': True, 'enabled': room.settings.rtmp_enabled, 'rtmp_url': room.settings.rtmp_url})

    @app.post('/api/upload')
    async def api_upload():
        files = await request.files
        file_storage = files.get("file")
        if not file_storage:
            return jsonify({"error": "file required"}), 400
        return jsonify(await save_upload(file_storage))

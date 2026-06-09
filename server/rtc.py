from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
try:
    from aiortc.rtcconfiguration import RTCConfiguration, RTCBundlePolicy
except Exception:  # pragma: no cover - aiortc stubs in test env may not provide submodule
    RTCConfiguration = None
    RTCBundlePolicy = None
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp

from server.state import AppState
from server.utils import now_ms


log = logging.getLogger("server.rtc")

GLOBAL_MEDIA_RELAY = MediaRelay()


class StreamOfflineError(RuntimeError):
    """Raised when watcher negotiation is requested without live source tracks."""


@dataclass
class BroadcasterSession:
    sid: str
    pc: RTCPeerConnection
    tracks: Dict[str, Any]


class RTCManager:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.relay = GLOBAL_MEDIA_RELAY
        self.broadcasters: Dict[str, BroadcasterSession] = {}
        self.live_video_source: Dict[str, Any] = {}
        self.live_audio_source: Dict[str, Any] = {}
        self.broadcast_live_event: Dict[str, asyncio.Event] = {}
        self.viewers: Dict[str, Dict[str, RTCPeerConnection]] = {}
        self._pending_cleanup: Dict[str, asyncio.Task] = {}
        self.disconnect_grace_seconds = 60
        self._pending_broadcaster_ice: Dict[tuple[str, str], list[RTCIceCandidate]] = {}
        self._viewer_offer_cache: Dict[tuple[str, str], Dict[str, str]] = {}
        self._pending_viewer_ice: Dict[tuple[str, str], list[RTCIceCandidate]] = {}
        self._pending_broadcaster_ice_seen: Dict[tuple[str, str], set[str]] = {}
        self._pending_viewer_ice_seen: Dict[tuple[str, str], set[str]] = {}
        self._ice_queue_started: set[tuple[str, str, str]] = set()
        self.max_pending_ice = 64
        self.viewer_offer_ttl_ms = 8000

    async def _emit_room(self, room_id: str, event: str, payload: Dict[str, Any]) -> None:
        emitter = getattr(self.state, "ws_emit_room", None)
        if callable(emitter):
            await emitter(room_id, {"type": event, "payload": payload})

    async def _emit_status(self, room_id: str) -> None:
        room = self.state.ensure_room(room_id)
        await self._emit_room(
            room_id,
            "room_status",
            {
                "room": room_id,
                "viewer_count": len(room.viewers),
                "broadcaster_present": room.broadcaster_sid is not None,
                "ai": {"enabled": False, "power": False, "mode": "disabled", "fallback": False},
                "stt_enabled": room.settings.stt_enabled,
                "tts_enabled": room.settings.tts_enabled,
                "ts": now_ms(),
            },
        )

    def _room_live_event(self, room_id: str) -> asyncio.Event:
        return self.broadcast_live_event.setdefault(room_id, asyncio.Event())

    def has_live_source(self, room_id: str) -> bool:
        if self.live_video_source.get(room_id) or self.live_audio_source.get(room_id):
            return True
        session = self.broadcasters.get(room_id)
        if not session:
            return False
        return bool(session.tracks.get("video") or session.tracks.get("audio"))

    def _new_peer_connection(self) -> RTCPeerConnection:
        if RTCConfiguration is None or RTCBundlePolicy is None:
            return RTCPeerConnection()
        cfg = RTCConfiguration(bundlePolicy=RTCBundlePolicy.MAX_BUNDLE)
        try:
            return RTCPeerConnection(configuration=cfg)
        except TypeError:
            return RTCPeerConnection()

    async def _wait_ice_complete(self, pc: RTCPeerConnection, timeout_s: float = 5.0) -> None:
        if pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @pc.on("icegatheringstatechange")
        def _on_state() -> None:
            if pc.iceGatheringState == "complete":
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass

    async def _schedule_cleanup(self, room_id: str, sid: str, role: str) -> None:
        key = f"{room_id}:{sid}:{role}"
        prev = self._pending_cleanup.get(key)
        if prev:
            prev.cancel()

        async def _runner() -> None:
            await asyncio.sleep(self.disconnect_grace_seconds)
            if role == "broadcaster":
                b = self.broadcasters.get(room_id)
                if b and b.sid == sid and b.pc.connectionState == "disconnected":
                    await self.stop_broadcaster(room_id, sid)
            else:
                vpc = self.viewers.get(room_id, {}).get(sid)
                if vpc and vpc.connectionState == "disconnected":
                    await self.stop_viewer(room_id, sid)

        self._pending_cleanup[key] = asyncio.create_task(_runner())




    def _candidate_key(self, candidate: Any) -> str:
        if candidate is None:
            return "<none>"
        foundation = getattr(candidate, "foundation", None)
        component = getattr(candidate, "component", None)
        protocol = getattr(candidate, "protocol", None)
        ip = getattr(candidate, "ip", None)
        port = getattr(candidate, "port", None)
        sdp_mid = getattr(candidate, "sdpMid", None)
        sdp_mline = getattr(candidate, "sdpMLineIndex", None)
        if any(v is not None for v in (foundation, component, protocol, ip, port, sdp_mid, sdp_mline)):
            return f"{foundation}|{component}|{protocol}|{ip}|{port}|{sdp_mid}|{sdp_mline}"
        return repr(candidate)

    def _queue_ice_candidate(
        self,
        queue: Dict[tuple[str, str], list[RTCIceCandidate]],
        seen_map: Dict[tuple[str, str], set[str]],
        role: str,
        reason: str,
        room_id: str,
        sid: str,
        candidate: RTCIceCandidate,
    ) -> None:
        key = (room_id, sid)
        seen = seen_map.setdefault(key, set())
        cand_key = self._candidate_key(candidate)
        if cand_key in seen:
            return
        seen.add(cand_key)
        q = queue.setdefault(key, [])
        q.append(candidate)
        dropped = 0
        if len(q) > self.max_pending_ice:
            dropped = len(q) - self.max_pending_ice
            removed = q[:-self.max_pending_ice]
            del q[:-self.max_pending_ice]
            removed_keys = {self._candidate_key(item) for item in removed}
            seen.difference_update(removed_keys)
        started_key = (role, room_id, sid)
        if started_key not in self._ice_queue_started:
            self._ice_queue_started.add(started_key)
            log.info("%s ICE queue started room=%s sid=%s reason=%s", role, room_id, sid, reason)
        elif dropped == 0:
            log.debug("%s ICE queued room=%s sid=%s count=%s reason=%s", role, room_id, sid, len(q), reason)
        if dropped > 0:
            log.warning("%s ICE queue capped room=%s sid=%s dropped=%s", role, room_id, sid, dropped)

    def _broadcaster_ice_key(self, room_id: str, sid: str) -> tuple[str, str]:
        return room_id, sid

    def _viewer_ice_key(self, room_id: str, sid: str) -> tuple[str, str]:
        return room_id, sid

    async def _flush_broadcaster_ice(self, room_id: str, sid: str, pc: RTCPeerConnection) -> None:
        key = self._broadcaster_ice_key(room_id, sid)
        queued = self._pending_broadcaster_ice.pop(key, [])
        self._pending_broadcaster_ice_seen.pop(key, None)
        self._ice_queue_started.discard(("broadcaster", room_id, sid))
        if queued:
            log.info("broadcaster ICE queue flushed room=%s sid=%s count=%s", room_id, sid, len(queued))
        for cand in queued:
            try:
                await pc.addIceCandidate(cand)
            except Exception:
                log.exception("failed queued broadcaster ICE apply")

    async def _flush_viewer_ice(self, room_id: str, sid: str, pc: RTCPeerConnection) -> None:
        key = self._viewer_ice_key(room_id, sid)
        queued = self._pending_viewer_ice.pop(key, [])
        self._pending_viewer_ice_seen.pop(key, None)
        self._ice_queue_started.discard(("viewer", room_id, sid))
        if queued:
            log.info("viewer ICE queue flushed room=%s sid=%s count=%s", room_id, sid, len(queued))
        for cand in queued:
            try:
                await pc.addIceCandidate(cand)
            except Exception:
                log.exception("failed queued viewer ICE apply")

    async def enable_stt_for_broadcaster(self, room_id: str, sid: str) -> tuple[bool, str]:
        room = self.state.ensure_room(room_id)
        if room.broadcaster_sid != sid:
            return False, "not_broadcaster"
        room.settings.stt_enabled = True
        await self._emit_room(room_id, "stt_status", {"enabled": True, "reason": "enabled", "ts": now_ms()})
        await self._emit_status(room_id)
        return True, "enabled"

    async def disable_stt_for_broadcaster(self, room_id: str, sid: str) -> tuple[bool, str]:
        room = self.state.ensure_room(room_id)
        if room.broadcaster_sid != sid:
            return False, "not_broadcaster"
        room.settings.stt_enabled = False
        await self._emit_room(room_id, "stt_status", {"enabled": False, "reason": "disabled", "ts": now_ms()})
        await self._emit_status(room_id)
        return True, "disabled"

    async def start_broadcaster_from_offer(self, room_id: str, sid: str, sdp: str, sdp_type: str) -> Dict[str, str]:
        existing = self.broadcasters.get(room_id)
        if existing and existing.sid != sid:
            await self.stop_broadcaster(room_id, existing.sid)
            existing = None
        stale = [k for k in self._pending_broadcaster_ice.keys() if k[0] == room_id and k[1] != sid]
        for k in stale:
            self._pending_broadcaster_ice.pop(k, None)
            self._pending_broadcaster_ice_seen.pop(k, None)
            self._ice_queue_started.discard(("broadcaster", k[0], k[1]))

        if existing:
            pc = existing.pc
            session = existing
        else:
            pc = self._new_peer_connection()
            session = BroadcasterSession(sid=sid, pc=pc, tracks={})
            self.broadcasters[room_id] = session

            @pc.on("connectionstatechange")
            async def _on_connectionstatechange() -> None:
                st = pc.connectionState
                log.info("broadcaster state room=%s sid=%s state=%s", room_id, sid, st)
                await self._emit_room(room_id, "webrtc_state", {"room": room_id, "role": "broadcaster", "state": st, "ts": now_ms()})
                if st == "disconnected":
                    await self._schedule_cleanup(room_id, sid, "broadcaster")
                if st in {"failed", "closed"}:
                    await self.stop_broadcaster(room_id, sid)

            @pc.on("track")
            async def _on_track(track: Any) -> None:
                session.tracks[track.kind] = track
                if track.kind == "video":
                    self.live_video_source[room_id] = track
                elif track.kind == "audio":
                    self.live_audio_source[room_id] = track
                if self.has_live_source(room_id):
                    self._room_live_event(room_id).set()
                    room = self.state.ensure_room(room_id)
                    room.media.live_active = True
                    room.media.mode = "live"
                log.info("broadcaster track published room=%s sid=%s kind=%s live_video=%s live_audio=%s", room_id, sid, track.kind, bool(self.live_video_source.get(room_id)), bool(self.live_audio_source.get(room_id)))
                await self._emit_room(room_id, "stream_started", {"room": room_id, "kind": track.kind, "ts": now_ms()})
                await self._emit_room(room_id, "broadcaster-start", {"room": room_id, "kind": track.kind, "ts": now_ms()})
                await self._emit_status(room_id)

        room = self.state.ensure_room(room_id)
        room.broadcaster_sid = sid

        if pc.signalingState != "stable":
            log.info("broadcaster offer while signaling=%s; resetting peer for room=%s", pc.signalingState, room_id)
            await self.stop_broadcaster(room_id, sid)
            return await self.start_broadcaster_from_offer(room_id, sid, sdp, sdp_type)

        log.info("apply viewer remote answer room=%s sid=%s state=%s", room_id, sid, pc.signalingState)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        await self._flush_broadcaster_ice(room_id, sid, pc)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_complete(pc)
        await self._emit_status(room_id)
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    def _viewer_offer_cache_valid(self, room_id: str, sid: str, pc: Any, cached: Dict[str, str]) -> bool:
        if not cached or not cached.get("sdp"):
            return False
        created_at = int(cached.get("created_at") or 0)
        age = now_ms() - created_at if created_at else self.viewer_offer_ttl_ms + 1
        if age > self.viewer_offer_ttl_ms:
            log.info("drop stale pending viewer offer room=%s sid=%s age_ms=%s", room_id, sid, age)
            return False
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            return False
        if pc.signalingState != "have-local-offer" or pc.remoteDescription is not None:
            return False
        return True

    async def start_viewer_offer(self, room_id: str, sid: str) -> Dict[str, str]:
        prev = self.viewers.get(room_id, {}).get(sid)
        cached = self._viewer_offer_cache.get((room_id, sid))
        if prev and self._viewer_offer_cache_valid(room_id, sid, prev, cached or {}):
            log.info("reuse pending viewer offer room=%s sid=%s", room_id, sid)
            return {"sdp": cached["sdp"], "type": cached.get("type") or "offer"}
        if cached:
            self._viewer_offer_cache.pop((room_id, sid), None)
        if prev and prev.localDescription is not None and prev.remoteDescription is None and prev.signalingState == "have-local-offer":
            try:
                age = now_ms() - int((cached or {}).get("created_at") or 0)
                if 0 <= age <= self.viewer_offer_ttl_ms and prev.connectionState not in {"failed", "closed", "disconnected"}:
                    return {"sdp": prev.localDescription.sdp, "type": prev.localDescription.type}
            except Exception:
                pass
        if prev:
            try:
                await prev.close()
            except Exception:
                log.exception("error closing previous viewer pc")
        pc = self._new_peer_connection()
        self.viewers.setdefault(room_id, {})[sid] = pc
        room = self.state.ensure_room(room_id)
        room.viewers[sid] = pc

        @pc.on("connectionstatechange")
        async def _on_connectionstatechange() -> None:
            st = pc.connectionState
            log.info("viewer state room=%s sid=%s state=%s", room_id, sid, st)
            await self._emit_room(room_id, "webrtc_state", {"room": room_id, "role": "watch", "state": st, "ts": now_ms()})
            if st == "disconnected":
                self._viewer_offer_cache.pop((room_id, sid), None)
                await self._schedule_cleanup(room_id, sid, "viewer")
            if st in {"failed", "closed"}:
                self._viewer_offer_cache.pop((room_id, sid), None)
                await self.stop_viewer(room_id, sid)

        if not self.has_live_source(room_id):
            log.info("viewer offer rejected no live source room=%s sid=%s", room_id, sid)
            try:
                await pc.close()
            except Exception:
                pass
            self.viewers.get(room_id, {}).pop(sid, None)
            room.viewers.pop(sid, None)
            raise StreamOfflineError("stream_offline")

        session = self.broadcasters.get(room_id)
        source_video = self.live_video_source.get(room_id) or (session.tracks.get("video") if session else None)
        source_audio = self.live_audio_source.get(room_id) or (session.tracks.get("audio") if session else None)
        if source_video:
            try:
                pc.addTrack(self.relay.subscribe(source_video))
            except Exception:
                log.exception("failed to add relayed video track")
        if source_audio:
            try:
                pc.addTrack(self.relay.subscribe(source_audio))
            except Exception:
                log.exception("failed to add relayed audio track")
        log.info("viewer subscribed room=%s sid=%s has_video=%s has_audio=%s watchers=%s", room_id, sid, bool(source_video), bool(source_audio), len(self.viewers.get(room_id, {})))

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._wait_ice_complete(pc)
        out = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        self._viewer_offer_cache[(room_id, sid)] = {**out, "created_at": now_ms()}
        await self._emit_status(room_id)
        return out

    async def set_viewer_answer(self, room_id: str, sid: str, sdp: str, sdp_type: str) -> None:
        pc = self.viewers.get(room_id, {}).get(sid)
        if not pc:
            raise RuntimeError("Viewer peer not found")
        log.info("apply viewer remote answer room=%s sid=%s state=%s", room_id, sid, pc.signalingState)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        self._viewer_offer_cache.pop((room_id, sid), None)
        await self._flush_viewer_ice(room_id, sid, pc)

    def parse_ice(self, payload: Dict[str, Any]) -> Optional[RTCIceCandidate]:
        payload = payload or {}
        cand = payload.get("candidate")
        if isinstance(cand, dict):
            nested = cand
            cand = nested.get("candidate") or ""
            payload = {**payload, **nested}
        if not cand:
            return None
        if isinstance(cand, str) and cand.startswith("candidate:"):
            cand = cand[len("candidate:"):]
        c = candidate_from_sdp(str(cand))
        c.sdpMid = payload.get("sdpMid")
        c.sdpMLineIndex = payload.get("sdpMLineIndex")
        return c

    async def add_broadcaster_ice_candidate(self, room_id: str, sid: str, candidate: Optional[RTCIceCandidate]) -> None:
        b = self.broadcasters.get(room_id)
        key = self._broadcaster_ice_key(room_id, sid)
        if not b or b.sid != sid:
            if candidate is not None:
                self._queue_ice_candidate(
                    self._pending_broadcaster_ice,
                    self._pending_broadcaster_ice_seen,
                    "broadcaster",
                    "session_pending",
                    room_id,
                    sid,
                    candidate,
                )
            return
        if candidate is None:
            await b.pc.addIceCandidate(None)
            log.debug("broadcaster ICE end-of-candidates room=%s sid=%s", room_id, sid)
            return
        if b.pc.remoteDescription is None:
            self._queue_ice_candidate(
                self._pending_broadcaster_ice,
                self._pending_broadcaster_ice_seen,
                "broadcaster",
                "remote_description_pending",
                room_id,
                sid,
                candidate,
            )
            return
        await b.pc.addIceCandidate(candidate)
        log.debug("broadcaster ICE applied room=%s sid=%s", room_id, sid)

    async def add_viewer_ice_candidate(self, room_id: str, sid: str, candidate: Optional[RTCIceCandidate]) -> None:
        pc = self.viewers.get(room_id, {}).get(sid)
        key = self._viewer_ice_key(room_id, sid)
        if not pc:
            if candidate is not None:
                self._queue_ice_candidate(
                    self._pending_viewer_ice,
                    self._pending_viewer_ice_seen,
                    "viewer",
                    "session_pending",
                    room_id,
                    sid,
                    candidate,
                )
            return
        if candidate is None:
            await pc.addIceCandidate(None)
            log.debug("viewer ICE end-of-candidates room=%s sid=%s", room_id, sid)
            return
        if pc.remoteDescription is None:
            self._queue_ice_candidate(
                self._pending_viewer_ice,
                self._pending_viewer_ice_seen,
                "viewer",
                "remote_description_pending",
                room_id,
                sid,
                candidate,
            )
            return
        await pc.addIceCandidate(candidate)
        log.debug("viewer ICE applied room=%s sid=%s", room_id, sid)

    async def stop_viewer(self, room_id: str, sid: str) -> None:
        pc = self.viewers.get(room_id, {}).pop(sid, None)
        room = self.state.ensure_room(room_id)
        room.viewers.pop(sid, None)
        if pc:
            try:
                await pc.close()
            except Exception:
                log.exception("error closing viewer pc")
        vkey = self._viewer_ice_key(room_id, sid)
        self._pending_viewer_ice.pop(vkey, None)
        self._viewer_offer_cache.pop((room_id, sid), None)
        self._pending_viewer_ice_seen.pop(vkey, None)
        self._ice_queue_started.discard(("viewer", room_id, sid))
        self.state.remove_room_if_empty(room_id)
        await self._emit_status(room_id)

    async def stop_broadcaster(self, room_id: str, sid: str) -> None:
        b = self.broadcasters.get(room_id)
        if not b or b.sid != sid:
            return
        try:
            await b.pc.close()
        except Exception:
            log.exception("error closing broadcaster pc")
        self.broadcasters.pop(room_id, None)
        # clear all pending viewer offers for the room on broadcaster stop
        for k in [k for k in self._viewer_offer_cache.keys() if k[0] == room_id]:
            self._viewer_offer_cache.pop(k, None)
        bkey = self._broadcaster_ice_key(room_id, sid)
        self._pending_broadcaster_ice.pop(bkey, None)
        self._pending_broadcaster_ice_seen.pop(bkey, None)
        self._ice_queue_started.discard(("broadcaster", room_id, sid))

        self.live_video_source.pop(room_id, None)
        self.live_audio_source.pop(room_id, None)
        self._room_live_event(room_id).clear()

        room = self.state.ensure_room(room_id)
        room.broadcaster_sid = None
        room.media.live_active = False
        room.media.mode = "upload" if room.media.latest_upload_url else "none"

        viewers = list(self.viewers.get(room_id, {}).keys())
        for vsid in viewers:
            await self.stop_viewer(room_id, vsid)

        await self._emit_room(room_id, "stream_stopped", {"room": room_id, "ts": now_ms()})
        await self._emit_room(room_id, "broadcaster-stop", {"room": room_id, "ts": now_ms()})
        await self._emit_status(room_id)

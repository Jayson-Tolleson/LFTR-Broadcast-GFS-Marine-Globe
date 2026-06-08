from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class RoomSettings:
    ai_enabled: bool = False
    ai_status: str = "idle"
    ai_mode: str = "active"
    tts_enabled: bool = False
    hear_ai_voice: bool = False
    stt_enabled: bool = True
    mic_enabled: bool = True
    camera_enabled: bool = True
    screen_enabled: bool = False
    noise_cancel_enabled: bool = True
    web_search_enabled: bool = True
    attachments_enabled: bool = True
    record_enabled: bool = False
    rtmp_enabled: bool = False
    rtmp_url: str = ""


@dataclass
class MediaState:
    mode: str = "none"  # live | upload | none
    live_active: bool = False
    latest_upload_url: Optional[str] = None
    latest_upload_mime: Optional[str] = None
    latest_upload_at: Optional[int] = None
    location_id: Optional[str] = None
    label: str = "PUBLIC ACCESS"


@dataclass
class RoomRuntime:
    chat_connected: bool = False
    broadcast_connected: bool = False
    watch_connected: bool = False
    broadcaster_present: bool = False
    viewer_count: int = 0


@dataclass
class RoomState:
    broadcaster_sid: Optional[str] = None
    viewers: Dict[str, Any] = field(default_factory=dict)
    settings: RoomSettings = field(default_factory=RoomSettings)
    media: MediaState = field(default_factory=MediaState)
    runtime: RoomRuntime = field(default_factory=RoomRuntime)
    latest_upload: Optional[Dict[str, Any]] = None



def get_default_room_state() -> RoomState:
    return RoomState()


class AppState:
    def __init__(self, default_room: str = "default") -> None:
        self.default_room = default_room
        self.rooms: Dict[str, RoomState] = {}
        self.sid_meta: Dict[str, Tuple[str, str]] = {}

    def ensure_room(self, room_id: str) -> RoomState:
        room = self.rooms.get(room_id)
        if room is None:
            room = get_default_room_state()
            self.rooms[room_id] = room
        return room

    def get_room(self, room_id: str) -> Optional[RoomState]:
        return self.rooms.get(room_id)

    def remove_room_if_empty(self, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room:
            return
        if room.broadcaster_sid is None and not room.viewers:
            self.rooms.pop(room_id, None)

    def set_sid_meta(self, sid: str, room_id: str, role: str) -> None:
        self.sid_meta[sid] = (room_id, role)

    def get_sid_meta(self, sid: str) -> Tuple[str, str]:
        return self.sid_meta.get(sid, (self.default_room, "unknown"))

    def pop_sid_meta(self, sid: str) -> Tuple[str, str]:
        return self.sid_meta.pop(sid, (self.default_room, "unknown"))

    def room_state_payload(self, room_id: str) -> dict[str, Any]:
        room = self.ensure_room(room_id)
        room.runtime.viewer_count = len(room.viewers)
        room.runtime.broadcaster_present = room.broadcaster_sid is not None
        return {
            "room": room_id,
            "settings": asdict(room.settings),
            "runtime": asdict(room.runtime),
            "media": asdict(room.media),
        }

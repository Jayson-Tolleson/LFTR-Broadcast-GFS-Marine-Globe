from __future__ import annotations

import time
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs



def now_ms() -> int:
    return int(time.time() * 1000)



def sanitize_room(room: str, default_room: str = "default", max_len: int = 48) -> str:
    raw = (room or "").strip()
    clean = "".join([c for c in raw if c.isalnum() or c in ("-", "_")])
    clean = clean[:max_len]
    return clean or default_room



def parse_connect_query(environ: Dict[str, Any], default_room: str, max_room_len: int) -> Tuple[str, str]:
    query = environ.get("QUERY_STRING", "") or ""
    parsed = parse_qs(query)
    room = sanitize_room((parsed.get("room", [default_room])[0] or default_room), default_room, max_room_len)
    role = (parsed.get("role", ["unknown"])[0] or "unknown").strip()
    if role not in {"broadcast", "watch"}:
        role = "unknown"
    return room, role

from __future__ import annotations

from pathlib import Path
from uuid import uuid4


UPLOAD_ROOT = Path(__file__).resolve().parent.parent.parent / "uploads"
IMAGE_DIR = UPLOAD_ROOT / "images"
VIDEO_DIR = UPLOAD_ROOT / "video"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)


async def save_upload(file_storage) -> dict:
    filename = (file_storage.filename or "upload.bin").strip()
    suffix = Path(filename).suffix or ".bin"
    mimetype = (file_storage.mimetype or "").lower()

    is_image = mimetype.startswith("image/")
    is_video = mimetype.startswith("video/")
    target_dir = IMAGE_DIR if is_image else VIDEO_DIR if is_video else VIDEO_DIR

    target_name = f"{uuid4().hex[:12]}{suffix}"
    target_path = target_dir / target_name
    await file_storage.save(target_path)

    relative_dir = "images" if target_dir == IMAGE_DIR else "video"
    return {"url": f"/uploads/{relative_dir}/{target_name}"}

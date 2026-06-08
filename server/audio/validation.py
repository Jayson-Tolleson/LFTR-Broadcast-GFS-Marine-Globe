from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

PCM16_ENCODING = "linear16"
PCM16_SAMPLE_RATE = 16000
PCM16_CHANNELS = 1
MAX_PCM16_CHUNK_BYTES = 256_000


class AudioValidationError(ValueError):
    """Raised when incoming STT audio metadata/payload is invalid."""


@dataclass(frozen=True)
class AudioChunk:
    audio_bytes: bytes
    encoding: str
    sample_rate_hz: int
    channels: int


def _parse_int(value: object, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AudioValidationError(f"invalid_{field_name}") from exc
    return parsed


def decode_audio_chunk_payload(payload: dict) -> AudioChunk:
    encoding = str(payload.get("encoding") or "").strip().lower()
    if encoding != PCM16_ENCODING:
        raise AudioValidationError("unsupported_encoding")

    sample_rate_hz = _parse_int(payload.get("sampleRate", payload.get("sample_rate_hz", payload.get("sample_rate"))), "sample_rate")
    if sample_rate_hz != PCM16_SAMPLE_RATE:
        raise AudioValidationError("unsupported_sample_rate")

    channels = _parse_int(payload.get("channels", payload.get("channel_count")), "channels")
    if channels != PCM16_CHANNELS:
        raise AudioValidationError("unsupported_channels")

    b64_data = str(payload.get("data") or "")
    if not b64_data:
        raise AudioValidationError("missing_audio_data")

    try:
        audio_bytes = base64.b64decode(b64_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AudioValidationError("invalid_base64_audio") from exc

    if not audio_bytes:
        raise AudioValidationError("empty_audio_chunk")
    if len(audio_bytes) > MAX_PCM16_CHUNK_BYTES:
        raise AudioValidationError("audio_chunk_too_large")
    if len(audio_bytes) % 2 != 0:
        raise AudioValidationError("misaligned_linear16_chunk")

    return AudioChunk(
        audio_bytes=audio_bytes,
        encoding=encoding,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
    )

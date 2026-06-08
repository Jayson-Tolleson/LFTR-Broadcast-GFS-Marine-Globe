from __future__ import annotations

import base64
import logging
from pathlib import Path
from uuid import uuid4

try:
    from google.api_core.exceptions import DeadlineExceeded, InternalServerError, ServiceUnavailable
    from google.api_core.exceptions import InvalidArgument
except Exception:  # pragma: no cover - optional dependency wiring
    class _TransientExc(Exception):
        pass

    DeadlineExceeded = InternalServerError = ServiceUnavailable = _TransientExc
    InvalidArgument = ValueError

from server.audio.validation import PCM16_CHANNELS, PCM16_ENCODING, PCM16_SAMPLE_RATE

from .auth import maybe_apply_google_credentials_env, resolve_gcp_auth_mode


log = logging.getLogger("server.ai.speech")
AUDIO_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

TRANSIENT_STT_ERRORS = (ServiceUnavailable, DeadlineExceeded, InternalServerError)


def _credentials_ready() -> bool:
    maybe_apply_google_credentials_env()
    return resolve_gcp_auth_mode() in {"adc_ok", "explicit_key_ok"}


def synthesize_voice(text: str) -> str:
    token = uuid4().hex[:10]
    out = AUDIO_DIR / f"ai_{token}.mp3"

    if not text:
        out.write_bytes(b"")
        return f"/uploads/audio/{out.name}"

    if not _credentials_ready():
        out.write_bytes(b"")
        return f"/uploads/audio/{out.name}"

    try:
        from google.cloud import texttospeech

        client = texttospeech.TextToSpeechClient()
        input_text = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)

        response = client.synthesize_speech(input=input_text, voice=voice, audio_config=audio_config)
        out.write_bytes(response.audio_content or b"")
    except Exception:
        log.exception("google tts failed")
        out.write_bytes(b"")

    return f"/uploads/audio/{out.name}"


def _validate_pcm_contract(audio_bytes: bytes, sample_rate_hz: int, channels: int, encoding: str) -> None:
    if encoding.strip().lower() != PCM16_ENCODING:
        raise ValueError("unsupported_encoding")
    if sample_rate_hz != PCM16_SAMPLE_RATE:
        raise ValueError("invalid_sample_rate")
    if channels != PCM16_CHANNELS:
        raise ValueError("invalid_channels")
    if not audio_bytes:
        raise ValueError("empty_audio")
    if len(audio_bytes) % 2 != 0:
        raise ValueError("invalid_linear16_frame_alignment")


def transcribe_pcm16_chunk(
    audio_bytes: bytes,
    *,
    sample_rate_hz: int = PCM16_SAMPLE_RATE,
    channels: int = PCM16_CHANNELS,
    language_code: str = "en-US",
) -> str:
    _validate_pcm_contract(audio_bytes, sample_rate_hz, channels, PCM16_ENCODING)

    if not _credentials_ready():
        return ""

    from google.cloud import speech

    client = speech.SpeechClient()
    recognition_audio = speech.RecognitionAudio(content=audio_bytes)
    recognition_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=PCM16_SAMPLE_RATE,
        audio_channel_count=PCM16_CHANNELS,
        language_code=language_code or "en-US",
        enable_automatic_punctuation=True,
    )

    try:
        response = client.recognize(config=recognition_config, audio=recognition_audio)
    except TRANSIENT_STT_ERRORS as exc:
        log.warning("google stt transient error; retrying once err=%s", exc.__class__.__name__)
        response = client.recognize(config=recognition_config, audio=recognition_audio)
    except InvalidArgument as exc:
        log.warning("google stt invalid argument sample_rate=%s channels=%s encoding=%s err=%s", sample_rate_hz, channels, PCM16_ENCODING, str(exc))
        raise ValueError("invalid_stt_request") from exc

    parts: list[str] = []
    for result in (response.results or []):
        if result.alternatives:
            transcript = (result.alternatives[0].transcript or "").strip()
            if transcript:
                parts.append(transcript)
    return " ".join(parts).strip()


def transcribe_audio_chunk(
    audio_b64: str,
    *,
    sample_rate_hz: int,
    channels: int,
    encoding: str = PCM16_ENCODING,
    language_code: str = "en-US",
) -> str:
    if not audio_b64:
        return ""

    audio_bytes = base64.b64decode(audio_b64)
    return transcribe_pcm16_chunk(
        audio_bytes,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        language_code=language_code,
    )

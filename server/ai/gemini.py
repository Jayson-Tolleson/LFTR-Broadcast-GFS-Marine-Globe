from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Generator

from .auth import get_effective_google_project, maybe_apply_google_credentials_env, resolve_gcp_auth_mode


log = logging.getLogger("server.ai.gemini")
_BROADCAST_STYLE_PROMPT = (
    "You are a live broadcast host assistant. "
    "Reply in 1-2 concise sentences, confident and conversational. "
    "Avoid rambling, avoid repetition, and skip disclaimers unless safety-critical. "
    "If user asks for detail, give at most 4 short bullet points."
)


@dataclass
class StubProvider:
    def generate_content(self, prompt: str) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            return "I didn't catch that."
        return f"AI: Based on current feed, {prompt}"

    def stream_content(self, prompt: str) -> Generator[str, None, None]:
        yield self.generate_content(prompt)


class VertexGeminiProvider:
    def __init__(self) -> None:
        maybe_apply_google_credentials_env()
        self.project = get_effective_google_project()
        self.location = os.getenv("VERTEX_LOCATION", "global").strip() or "global"
        self.model_name = os.getenv("VERTEX_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

        # Use google-genai client for Vertex to avoid deprecated vertexai.generative_models.
        from google import genai

        self.client = genai.Client(vertexai=True, project=self.project, location=self.location)

    def generate_content(self, prompt: str) -> str:
        response = self.client.models.generate_content(model=self.model_name, contents=f"{_BROADCAST_STYLE_PROMPT}\nUser: {prompt}")
        return (getattr(response, "text", "") or "").strip()

    def stream_content(self, prompt: str) -> Generator[str, None, None]:
        for chunk in self.client.models.generate_content_stream(model=self.model_name, contents=f"{_BROADCAST_STYLE_PROMPT}\nUser: {prompt}"):
            text = (getattr(chunk, "text", "") or "")
            if text:
                yield text


_PROVIDER = None
_PROVIDER_KIND = "stub"


def _vertex_requested() -> bool:
    ai_provider = os.getenv("AI_PROVIDER", "").strip().lower()
    if ai_provider == "vertex":
        return True
    return bool(get_effective_google_project() and resolve_gcp_auth_mode() in {"adc_ok", "explicit_key_ok"})


def _get_provider():
    global _PROVIDER, _PROVIDER_KIND
    if _PROVIDER is not None:
        return _PROVIDER

    if _vertex_requested():
        try:
            _PROVIDER = VertexGeminiProvider()
            _PROVIDER_KIND = "vertex"
            log.info("[AI] Vertex Gemini enabled model=%s location=%s", _PROVIDER.model_name, _PROVIDER.location)
            return _PROVIDER
        except Exception:
            log.warning("[AI] Vertex credentials/client unavailable auth_mode=%s — AI disabled", resolve_gcp_auth_mode())

    _PROVIDER = StubProvider()
    _PROVIDER_KIND = "stub"
    return _PROVIDER


def provider_name() -> str:
    _get_provider()
    return _PROVIDER_KIND


def generate_ai_reply(text: str) -> dict:
    provider = _get_provider()
    prompt = (text or "").strip()
    reply = provider.generate_content(prompt)
    if not reply:
        reply = "I didn't catch that."

    from server.ai.speech import synthesize_voice

    return {"text": reply, "voice": synthesize_voice(reply), "provider": _PROVIDER_KIND}


async def stream_ai_reply(text: str):
    provider = _get_provider()
    prompt = (text or "").strip()
    if not prompt:
        yield "I didn't catch that."
        return

    def _collect_tokens() -> list[str]:
        return list(provider.stream_content(prompt))

    tokens = await asyncio.to_thread(_collect_tokens)
    if not tokens:
        fallback = provider.generate_content(prompt)
        if fallback:
            yield fallback
        return

    for token in tokens:
        yield token

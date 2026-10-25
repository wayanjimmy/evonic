"""Deterministic provider reserved for automated tests and local development."""

from __future__ import annotations

import hashlib
import io
import wave
from typing import Any, Mapping

from .base import (
    AudioArtifact,
    ProviderCapabilities,
    ProviderConfigField,
    SpeechProvider,
    SpeechRequest,
    SpeechResult,
)


def _silent_wav(milliseconds: int = 200, rate: int = 8_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * (rate * milliseconds // 1000))
    return buffer.getvalue()


class DeterministicMockSpeechProvider(SpeechProvider):
    """Offline provider that never calls a network and always returns valid WAV."""

    id = "mock"
    display_name = "Deterministic Mock"
    is_local = True
    capabilities = ProviderCapabilities(
        supported_voices=("mock-voice",),
        supported_models=("deterministic-mock-v1",),
        supported_output_formats=("wav",),
        supports_speed=True,
        supports_instructions=True,
    )
    config_fields = (
        ProviderConfigField("mock_enabled", "Enable deterministic mock provider", type="boolean", default=False),
    )

    def synthesize(self, request: SpeechRequest, config: Mapping[str, Any]) -> SpeechResult:
        digest = hashlib.sha256(request.text.encode("utf-8")).hexdigest()[:12]
        return SpeechResult(
            provider_id=self.id,
            artifact=AudioArtifact("audio/wav", f"mock-{digest}.wav", _silent_wav()),
            model="deterministic-mock-v1",
            voice="mock-voice",
        )

"""Fixed-host OpenAI text-to-speech provider (``POST /v1/audio/speech``)."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .base import (
    AudioArtifact,
    ProviderCapabilities,
    ProviderConfigField,
    SafeErrorCode,
    SpeechGenerationError,
    SpeechProvider,
    SpeechRequest,
    SpeechResult,
)
from .network import BoundedHttpClient, SafeEndpoint, bounded_timeout

_API_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini-tts"
_MODELS = (_DEFAULT_MODEL, "tts-1", "tts-1-hd")
_DEFAULT_VOICE = "alloy"
_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse")
_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav"}


class OpenAISpeechProvider(SpeechProvider):
    """Synthesize speech with an approved OpenAI TTS model."""

    id = "openai"
    display_name = "OpenAI text to speech"
    capabilities = ProviderCapabilities(
        supported_voices=_VOICES,
        supported_models=_MODELS,
        supported_output_formats=tuple(_MIME),
        max_text_length=4_000,
        supports_speed=True,
        supports_instructions=True,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField("openai_api_key", "OpenAI API Key", required=True, secret=True,
                            description="OpenAI API key. Write-only."),
        ProviderConfigField("openai_model", "OpenAI TTS Model", default=_DEFAULT_MODEL, choices=_MODELS),
        ProviderConfigField("openai_voice", "Default Voice", default=_DEFAULT_VOICE, choices=_VOICES),
        ProviderConfigField("openai_timeout_seconds", "Timeout (seconds)", type="number", default=60),
    )

    @staticmethod
    def _credential(config: Mapping[str, Any]) -> str:
        value = config.get("openai_api_key")
        if not isinstance(value, str) or not value.strip():
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "OpenAI requires an administrator-configured credential.")
        return value.strip()

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(SafeEndpoint(_API_BASE_URL, bounded_timeout(config.get("openai_timeout_seconds"))))

    def test_connection(self, config: Mapping[str, Any]) -> None:
        self._client(config).request("GET", "/models", headers={"Authorization": f"Bearer {self._credential(config)}"}, max_bytes=2 * 1024 * 1024)

    def synthesize(self, request: SpeechRequest, config: Mapping[str, Any]) -> SpeechResult:
        credential = self._credential(config)
        model = request.model or config.get("openai_model") or _DEFAULT_MODEL
        voice = request.voice or config.get("openai_voice") or _DEFAULT_VOICE
        if model not in _MODELS:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected OpenAI model is not approved.")
        if voice not in _VOICES:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected voice is not supported by OpenAI.")
        fmt = request.output_format or "mp3"
        payload: dict[str, Any] = {"model": model, "voice": voice, "input": request.text, "response_format": fmt}
        if request.speed is not None:
            payload["speed"] = request.speed
        if request.instructions and model == "gpt-4o-mini-tts":
            payload["instructions"] = request.instructions
        import json
        data, content_type = self._client(config).request(
            "POST", "/audio/speech",
            body=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json", "Accept": "audio/*"},
        )
        if not content_type.startswith("audio/"):
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider did not return audio.")
        return SpeechResult(
            provider_id=self.id,
            artifact=AudioArtifact(_MIME[fmt], f"openai.{fmt}", data),
            model=model,
            voice=voice,
        )

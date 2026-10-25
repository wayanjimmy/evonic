"""Registry of approved Text to Speech providers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from .base import (
    SafeErrorCode,
    SpeechGenerationError,
    SpeechProvider,
    SpeechRequest,
    SpeechResult,
)


class SpeechProviderRegistry:
    """In-memory registry that resolves and validates approved providers."""

    def __init__(self) -> None:
        self._providers: Dict[str, SpeechProvider] = {}

    def register(self, provider: SpeechProvider) -> None:
        provider_id = getattr(provider, "id", "")
        if not provider_id or not isinstance(provider_id, str):
            raise ValueError("Speech providers require a non-empty string id.")
        if provider_id in self._providers:
            raise ValueError(f"Speech provider '{provider_id}' is already registered.")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> SpeechProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_NOT_FOUND, "The selected speech provider is not available.")
        return provider

    def list(self) -> Iterable[SpeechProvider]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def resolve(self, provider_id: Optional[str], default_provider_id: Optional[str]) -> SpeechProvider:
        selected = provider_id or default_provider_id
        if not selected:
            raise SpeechGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION,
                "No speech provider is selected. Configure a default provider or select one explicitly.",
            )
        return self.get(selected)

    def synthesize(
        self,
        request: SpeechRequest,
        config: Mapping[str, Any],
        provider_id: Optional[str] = None,
        default_provider_id: Optional[str] = None,
    ) -> SpeechResult:
        provider = self.resolve(provider_id, default_provider_id)
        self._validate_request(request, provider)
        result = provider.synthesize(request, config)
        if result.provider_id != provider.id:
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned a mismatched provider identifier.")
        result.validate()
        return result

    @staticmethod
    def _validate_request(request: SpeechRequest, provider: SpeechProvider) -> None:
        caps = provider.capabilities
        if not request.text.strip():
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "Text to speak is required.")
        if len(request.text) > caps.max_text_length:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "Text is too long for the selected provider.")
        if request.output_format not in caps.supported_output_formats:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested output format is not supported by the selected provider.")
        if request.voice and caps.supported_voices and request.voice not in caps.supported_voices:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested voice is not supported by the selected provider. Supported voices: " + ", ".join(caps.supported_voices) + ".")
        if request.model and caps.supported_models and request.model not in caps.supported_models:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested model is not supported by the selected provider.")
        if request.speed is not None and not caps.supports_speed:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support speed.")
        if request.instructions and not caps.supports_instructions:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support style instructions.")


from .elevenlabs import ElevenLabsSpeechProvider
from .google import GoogleGeminiSpeechProvider
from .mock import DeterministicMockSpeechProvider
from .openai import OpenAISpeechProvider
from .openrouter import OpenRouterSpeechProvider

provider_registry = SpeechProviderRegistry()
provider_registry.register(ElevenLabsSpeechProvider())
provider_registry.register(GoogleGeminiSpeechProvider())
provider_registry.register(OpenAISpeechProvider())
provider_registry.register(OpenRouterSpeechProvider())
provider_registry.register(DeterministicMockSpeechProvider())

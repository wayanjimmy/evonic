"""Provider contracts and registry for the Image Generator skill."""

from .base import (
    ImageArtifact,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProvider,
    ProviderCapabilities,
    ProviderConfigField,
    SafeErrorCode,
)
from .automatic1111 import Automatic1111Provider
from .comfyui import ComfyUiProvider
from .google import GoogleGeminiProvider
from .mock import DeterministicMockProvider
from .openrouter import OpenRouterProvider
from .registry import ImageProviderRegistry, provider_registry

__all__ = [
    "Automatic1111Provider",
    "ComfyUiProvider",
    "DeterministicMockProvider",
    "GoogleGeminiProvider",
    "ImageArtifact",
    "ImageGenerationError",
    "ImageGenerationRequest",
    "ImageGenerationResult",
    "ImageProvider",
    "ImageProviderRegistry",
    "OpenRouterProvider",
    "ProviderCapabilities",
    "ProviderConfigField",
    "SafeErrorCode",
    "provider_registry",
]

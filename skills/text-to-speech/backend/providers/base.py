"""Provider-neutral contracts for text-to-speech.

No network or provider SDK code lives here.  Providers implement
:class:`SpeechProvider` and return the normalized types below so the tool
executor stays provider agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence


class SafeErrorCode(str, Enum):
    """Stable, non-sensitive categories suitable for agent-facing errors."""

    INVALID_REQUEST = "invalid_request"
    PROVIDER_NOT_FOUND = "provider_not_found"
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_CONFIGURATION = "provider_configuration"
    PERMISSION_DENIED = "permission_denied"
    CONTENT_REJECTED = "content_rejected"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    GENERATION_FAILED = "generation_failed"
    ARTIFACT_INVALID = "artifact_invalid"


class SpeechGenerationError(Exception):
    """Provider failure that exposes a safe category and sanitized message."""

    def __init__(self, code: SafeErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code.value, "message": self.message}


@dataclass(frozen=True)
class ProviderConfigField:
    """Declarative schema for provider-specific configuration fields."""

    name: str
    label: str
    type: str = "string"
    required: bool = False
    secret: bool = False
    default: Any = ""
    description: str = ""
    choices: Sequence[str] = ()


@dataclass(frozen=True)
class ProviderCapabilities:
    """Synthesis options a provider can support."""

    supported_voices: Sequence[str] = ()
    supported_models: Sequence[str] = ()
    supported_output_formats: Sequence[str] = ("wav",)
    max_text_length: int = 4_000
    supports_speed: bool = False
    supports_instructions: bool = False


@dataclass(frozen=True)
class SpeechRequest:
    """A validated, provider-neutral request to synthesize speech."""

    text: str
    voice: Optional[str] = None
    model: Optional[str] = None
    output_format: str = "wav"
    speed: Optional[float] = None
    instructions: Optional[str] = None
    provider_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioArtifact:
    """Audio output before Evonic artifact persistence."""

    mime_type: str
    filename: str
    data: bytes

    def validate(self) -> None:
        if not self.mime_type.startswith("audio/"):
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an invalid audio MIME type.")
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsafe audio filename.")
        if not self.data:
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an empty audio artifact.")


@dataclass(frozen=True)
class SpeechResult:
    """Normalized successful synthesis response."""

    provider_id: str
    artifact: AudioArtifact
    model: Optional[str] = None
    voice: Optional[str] = None
    warnings: Sequence[str] = ()

    def validate(self) -> None:
        self.artifact.validate()


class SpeechProvider(ABC):
    """Contract implemented by each approved speech provider."""

    id: str
    display_name: str
    capabilities: ProviderCapabilities
    config_fields: Sequence[ProviderConfigField] = ()
    is_local: bool = False

    @abstractmethod
    def synthesize(self, request: SpeechRequest, config: Mapping[str, Any]) -> SpeechResult:
        """Synthesize speech or raise :class:`SpeechGenerationError`.

        Implementations must never expose credentials, raw provider payloads, or
        internal network details in error messages.
        """

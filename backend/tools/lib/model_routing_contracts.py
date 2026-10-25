"""Provider-neutral model routing and capability contracts.

This module deliberately contains no persistence, provider invocation, or credential
resolution.  It defines the safe boundary shared by registry validation, profile
assignment, routing, and fallback code.  A provider is one configured endpoint and
account connection; models reference that provider.  There is intentionally no
provider-deployment entity in this contract.

The only credential-bearing object is :class:`ResolvedModelRoute`, whose
``to_public_dict`` method deliberately omits credentials and endpoint internals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


CAPABILITY_SCHEMA_VERSION = "model-capabilities/v1"
ROUTING_CONTRACT_VERSION = "model-routing/v1"


class ContractValidationError(ValueError):
    """Stable contract validation error with a machine-readable code and path."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        super().__init__(message)


def _fail(code: str, path: str, message: str) -> None:
    raise ContractValidationError(code, path, message)


class AdapterKey(str, Enum):
    """Versioned protocol adapters, named for their native wire contract."""

    OPENAI_CHAT_COMPLETIONS_V1 = "openai.chat_completions.v1"
    OPENAI_RESPONSES_V1 = "openai.responses.v1"
    ANTHROPIC_MESSAGES_V1 = "anthropic.messages.v1"
    OLLAMA_CHAT_V1 = "ollama.chat.v1"
    CODEX_RESPONSES_V1 = "codex.responses.v1"
    SYSTEMONE_QUESTIONS_V1 = "systemone.questions.v1"


class Operation(str, Enum):
    CHAT = "chat"
    DECISION = "decision"
    VISION = "vision"
    TOOLS = "tools"
    STREAMING = "streaming"
    STRICT_OUTPUT = "strict_output"


class CapabilityEvidence(str, Enum):
    UNSUPPORTED = "unsupported"
    ADVERTISED = "advertised"
    VERIFIED = "verified"


class CompatibilityCode(str, Enum):
    COMPATIBLE = "compatible"
    MODEL_DISABLED = "model_disabled"
    PROVIDER_DISABLED = "provider_disabled"
    MISSING_OPERATION = "missing_operation"
    MISSING_SCHEMA_VERSION = "missing_schema_version"
    MISSING_CONFIDENCE = "missing_confidence"
    UNKNOWN_REQUIREMENT = "unknown_requirement"
    EGRESS_RESTRICTED = "egress_restricted"


class AdapterFailureCode(str, Enum):
    UNKNOWN_ADAPTER = "unknown_adapter"
    INVALID_ENDPOINT = "invalid_endpoint"
    CREDENTIAL_UNRESOLVED = "credential_unresolved"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED_OPERATION = "unsupported_operation"


@dataclass(frozen=True)
class AdapterFailure:
    """Safe, typed adapter outcome; provider-native exception details stay private."""

    code: AdapterFailureCode
    message: str
    retryable: bool = False


class CredentialResolutionState(str, Enum):
    NOT_REQUIRED = "not_required"
    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CredentialResolution:
    """Credential resolution status without the credential material itself."""

    state: CredentialResolutionState
    source: str | None = None

    def to_public_dict(self) -> dict[str, str | None]:
        return {"state": self.state.value, "source": self.source}


@dataclass(frozen=True)
class ModelCapabilities:
    """Canonical capability declaration persisted as ``model-capabilities/v1``."""

    operations: Mapping[Operation, CapabilityEvidence]
    schema_versions: frozenset[str] = frozenset()
    confidence: CapabilityEvidence = CapabilityEvidence.UNSUPPORTED
    data_egress: frozenset[str] = frozenset({"public"})
    version: str = CAPABILITY_SCHEMA_VERSION

    def supports(self, operation: Operation) -> bool:
        return self.operations.get(operation, CapabilityEvidence.UNSUPPORTED) is not CapabilityEvidence.UNSUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "operations": {key.value: self.operations[key].value for key in sorted(self.operations, key=lambda item: item.value)},
            "schema_versions": sorted(self.schema_versions),
            "confidence": self.confidence.value,
            "data_egress": sorted(self.data_egress),
        }


@dataclass(frozen=True)
class OperationRequirements:
    """All requirements must be met; requirements never imply one another."""

    operations: frozenset[Operation]
    schema_version: str | None = None
    require_confidence: bool = False
    data_classification: str = "public"


@dataclass(frozen=True)
class CompatibilityResult:
    compatible: bool
    code: CompatibilityCode
    missing_operations: tuple[Operation, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ProviderRoute:
    provider_id: str
    endpoint: str
    adapter_key: AdapterKey
    enabled: bool = True


@dataclass(frozen=True)
class ResolvedModelRoute:
    """Internal resolved route. Keep this object out of public logs and responses."""

    model_id: str
    provider: ProviderRoute
    native_model_name: str
    capabilities: ModelCapabilities
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)
    credential_resolution: CredentialResolution = field(
        default_factory=lambda: CredentialResolution(CredentialResolutionState.NOT_REQUIRED),
        repr=False,
    )
    enabled: bool = True
    legacy_provenance: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Return whitelisted metadata only; credentials and endpoint are excluded."""
        return {
            "contract_version": ROUTING_CONTRACT_VERSION,
            "model_id": self.model_id,
            "provider_id": self.provider.provider_id,
            "adapter_key": self.provider.adapter_key.value,
            "native_model_name": self.native_model_name,
            "enabled": self.enabled,
            "provider_enabled": self.provider.enabled,
            "capabilities": self.capabilities.to_dict(),
            "credential_resolution": self.credential_resolution.to_public_dict(),
            "legacy_provenance": self.legacy_provenance,
        }


def _as_string_set(value: Any, path: str) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail("invalid_array", path, "must be an array of non-empty strings")
    result = frozenset(value)
    if len(result) != len(value) or any(not isinstance(item, str) or not item for item in result):
        _fail("invalid_array", path, "must contain unique, non-empty strings")
    return result


def parse_capabilities(raw: Mapping[str, Any]) -> ModelCapabilities:
    """Strictly parse canonical capabilities; unknown fields never affect routing."""
    if not isinstance(raw, Mapping):
        _fail("invalid_object", "capabilities", "must be an object")
    allowed = {"version", "operations", "schema_versions", "confidence", "data_egress"}
    extra = set(raw) - allowed
    if extra:
        _fail("unknown_property", "capabilities", f"contains unsupported properties: {', '.join(sorted(extra))}")
    if raw.get("version") != CAPABILITY_SCHEMA_VERSION:
        _fail("unsupported_version", "capabilities.version", f"must be {CAPABILITY_SCHEMA_VERSION}")
    operations_raw = raw.get("operations")
    if not isinstance(operations_raw, Mapping):
        _fail("invalid_object", "capabilities.operations", "must be an object")
    operations: dict[Operation, CapabilityEvidence] = {}
    for name, evidence in operations_raw.items():
        try:
            operation = Operation(name)
            operations[operation] = CapabilityEvidence(evidence)
        except (TypeError, ValueError):
            _fail("invalid_capability", f"capabilities.operations.{name}", "contains an unknown operation or evidence value")
    try:
        confidence = CapabilityEvidence(raw.get("confidence", "unsupported"))
    except (TypeError, ValueError):
        _fail("invalid_capability", "capabilities.confidence", "must be unsupported, advertised, or verified")
    return ModelCapabilities(
        operations=operations,
        schema_versions=_as_string_set(raw.get("schema_versions", []), "capabilities.schema_versions"),
        confidence=confidence,
        data_egress=_as_string_set(raw.get("data_egress", ["public"]), "capabilities.data_egress"),
    )


def validate_endpoint(endpoint: str) -> str:
    """Accept an absolute HTTP(S) endpoint without inferring a protocol adapter."""
    if not isinstance(endpoint, str) or not endpoint:
        _fail("invalid_endpoint", "provider.endpoint", "must be a non-empty absolute HTTP(S) URL")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _fail("invalid_endpoint", "provider.endpoint", "must be a non-empty absolute HTTP(S) URL")
    return endpoint.rstrip("/")


def compatibility_for(route: ResolvedModelRoute, requirements: OperationRequirements) -> CompatibilityResult:
    """Check the complete requirement set, returning structured diagnostic data."""
    if not route.enabled:
        return CompatibilityResult(False, CompatibilityCode.MODEL_DISABLED, message="Model is disabled.")
    if not route.provider.enabled:
        return CompatibilityResult(False, CompatibilityCode.PROVIDER_DISABLED, message="Provider is disabled.")
    missing = tuple(sorted((operation for operation in requirements.operations if not route.capabilities.supports(operation)), key=lambda item: item.value))
    if missing:
        return CompatibilityResult(False, CompatibilityCode.MISSING_OPERATION, missing, "Model lacks required operations.")
    if requirements.schema_version and requirements.schema_version not in route.capabilities.schema_versions:
        return CompatibilityResult(False, CompatibilityCode.MISSING_SCHEMA_VERSION, message="Model lacks the requested output schema version.")
    if requirements.require_confidence and route.capabilities.confidence is CapabilityEvidence.UNSUPPORTED:
        return CompatibilityResult(False, CompatibilityCode.MISSING_CONFIDENCE, message="Model does not provide confidence provenance.")
    if requirements.data_classification not in route.capabilities.data_egress:
        return CompatibilityResult(False, CompatibilityCode.EGRESS_RESTRICTED, message="Model route is not approved for this data classification.")
    return CompatibilityResult(True, CompatibilityCode.COMPATIBLE)


def fallback_eligible(candidate: ResolvedModelRoute, requirements: OperationRequirements, failure: AdapterFailure) -> bool:
    """A fallback must be compatible and failure/retry policy must permit it."""
    return failure.retryable and compatibility_for(candidate, requirements).compatible


def normalize_provider_confidence(value: Any) -> float | None:
    """Validate provider confidence without inventing a value when it is absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_confidence", "result.confidence", "must be null or a finite number from 0 through 1")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        _fail("invalid_confidence", "result.confidence", "must be null or a finite number from 0 through 1")
    return confidence


__all__ = [
    "AdapterFailure", "AdapterFailureCode", "AdapterKey", "CAPABILITY_SCHEMA_VERSION",
    "CapabilityEvidence", "CompatibilityCode", "CompatibilityResult", "ContractValidationError",
    "CredentialResolution", "CredentialResolutionState", "ModelCapabilities", "Operation",
    "OperationRequirements", "ProviderRoute", "ROUTING_CONTRACT_VERSION", "ResolvedModelRoute",
    "compatibility_for", "fallback_eligible", "normalize_provider_confidence", "parse_capabilities",
    "validate_endpoint",
]

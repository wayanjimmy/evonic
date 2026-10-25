"""Versioned, provider-neutral Decision Models contracts.

This module deliberately defines data and validation only.  Profile persistence,
provider credentials, concrete provider invocation, and consumer policy belong to
other layers.  The wire representation returned by ``to_dict`` is canonical:
object keys are emitted in a fixed order and schema/result fields are sorted by
field key.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Any, Literal, Mapping, Protocol, Sequence

CONTRACT_VERSION = "decision-models/v1"
SCHEMA_VERSION = "decision-schema/v1"
PROFILE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

BUILTIN_PROFILE_KEYS = frozenset({
    "safety.command",
    "agent.task_complexity",
    "agent.path_boundary",
    "agent.goal_completion",
})

__all__ = [
    "BUILTIN_PROFILE_KEYS", "CONTRACT_VERSION", "SCHEMA_VERSION",
    "ContractValidationError", "DecisionAdapter", "DecisionCancellation",
    "DecisionCandidate", "DecisionError", "DecisionErrorCode", "DecisionField",
    "DecisionFieldResult", "DecisionInvocation", "DecisionProfileRef",
    "DecisionResult", "DecisionSchema", "FallbackMetadata", "FieldKind",
    "ProviderLogit", "ProviderMetadata", "TimingMetadata", "parse_schema",
    "validate_invocation", "validate_result",
]


class ContractValidationError(ValueError):
    """A stable validation failure with a machine-readable code and path."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        super().__init__(message)


def _fail(code: str, path: str, message: str) -> None:
    raise ContractValidationError(code, path, message)


def _finite_unit(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_number", path, "must be a finite number from 0 through 1")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        _fail("out_of_range", path, "must be a finite number from 0 through 1")
    return number


def _key(value: Any, path: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail("invalid_key", path, "contains invalid characters or structure")
    return value


def _json_value(value: Any, path: str) -> Any:
    """Allow only JSON-compatible, finite context/default values."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            _fail("invalid_json_value", path, "number must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            _fail("invalid_json_value", path, "object keys must be strings")
        return {key: _json_value(value[key], f"{path}.{key}") for key in sorted(value)}
    _fail("invalid_json_value", path, "must be JSON-compatible")


class FieldKind(str, Enum):
    """The deliberately small v1 field DSL: categorical, boolean/null, score."""

    CHOICE = "choice"
    BOOLEAN = "boolean"
    SCORE = "score"


@dataclass(frozen=True)
class DecisionField:
    """One requested result field.

    ``nullable`` applies to all kinds.  A choice field supplies two or more
    unique string ``choices``; a score field supplies inclusive finite bounds.
    Boolean fields intentionally need neither choices nor bounds.
    """

    key: str
    kind: FieldKind
    description: str = ""
    required: bool = True
    nullable: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key, "kind": self.kind.value, "description": self.description,
            "required": self.required, "nullable": self.nullable,
        }
        if self.default is not None:
            data["default"] = self.default
        if self.kind is FieldKind.CHOICE:
            data["choices"] = list(self.choices)
        if self.kind is FieldKind.SCORE:
            data["minimum"] = self.minimum
            data["maximum"] = self.maximum
        return data


@dataclass(frozen=True)
class DecisionSchema:
    version: str
    fields: tuple[DecisionField, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "fields": [field.to_dict() for field in self.fields]}

    def field(self, key: str) -> DecisionField:
        for field in self.fields:
            if field.key == key:
                return field
        raise KeyError(key)


def _validate_field(raw: Mapping[str, Any], path: str) -> DecisionField:
    allowed = {"key", "kind", "description", "required", "nullable", "default", "choices", "minimum", "maximum"}
    extra = set(raw) - allowed
    if extra:
        _fail("unknown_property", path, f"contains unsupported properties: {', '.join(sorted(extra))}")
    key = _key(raw.get("key"), f"{path}.key", FIELD_KEY_PATTERN)
    try:
        kind = FieldKind(raw.get("kind"))
    except (TypeError, ValueError):
        _fail("invalid_kind", f"{path}.kind", "must be choice, boolean, or score")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        _fail("invalid_description", f"{path}.description", "must be a string no longer than 2000 characters")
    required, nullable = raw.get("required", True), raw.get("nullable", False)
    if not isinstance(required, bool) or not isinstance(nullable, bool):
        _fail("invalid_boolean", path, "required and nullable must be booleans")
    choices = raw.get("choices", ())
    minimum, maximum = raw.get("minimum"), raw.get("maximum")
    if kind is FieldKind.CHOICE:
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or len(choices) < 2:
            _fail("invalid_choices", f"{path}.choices", "choice fields require at least two choices")
        if any(not isinstance(choice, str) or not choice for choice in choices) or len(set(choices)) != len(choices):
            _fail("invalid_choices", f"{path}.choices", "choices must be unique, non-empty strings")
        if minimum is not None or maximum is not None:
            _fail("invalid_bounds", path, "only score fields may set bounds")
    elif kind is FieldKind.BOOLEAN:
        if choices not in ((), None) or minimum is not None or maximum is not None:
            _fail("invalid_field_shape", path, "boolean fields cannot set choices or bounds")
        choices = ()
    else:
        if choices not in ((), None):
            _fail("invalid_field_shape", path, "score fields cannot set choices")
        if minimum is None or maximum is None:
            _fail("missing_bounds", path, "score fields require minimum and maximum")
        if isinstance(minimum, bool) or isinstance(maximum, bool) or not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
            _fail("invalid_bounds", path, "bounds must be finite numbers")
        minimum, maximum = float(minimum), float(maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
            _fail("invalid_bounds", path, "minimum must be finite and less than maximum")
    default = raw.get("default")
    if "default" in raw:
        _validate_field_value(DecisionField(key, kind, nullable=nullable, choices=tuple(choices), minimum=minimum, maximum=maximum), default, f"{path}.default")
    elif not required and not nullable:
        _fail("missing_default", path, "optional, non-nullable fields require a default")
    return DecisionField(key, kind, description, required, nullable, default, tuple(choices), minimum, maximum)


def parse_schema(raw: Mapping[str, Any]) -> DecisionSchema:
    """Parse and validate the constrained v1 DSL, returning canonical field order."""
    if not isinstance(raw, Mapping):
        _fail("invalid_schema", "schema", "must be an object")
    if set(raw) - {"version", "fields"}:
        _fail("unknown_property", "schema", "contains unsupported properties")
    if raw.get("version") != SCHEMA_VERSION:
        _fail("unsupported_version", "schema.version", f"must be {SCHEMA_VERSION}")
    items = raw.get("fields")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        _fail("invalid_fields", "schema.fields", "must be a non-empty array")
    fields = tuple(_validate_field(item, f"schema.fields[{index}]") if isinstance(item, Mapping) else _fail("invalid_field", f"schema.fields[{index}]", "must be an object") for index, item in enumerate(items))
    if len({field.key for field in fields}) != len(fields):
        _fail("duplicate_field", "schema.fields", "field keys must be unique")
    return DecisionSchema(SCHEMA_VERSION, tuple(sorted(fields, key=lambda field: field.key)))


def _validate_field_value(field: DecisionField, value: Any, path: str) -> Any:
    if value is None:
        if field.nullable:
            return None
        _fail("null_not_allowed", path, "field is not nullable")
    if field.kind is FieldKind.CHOICE:
        if not isinstance(value, str) or value not in field.choices:
            _fail("invalid_choice", path, "must be one of the declared choices")
    elif field.kind is FieldKind.BOOLEAN:
        if not isinstance(value, bool):
            _fail("invalid_boolean", path, "must be a boolean")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not field.minimum <= float(value) <= field.maximum:
            _fail("invalid_score", path, "must be finite and within inclusive schema bounds")
        return float(value)
    return value


@dataclass(frozen=True)
class DecisionProfileRef:
    key: str
    version: str


@dataclass(frozen=True)
class DecisionCancellation:
    """Cooperative cancellation data; never serialized or sent as provider policy."""
    deadline_unix_ms: int | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class DecisionInvocation:
    profile: DecisionProfileRef
    schema: DecisionSchema
    input_context: Mapping[str, Any]
    policy_metadata: Mapping[str, Any] | None = None
    correlation_id: str | None = None
    cancellation: DecisionCancellation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "profile": {"key": self.profile.key, "version": self.profile.version}, "schema": self.schema.to_dict(), "input_context": _json_value(self.input_context, "input_context"), "policy_metadata": _json_value(self.policy_metadata or {}, "policy_metadata"), "correlation_id": self.correlation_id}


def validate_invocation(raw: Mapping[str, Any]) -> DecisionInvocation:
    if not isinstance(raw, Mapping):
        _fail("invalid_invocation", "invocation", "must be an object")
    if raw.get("contract_version", CONTRACT_VERSION) != CONTRACT_VERSION:
        _fail("unsupported_version", "contract_version", f"must be {CONTRACT_VERSION}")
    profile = raw.get("profile")
    if not isinstance(profile, Mapping):
        _fail("invalid_profile", "profile", "must be an object")
    key = _key(profile.get("key"), "profile.key", PROFILE_KEY_PATTERN)
    version = profile.get("version")
    if not isinstance(version, str) or not version:
        _fail("invalid_version", "profile.version", "must be a non-empty string")
    context = raw.get("input_context", {})
    policy = raw.get("policy_metadata")
    if not isinstance(context, Mapping) or (policy is not None and not isinstance(policy, Mapping)):
        _fail("invalid_context", "input_context", "context and policy metadata must be objects")
    correlation_id = raw.get("correlation_id")
    if correlation_id is not None and (not isinstance(correlation_id, str) or len(correlation_id) > 256):
        _fail("invalid_correlation_id", "correlation_id", "must be a string no longer than 256 characters")
    raw_cancellation = raw.get("cancellation")
    cancellation = None
    if raw_cancellation is not None:
        if not isinstance(raw_cancellation, Mapping) or set(raw_cancellation) - {"deadline_unix_ms", "cancelled"}:
            _fail("invalid_cancellation", "cancellation", "must contain only deadline_unix_ms and cancelled")
        deadline = raw_cancellation.get("deadline_unix_ms")
        cancelled = raw_cancellation.get("cancelled", False)
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < 0):
            _fail("invalid_cancellation", "cancellation.deadline_unix_ms", "must be a non-negative integer")
        if not isinstance(cancelled, bool):
            _fail("invalid_cancellation", "cancellation.cancelled", "must be a boolean")
        cancellation = DecisionCancellation(deadline, cancelled)
    return DecisionInvocation(DecisionProfileRef(key, version), parse_schema(raw.get("schema")), _json_value(context, "input_context"), _json_value(policy or {}, "policy_metadata"), correlation_id, cancellation)


@dataclass(frozen=True)
class DecisionCandidate:
    value: Any
    score: float | None = None
    probability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"value": self.value}
        if self.score is not None: result["score"] = self.score
        if self.probability is not None: result["probability"] = self.probability
        return result


@dataclass(frozen=True)
class ProviderLogit:
    """Raw provider value only; adapters must omit it unless actually supplied."""
    token: str
    value: float

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "value": self.value}


@dataclass(frozen=True)
class DecisionFieldResult:
    key: str
    value: Any
    confidence: float | None = None
    candidates: tuple[DecisionCandidate, ...] = ()
    provider_logits: tuple[ProviderLogit, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = {"key": self.key, "value": self.value}
        if self.confidence is not None: result["confidence"] = self.confidence
        if self.candidates: result["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        if self.provider_logits: result["provider_logits"] = [logit.to_dict() for logit in self.provider_logits]
        return result


class DecisionErrorCode(str, Enum):
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INVALID_REQUEST = "invalid_request"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_RESPONSE_INVALID = "provider_response_invalid"
    NO_COMPATIBLE_MODEL = "no_compatible_model"
    INTERNAL = "internal"


@dataclass(frozen=True)
class DecisionError:
    code: DecisionErrorCode
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True)
class ProviderMetadata:
    provider_key: str
    model_id: str
    model_revision: str | None = None
    usage: Mapping[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"provider_key": self.provider_key, "model_id": self.model_id}
        if self.model_revision is not None: result["model_revision"] = self.model_revision
        if self.usage is not None: result["usage"] = dict(self.usage)
        return result


@dataclass(frozen=True)
class TimingMetadata:
    total_ms: int
    provider_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"total_ms": self.total_ms}
        if self.provider_ms is not None: result["provider_ms"] = self.provider_ms
        return result


@dataclass(frozen=True)
class FallbackMetadata:
    used: bool
    reason: str | None = None
    from_profile_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"used": self.used}
        if self.reason is not None: result["reason"] = self.reason
        if self.from_profile_version is not None: result["from_profile_version"] = self.from_profile_version
        return result


@dataclass(frozen=True)
class DecisionResult:
    profile: DecisionProfileRef
    schema_version: str
    fields: tuple[DecisionFieldResult, ...]
    provider: ProviderMetadata | None
    timing: TimingMetadata
    attempts: int
    warnings: tuple[str, ...] = ()
    fallback: FallbackMetadata | None = None
    error: DecisionError | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"contract_version": CONTRACT_VERSION, "profile": {"key": self.profile.key, "version": self.profile.version}, "schema_version": self.schema_version, "fields": [field.to_dict() for field in self.fields], "provider": self.provider.to_dict() if self.provider else None, "timing": self.timing.to_dict(), "attempts": self.attempts, "warnings": list(self.warnings), "fallback": self.fallback.to_dict() if self.fallback else None, "error": self.error.to_dict() if self.error else None}
        return result


def validate_result(schema: DecisionSchema, result: DecisionResult) -> DecisionResult:
    """Validate and canonically order a normalized result against its schema."""
    if result.schema_version != schema.version:
        _fail("schema_version_mismatch", "result.schema_version", "must match the request schema")
    if result.attempts < 0 or result.timing.total_ms < 0 or (result.timing.provider_ms is not None and result.timing.provider_ms < 0):
        _fail("invalid_timing", "result", "attempt counts and timing must be non-negative")
    seen: set[str] = set()
    normalized: list[DecisionFieldResult] = []
    for index, field_result in enumerate(result.fields):
        path = f"result.fields[{index}]"
        if field_result.key in seen: _fail("duplicate_field", path, "result field key is duplicated")
        seen.add(field_result.key)
        try: field = schema.field(field_result.key)
        except KeyError: _fail("unknown_field", path, "is absent from schema")
        value = _validate_field_value(field, field_result.value, f"{path}.value")
        confidence = None if field_result.confidence is None else _finite_unit(field_result.confidence, f"{path}.confidence")
        candidates = tuple(field_result.candidates)
        for candidate_index, candidate in enumerate(candidates):
            _validate_field_value(field, candidate.value, f"{path}.candidates[{candidate_index}].value")
            if candidate.score is not None and (isinstance(candidate.score, bool) or not isinstance(candidate.score, (int, float)) or not math.isfinite(float(candidate.score))): _fail("invalid_score", path, "candidate score must be finite")
            if candidate.probability is not None: _finite_unit(candidate.probability, f"{path}.candidates[{candidate_index}].probability")
        for logit in field_result.provider_logits:
            if not isinstance(logit.token, str) or not isinstance(logit.value, (int, float)) or isinstance(logit.value, bool) or not math.isfinite(float(logit.value)):
                _fail("invalid_logit", path, "provider logits must be raw finite numeric provider values")
        normalized.append(DecisionFieldResult(field_result.key, value, confidence, candidates, tuple(field_result.provider_logits)))
    required = {field.key for field in schema.fields if field.required}
    if result.error is None and not required.issubset(seen): _fail("missing_required_field", "result.fields", "successful result omits a required field")
    return DecisionResult(result.profile, result.schema_version, tuple(sorted(normalized, key=lambda item: item.key)), result.provider, result.timing, result.attempts, tuple(result.warnings), result.fallback, result.error)


class DecisionAdapter(Protocol):
    """Provider adapters return normalized results and never own profile policy."""
    key: str
    def decide(self, invocation: DecisionInvocation) -> DecisionResult: ...

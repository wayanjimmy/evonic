"""Focused, network-free tests for Decision Models v1 contracts."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.tools.lib.decision_models import (  # noqa: E402
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    ContractValidationError,
    DecisionCandidate,
    DecisionError,
    DecisionErrorCode,
    DecisionFieldResult,
    DecisionProfileRef,
    DecisionResult,
    FallbackMetadata,
    ProviderLogit,
    ProviderMetadata,
    TimingMetadata,
    parse_schema,
    validate_invocation,
    validate_result,
)


def _schema():
    return parse_schema({
        "version": SCHEMA_VERSION,
        "fields": [
            {"key": "risk", "kind": "score", "description": "Risk score", "minimum": 0, "maximum": 10},
            {"key": "allowed", "kind": "boolean", "required": False, "default": False},
            {"key": "action", "kind": "choice", "choices": ["allow", "review", "block"]},
        ],
    })


@pytest.mark.parametrize("field", [
    {"key": "action", "kind": "choice", "choices": ["allow", "block"], "default": "allow"},
    {"key": "is_safe", "kind": "boolean", "nullable": True, "default": None},
    {"key": "risk", "kind": "score", "minimum": 0, "maximum": 1, "default": 0.5},
])
def test_each_supported_primitive_parses(field):
    schema = parse_schema({"version": SCHEMA_VERSION, "fields": [field]})
    assert schema.to_dict()["fields"][0]["kind"] == field["kind"]


@pytest.mark.parametrize("field, code", [
    ({"key": "action", "kind": "choice", "choices": ["only"]}, "invalid_choices"),
    ({"key": "action", "kind": "choice", "choices": ["x", "x"]}, "invalid_choices"),
    ({"key": "is_safe", "kind": "boolean", "choices": ["true", "false"]}, "invalid_field_shape"),
    ({"key": "risk", "kind": "score", "minimum": 2, "maximum": 1}, "invalid_bounds"),
    ({"key": "risk", "kind": "score", "minimum": 0}, "missing_bounds"),
    ({"key": "maybe", "kind": "unknown"}, "invalid_kind"),
])
def test_invalid_primitive_schemas_are_rejected(field, code):
    with pytest.raises(ContractValidationError) as exc:
        parse_schema({"version": SCHEMA_VERSION, "fields": [field]})
    assert exc.value.code == code


def test_schema_canonicalizes_multi_field_order_and_defaults():
    schema = _schema()
    serialized = schema.to_dict()
    assert [field["key"] for field in serialized["fields"]] == ["action", "allowed", "risk"]
    assert serialized["fields"][1]["default"] is False


def test_schema_rejects_duplicate_fields_unknown_properties_and_unsupported_version():
    with pytest.raises(ContractValidationError, match="field keys"):
        parse_schema({"version": SCHEMA_VERSION, "fields": [
            {"key": "answer", "kind": "boolean"}, {"key": "answer", "kind": "boolean"},
        ]})
    with pytest.raises(ContractValidationError) as exc:
        parse_schema({"version": SCHEMA_VERSION, "fields": [{"key": "answer", "kind": "boolean", "extra": True}]})
    assert exc.value.code == "unknown_property"
    with pytest.raises(ContractValidationError) as exc:
        parse_schema({"version": "decision-schema/v2", "fields": [{"key": "answer", "kind": "boolean"}]})
    assert exc.value.code == "unsupported_version"


def test_invocation_has_context_policy_correlation_and_is_deterministic():
    invocation = validate_invocation({
        "contract_version": CONTRACT_VERSION,
        "profile": {"key": "agent.goal_completion", "version": "1.0"},
        "schema": _schema().to_dict(),
        "input_context": {"z": 1, "a": {"b": True}},
        "policy_metadata": {"purpose": "evaluation"},
        "correlation_id": "correlation-1",
        "cancellation": {"deadline_unix_ms": 123, "cancelled": False},
    })
    payload = invocation.to_dict()
    assert list(payload["input_context"]) == ["a", "z"]
    assert payload["policy_metadata"] == {"purpose": "evaluation"}
    assert payload["correlation_id"] == "correlation-1"
    assert invocation.cancellation is not None
    assert invocation.cancellation.deadline_unix_ms == 123
    assert "cancellation" not in payload


def test_result_validates_values_orders_fields_and_keeps_raw_logits_distinct():
    schema = _schema()
    result = DecisionResult(
        profile=DecisionProfileRef("safety.command", "1.0"),
        schema_version=SCHEMA_VERSION,
        fields=(
            DecisionFieldResult("risk", 2.5, confidence=0.8),
            DecisionFieldResult("action", "review", candidates=(
                DecisionCandidate("review", score=0.42, probability=0.7),
                DecisionCandidate("allow", score=0.11, probability=0.2),
            ), provider_logits=(ProviderLogit("review", -0.13),)),
        ),
        provider=ProviderMetadata("example", "model-1"),
        timing=TimingMetadata(15, 12),
        attempts=1,
        warnings=("provider returned no optional boolean",),
        fallback=FallbackMetadata(False),
    )
    normalized = validate_result(schema, result)
    payload = normalized.to_dict()
    assert [field["key"] for field in payload["fields"]] == ["action", "risk"]
    action = payload["fields"][0]
    assert action["candidates"][0] == {"value": "review", "score": 0.42, "probability": 0.7}
    assert action["provider_logits"] == [{"token": "review", "value": -0.13}]
    assert "logit" not in action["candidates"][0]


def test_successful_result_requires_required_values_but_error_result_may_omit_them():
    schema = _schema()
    base = dict(profile=DecisionProfileRef("safety.command", "1.0"), schema_version=SCHEMA_VERSION,
                fields=(), provider=None, timing=TimingMetadata(0), attempts=0)
    with pytest.raises(ContractValidationError) as exc:
        validate_result(schema, DecisionResult(**base))
    assert exc.value.code == "missing_required_field"
    error_result = validate_result(schema, DecisionResult(
        **base, error=DecisionError(DecisionErrorCode.PROVIDER_UNAVAILABLE, "unavailable", retryable=True)))
    assert error_result.to_dict()["error"]["code"] == "provider_unavailable"


def test_result_rejects_invalid_values_probabilities_and_timing():
    schema = _schema()
    base = dict(profile=DecisionProfileRef("safety.command", "1.0"), schema_version=SCHEMA_VERSION,
                provider=None, timing=TimingMetadata(1), attempts=1)
    with pytest.raises(ContractValidationError) as exc:
        validate_result(schema, DecisionResult(**base, fields=(DecisionFieldResult("action", "other"),)))
    assert exc.value.code == "invalid_choice"
    with pytest.raises(ContractValidationError) as exc:
        validate_result(schema, DecisionResult(**base, fields=(
            DecisionFieldResult("action", "allow", candidates=(DecisionCandidate("allow", probability=1.2),)),
            DecisionFieldResult("risk", 1),
        )))
    assert exc.value.code == "out_of_range"
    invalid_timing = dict(base, timing=TimingMetadata(-1))
    with pytest.raises(ContractValidationError) as exc:
        validate_result(schema, DecisionResult(**invalid_timing, fields=(
            DecisionFieldResult("action", "allow"), DecisionFieldResult("risk", 1),
        )))
    assert exc.value.code == "invalid_timing"

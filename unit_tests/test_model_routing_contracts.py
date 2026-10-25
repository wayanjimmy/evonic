"""Contract tests for provider-neutral model routing metadata."""
import pytest

from backend.tools.lib.model_routing_contracts import (
    AdapterFailure,
    AdapterFailureCode,
    AdapterKey,
    CapabilityEvidence,
    CompatibilityCode,
    ContractValidationError,
    CredentialResolution,
    CredentialResolutionState,
    ModelCapabilities,
    Operation,
    OperationRequirements,
    ProviderRoute,
    ResolvedModelRoute,
    compatibility_for,
    fallback_eligible,
    normalize_provider_confidence,
    parse_capabilities,
    validate_endpoint,
)


def _capabilities(**overrides):
    raw = {
        "version": "model-capabilities/v1",
        "operations": {
            "chat": "verified",
            "decision": "verified",
            "strict_output": "advertised",
        },
        "schema_versions": ["decision-schema/v1"],
        "confidence": "advertised",
        "data_egress": ["public", "internal"],
    }
    raw.update(overrides)
    return parse_capabilities(raw)


def _route(**overrides):
    route = ResolvedModelRoute(
        model_id="provider/model",
        provider=ProviderRoute("provider", "https://example.invalid/v1", AdapterKey.OPENAI_CHAT_COMPLETIONS_V1),
        native_model_name="model",
        capabilities=_capabilities(),
        credentials={"api_key": "top-secret"},
        legacy_provenance="provider_base_url",
    )
    values = {field: getattr(route, field) for field in route.__dataclass_fields__}
    values.update(overrides)
    return ResolvedModelRoute(**values)


def test_capabilities_are_canonical_and_unknown_values_are_rejected():
    capabilities = _capabilities()
    assert capabilities.to_dict()["operations"] == {
        "chat": "verified", "decision": "verified", "strict_output": "advertised"
    }

    with pytest.raises(ContractValidationError) as exc:
        parse_capabilities({"version": "model-capabilities/v1", "operations": {}, "unsafe": True})
    assert exc.value.code == "unknown_property"

    with pytest.raises(ContractValidationError) as exc:
        parse_capabilities({"version": "model-capabilities/v1", "operations": {"unknown": "verified"}})
    assert exc.value.code == "invalid_capability"


def test_compatibility_requires_all_operations_schema_confidence_and_egress():
    requirements = OperationRequirements(
        operations=frozenset({Operation.DECISION, Operation.STRICT_OUTPUT}),
        schema_version="decision-schema/v1",
        require_confidence=True,
        data_classification="internal",
    )
    assert compatibility_for(_route(), requirements).compatible is True

    incompatible = compatibility_for(
        _route(capabilities=_capabilities(operations={"decision": "verified"})), requirements
    )
    assert incompatible.code is CompatibilityCode.MISSING_OPERATION
    assert incompatible.missing_operations == (Operation.STRICT_OUTPUT,)

    no_confidence = compatibility_for(
        _route(capabilities=_capabilities(confidence="unsupported")), requirements
    )
    assert no_confidence.code is CompatibilityCode.MISSING_CONFIDENCE

    restricted = compatibility_for(
        _route(capabilities=_capabilities(data_egress=["public"])), requirements
    )
    assert restricted.code is CompatibilityCode.EGRESS_RESTRICTED


def test_public_route_redacts_credentials_and_endpoint():
    public = _route().to_public_dict()
    assert public["adapter_key"] == "openai.chat_completions.v1"
    assert "credentials" not in public
    assert "endpoint" not in public
    assert "top-secret" not in str(public)
    assert "example.invalid" not in str(public)


def test_adapter_keys_name_versioned_wire_protocols():
    assert {key.value for key in AdapterKey} == {
        "openai.chat_completions.v1", "openai.responses.v1", "anthropic.messages.v1",
        "ollama.chat.v1", "codex.responses.v1", "systemone.questions.v1",
    }


def test_public_route_reports_credential_state_without_material():
    route = _route(credential_resolution=CredentialResolution(
        CredentialResolutionState.RESOLVED, source="provider_vault",
    ))
    public = route.to_public_dict()
    assert public["credential_resolution"] == {"state": "resolved", "source": "provider_vault"}
    assert "top-secret" not in str(public)


def test_fallback_requires_a_retryable_failure_and_full_compatibility():
    requirements = OperationRequirements(operations=frozenset({Operation.DECISION}))
    retryable = AdapterFailure(AdapterFailureCode.TIMEOUT, "timed out", retryable=True)
    terminal = AdapterFailure(AdapterFailureCode.INVALID_RESPONSE, "bad response", retryable=False)
    assert fallback_eligible(_route(), requirements, retryable) is True
    assert fallback_eligible(_route(), requirements, terminal) is False
    assert fallback_eligible(_route(enabled=False), requirements, retryable) is False


def test_endpoint_validation_never_inferrs_protocol_from_hostname():
    assert validate_endpoint("https://openai.example/v1/") == "https://openai.example/v1"
    with pytest.raises(ContractValidationError):
        validate_endpoint("openai.example/v1")


def test_missing_provider_confidence_is_preserved_as_null():
    assert normalize_provider_confidence(None) is None
    assert normalize_provider_confidence(0.25) == 0.25
    with pytest.raises(ContractValidationError):
        normalize_provider_confidence(1.1)

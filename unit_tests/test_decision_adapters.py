"""Focused tests for decision-capable registry models and the OpenAI adapter."""
from unittest.mock import MagicMock, patch

from backend.tools.lib.decision_adapters import (
    OpenAIStructuredDecisionAdapter,
    public_decision_capabilities,
    validate_decision_compatibility,
)
from backend.tools.lib.decision_models import (
    DecisionErrorCode,
    parse_schema,
    validate_invocation,
)


def _invocation():
    return validate_invocation({
        "profile": {"key": "agent.goal_completion", "version": "1"},
        "schema": {"version": "decision-schema/v1", "fields": [
            {"key": "allowed", "kind": "boolean"},
            {"key": "action", "kind": "choice", "choices": ["allow", "block"]},
        ]},
    })


def _model(**overrides):
    model = {
        "id": "provider/model", "provider": "provider", "model_name": "model",
        "base_url": "https://example.invalid/v1", "api_key": "secret", "api_format": "openai",
        "enabled": 1, "timeout": 10,
        "capabilities": {"decision": {"structured_output": True,
            "schema_primitives": ["choice", "boolean", "score"],
            "candidate_scores": False, "provider_logits": False}},
    }
    model.update(overrides)
    return model


def test_public_capabilities_exclude_connection_credentials():
    payload = public_decision_capabilities(_model())
    assert payload["decision"]["structured_output"] is True
    assert "secret" not in str(payload)
    assert "base_url" not in payload


def test_compatibility_rejects_unsupported_schema_before_invocation():
    model = _model(capabilities={"decision": {"structured_output": True, "schema_primitives": ["choice"]}})
    try:
        validate_decision_compatibility(model, _invocation())
    except ValueError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("incompatible schema was accepted")


def test_openai_adapter_maps_structured_output_and_omits_unavailable_logits():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "choices": [{"message": {"content": '{"allowed": true, "action": "allow"}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }
    with patch("backend.tools.lib.decision_adapters.requests.post", return_value=response) as post:
        result = OpenAIStructuredDecisionAdapter(_model()).decide(_invocation())
    assert result.error is None
    assert result.fields[0].value == "allow"
    assert result.fields[0].provider_logits == ()
    assert result.fields[1].value is True
    assert result.provider.usage == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    assert result.timing.provider_ms is not None
    request_payload = post.call_args.kwargs["json"]
    assert request_payload["response_format"]["type"] == "json_schema"


def test_cancelled_request_does_not_call_provider():
    invocation = validate_invocation({
        "profile": {"key": "agent.goal_completion", "version": "1"},
        "schema": parse_schema({"version": "decision-schema/v1", "fields": [{"key": "allowed", "kind": "boolean"}]}).to_dict(),
        "cancellation": {"cancelled": True},
    })
    with patch("backend.tools.lib.decision_adapters.requests.post") as post:
        result = OpenAIStructuredDecisionAdapter(_model()).decide(invocation)
    assert result.error.code is DecisionErrorCode.CANCELLED
    post.assert_not_called()

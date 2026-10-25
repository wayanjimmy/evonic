"""Regression coverage for explicit, safe protocol route adapters."""

from backend.tools.lib.model_route_adapters import RouteAdapterRegistry
from backend.tools.lib.model_routing_contracts import (
    AdapterFailureCode, AdapterKey, CapabilityEvidence, CredentialResolution,
    CredentialResolutionState, ModelCapabilities, Operation, ProviderRoute,
    ResolvedModelRoute,
)


def _route(*, endpoint="https://cloud.example/v1", adapter=AdapterKey.OPENAI_CHAT_COMPLETIONS_V1,
           credential_state=CredentialResolutionState.RESOLVED):
    return ResolvedModelRoute(
        "provider/model", ProviderRoute("provider", endpoint, adapter), "native-model",
        ModelCapabilities({Operation.DECISION: CapabilityEvidence.VERIFIED,
                           Operation.STRICT_OUTPUT: CapabilityEvidence.VERIFIED}),
        credentials={"api_key": "must-not-leak"},
        credential_resolution=CredentialResolution(credential_state, "internal"),
    )


def test_openai_adapter_is_selected_independently_of_cloud_or_local_endpoint():
    registry = RouteAdapterRegistry()
    cloud = _route(endpoint="https://cloud.example/v1")
    local = _route(endpoint="http://127.0.0.1:8080/v1")
    assert registry.validate(cloud, Operation.DECISION) is None
    assert registry.validate(local, Operation.DECISION) is None


def test_route_adapter_reports_safe_typed_credential_failure():
    failure = RouteAdapterRegistry().validate(
        _route(credential_state=CredentialResolutionState.UNAVAILABLE), Operation.DECISION)
    assert failure and failure.code is AdapterFailureCode.CREDENTIAL_UNRESOLVED
    assert "must-not-leak" not in failure.message


def test_unregistered_adapter_is_a_typed_failure_without_route_details():
    registry = RouteAdapterRegistry({})
    failure = registry.validate(_route(), Operation.DECISION)
    assert failure and failure.code is AdapterFailureCode.UNKNOWN_ADAPTER
    assert "cloud.example" not in failure.message

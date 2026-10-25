"""Registry-backed protocol adapters for resolved model routes.

Only this module receives credential-bearing :class:`ResolvedModelRoute` objects.
Its failures are deliberately typed and contain no endpoint, credentials, or
provider response bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backend.tools.lib.model_routing_contracts import (
    AdapterFailure, AdapterFailureCode, AdapterKey, CredentialResolutionState,
    Operation, ResolvedModelRoute, validate_endpoint,
)


@dataclass(frozen=True)
class RouteAdapter:
    """Protocol metadata and request construction for one versioned wire format."""

    key: AdapterKey
    supported_operations: frozenset[Operation]
    auth_strategies: frozenset[str] = frozenset({"api_key", "none"})
    retry_safe_operations: frozenset[Operation] = frozenset()

    def validate(self, route: ResolvedModelRoute, operation: Operation) -> AdapterFailure | None:
        if operation not in self.supported_operations:
            return AdapterFailure(AdapterFailureCode.UNSUPPORTED_OPERATION, "The configured adapter does not support this operation.")
        try:
            validate_endpoint(route.provider.endpoint)
        except ValueError:
            return AdapterFailure(AdapterFailureCode.INVALID_ENDPOINT, "The configured provider endpoint is invalid.")
        if route.credential_resolution.state is CredentialResolutionState.UNAVAILABLE:
            return AdapterFailure(AdapterFailureCode.CREDENTIAL_UNRESOLVED, "Provider credentials could not be resolved.")
        return None

    def discovery_path(self) -> str:
        """Return the synthetic, prompt-free discovery probe for this protocol."""
        return "/tags" if self.key is AdapterKey.OLLAMA_CHAT_V1 else "/models"

    def parse_discovered_models(self, payload: Mapping[str, Any]) -> list[dict[str, str]]:
        """Normalize a model-list response without retaining provider response data."""
        if self.key is AdapterKey.OLLAMA_CHAT_V1:
            raw_models = payload.get("models", [])
            return [
                {"id": item.get("name", ""), "name": item.get("name", "")}
                for item in raw_models if isinstance(item, Mapping)
            ]
        raw_models = payload.get("models", payload.get("data", []))
        if not isinstance(raw_models, list):
            return []
        models = []
        for item in raw_models:
            if isinstance(item, str):
                models.append({"id": item, "name": item})
            elif isinstance(item, Mapping):
                model_id = item.get("id") or item.get("slug") or item.get("model") or item.get("name", "")
                models.append({"id": str(model_id), "name": str(model_id)})
        return models

    def model_config(self, route: ResolvedModelRoute, *, timeout: float) -> dict[str, Any]:
        """Lower an internal route into the legacy adapter's private input shape."""
        return {
            "id": route.model_id,
            "provider": route.provider.provider_id,
            "model_name": route.native_model_name,
            "base_url": route.provider.endpoint,
            "api_key": route.credentials.get("api_key"),
            "adapter_key": route.provider.adapter_key.value,
            "endpoint": route.provider.endpoint,
            "enabled": route.enabled and route.provider.enabled,
            "timeout": timeout,
            "capabilities": route.capabilities.to_dict(),
        }


class RouteAdapterRegistry:
    """Explicit adapter lookup; never infer protocol from hostname or locality."""

    def __init__(self, adapters: Mapping[AdapterKey, RouteAdapter] | None = None) -> None:
        # An explicitly empty mapping must stay empty; only ``None`` selects the
        # default registry.  Truthiness checks would silently re-add defaults.
        self._adapters = dict(default_route_adapters() if adapters is None else adapters)

    def get(self, key: AdapterKey) -> RouteAdapter | None:
        return self._adapters.get(key)

    def validate(self, route: ResolvedModelRoute, operation: Operation) -> AdapterFailure | None:
        adapter = self.get(route.provider.adapter_key)
        if adapter is None:
            return AdapterFailure(AdapterFailureCode.UNKNOWN_ADAPTER, "The configured protocol adapter is not registered.")
        return adapter.validate(route, operation)


def default_route_adapters() -> dict[AdapterKey, RouteAdapter]:
    """Supported protocol keys. Decision lowering is currently OpenAI-compatible."""
    chat = frozenset({Operation.CHAT, Operation.STREAMING, Operation.TOOLS, Operation.VISION, Operation.STRICT_OUTPUT})
    return {
        AdapterKey.OPENAI_CHAT_COMPLETIONS_V1: RouteAdapter(AdapterKey.OPENAI_CHAT_COMPLETIONS_V1, chat | {Operation.DECISION}, retry_safe_operations=frozenset({Operation.DECISION})),
        AdapterKey.OPENAI_RESPONSES_V1: RouteAdapter(AdapterKey.OPENAI_RESPONSES_V1, chat),
        AdapterKey.ANTHROPIC_MESSAGES_V1: RouteAdapter(AdapterKey.ANTHROPIC_MESSAGES_V1, chat),
        AdapterKey.OLLAMA_CHAT_V1: RouteAdapter(AdapterKey.OLLAMA_CHAT_V1, chat),
        AdapterKey.CODEX_RESPONSES_V1: RouteAdapter(AdapterKey.CODEX_RESPONSES_V1, chat),
        AdapterKey.SYSTEMONE_QUESTIONS_V1: RouteAdapter(AdapterKey.SYSTEMONE_QUESTIONS_V1, frozenset({Operation.DECISION})),
    }


DEFAULT_ROUTE_ADAPTER_REGISTRY = RouteAdapterRegistry()

__all__ = ["DEFAULT_ROUTE_ADAPTER_REGISTRY", "RouteAdapter", "RouteAdapterRegistry", "default_route_adapters"]

"""Read-only tool that lists the Image Generator providers an agent may use.

Only providers that are **available** (registered by the skill) and **enabled**
by an administrator are returned. Disabled providers are deliberately omitted so
an agent can never discover or select them by accident. The tool never contacts a
provider, never mutates configuration, and never returns credential values.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any, Dict, List, Mapping


def _load_providers():
    """Import the skill's ``providers`` package by file path.

    Evonic loads skill tools under a synthetic single-level package
    (``skill_tools_<skill>.<tool>``), so a relative import such as
    ``from ..providers import ...`` would fail with
    "attempted relative import beyond top-level package". Loading the package
    explicitly from disk keeps this tool self-contained and import-safe.
    """
    name = "skill_image_generator_providers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    directory = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "providers"
    )
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(directory, "__init__.py"),
        submodule_search_locations=[directory],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


_providers = _load_providers()
provider_registry = _providers.provider_registry
SafeErrorCode = _providers.SafeErrorCode


def _boolean(value: Any) -> bool:
    """Coerce stored configuration values (bool, str, number) to a strict bool."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _provider_enabled(provider_id: str, config: Mapping[str, Any]) -> bool:
    """Whether an administrator explicitly enabled the provider toggle.

    Toggle keys follow the manifest convention ``<provider_id>_enabled`` with
    dashes replaced by underscores (for example ``google-gemini`` becomes
    ``google_gemini_enabled``). Missing toggles default to disabled.
    """
    config_key = f"{provider_id.strip().replace('-', '_')}_enabled"
    return _boolean(config.get(config_key))


def _provider_usable(provider, config: Mapping[str, Any]) -> bool:
    """A provider is listable when it is registered *and* enabled.

    Enabling the provider is the only administrator gate; local providers need
    no separate opt-in.
    """
    return _provider_enabled(getattr(provider, "id", ""), config)


def _missing_required_config(provider, config: Mapping[str, Any]) -> List[str]:
    """Labels of required configuration fields that have no value yet.

    Only human-readable labels are returned. Credential values are never read
    into the response, so secrets cannot leak through this tool.
    """
    missing: List[str] = []
    for field in getattr(provider, "config_fields", ()) or ():
        if not getattr(field, "required", False):
            continue
        value = config.get(getattr(field, "name", ""))
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(getattr(field, "label", None) or getattr(field, "name", "configuration"))
    return missing


def _capabilities(provider) -> Dict[str, Any]:
    """Return the provider's declared generation capabilities (no secrets)."""
    capabilities = provider.capabilities
    return {
        "supported_sizes": list(capabilities.supported_sizes),
        "max_images_per_request": capabilities.max_images_per_request,
        "supported_output_formats": list(capabilities.supported_output_formats),
        "supported_models": list(capabilities.supported_models),
        "supports_negative_prompt": capabilities.supports_negative_prompt,
        "supports_seed": capabilities.supports_seed,
        "supports_style": capabilities.supports_style,
        "supports_transparency": capabilities.supports_transparency,
        "supports_reference_images": capabilities.supports_reference_images,
    }


def execute(agent: dict, args: dict) -> dict:
    """List the currently available and enabled image providers.

    Returns a success payload with one entry per usable provider, ordered by
    provider id. Disabled providers (and enabled-but-unavailable providers such
    as ones with no registered adapter) are omitted entirely.
    """
    try:
        from backend.skills_manager import skills_manager

        config = skills_manager.get_skill_config("image-generator")
        default_provider = str(config.get("default_provider") or "").strip()

        providers: List[Dict[str, Any]] = []
        for provider in provider_registry.list():
            if not _provider_usable(provider, config):
                continue
            missing = _missing_required_config(provider, config)
            providers.append(
                {
                    "id": provider.id,
                    "display_name": provider.display_name,
                    "local": bool(getattr(provider, "is_local", False)),
                    "is_default": provider.id == default_provider,
                    "configured": not missing,
                    "missing_config": missing,
                    "capabilities": _capabilities(provider),
                }
            )

        enabled_default = next(
            (entry["id"] for entry in providers if entry["is_default"]), None
        )

        result: Dict[str, Any] = {
            "status": "success",
            "skill": "image-generator",
            "default_provider": enabled_default,
            "count": len(providers),
            "providers": providers,
        }
        if not providers:
            result["message"] = (
                "No image providers are currently available and enabled. "
                "An administrator must enable one before images can be generated."
            )
        return result
    except Exception:
        return {
            "status": "error",
            "error": {
                "code": SafeErrorCode.PROVIDER_CONFIGURATION.value,
                "message": "Available image providers could not be listed.",
            },
        }

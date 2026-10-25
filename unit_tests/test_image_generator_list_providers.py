"""Coverage for the Image Generator ``list_providers`` tool backend.

The tool is a discovery surface: it must advertise exactly the providers that
are both **available** (registered in the skill registry) and **enabled** by an
administrator. Disabled and unregistered providers must never be listed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT_DIR / "skills" / "image-generator"
BACKEND_DIR = SKILL_DIR / "backend"


def _load_tool_module():
    """Load the skill tool under a unique package so its imports resolve."""
    package_name = "test_image_generator_list_providers_backend"
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]

    spec = importlib.util.spec_from_file_location(
        package_name,
        BACKEND_DIR / "__init__.py",
        submodule_search_locations=[str(BACKEND_DIR)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    assert spec.loader is not None
    spec.loader.exec_module(package)
    return __import__(f"{package_name}.tools.list_providers", fromlist=["execute"])


@pytest.fixture
def list_tool():
    return _load_tool_module()


def _set_config(monkeypatch, config):
    monkeypatch.setattr(
        "backend.skills_manager.skills_manager.get_skill_config",
        lambda _skill_id: dict(config),
    )


def test_lists_only_enabled_and_available_providers(list_tool, monkeypatch):
    _set_config(
        monkeypatch,
        {
            "default_provider": "openrouter",
            "openrouter_enabled": True,
            "google_gemini_enabled": False,
            "mock_enabled": False,
            "automatic1111_enabled": False,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    assert result["status"] == "success"
    assert result["default_provider"] == "openrouter"
    assert result["count"] == 1
    ids = [provider["id"] for provider in result["providers"]]
    assert ids == ["openrouter"]
    assert result["providers"][0]["is_default"] is True
    assert result["providers"][0]["local"] is False


def test_disabled_providers_are_never_listed(list_tool, monkeypatch):
    _set_config(
        monkeypatch,
        {
            "default_provider": "",
            "google_gemini_enabled": False,
            "openrouter_enabled": False,
            "mock_enabled": False,
            "automatic1111_enabled": False,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["providers"] == []
    assert result["default_provider"] is None
    assert "message" in result


def test_local_providers_are_listed_whenever_enabled(list_tool, monkeypatch):
    _set_config(
        monkeypatch,
        {
            "default_provider": "mock",
            "mock_enabled": True,
            "automatic1111_enabled": True,
            "google_gemini_enabled": False,
            "openrouter_enabled": False,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    # Enabling a provider is the only gate; local adapters need no extra opt-in.
    # automatic1111 lacks required endpoint configuration but is still advertised
    # with ``configured: false``.
    assert [provider["id"] for provider in result["providers"]] == ["automatic1111", "mock"]


def test_enabled_but_unregistered_provider_is_omitted(list_tool, monkeypatch):
    # A toggle for a provider that has no registered adapter cannot make it
    # available, so it must never be advertised.
    _set_config(
        monkeypatch,
        {
            "default_provider": "",
            "unregistered_provider_enabled": True,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    assert result["count"] == 0
    assert result["providers"] == []


def test_registered_comfyui_provider_is_listed_when_enabled(list_tool, monkeypatch):
    # ``comfyui`` is a registered local adapter: enabling it is the only gate.
    _set_config(
        monkeypatch,
        {
            "default_provider": "comfyui",
            "comfyui_enabled": True,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    assert [provider["id"] for provider in result["providers"]] == ["comfyui"]
    comfyui = result["providers"][0]
    assert comfyui["local"] is True
    assert comfyui["is_default"] is True
    assert comfyui["configured"] is False
    assert "ComfyUI Endpoint" in comfyui["missing_config"]
    assert comfyui["capabilities"]["supports_seed"] is True


def test_missing_required_config_is_reported_without_secrets(list_tool, monkeypatch):
    _set_config(
        monkeypatch,
        {
            "default_provider": "google-gemini",
            "google_gemini_enabled": True,
            "google_gemini_api_key": "",
        },
    )

    unconfigured = list_tool.execute({"id": "agent"}, {})
    provider = unconfigured["providers"][0]
    assert provider["id"] == "google-gemini"
    assert provider["configured"] is False
    assert "Google Gemini API Key" in provider["missing_config"]

    _set_config(
        monkeypatch,
        {
            "default_provider": "google-gemini",
            "google_gemini_enabled": True,
            "google_gemini_api_key": "secret-value",
        },
    )

    configured = list_tool.execute({"id": "agent"}, {})
    provider = configured["providers"][0]
    assert provider["configured"] is True
    assert provider["missing_config"] == []
    # The credential value must never appear anywhere in the payload.
    assert "secret-value" not in repr(configured)


def test_capabilities_and_stable_ordering(list_tool, monkeypatch):
    _set_config(
        monkeypatch,
        {
            "default_provider": "",
            "google_gemini_enabled": True,
            "openrouter_enabled": True,
            "mock_enabled": True,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    assert [provider["id"] for provider in result["providers"]] == [
        "google-gemini",
        "mock",
        "openrouter",
    ]
    google = result["providers"][0]
    capabilities = google["capabilities"]
    assert "1024x1024" in capabilities["supported_sizes"]
    assert capabilities["supported_output_formats"] == ["png", "jpeg"]
    assert capabilities["max_images_per_request"] == 1
    assert capabilities["supports_seed"] is False


def test_default_provider_hidden_when_disabled(list_tool, monkeypatch):
    _set_config(
        monkeypatch,
        {
            "default_provider": "mock",
            "mock_enabled": False,
            "google_gemini_enabled": True,
        },
    )

    result = list_tool.execute({"id": "agent"}, {})

    assert [provider["id"] for provider in result["providers"]] == ["google-gemini"]
    assert result["default_provider"] is None
    assert all(not provider["is_default"] for provider in result["providers"])

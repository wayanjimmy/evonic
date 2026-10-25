"""Contract tests for the fixed-host OpenRouter image-generation adapter."""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
import types

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT_DIR, "skills", "image-generator", "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from providers import ImageGenerationError, ImageGenerationRequest, SafeErrorCode, provider_registry
from providers.openrouter import OpenRouterProvider

_PNG = b"\x89PNG\r\n\x1a\nvalid"
_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 valid"


def _config(**overrides):
    config = {
        "openrouter_api_key": "test-key",
        "openrouter_model": "google/gemini-2.5-flash-image",
        "openrouter_allowed_models": "openai/gpt-image-1",
        "openrouter_timeout_seconds": 90,
    }
    config.update(overrides)
    return config


def _response(image: bytes = _PNG, media_type: str = "image/png", count: int = 1):
    encoded = base64.b64encode(image).decode("ascii")
    return {"data": [{"b64_json": encoded, "media_type": media_type} for _ in range(count)]}


class _Client:
    def __init__(self, captured, response):
        self.captured = captured
        self.response = response

    def json(self, method, path, payload=None, *, headers=None):
        self.captured.update(method=method, path=path, payload=payload, headers=headers)
        return self.response


def test_openrouter_provider_is_registered_as_a_cloud_provider():
    assert "openrouter" in [provider.id for provider in provider_registry.list()]
    assert OpenRouterProvider.id == "openrouter"
    assert OpenRouterProvider.is_local is False


def test_openrouter_sends_only_the_fixed_images_request(monkeypatch):
    provider = OpenRouterProvider()
    captured = {}
    monkeypatch.setattr(provider, "_client", lambda _config: _Client(captured, _response()))

    result = provider.generate(ImageGenerationRequest(prompt="A cake shop banner", size="1344x768"), _config())

    assert captured["method"] == "POST"
    assert captured["path"] == "/images"
    assert captured["payload"] == {
        "model": "google/gemini-2.5-flash-image",
        "prompt": "A cake shop banner",
        "aspect_ratio": "16:9",
    }
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert result.provider_id == "openrouter"
    assert result.model == "google/gemini-2.5-flash-image"
    assert result.artifacts[0].data == _PNG
    assert result.artifacts[0].filename == "openrouter-1.png"


@pytest.mark.parametrize("size,ratio", [
    ("1024x1024", None), ("1920x1080", "16:9"), ("768x1344", "9:16"), ("1152x864", "4:3"),
    ("864x1152", "3:4"), ("1248x832", "3:2"), ("832x1248", "2:3"), ("1080x1350", "4:5"), ("1350x1080", "5:4"),
])
def test_openrouter_maps_sizes_to_aspect_ratios(monkeypatch, size, ratio):
    provider = OpenRouterProvider()
    captured = {}
    monkeypatch.setattr(provider, "_client", lambda _config: _Client(captured, _response()))
    provider.generate(ImageGenerationRequest(prompt="x", size=size), _config())
    assert captured["payload"].get("aspect_ratio") == ratio


def test_openrouter_count_format_and_webp(monkeypatch):
    provider = OpenRouterProvider()
    captured = {}
    monkeypatch.setattr(provider, "_client", lambda _config: _Client(captured, _response(_WEBP, "image/webp", 3)))
    result = provider.generate(ImageGenerationRequest(prompt="x", count=3, output_format="webp"), _config())
    assert captured["payload"]["n"] == 3
    assert captured["payload"]["output_format"] == "webp"
    assert [artifact.filename for artifact in result.artifacts] == ["openrouter-1.webp", "openrouter-2.webp", "openrouter-3.webp"]


def test_openrouter_only_allows_approved_models(monkeypatch):
    provider = OpenRouterProvider()
    captured = {}
    monkeypatch.setattr(provider, "_client", lambda _config: _Client(captured, _response()))
    provider.generate(ImageGenerationRequest(prompt="x", model="openai/gpt-image-1"), _config())
    assert captured["payload"]["model"] == "openai/gpt-image-1"

    with pytest.raises(ImageGenerationError) as refused:
        provider.generate(ImageGenerationRequest(prompt="x", model="openai/gpt-image-2"), _config())
    assert refused.value.code is SafeErrorCode.INVALID_REQUEST


def test_openrouter_requires_administrator_credential_before_request(monkeypatch):
    provider = OpenRouterProvider()
    monkeypatch.setattr(provider, "_client", lambda _config: pytest.fail("request must not be attempted"))
    config = _config()
    config.pop("openrouter_api_key")
    with pytest.raises(ImageGenerationError) as error:
        provider.generate(ImageGenerationRequest(prompt="A bird"), config)
    assert error.value.code is SafeErrorCode.PROVIDER_CONFIGURATION


def test_openrouter_rejects_unsupported_or_mismatched_images():
    provider = OpenRouterProvider()
    with pytest.raises(ImageGenerationError) as svg:
        provider._artifacts({"data": [{"b64_json": base64.b64encode(b"<svg/>").decode(), "media_type": "image/svg+xml"}]})
    assert svg.value.code is SafeErrorCode.ARTIFACT_INVALID

    with pytest.raises(ImageGenerationError) as mismatch:
        provider._artifacts(_response(b"not-a-png"))
    assert mismatch.value.code is SafeErrorCode.ARTIFACT_INVALID

    with pytest.raises(ImageGenerationError) as empty:
        provider._artifacts({"data": []})
    assert empty.value.code is SafeErrorCode.ARTIFACT_INVALID


def test_generate_image_tool_loads_the_way_evonic_loads_skill_tools():
    """Evonic imports a skill tool as ``skill_tools_<skill>.<tool>`` (one package level)."""
    tool_path = os.path.join(BACKEND_DIR, "tools", "generate_image.py")
    package_name = "skill_tools_image-generator-test"
    package = types.ModuleType(package_name)
    package.__package__ = package_name
    package.__path__ = [os.path.dirname(tool_path)]
    sys.modules[package_name] = package
    try:
        spec = importlib.util.spec_from_file_location(f"{package_name}.generate_image", tool_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        assert "openrouter" in [provider.id for provider in module.provider_registry.list()]
    finally:
        sys.modules.pop(f"{package_name}.generate_image", None)
        sys.modules.pop(package_name, None)

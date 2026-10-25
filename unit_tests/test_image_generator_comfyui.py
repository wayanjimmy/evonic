"""Submission, injection, and safety tests for the ComfyUI image provider.

ComfyUI exposes an administrator-approved *workflow* rather than a fixed
text-to-image schema, so these tests pin down three things:

* the adapter only fills the provider-neutral prompt/seed/size fields into the
  approved graph and never accepts free-form graph input from an agent,
* it submits to ``POST /prompt``, polls ``GET /history/<id>`` and fetches the
  first output via ``GET /view``, and
* the endpoint stays behind the shared local-provider opt-in and trusted-host
  checks.
"""

import json
import os
import socket
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT_DIR, "skills", "image-generator", "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from providers import ImageGenerationError, ImageGenerationRequest, SafeErrorCode
from providers import comfyui as comfyui_module
from providers.comfyui import ComfyUiProvider
from providers.network import configured_endpoint

PNG = b"\x89PNG\r\n\x1a\nvalid-image-bytes"


def _config(**overrides):
    config = {
        "comfyui_endpoint": "http://127.0.0.1:8188",
        "comfyui_timeout_seconds": 600,
        "comfyui_polling_interval_seconds": 1,
    }
    config.update(overrides)
    return config


def _reference_workflow():
    """Mirror the broken shape of the production reference workflow."""
    return {
        "39": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen.safetensors"}},
        "268": {"class_type": "CLIPTextEncode", "inputs": {"text": "template", "clip": ["39", 0]}},
        "306": {"class_type": "easy seed", "inputs": {"seed": 1}},
        "455": {"class_type": "Lora Loader Stack (rgthree)", "inputs": {"clip": ["455", 1], "model": ["217", 0]}},
        "456": {"class_type": "Lora Loader Stack (rgthree)", "inputs": {"clip": ["455", 1], "model": ["455", 0]}},
        "457": {"class_type": "SaveImageExtended", "inputs": {"save_metadata": True, "images": ["456", 0]}},
        "458": {"class_type": "SaveImageExtended", "inputs": {"save_metadata": True, "images": ["456", 0]}},
        "348": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1280, "height": 960, "batch_size": 1}},
    }


def _generic_workflow():
    """A minimal standard workflow with no preferred node identifiers."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "template", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {"seed": 0, "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]},
        },
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "Evonic", "images": ["5", 0]}},
    }


def _history(images=None, status=None):
    entry = {"outputs": {"7": {"images": images if images is not None else [{"filename": "Evonic_00001_.png", "subfolder": "", "type": "output"}]}}}
    if status is not None:
        entry["status"] = status
    return {"job-1": entry}


class _Client:
    """Stand-in for BoundedHttpClient that records every request."""

    def __init__(self, history, content_type="image/png", data=PNG):
        self.calls = []
        self._history = history
        self._content_type = content_type
        self._data = data

    def json(self, method, path, payload=None, *, headers=None):
        self.calls.append({"method": method, "path": path, "payload": payload, "headers": dict(headers or {})})
        if path == "/prompt":
            return {"prompt_id": "job-1"}
        if path.startswith("/history/"):
            return self._history
        raise AssertionError(f"unexpected json path: {path}")

    def request(self, method, path, *, body=None, headers=None, max_bytes=None):
        self.calls.append({"method": method, "path": path, "payload": None, "headers": dict(headers or {})})
        return self._data, self._content_type


@pytest.fixture
def install_workflow(tmp_path, monkeypatch):
    """Install a workflow template and point the provider at its directory."""

    def _install(workflow, name="default"):
        (tmp_path / f"{name}.json").write_text(json.dumps(workflow), encoding="utf-8")
        monkeypatch.setattr(ComfyUiProvider, "_workflow_directory", staticmethod(lambda: str(tmp_path)))

    return _install


def _generate(provider, client, request, config=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(provider, "_client", lambda _config: client)
    return provider.generate(request, config or _config())


def test_comfyui_submits_approved_workflow_and_fetches_output(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_reference_workflow())
    client = _Client(_history())

    result = _generate(
        provider,
        client,
        ImageGenerationRequest(prompt="a lighthouse at night", size="768x1344", seed=7),
        monkeypatch=monkeypatch,
    )

    submit = client.calls[0]
    assert submit["method"] == "POST" and submit["path"] == "/prompt"
    submitted = submit["payload"]["prompt"]
    assert isinstance(submit["payload"]["client_id"], str) and submit["payload"]["client_id"]

    # Prompt and seed land on the approved nodes.
    assert submitted["268"]["inputs"]["text"] == "a lighthouse at night"
    assert submitted["306"]["inputs"]["seed"] == 7
    # Requested dimensions land on the discoverable latent node.
    assert (submitted["348"]["inputs"]["width"], submitted["348"]["inputs"]["height"]) == (768, 1344)
    # Documented workflow quirks are repaired before submission.
    assert submitted["455"]["inputs"]["clip"] == ["39", 0]
    assert submitted["456"]["inputs"]["clip"] == ["39", 0]
    assert submitted["457"]["inputs"]["save_metadata"] is False
    assert submitted["458"]["inputs"]["save_metadata"] is False

    # The first output image is fetched through the same-origin /view path.
    view = client.calls[-1]
    assert view["method"] == "GET"
    assert view["path"].startswith("/view?")
    assert "Evonic_00001_.png" in view["path"]

    assert result.provider_id == "comfyui"
    assert len(result.artifacts) == 1
    assert result.artifacts[0].data == PNG
    assert result.artifacts[0].mime_type == "image/png"


def test_comfyui_discovers_prompt_and_seed_in_a_generic_workflow(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    template = _generic_workflow()
    install_workflow(template)
    client = _Client(_history())

    _generate(
        provider,
        client,
        ImageGenerationRequest(prompt="an autumn forest", size="512x512", seed=42),
        monkeypatch=monkeypatch,
    )

    submitted = client.calls[0]["payload"]["prompt"]
    # The prompt follows the sampler's positive conditioning edge.
    assert submitted["2"]["inputs"]["text"] == "an autumn forest"
    # Seed is discovered on the sampler when no preferred seed node exists.
    assert submitted["5"]["inputs"]["seed"] == 42
    assert (submitted["4"]["inputs"]["width"], submitted["4"]["inputs"]["height"]) == (512, 512)
    # The approved template object is never mutated in place.
    assert template["2"]["inputs"]["text"] == "template"
    assert template["5"]["inputs"]["seed"] == 0


def test_comfyui_generates_a_random_seed_when_none_requested(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_reference_workflow())
    client = _Client(_history())

    _generate(provider, client, ImageGenerationRequest(prompt="a red kite"), monkeypatch=monkeypatch)

    seed = client.calls[0]["payload"]["prompt"]["306"]["inputs"]["seed"]
    assert isinstance(seed, int)
    assert 0 <= seed < 2 ** 53


def test_comfyui_submits_once_per_requested_image(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_reference_workflow())
    client = _Client(_history())

    result = _generate(
        provider, client, ImageGenerationRequest(prompt="a fox", count=2, seed=5), monkeypatch=monkeypatch
    )

    submits = [call for call in client.calls if call["path"] == "/prompt"]
    assert len(submits) == 2
    assert len(result.artifacts) == 2


def test_comfyui_missing_workflow_template_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(ComfyUiProvider, "_workflow_directory", staticmethod(lambda: str(tmp_path)))

    with pytest.raises(ImageGenerationError) as error:
        ComfyUiProvider._load_workflow(_config(comfyui_workflow_template="does-not-exist"))

    assert error.value.code is SafeErrorCode.PROVIDER_CONFIGURATION


def test_comfyui_rejects_unsafe_template_names(install_workflow):
    install_workflow(_reference_workflow())

    for name in ("../escape", "nested/name", "bad\\name", ".hidden"):
        with pytest.raises(ImageGenerationError) as error:
            ComfyUiProvider._load_workflow(_config(comfyui_workflow_template=name))
        assert error.value.code is SafeErrorCode.PROVIDER_CONFIGURATION

    # A blank template name intentionally falls back to the bundled default.
    assert ComfyUiProvider._load_workflow(_config(comfyui_workflow_template="")) == _reference_workflow()


def test_comfyui_loads_a_template_name_containing_spaces(tmp_path, monkeypatch):
    # Administrators routinely name templates with spaces; the sanitizer must
    # allow them while still blocking path separators (see the rejection test).
    (tmp_path / "KREA2-TURBO v2.json").write_text(json.dumps(_reference_workflow()), encoding="utf-8")
    monkeypatch.setattr(ComfyUiProvider, "_workflow_directory", staticmethod(lambda: str(tmp_path)))

    workflow = ComfyUiProvider._load_workflow(_config(comfyui_workflow_template="KREA2-TURBO v2"))

    assert "268" in workflow


def test_comfyui_sends_api_key_as_bearer_header(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_reference_workflow())
    client = _Client(_history())

    _generate(
        provider,
        client,
        ImageGenerationRequest(prompt="a blue whale"),
        config=_config(comfyui_api_key="top-secret-value"),
        monkeypatch=monkeypatch,
    )

    assert client.calls
    assert all(call["headers"].get("Authorization") == "Bearer top-secret-value" for call in client.calls)


def test_comfyui_reports_execution_errors(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_reference_workflow())
    client = _Client(_history(status={"status_str": "error"}))

    with pytest.raises(ImageGenerationError) as error:
        _generate(provider, client, ImageGenerationRequest(prompt="a boat"), monkeypatch=monkeypatch)

    assert error.value.code is SafeErrorCode.GENERATION_FAILED


def test_comfyui_reports_timeout_without_completing(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_reference_workflow())
    client = _Client({})

    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = _Clock()
    monkeypatch.setattr(comfyui_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(comfyui_module.time, "sleep", clock.sleep)

    with pytest.raises(ImageGenerationError) as error:
        _generate(
            provider,
            client,
            ImageGenerationRequest(prompt="a slow render"),
            config=_config(comfyui_timeout_seconds=5),
            monkeypatch=monkeypatch,
        )

    assert error.value.code is SafeErrorCode.PROVIDER_UNAVAILABLE


def test_comfyui_local_endpoint_is_accepted_without_extra_gates(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 0))])

    # A private/HTTP administrator endpoint is allowed directly: enabling the
    # provider is the only gate, with no opt-in flag or trusted-host list.
    endpoint = configured_endpoint(_config(comfyui_timeout_seconds=60), "comfyui", local=True)
    assert endpoint.base_url == "http://127.0.0.1:8188"
    assert endpoint.allow_private_network is True
    assert ComfyUiProvider()._client(_config()) is not None


def test_comfyui_requires_a_configured_endpoint():
    with pytest.raises(ImageGenerationError) as error:
        configured_endpoint({"comfyui_endpoint": ""}, "comfyui", local=True)
    assert error.value.code is SafeErrorCode.PROVIDER_CONFIGURATION


def _linked_prompt_workflow():
    """A graph whose encoder prompt is linked through a user-prompt primitive."""
    return {
        "30:3": {
            "class_type": "KSampler",
            "inputs": {"seed": 0, "positive": ["30:6", 0], "negative": ["30:13", 0], "latent_image": ["30:5", 0]},
        },
        "30:5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "30:6": {"class_type": "CLIPTextEncode", "inputs": {"text": ["30:28", 0], "clip": ["30:11", 0]}},
        "30:18": {
            "class_type": "PrimitiveStringMultiline",
            "_meta": {"title": "Text String (System Prompt)"},
            "inputs": {"value": "system prompt text"},
        },
        "30:19": {
            "class_type": "PrimitiveStringMultiline",
            "_meta": {"title": "Text String (User Prompt)"},
            "inputs": {"value": "template"},
        },
    }


def test_comfyui_injects_prompt_into_user_prompt_primitive(install_workflow, monkeypatch):
    provider = ComfyUiProvider()
    install_workflow(_linked_prompt_workflow())
    client = _Client(_history())

    _generate(
        provider,
        client,
        ImageGenerationRequest(prompt="a starlit lake", size="768x1344", seed=11),
        monkeypatch=monkeypatch,
    )

    submitted = client.calls[0]["payload"]["prompt"]
    # The prompt is written to the node titled as the user prompt ...
    assert submitted["30:19"]["inputs"]["value"] == "a starlit lake"
    # ... the system prompt is preserved ...
    assert submitted["30:18"]["inputs"]["value"] == "system prompt text"
    # ... and the linked encoder text is left untouched.
    assert submitted["30:6"]["inputs"]["text"] == ["30:28", 0]
    assert submitted["30:5"]["inputs"]["width"] == 768
    assert submitted["30:3"]["inputs"]["seed"] == 11


def test_comfyui_disables_save_image_extended_metadata():
    # SaveImageExtended raises KeyError('workflow') when saving metadata via the
    # API; the fix must track the node class, not a hard-coded node id.
    workflow = {
        "30:6": {"class_type": "CLIPTextEncode", "inputs": {"text": "template", "clip": ["30:11", 0]}},
        "51": {"class_type": "SaveImageExtended", "inputs": {"save_metadata": True, "images": ["30:8", 0]}},
    }

    prepared = ComfyUiProvider._prepare_workflow(workflow, ImageGenerationRequest(prompt="x", size="512x512"), 1)

    assert prepared["51"]["inputs"]["save_metadata"] is False
    assert workflow["51"]["inputs"]["save_metadata"] is True  # source untouched


def test_comfyui_capabilities_describe_a_local_workflow_provider():
    provider = ComfyUiProvider()

    assert provider.id == "comfyui"
    assert provider.is_local is True
    assert provider.capabilities.supports_seed is True
    assert provider.capabilities.supports_negative_prompt is False
    assert provider.capabilities.max_images_per_request >= 1
    assert "png" in provider.capabilities.supported_output_formats
    assert {"768x1024", "768x1344", "832x1248", "1024x768", "1344x768", "1248x832"} <= set(
        provider.capabilities.supported_sizes
    )

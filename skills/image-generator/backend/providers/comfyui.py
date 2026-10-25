"""Approved local ComfyUI image provider.

ComfyUI exposes a queue-based API rather than a fixed text-to-image schema, so
this adapter intentionally accepts **no** agent input beyond the provider-neutral
prompt/seed/size fields.  The graph it submits is an administrator-approved
workflow template that lives on the Evonic host; the adapter only fills in the
prompt, seed and (when discoverable) latent dimensions.

Security posture:

* The endpoint is validated by :func:`configured_endpoint` with ``local=True``,
  which permits a private/HTTP administrator endpoint before any request is sent.
* Every outbound call goes through :class:`BoundedHttpClient`, so responses are
  size-capped, redirects are refused, and paths cannot escape the approved base
  URL.
* Credentials are read from administrator-only configuration and are never
  logged or echoed in error messages.
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from .base import (
    ImageArtifact,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProvider,
    ProviderCapabilities,
    ProviderConfigField,
    SafeErrorCode,
)
from .network import BoundedHttpClient, configured_endpoint

# Each HTTP round-trip is deliberately short; the administrator-configured
# ``comfyui_timeout_seconds`` instead bounds the overall asynchronous job, which
# lets a slow render queue exceed the shared per-request timeout without ever
# holding a single socket open that long.
_REQUEST_TIMEOUT_SECONDS = 60
_DEFAULT_JOB_TIMEOUT_SECONDS = 600
_MAX_JOB_TIMEOUT_SECONDS = 1_800
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_MIN_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_INTERVAL_SECONDS = 10.0

_DEFAULT_WORKFLOW_TEMPLATE = "default"
_WORKFLOW_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows")
# Workflow template names are administrator-supplied, so restrict them to a safe
# filename shape before they are resolved to a path on disk.
# Administrators commonly name templates with spaces (for example
# "KREA2-TURBO v2"), so internal spaces are allowed; path separators,
# parent-directory segments, and leading dots are not.
_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
# Node seeds are typically 64-bit; keeping requests well inside that range keeps
# the workflow JSON round-trippable through ComfyUI's JSON API.
_MAX_SEED = 2 ** 53

# Well-known node identifiers from the reference production workflow.  They are
# only *preferred* hints: if a template omits them the adapter falls back to
# structural discovery so administrator-supplied workflows still work.
_PREFERRED_PROMPT_NODE = "268"
_PREFERRED_SEED_NODE = "306"
_CLIP_SOURCE_NODE = "39"
_SELF_REFERENCING_CLIP_NODES = ("455", "456")


class ComfyUiProvider(ImageProvider):
    """Local ComfyUI provider driven by an approved workflow template."""

    id = "comfyui"
    display_name = "ComfyUI"
    is_local = True
    capabilities = ProviderCapabilities(
        supported_sizes=(
            "512x512",
            "768x768",
            "1024x1024",
            "768x1024",
            "768x1344",
            "832x1248",
            "1024x768",
            "1344x768",
            "1248x832",
        ),
        max_images_per_request=4,
        supported_output_formats=("png",),
        supports_negative_prompt=False,
        supports_seed=True,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField(
            name="comfyui_endpoint",
            label="ComfyUI Endpoint",
            required=True,
            description="Base URL for the approved local ComfyUI API.",
        ),
        ProviderConfigField(
            name="comfyui_workflow_template",
            label="ComfyUI Workflow Template",
            default=_DEFAULT_WORKFLOW_TEMPLATE,
            description="Name of the administrator-approved workflow template (a JSON file under this provider's workflows directory) used for image generation.",
        ),
        ProviderConfigField(
            name="comfyui_timeout_seconds",
            label="ComfyUI Job Timeout (seconds)",
            type="number",
            default=_DEFAULT_JOB_TIMEOUT_SECONDS,
            description="Maximum time to wait for a ComfyUI job to finish, from 1 through 1800 seconds.",
        ),
        ProviderConfigField(
            name="comfyui_polling_interval_seconds",
            label="ComfyUI Polling Interval (seconds)",
            type="number",
            default=_DEFAULT_POLL_INTERVAL_SECONDS,
            description="Interval used to check asynchronous ComfyUI jobs, from 1 through 10 seconds.",
        ),
        ProviderConfigField(
            name="comfyui_api_key",
            label="ComfyUI API Key",
            type="secret",
            secret=True,
            description="Optional credential for ComfyUI deployments that require API authentication.",
        ),
    )

    # -- configuration --------------------------------------------------

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        """Build the bounded client from administrator-only configuration."""
        settings = dict(config)
        # ``configured_endpoint`` reads ``<prefix>_timeout_seconds`` as the
        # per-request timeout; present a bounded value so the administrator can
        # still configure a long job timeout without exceeding the HTTP cap.
        settings["comfyui_timeout_seconds"] = _REQUEST_TIMEOUT_SECONDS
        return BoundedHttpClient(configured_endpoint(settings, "comfyui", local=True))

    @staticmethod
    def _headers(config: Mapping[str, Any]) -> Mapping[str, str]:
        """Return fixed authentication headers, or none when unconfigured."""
        credential = config.get("comfyui_api_key")
        if isinstance(credential, str) and credential.strip():
            return {"Authorization": f"Bearer {credential.strip()}"}
        return {}

    @staticmethod
    def _job_timeout(config: Mapping[str, Any]) -> int:
        try:
            value = int(config.get("comfyui_timeout_seconds", _DEFAULT_JOB_TIMEOUT_SECONDS))
        except (TypeError, ValueError) as exc:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI job timeout must be an integer."
            ) from exc
        if not 1 <= value <= _MAX_JOB_TIMEOUT_SECONDS:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI job timeout is outside the permitted range."
            )
        return value

    @staticmethod
    def _poll_interval(config: Mapping[str, Any]) -> float:
        try:
            value = float(config.get("comfyui_polling_interval_seconds", _DEFAULT_POLL_INTERVAL_SECONDS))
        except (TypeError, ValueError) as exc:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI polling interval must be a number."
            ) from exc
        if not _MIN_POLL_INTERVAL_SECONDS <= value <= _MAX_POLL_INTERVAL_SECONDS:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI polling interval is outside the permitted range."
            )
        return value

    # -- workflow loading ----------------------------------------------

    @staticmethod
    def _workflow_directory() -> str:
        """Directory holding administrator-approved workflow templates."""
        return _WORKFLOW_DIRECTORY

    @classmethod
    def _load_workflow(cls, config: Mapping[str, Any]) -> Dict[str, Any]:
        """Load and validate the approved workflow template for this deployment."""
        template = config.get("comfyui_workflow_template") or _DEFAULT_WORKFLOW_TEMPLATE
        if not isinstance(template, str):
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI workflow template name is invalid."
            )
        name = template.strip()
        if not name or not _TEMPLATE_NAME.fullmatch(name) or ".." in name:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI workflow template name is invalid."
            )
        filename = name if name.endswith(".json") else f"{name}.json"

        directory = os.path.realpath(cls._workflow_directory())
        path = os.path.realpath(os.path.join(directory, filename))
        # Defense in depth: the sanitized name must still resolve inside the
        # template directory before anything is read from disk.
        if os.path.dirname(path) != directory:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI workflow template name is invalid."
            )
        try:
            with open(path, encoding="utf-8") as handle:
                workflow = json.load(handle)
        except FileNotFoundError as exc:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The configured ComfyUI workflow template is not available."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The configured ComfyUI workflow template could not be read."
            ) from exc
        if not isinstance(workflow, dict) or not workflow:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The configured ComfyUI workflow template is not a valid workflow."
            )
        return workflow

    # -- workflow preparation ------------------------------------------

    @classmethod
    def _prepare_workflow(
        cls, workflow: Mapping[str, Any], request: ImageGenerationRequest, seed: int
    ) -> Dict[str, Any]:
        """Return an isolated workflow copy tuned for a single submission."""
        prepared = copy.deepcopy(dict(workflow))
        cls._apply_known_quirks(prepared)
        prompt_inputs, prompt_key = cls._prompt_inputs(prepared)
        prompt_inputs[prompt_key] = request.prompt
        seed_inputs = cls._seed_inputs(prepared, required=request.seed is not None)
        if seed_inputs is not None:
            seed_inputs["seed"] = seed
        latent_inputs = cls._latent_inputs(prepared)
        if latent_inputs is not None:
            width, height = cls._dimensions(request.size)
            latent_inputs["width"] = width
            latent_inputs["height"] = height
        return prepared

    @staticmethod
    def _dimensions(size: str) -> tuple[int, int]:
        width, _, height = str(size).partition("x")
        try:
            return int(width), int(height)
        except ValueError as exc:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The ComfyUI workflow requires a valid image size."
            ) from exc

    @staticmethod
    def _apply_known_quirks(workflow: Dict[str, Any]) -> None:
        """Repair documented ComfyUI quirks before submission.

        Each fix is a no-op unless the exact broken shape is present, so
        unrelated templates are left untouched.
        """
        for node_id in _SELF_REFERENCING_CLIP_NODES:
            node = workflow.get(node_id)
            inputs = node.get("inputs") if isinstance(node, Mapping) else None
            clip = inputs.get("clip") if isinstance(inputs, dict) else None
            if not isinstance(clip, (list, tuple)) or not clip or _CLIP_SOURCE_NODE not in workflow:
                continue
            target_id = str(clip[0])
            target = workflow.get(target_id)
            target_class = str(target.get("class_type") or "").lower() if isinstance(target, Mapping) else ""
            # The reference workflow chains its Lora loaders so node 455 points
            # at itself and node 456 points at 455; both break submission until
            # their ``clip`` input is routed to the real CLIP loader.  Only
            # redirect references that resolve to another Lora loader, so a
            # template that already points at its CLIP source is left untouched.
            if target_id == node_id or "lora" in target_class:
                inputs["clip"] = [_CLIP_SOURCE_NODE, 0]
        # SaveImageExtended (common in community workflows) raises
        # ``KeyError: 'workflow'`` when it records metadata through the API, so
        # disable metadata saving on every instance of that node class -- not on
        # a fixed set of node ids.
        for node in workflow.values():
            if not isinstance(node, Mapping):
                continue
            inputs = node.get("inputs")
            class_type = str(node.get("class_type") or "").lower()
            if isinstance(inputs, dict) and "saveimageextended" in class_type and "save_metadata" in inputs:
                inputs["save_metadata"] = False

    @staticmethod
    def _prompt_inputs(workflow: Mapping[str, Any]) -> tuple[Dict[str, Any], str]:
        """Locate the node and input key that receive the user prompt."""
        preferred = workflow.get(_PREFERRED_PROMPT_NODE)
        if isinstance(preferred, Mapping):
            inputs = preferred.get("inputs")
            if isinstance(inputs, dict) and isinstance(inputs.get("text"), str):
                return inputs, "text"
        # Follow the first sampler's positive conditioning edge.
        for node in workflow.values():
            if not isinstance(node, Mapping):
                continue
            inputs = node.get("inputs")
            if "ksampler" not in str(node.get("class_type") or "").lower() or not isinstance(inputs, Mapping):
                continue
            reference = inputs.get("positive")
            if isinstance(reference, (list, tuple)) and reference:
                target = workflow.get(str(reference[0]))
                target_inputs = target.get("inputs") if isinstance(target, Mapping) else None
                if isinstance(target_inputs, dict) and isinstance(target_inputs.get("text"), str):
                    return target_inputs, "text"
        # Fall back to the first text-encoding node carrying a literal prompt.
        for node in workflow.values():
            if not isinstance(node, Mapping):
                continue
            inputs = node.get("inputs")
            class_type = str(node.get("class_type") or "").lower()
            if isinstance(inputs, dict) and isinstance(inputs.get("text"), str) and (
                "encode" in class_type or "text" in class_type or "prompt" in class_type
            ):
                return inputs, "text"
        # Some templates (for example the Krea-2 graph) feed the positive prompt
        # through a string primitive, so the encoder text is a link rather than a
        # literal.  Fall back to that primitive when it is unambiguous.
        return ComfyUiProvider._user_prompt_primitive(workflow)

    @staticmethod
    def _user_prompt_primitive(workflow: Mapping[str, Any]) -> tuple[Dict[str, Any], str]:
        """Locate a writable user-prompt string primitive.

        Only a node titled as the user prompt (or an unambiguous single string
        node) is selected, so a system-prompt primitive is never overwritten.
        """
        candidates: List[tuple[Dict[str, Any], str, str]] = []
        for node in workflow.values():
            if not isinstance(node, Mapping):
                continue
            inputs = node.get("inputs")
            class_type = str(node.get("class_type") or "").lower()
            if not isinstance(inputs, dict) or "string" not in class_type:
                continue
            if isinstance(inputs.get("value"), str):
                key = "value"
            elif isinstance(inputs.get("text"), str):
                key = "text"
            else:
                continue
            meta = node.get("_meta")
            title = str(meta.get("title") or "").lower() if isinstance(meta, Mapping) else ""
            candidates.append((inputs, key, title))
        for inputs, key, title in candidates:
            if "user prompt" in title or "user" in title:
                return inputs, key
        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1]
        raise ImageGenerationError(
            SafeErrorCode.PROVIDER_CONFIGURATION, "The configured ComfyUI workflow has no prompt input to fill."
        )

    @staticmethod
    def _seed_inputs(workflow: Mapping[str, Any], *, required: bool) -> Optional[Dict[str, Any]]:
        """Locate the node whose ``inputs.seed`` should be set, if any."""
        preferred = workflow.get(_PREFERRED_SEED_NODE)
        if isinstance(preferred, Mapping):
            inputs = preferred.get("inputs")
            if isinstance(inputs, dict) and "seed" in inputs:
                return inputs
        for node in workflow.values():
            if not isinstance(node, Mapping):
                continue
            inputs = node.get("inputs")
            class_type = str(node.get("class_type") or "").lower()
            if isinstance(inputs, dict) and "seed" in inputs and (
                "ksampler" in class_type or "seed" in class_type or "noise" in class_type
            ):
                return inputs
        for node in workflow.values():
            inputs = node.get("inputs") if isinstance(node, Mapping) else None
            if isinstance(inputs, dict) and "seed" in inputs:
                return inputs
        if required:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION, "The configured ComfyUI workflow cannot accept a seed."
            )
        return None

    @staticmethod
    def _latent_inputs(workflow: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Locate a latent-image node exposing ``width``/``height``, if any."""
        candidates = []
        for node in workflow.values():
            if not isinstance(node, Mapping):
                continue
            inputs = node.get("inputs")
            class_type = str(node.get("class_type") or "").lower()
            if isinstance(inputs, dict) and "width" in inputs and "height" in inputs:
                candidates.append((class_type, inputs))
        for class_type, inputs in candidates:
            if "latent" in class_type or "empty" in class_type:
                return inputs
        return candidates[0][1] if candidates else None

    @staticmethod
    def _seed_for(requested: Optional[int], index: int) -> int:
        """Return a per-image seed, varying it across a multi-image request."""
        if requested is None:
            return random.randrange(0, _MAX_SEED)
        return (int(requested) + index) % _MAX_SEED

    # -- ComfyUI API ----------------------------------------------------

    def _submit(self, client: BoundedHttpClient, workflow: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        """Queue the prepared workflow and return its prompt identifier."""
        response = client.json(
            "POST",
            "/prompt",
            {"prompt": dict(workflow), "client_id": str(uuid.uuid4())},
            headers=headers,
        )
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "ComfyUI did not accept the workflow.")
        return prompt_id

    @staticmethod
    def _await_outputs(
        client: BoundedHttpClient,
        prompt_id: str,
        headers: Mapping[str, str],
        timeout_seconds: int,
        interval_seconds: float,
    ) -> Mapping[str, Any]:
        """Poll ``/history`` until the job yields outputs or the deadline passes."""
        deadline = time.monotonic() + timeout_seconds
        path = f"/history/{prompt_id}"
        while True:
            try:
                history = client.json("GET", path, headers=headers)
            except ImageGenerationError as exc:
                # A transient connection blip should not abort a slow render.
                if exc.code is not SafeErrorCode.PROVIDER_UNAVAILABLE:
                    raise
                history = {}
            entry = history.get(prompt_id) if isinstance(history, Mapping) else None
            if isinstance(entry, Mapping):
                status = entry.get("status")
                if isinstance(status, Mapping) and str(status.get("status_str") or "").lower() == "error":
                    raise ImageGenerationError(
                        SafeErrorCode.GENERATION_FAILED, "ComfyUI failed to execute the workflow."
                    )
                if isinstance(entry.get("outputs"), Mapping):
                    return entry
            if time.monotonic() >= deadline:
                raise ImageGenerationError(
                    SafeErrorCode.PROVIDER_UNAVAILABLE, "ComfyUI did not finish the job within the configured timeout."
                )
            time.sleep(interval_seconds)

    @staticmethod
    def _output_images(entry: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        """Flatten the history entry's produced image descriptors."""
        outputs = entry.get("outputs")
        images: List[Mapping[str, Any]] = []
        if not isinstance(outputs, Mapping):
            return images
        for node_output in outputs.values():
            if not isinstance(node_output, Mapping):
                continue
            produced = node_output.get("images")
            if not isinstance(produced, (list, tuple)):
                continue
            for image in produced:
                if isinstance(image, Mapping) and isinstance(image.get("filename"), str) and image["filename"]:
                    images.append(image)
        return images

    @staticmethod
    def _view_path(image: Mapping[str, Any]) -> str:
        query = urlencode(
            {
                "filename": str(image.get("filename") or ""),
                "subfolder": str(image.get("subfolder") or ""),
                "type": str(image.get("type") or "output"),
            }
        )
        return f"/view?{query}"

    @classmethod
    def _artifact(cls, image: Mapping[str, Any], data: bytes, content_type: str, index: int) -> ImageArtifact:
        mime_type = str(content_type or "").split(";")[0].strip().lower()
        if mime_type == "image/jpg":
            mime_type = "image/jpeg"
        if mime_type not in _EXTENSION_BY_MIME:
            extension = os.path.splitext(str(image.get("filename") or ""))[1].lower()
            mime_type = next((known for known, ext in _EXTENSION_BY_MIME.items() if ext == extension), "")
        if mime_type not in _EXTENSION_BY_MIME:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "ComfyUI returned an unsupported image type.")
        filename = cls._filename(image.get("filename"), _EXTENSION_BY_MIME[mime_type], index)
        return ImageArtifact(mime_type=mime_type, filename=filename, data=data)

    @staticmethod
    def _filename(raw: Any, extension: str, index: int) -> str:
        candidate = os.path.basename(raw) if isinstance(raw, str) else ""
        if not candidate or not _SAFE_FILENAME.fullmatch(candidate):
            return f"comfyui-{index + 1}{extension}"
        if not candidate.lower().endswith(extension):
            candidate = f"{os.path.splitext(candidate)[0]}{extension}"
        return candidate

    # -- public API -----------------------------------------------------

    def generate(self, request: ImageGenerationRequest, config: Mapping[str, Any]) -> ImageGenerationResult:
        """Submit one job per requested image and fetch the first output of each."""
        client = self._client(config)
        workflow = self._load_workflow(config)
        headers = self._headers(config)
        timeout_seconds = self._job_timeout(config)
        interval_seconds = self._poll_interval(config)

        artifacts: List[ImageArtifact] = []
        for index in range(request.count):
            seed = self._seed_for(request.seed, index)
            prepared = self._prepare_workflow(workflow, request, seed)
            prompt_id = self._submit(client, prepared, headers)
            entry = self._await_outputs(client, prompt_id, headers, timeout_seconds, interval_seconds)
            images = self._output_images(entry)
            if not images:
                raise ImageGenerationError(
                    SafeErrorCode.GENERATION_FAILED, "ComfyUI completed the job without returning an image."
                )
            image = images[0]
            data, content_type = client.request("GET", self._view_path(image), headers=headers)
            artifacts.append(self._artifact(image, data, content_type, index))
        return ImageGenerationResult(provider_id=self.id, artifacts=tuple(artifacts), model=request.model or None)

    def test_connection(self, config: Mapping[str, Any]) -> None:
        """Verify the endpoint answers without generating an image."""
        self._client(config).json("GET", "/system_stats", headers=self._headers(config))

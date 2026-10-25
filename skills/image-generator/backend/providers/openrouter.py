"""Fixed-host OpenRouter image-generation provider.

Uses OpenRouter's Images API (``POST /api/v1/images``).  The API host, path and
authentication header are adapter-controlled; agents may only pick a model from
the administrator's allowlist, and a WIDTHxHEIGHT size is sent as an aspect
ratio.  Images come back base64-encoded in ``data[].b64_json``.
"""
from __future__ import annotations

import base64
from typing import Any, Mapping, Sequence

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
from .network import BoundedHttpClient, SafeEndpoint, bounded_timeout

_OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "google/gemini-2.5-flash-image"
_MAX_IMAGES = 4

# Sizes the tool accepts and the aspect ratio each one is sent as.  The model
# picks the exact pixel size for that ratio.
_RATIO_BY_SIZE = {
    "512x512": "1:1", "768x768": "1:1", "1024x1024": "1:1",
    "1024x576": "16:9", "1344x768": "16:9", "1920x1080": "16:9",
    "576x1024": "9:16", "768x1344": "9:16", "1080x1920": "9:16",
    "1024x768": "4:3", "1152x864": "4:3",
    "768x1024": "3:4", "864x1152": "3:4",
    "1200x800": "3:2", "1248x832": "3:2",
    "800x1200": "2:3", "832x1248": "2:3",
    "1080x1350": "4:5",
    "1350x1080": "5:4",
}
_SIZES = tuple(_RATIO_BY_SIZE)
_MIME_BY_FORMAT = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_EXTENSION_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}


def _model_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


class OpenRouterProvider(ImageProvider):
    """Generate images through OpenRouter with an administrator-approved model."""

    id = "openrouter"
    display_name = "OpenRouter image generation"
    capabilities = ProviderCapabilities(
        supported_sizes=_SIZES,
        max_images_per_request=_MAX_IMAGES,
        supported_output_formats=tuple(_MIME_BY_FORMAT),
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField(
            "openrouter_api_key", "OpenRouter API Key", required=True, secret=True,
            description="OpenRouter API key. It is write-only and never returned to agents.",
        ),
        ProviderConfigField(
            "openrouter_model", "OpenRouter Model", default=_DEFAULT_MODEL,
            description="Image model used when the agent does not pick one, e.g. google/gemini-2.5-flash-image.",
        ),
        ProviderConfigField(
            "openrouter_allowed_models", "Other Allowed OpenRouter Models",
            description="Comma-separated model IDs an agent may request besides the default.",
        ),
        ProviderConfigField(
            "openrouter_timeout_seconds", "OpenRouter Timeout (seconds)", type="number", default=90,
            description="Bounded request timeout from 1 through 120 seconds.",
        ),
    )

    @staticmethod
    def _credential(config: Mapping[str, Any]) -> str:
        value = config.get("openrouter_api_key")
        if not isinstance(value, str) or not value.strip():
            raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "OpenRouter requires an administrator-configured credential.")
        return value.strip()

    @staticmethod
    def _model(request: ImageGenerationRequest, config: Mapping[str, Any]) -> str:
        default = config.get("openrouter_model")
        default = default.strip() if isinstance(default, str) and default.strip() else _DEFAULT_MODEL
        if not request.model:
            return default
        if request.model == default or request.model in _model_list(config.get("openrouter_allowed_models")):
            return request.model
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected OpenRouter model is not approved.")

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(SafeEndpoint(
            base_url=_OPENROUTER_API_BASE_URL,
            timeout_seconds=bounded_timeout(config.get("openrouter_timeout_seconds", 90)),
        ))

    @staticmethod
    def _headers(credential: str) -> Mapping[str, str]:
        return {
            "Authorization": f"Bearer {credential}",
            "HTTP-Referer": "https://tiyasancloud.com",
            "X-Title": "TiyasanCloud",
        }

    def test_connection(self, config: Mapping[str, Any]) -> None:
        """Verify the key against OpenRouter's key endpoint (no image is generated)."""
        self._client(config).json("GET", "/key", headers=self._headers(self._credential(config)))

    @staticmethod
    def _artifacts(response: Mapping[str, Any]) -> tuple[ImageArtifact, ...]:
        items = response.get("data")
        if not isinstance(items, list):
            if response.get("error"):
                raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider rejected the image request.")
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid image response.")
        artifacts: list[ImageArtifact] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            mime_type = str(item.get("media_type") or "image/png").lower()
            extension = _EXTENSION_BY_MIME.get(mime_type)
            if not extension:
                raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an unsupported image type.")
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                try:
                    data = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid image artifact.") from exc
                if not data.startswith(_MAGIC[mime_type]):
                    raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid image artifact.")
                artifacts.append(ImageArtifact(
                    mime_type=mime_type,
                    filename=f"openrouter-{len(artifacts) + 1}.{extension}",
                    data=data,
                    revised_prompt=item.get("revised_prompt") if isinstance(item.get("revised_prompt"), str) else None,
                ))
            elif isinstance(item.get("url"), str) and item["url"].startswith("https://"):
                artifacts.append(ImageArtifact(
                    mime_type=mime_type,
                    filename=f"openrouter-{len(artifacts) + 1}.{extension}",
                    url=item["url"],
                ))
        if not artifacts:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned no image artifacts.")
        return tuple(artifacts)

    def generate(self, request: ImageGenerationRequest, config: Mapping[str, Any]) -> ImageGenerationResult:
        credential = self._credential(config)
        model = self._model(request, config)
        payload: dict[str, Any] = {"model": model, "prompt": request.prompt}
        if request.count > 1:
            payload["n"] = request.count
        ratio = _RATIO_BY_SIZE[request.size]
        if ratio != "1:1":
            payload["aspect_ratio"] = ratio
        if request.output_format and request.output_format != "png":
            payload["output_format"] = request.output_format
        response = self._client(config).json("POST", "/images", payload, headers=self._headers(credential))
        artifacts = self._artifacts(response)
        if len(artifacts) > request.count:
            artifacts = artifacts[: request.count]
        warnings: tuple[str, ...] = ()
        if len(artifacts) < request.count:
            warnings = (f"The model returned {len(artifacts)} of {request.count} requested images.",)
        return ImageGenerationResult(provider_id=self.id, artifacts=artifacts, model=model, warnings=warnings)

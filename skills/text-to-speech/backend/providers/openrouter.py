"""Fixed-host OpenRouter text-to-speech provider (``POST /api/v1/audio/speech``).

Host, path and auth header are adapter-controlled.  Agents may only pick a model
from the administrator's allowlist.  Voices are model-specific, so any
well-formed voice ID is passed through.  ``mp3`` is requested first; models that
only offer ``pcm`` (e.g. Gemini TTS) are retried with it and the raw 16-bit mono
PCM is wrapped as WAV.  OpenRouter does not document the PCM sample rate, so
24 kHz (native for Gemini and OpenAI) is assumed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .base import (
    AudioArtifact,
    ProviderCapabilities,
    ProviderConfigField,
    SafeErrorCode,
    SpeechGenerationError,
    SpeechProvider,
    SpeechRequest,
    SpeechResult,
)
from .google import _pcm_to_wav
from .network import BoundedHttpClient, SafeEndpoint, bounded_timeout

_API_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "hexgrad/kokoro-82m"
_DEFAULT_VOICE = "am_michael"
_VOICE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
# Personas offered in the settings dropdown.  Agents may use these, the
# configured default voice, and any admin-listed "Other Allowed Voices".
_PERSONAS = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_nova", "af_sky", "am_michael", "am_fenrir",
    "am_puck", "bf_emma", "bm_george", "bm_fable",
    "en_paul_neutral",
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
)


def _model_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


class OpenRouterSpeechProvider(SpeechProvider):
    """Synthesize speech through OpenRouter with an administrator-approved model."""

    id = "openrouter"
    display_name = "OpenRouter text to speech"
    capabilities = ProviderCapabilities(
        supported_output_formats=("mp3", "wav"),
        max_text_length=4_000,
        supports_speed=True,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField("openrouter_api_key", "OpenRouter API Key", required=True, secret=True,
                            description="OpenRouter API key. Write-only."),
        ProviderConfigField("openrouter_model", "OpenRouter TTS Model", default=_DEFAULT_MODEL),
        ProviderConfigField("openrouter_voice", "Default Voice", default=_DEFAULT_VOICE),
        ProviderConfigField("openrouter_allowed_models", "Other Allowed OpenRouter Models"),
        ProviderConfigField("openrouter_allowed_voices", "Other Allowed Voices"),
        ProviderConfigField("openrouter_timeout_seconds", "Timeout (seconds)", type="number", default=180),
    )

    @staticmethod
    def _credential(config: Mapping[str, Any]) -> str:
        value = config.get("openrouter_api_key")
        if not isinstance(value, str) or not value.strip():
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "OpenRouter requires an administrator-configured credential.")
        return value.strip()

    @staticmethod
    def _model(request: SpeechRequest, config: Mapping[str, Any]) -> str:
        default = config.get("openrouter_model")
        default = default.strip() if isinstance(default, str) and default.strip() else _DEFAULT_MODEL
        if not request.model or request.model == default:
            return default
        if request.model in _model_list(config.get("openrouter_allowed_models")):
            return request.model
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected OpenRouter model is not approved.")

    @staticmethod
    def _voice(request: SpeechRequest, config: Mapping[str, Any]) -> str:
        default = config.get("openrouter_voice")
        default = default.strip() if isinstance(default, str) and default.strip() else _DEFAULT_VOICE
        voice = request.voice or default
        approved = (*_PERSONAS, default, *_model_list(config.get("openrouter_allowed_voices")))
        if voice not in approved or not _VOICE_PATTERN.fullmatch(voice):
            names = ", ".join(dict.fromkeys(approved))
            raise SpeechGenerationError(
                SafeErrorCode.INVALID_REQUEST,
                f"The selected voice is not approved. Approved voices: {names}.",
            )
        return voice

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(SafeEndpoint(_API_BASE_URL, bounded_timeout(config.get("openrouter_timeout_seconds"))))

    @staticmethod
    def _headers(credential: str) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {credential}"}

    def test_connection(self, config: Mapping[str, Any]) -> None:
        """Verify the key against OpenRouter's key endpoint (no audio is generated)."""
        self._client(config).json("GET", "/key", headers=self._headers(self._credential(config)))

    def synthesize(self, request: SpeechRequest, config: Mapping[str, Any]) -> SpeechResult:
        credential = self._credential(config)
        model = self._model(request, config)
        voice = self._voice(request, config)
        payload: dict[str, Any] = {"model": model, "input": request.text, "voice": voice}
        if request.speed is not None:
            payload["speed"] = request.speed
        if request.output_format == "wav":
            return self._request_pcm(payload, credential, config, model, voice)
        try:
            data, content_type = self._post(payload, "mp3", credential, config)
        except SpeechGenerationError as exc:
            if exc.code is not SafeErrorCode.GENERATION_FAILED or "pcm" not in exc.message.lower():
                raise
            result = self._request_pcm(payload, credential, config, model, voice)
            return SpeechResult(
                provider_id=result.provider_id, artifact=result.artifact, model=model, voice=voice,
                warnings=("This model only offers PCM; returned WAV (assumed 24 kHz) instead of MP3.",),
            )
        if content_type != "audio/mpeg":
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider did not return audio.")
        return SpeechResult(
            provider_id=self.id,
            artifact=AudioArtifact("audio/mpeg", "openrouter.mp3", data),
            model=model,
            voice=voice,
        )

    def _post(self, payload: dict[str, Any], response_format: str, credential: str, config: Mapping[str, Any]) -> tuple[bytes, str]:
        return self._client(config).request(
            "POST", "/audio/speech",
            body=json.dumps({**payload, "response_format": response_format}).encode("utf-8"),
            headers={**self._headers(credential), "Content-Type": "application/json", "Accept": "audio/*"},
        )

    def _request_pcm(self, payload: dict[str, Any], credential: str, config: Mapping[str, Any], model: str, voice: str) -> SpeechResult:
        data, content_type = self._post(payload, "pcm", credential, config)
        if not content_type.startswith("audio/") or len(data) < 2:
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider did not return audio.")
        return SpeechResult(
            provider_id=self.id,
            artifact=AudioArtifact("audio/wav", "openrouter.wav", _pcm_to_wav(data[: len(data) // 2 * 2])),
            model=model,
            voice=voice,
        )

"""Fixed-host ElevenLabs text-to-speech provider (``POST /v1/text-to-speech/{voice_id}``).

Host, path and auth header are adapter-controlled.  Agents may only pick an
approved model and an approved voice ID (dropdown voices, the configured
default, or admin-listed "Other Allowed Voices").  WAV is produced by requesting
24 kHz 16-bit mono PCM and wrapping it.  Eleven v3/v4 have no instructions
field; expressiveness comes from inline audio tags such as ``[whispers]`` in the
text itself.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import quote

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

_API_BASE_URL = "https://api.elevenlabs.io"
_DEFAULT_MODEL = "eleven_v4"
_MODELS = (_DEFAULT_MODEL, "eleven_v4_turbo", "eleven_v3", "eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5")
_DEFAULT_VOICE = "o5s6XRBkPSTD4syv6mZg"
_VOICE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
_LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}$")
# Voices offered in the settings dropdown (library voices need a paid plan).
_VOICES = (
    "o5s6XRBkPSTD4syv6mZg", "RWiGLY9uXI70QL540WNd", "q8qwd1jY2jS3AWOBeq25", "I7sakys8pBZ1Z5f0UhT9",
    "Xb7hH8MSUJpSbSDYk0k2", "JBFqnCBsd6RMkjVDRZzb", "EXAVITQu4vr4xnSDxMaL", "pNInz6obpgDQGcFmaJgB",
    "onwK4e9ZLuTAKqWW03F9",
)
_PCM_RATE = 24_000
# ElevenLabs only accepts voice_settings.speed within this range.
_MIN_SPEED, _MAX_SPEED = 0.7, 1.2


def _id_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


class ElevenLabsSpeechProvider(SpeechProvider):
    """Synthesize speech with an approved ElevenLabs model and voice."""

    id = "elevenlabs"
    display_name = "ElevenLabs"
    capabilities = ProviderCapabilities(
        supported_models=_MODELS,
        supported_output_formats=("mp3", "wav"),
        max_text_length=4_000,
        supports_speed=True,
        supports_instructions=False,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField("elevenlabs_api_key", "ElevenLabs API Key", required=True, secret=True,
                            description="ElevenLabs API key. Write-only."),
        ProviderConfigField("elevenlabs_model", "ElevenLabs TTS Model", default=_DEFAULT_MODEL, choices=_MODELS),
        ProviderConfigField("elevenlabs_voice", "Default Voice", default=_DEFAULT_VOICE),
        ProviderConfigField("elevenlabs_language_code", "Language Code"),
        ProviderConfigField("elevenlabs_allowed_voices", "Other Allowed Voices"),
        ProviderConfigField("elevenlabs_timeout_seconds", "Timeout (seconds)", type="number", default=120),
    )

    @staticmethod
    def _credential(config: Mapping[str, Any]) -> str:
        value = config.get("elevenlabs_api_key")
        if not isinstance(value, str) or not value.strip():
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "ElevenLabs requires an administrator-configured credential.")
        return value.strip()

    @staticmethod
    def _model(request: SpeechRequest, config: Mapping[str, Any]) -> str:
        default = config.get("elevenlabs_model")
        model = request.model or (default.strip() if isinstance(default, str) and default.strip() else _DEFAULT_MODEL)
        if model not in _MODELS:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected ElevenLabs model is not approved.")
        return model

    @staticmethod
    def _voice(request: SpeechRequest, config: Mapping[str, Any]) -> str:
        default = config.get("elevenlabs_voice")
        default = default.strip() if isinstance(default, str) and default.strip() else _DEFAULT_VOICE
        voice = request.voice or default
        approved = (*_VOICES, default, *_id_list(config.get("elevenlabs_allowed_voices")))
        if voice not in approved or not _VOICE_PATTERN.fullmatch(voice):
            names = ", ".join(dict.fromkeys(approved))
            raise SpeechGenerationError(
                SafeErrorCode.INVALID_REQUEST,
                f"The selected voice is not approved. Approved voices: {names}.",
            )
        return voice

    @staticmethod
    def _language_code(config: Mapping[str, Any]) -> str:
        value = config.get("elevenlabs_language_code")
        value = value.strip().lower() if isinstance(value, str) else ""
        if value and not _LANGUAGE_PATTERN.fullmatch(value):
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "ElevenLabs language code must be an ISO 639-1 code such as 'id'.")
        return value

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(SafeEndpoint(_API_BASE_URL, bounded_timeout(config.get("elevenlabs_timeout_seconds") or 120)))

    def test_connection(self, config: Mapping[str, Any]) -> None:
        """Verify the key against the models endpoint (no audio is generated)."""
        self._client(config).request("GET", "/v1/models", headers={"xi-api-key": self._credential(config)}, max_bytes=2 * 1024 * 1024)

    def synthesize(self, request: SpeechRequest, config: Mapping[str, Any]) -> SpeechResult:
        credential = self._credential(config)
        model = self._model(request, config)
        voice = self._voice(request, config)
        payload: dict[str, Any] = {"text": request.text, "model_id": model}
        language = self._language_code(config)
        if language:
            payload["language_code"] = language
        if request.speed is not None:
            payload["voice_settings"] = {"speed": min(max(request.speed, _MIN_SPEED), _MAX_SPEED)}
        wav = request.output_format == "wav"
        output_format = f"pcm_{_PCM_RATE}" if wav else "mp3_44100_128"
        data, content_type = self._client(config).request(
            "POST", f"/v1/text-to-speech/{quote(voice, safe='')}?output_format={output_format}",
            body=json.dumps(payload).encode("utf-8"),
            headers={"xi-api-key": credential, "Content-Type": "application/json", "Accept": "audio/*"},
        )
        if wav:
            if len(data) < 2 or content_type in {"application/json", "text/html", "text/plain"}:
                raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider did not return audio.")
            artifact = AudioArtifact("audio/wav", "elevenlabs.wav", _pcm_to_wav(data[: len(data) // 2 * 2], _PCM_RATE))
        else:
            if not content_type.startswith("audio/"):
                raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider did not return audio.")
            artifact = AudioArtifact("audio/mpeg", "elevenlabs.mp3", data)
        return SpeechResult(provider_id=self.id, artifact=artifact, model=model, voice=voice)

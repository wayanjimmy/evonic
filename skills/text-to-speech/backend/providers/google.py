"""Fixed-host Google Gemini text-to-speech provider.

Gemini TTS returns raw 24 kHz mono 16-bit PCM in ``inlineData``; it is wrapped
into a WAV container here.  Host, path, auth header and the model/voice
allowlists are adapter-controlled.
"""
from __future__ import annotations

import base64
import io
import wave
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
from .network import BoundedHttpClient, SafeEndpoint, bounded_timeout

_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
_MODELS = (_DEFAULT_MODEL, "gemini-2.5-pro-preview-tts")
_DEFAULT_VOICE = "Kore"
_VOICES = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)
_SAMPLE_RATE = 24_000


def _pcm_to_wav(pcm: bytes, rate: int = _SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class GoogleGeminiSpeechProvider(SpeechProvider):
    """Synthesize speech with an approved Gemini TTS model."""

    id = "google-gemini"
    display_name = "Google Gemini text to speech"
    capabilities = ProviderCapabilities(
        supported_voices=_VOICES,
        supported_models=_MODELS,
        supported_output_formats=("wav",),
        max_text_length=4_000,
        supports_instructions=True,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField("google_gemini_api_key", "Google Gemini API Key", required=True, secret=True,
                            description="Google Generative Language API key. Write-only."),
        ProviderConfigField("google_gemini_model", "Google Gemini TTS Model", default=_DEFAULT_MODEL, choices=_MODELS),
        ProviderConfigField("google_gemini_voice", "Default Voice", default=_DEFAULT_VOICE, choices=_VOICES),
        ProviderConfigField("google_gemini_timeout_seconds", "Timeout (seconds)", type="number", default=60),
    )

    @staticmethod
    def _credential(config: Mapping[str, Any]) -> str:
        value = config.get("google_gemini_api_key")
        if not isinstance(value, str) or not value.strip():
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Google Gemini requires an administrator-configured credential.")
        return value.strip()

    @staticmethod
    def _model(request: SpeechRequest, config: Mapping[str, Any]) -> str:
        model = request.model or config.get("google_gemini_model") or _DEFAULT_MODEL
        if model not in _MODELS:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected Google Gemini model is not approved.")
        return model

    @staticmethod
    def _voice(request: SpeechRequest, config: Mapping[str, Any]) -> str:
        voice = request.voice or config.get("google_gemini_voice") or _DEFAULT_VOICE
        if voice not in _VOICES:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected voice is not supported by Google Gemini.")
        return voice

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(SafeEndpoint(_API_BASE_URL, bounded_timeout(config.get("google_gemini_timeout_seconds"))))

    def test_connection(self, config: Mapping[str, Any]) -> None:
        model = self._model(SpeechRequest(text="connection test"), config)
        self._client(config).json("GET", f"/models/{model}", headers={"x-goog-api-key": self._credential(config)})

    def synthesize(self, request: SpeechRequest, config: Mapping[str, Any]) -> SpeechResult:
        credential = self._credential(config)
        model = self._model(request, config)
        voice = self._voice(request, config)
        text = f"{request.instructions}: {request.text}" if request.instructions else request.text
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
            },
        }
        response = self._client(config).json("POST", f"/models/{model}:generateContent", payload, headers={"x-goog-api-key": credential})
        feedback = response.get("promptFeedback")
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            raise SpeechGenerationError(SafeErrorCode.CONTENT_REJECTED, "The provider rejected the text.")
        for candidate in response.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("finishReason") in {"SAFETY", "RECITATION", "BLOCKLIST"}:
                raise SpeechGenerationError(SafeErrorCode.CONTENT_REJECTED, "The provider rejected the text.")
            content = candidate.get("content")
            for part in (content.get("parts") if isinstance(content, Mapping) else None) or []:
                inline = part.get("inlineData") if isinstance(part, Mapping) else None
                if not isinstance(inline, Mapping) or not isinstance(inline.get("data"), str):
                    continue
                mime = str(inline.get("mimeType") or "")
                if not mime.startswith("audio/"):
                    raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid audio artifact.")
                try:
                    pcm = base64.b64decode(inline["data"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid audio artifact.") from exc
                rate = _SAMPLE_RATE
                if "rate=" in mime:
                    try:
                        rate = int(mime.split("rate=", 1)[1].split(";")[0])
                    except ValueError:
                        pass
                return SpeechResult(
                    provider_id=self.id,
                    artifact=AudioArtifact("audio/wav", "google-gemini.wav", _pcm_to_wav(pcm, rate)),
                    model=model,
                    voice=voice,
                )
        raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned no audio.")

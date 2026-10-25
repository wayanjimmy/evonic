"""Synthesize and persist validated audio artifacts for the Text to Speech skill."""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from io import BytesIO
from typing import Any, Iterator, Mapping
import hashlib
import importlib.util
import logging
import os
import re
import sys
import threading
import time
import uuid
import wave


def _load_providers():
    """Import the skill's providers package by path.

    Evonic loads this file as skill_tools_<skill>.generate_speech (a single
    package level), so relative imports beyond it fail.
    """
    name = "skill_text_to_speech_providers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    directory = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "providers")
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(directory, "__init__.py"), submodule_search_locations=[directory],
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
AudioArtifact = _providers.AudioArtifact
SpeechGenerationError = _providers.SpeechGenerationError
SpeechRequest = _providers.SpeechRequest
SafeErrorCode = _providers.SafeErrorCode
provider_registry = _providers.provider_registry

_MAX_TEXT_LENGTH = 4_000
_MAX_INSTRUCTIONS_LENGTH = 500
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
_ACTIVE_REQUESTS: dict[str, int] = defaultdict(int)
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EXTENSION_BY_MIME_TYPE = {"audio/wav": ".wav", "audio/mpeg": ".mp3"}
_DAY_SECONDS = 86_400


def _error(code, message: str) -> dict:
    # `error` stays a plain string: the chat UI renders it directly.
    return {"status": "error", "error": message, "error_code": code.value}


def _agent_key(agent: Mapping[str, Any]) -> str:
    for field in ("id", "agent_id", "name"):
        value = agent.get(field)
        if isinstance(value, (str, int)) and str(value):
            return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return "unknown-agent"


def _audit_event(agent: Mapping[str, Any], outcome: str, *, provider: str | None = None, characters: int = 0) -> None:
    """Emit minimal telemetry without text, credentials, or URLs."""
    logging.getLogger(__name__).info(
        "text_to_speech outcome=%s agent=%s provider=%s characters=%d",
        outcome, _agent_key(agent), provider or "none", characters,
    )


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _positive_int(value: Any, default: int, field: str, *, maximum: int | None = None) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be an integer.") from exc
    if parsed < 1:
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be at least 1.")
    if maximum is not None and parsed > maximum:
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} exceeds the permitted limit.")
    return parsed


@contextmanager
def _request_budget(agent: Mapping[str, Any], config: Mapping[str, Any], characters: int) -> Iterator[None]:
    """Apply in-process per-agent request, character, and concurrency limits."""
    key = _agent_key(agent)
    max_requests = _positive_int(config.get("requests_per_minute"), 10, "requests_per_minute", maximum=60)
    max_characters = _positive_int(config.get("characters_per_day"), 50_000, "characters_per_day", maximum=10_000_000)
    max_concurrent = _positive_int(config.get("max_concurrent_requests"), 1, "max_concurrent_requests", maximum=4)
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[key]
        while bucket and now - bucket[0][0] >= _DAY_SECONDS:
            bucket.popleft()
        recent = sum(1 for stamp, _ in bucket if now - stamp < 60)
        if _ACTIVE_REQUESTS[key] >= max_concurrent:
            raise SpeechGenerationError(SafeErrorCode.RATE_LIMITED, "Too many speech requests are already running for this agent.")
        if recent >= max_requests:
            raise SpeechGenerationError(SafeErrorCode.RATE_LIMITED, "Speech request limit exceeded; retry later.")
        if sum(chars for _, chars in bucket) + characters > max_characters:
            raise SpeechGenerationError(SafeErrorCode.QUOTA_EXCEEDED, "Daily speech character quota exceeded; retry later.")
        _ACTIVE_REQUESTS[key] += 1
        # Account at admission so failed provider calls cannot evade cost controls.
        bucket.append((now, characters))
    try:
        yield
    finally:
        with _RATE_LOCK:
            _ACTIVE_REQUESTS[key] = max(0, _ACTIVE_REQUESTS[key] - 1)


def _optional_text(args: Mapping[str, Any], field: str, max_length: int) -> str | None:
    value = args.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be text.")
    value = value.strip()
    if not value:
        return None
    if len(value) > max_length:
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} is too long.")
    return value


_OVERRIDE_FIELDS = ("provider", "model", "voice")


def _apply_override_policy(args: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[str]]:
    """Ignore agent-chosen provider/model/voice unless the administrator allows it."""
    if _boolean(config.get("allow_agent_overrides")):
        return args, []
    ignored = [field for field in _OVERRIDE_FIELDS if args.get(field)]
    if not ignored:
        return args, []
    kept = {key: value for key, value in args.items() if key not in ignored}
    return kept, [f"{', '.join(ignored)} ignored: the administrator's configured provider, model and voice are always used."]


def _normalize_request(args: Mapping[str, Any], config: Mapping[str, Any]) -> SpeechRequest:
    limit = min(_positive_int(config.get("max_text_length"), _MAX_TEXT_LENGTH, "max_text_length"), _MAX_TEXT_LENGTH)
    text = _optional_text(args, "text", limit)
    if not text:
        raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "Text to speak is required.")

    speed = args.get("speed")
    if speed is not None:
        if isinstance(speed, bool):
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "speed must be a number.")
        try:
            speed = float(speed)
        except (TypeError, ValueError) as exc:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "speed must be a number.") from exc
        if not 0.25 <= speed <= 4.0:
            raise SpeechGenerationError(SafeErrorCode.INVALID_REQUEST, "speed must be between 0.25 and 4.0.")

    output_format = (_optional_text(args, "output_format", 16) or "").lower().lstrip(".")
    return SpeechRequest(
        text=text,
        voice=_optional_text(args, "voice", 64),
        model=_optional_text(args, "model", 128),
        output_format=output_format,  # empty = provider default, resolved below
        speed=speed,
        instructions=_optional_text(args, "instructions", _MAX_INSTRUCTIONS_LENGTH),
    )


def _resolve_provider(args: Mapping[str, Any], config: Mapping[str, Any]):
    explicit = _optional_text(args, "provider", 128)
    default = str(config.get("default_provider") or "").strip() or None
    provider = provider_registry.resolve(explicit, default)
    if not _boolean(config.get(f"{provider.id.replace('-', '_')}_enabled")):
        raise SpeechGenerationError(SafeErrorCode.PROVIDER_DISABLED, "The selected speech provider is not enabled for this skill.")
    if provider.is_local and not _boolean(config.get("allow_local_providers")):
        raise SpeechGenerationError(SafeErrorCode.PROVIDER_DISABLED, "Local speech providers are not enabled for this skill.")
    return provider


def _adapt_to_provider(request: SpeechRequest, provider) -> tuple[SpeechRequest, list[str]]:
    """Apply the provider's default format and drop unsupported style hints.

    ``speed`` and ``instructions`` only tune delivery, so an unsupported one is
    ignored with a warning instead of failing the whole request.
    """
    from dataclasses import replace
    caps = provider.capabilities
    changes: dict[str, Any] = {}
    warnings: list[str] = []
    if not request.output_format:
        changes["output_format"] = caps.supported_output_formats[0]
    if request.speed is not None and not caps.supports_speed:
        changes["speed"] = None
        warnings.append("speed was ignored: the selected provider does not support it.")
    if request.instructions and not caps.supports_instructions:
        changes["instructions"] = None
        warnings.append("instructions were ignored: the selected provider does not support them.")
    return (replace(request, **changes) if changes else request), warnings


def _validate_audio(artifact: AudioArtifact) -> tuple[bytes, str, float | None]:
    """Verify the bytes really are the declared audio type; return duration if known."""
    data = artifact.data
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Generated audio exceeds the maximum artifact size.")
    mime = artifact.mime_type.lower()
    if mime == "audio/wav":
        try:
            with wave.open(BytesIO(data), "rb") as wav:
                rate, frames = wav.getframerate(), wav.getnframes()
        except (wave.Error, EOFError) as exc:
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned undecodable audio data.") from exc
        if rate <= 0 or frames <= 0:
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned empty audio.")
        return data, mime, round(frames / rate, 2)
    if mime == "audio/mpeg":
        if not (data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)):
            raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Generated audio MIME type does not match its content.")
        return data, mime, None
    raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsupported audio MIME type.")


def _validated_filename(filename: str, mime_type: str) -> str:
    if not _SAFE_FILENAME.fullmatch(filename) or ".." in filename:
        raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsafe audio filename.")
    stem, _ = os.path.splitext(filename)
    # Provider filenames are fixed per provider; make each artifact unique.
    return f"speech-{stem}-{uuid.uuid4().hex[:8]}{_EXTENSION_BY_MIME_TYPE[mime_type]}"


def _persist_audio(agent: Mapping[str, Any], filename: str, data: bytes) -> str:
    from backend.tools.save_artifact import _artifacts_dir, _chown_to_run_as, _resolve_run_as_user
    from backend.tools._workspace import effective_agent_id

    agent_id = effective_agent_id(dict(agent))
    if not agent_id:
        raise SpeechGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Agent context has no artifact destination.")
    directory = _artifacts_dir(agent_id, _resolve_run_as_user(dict(agent)))
    filepath = os.path.join(directory, filename)
    with open(filepath, "xb") as artifact_file:
        artifact_file.write(data)
    _chown_to_run_as(filepath, _resolve_run_as_user(dict(agent)))
    return filename


def execute(agent: dict, args: dict) -> dict:
    """Synthesize speech through one configured provider and return artifact metadata."""
    characters = 0
    try:
        from backend.skills_manager import skills_manager

        config = skills_manager.get_skill_config("text-to-speech")
        args, locked = _apply_override_policy(args, config)
        request = _normalize_request(args, config)
        characters = len(request.text)
        provider = _resolve_provider(args, config)
        request, warnings = _adapt_to_provider(request, provider)
        warnings = [*locked, *warnings]
        with _request_budget(agent, config, characters):
            result = provider_registry.synthesize(request, config, provider_id=provider.id)
            data, mime_type, duration = _validate_audio(result.artifact)
            filename = _validated_filename(result.artifact.filename, mime_type)
            stored = _persist_audio(agent, filename, data)
        _audit_event(agent, "success", provider=result.provider_id, characters=characters)
        artifact = {"filename": stored, "mime_type": mime_type, "size": len(data)}
        if duration is not None:
            artifact["duration_seconds"] = duration
        response = {
            "status": "success",
            "provider": result.provider_id,
            "model": result.model,
            "voice": result.voice,
            "artifact": artifact,
        }
        warnings = [*warnings, *result.warnings]
        if warnings:
            response["warnings"] = warnings
        return response
    except SpeechGenerationError as exc:
        _audit_event(agent, exc.code.value, characters=characters)
        return _error(exc.code, exc.message)
    except FileExistsError:
        _audit_event(agent, SafeErrorCode.ARTIFACT_INVALID.value, characters=characters)
        return _error(SafeErrorCode.ARTIFACT_INVALID, "Generated artifact filename already exists; retry the request.")
    except Exception:
        _audit_event(agent, SafeErrorCode.GENERATION_FAILED.value, characters=characters)
        return _error(SafeErrorCode.GENERATION_FAILED, "Speech generation could not be completed.")

"""DMSS adapter for the built-in ``safety.command`` Decision Profile.

This module intentionally owns only the narrow bridge between the generic
Decision Runtime and DMSS.  It does not execute commands and does not decide
whether a command may ultimately run: HMADS and ``SafetyPipeline`` retain that
policy authority.  Any unavailable, malformed, secret-bearing, timed-out, or
low-confidence profile result is represented as a typed fallback outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Mapping

from backend.tools.lib.decision_models import DecisionCancellation
from backend.tools.lib.decision_runtime import DecisionRuntime, DecisionRuntimeError, RuntimeRequest

_LOG = logging.getLogger(__name__)
PROFILE_KEY = "safety.command"
PROFILE_VERSION = "decim/v1"
MAX_COMMAND_CHARS = 12_000

SAFETY_COMMAND_SPEC = {
    "profile_key": PROFILE_KEY,
    "display_name": "Command Safety",
    "description": "Classifies a synthetic command for DMSS; it never executes commands or tools.",
    "schema_version": "decision-schema/v1",
    "policy_version": PROFILE_VERSION,
    "schema": {"version": "decision-schema/v1", "fields": [
        {"key": "decision", "kind": "choice", "description": "DMSS safety disposition.",
         "choices": ["allow", "review", "block"]},
    ]},
    "playground_preset": {"command": "printf 'hello from the safety playground'", "tool_type": "bash", "execution_boundary": "sandboxed_docker"},
}


@dataclass(frozen=True)
class SafetyCommandOutcome:
    """A normalized profile attempt, suitable for DMSS fallback handling."""

    attempted: bool
    mode: str | None = None
    decision: str | None = None
    confidence: float | None = None
    model_id: str | None = None
    provider_key: str | None = None
    latency_ms: int | None = None
    correlation_id: str | None = None
    fallback_reason: str | None = None


class SafetyCommandDecisionRunner:
    """Bounded, inference-only profile consumer used exclusively by DMSS."""

    def __init__(self, *, runtime_factory: Callable[[], DecisionRuntime] | None = None,
                 db_provider: Callable[[], Any] | None = None, timeout_ms: int = 3_000) -> None:
        self._runtime_factory = runtime_factory
        self._db_provider = db_provider
        self.timeout_ms = timeout_ms

    def _db(self) -> Any:
        if self._db_provider:
            return self._db_provider()
        from models.db import db
        return db

    def _runtime(self) -> DecisionRuntime:
        return self._runtime_factory() if self._runtime_factory else DecisionRuntime(self._db())

    def decide(self, command: str, tool_type: str, execution_boundary: str,
               correlation_id: str | None = None) -> SafetyCommandOutcome:
        """Evaluate command text only; callers must always retain HMADS fallback."""
        profile = self._db().get_decision_profile(PROFILE_KEY)
        if not profile:
            return SafetyCommandOutcome(False, fallback_reason="profile_missing")
        if not profile.get("enabled") or profile.get("mode") == "off":
            return SafetyCommandOutcome(False, mode=profile.get("mode"), fallback_reason="profile_disabled")
        mode = profile.get("mode")
        if mode not in {"shadow", "enforce"}:
            return SafetyCommandOutcome(True, fallback_reason="profile_invalid")
        if len(command) > MAX_COMMAND_CHARS:
            return SafetyCommandOutcome(True, mode=mode, fallback_reason="payload_too_large")
        try:
            result = self._runtime().invoke(RuntimeRequest(
                profile_key=PROFILE_KEY, profile_version=PROFILE_VERSION,
                input_context={"command": command, "tool_type": tool_type,
                               "execution_boundary": execution_boundary},
                policy_metadata={"consumer": "dmss"}, correlation_id=correlation_id,
                cancellation=DecisionCancellation(deadline_unix_ms=int(time.time() * 1000) + self.timeout_ms),
                channel="dmss", rate_limit_key=PROFILE_KEY, activity_source="production",
            ))
            if result.error:
                return SafetyCommandOutcome(True, mode=mode, fallback_reason=result.error.code.value)
            values = {field.key: field for field in result.fields}
            field = values.get("decision")
            if set(values) != {"decision"} or field is None or field.value not in {"allow", "review", "block"}:
                return SafetyCommandOutcome(True, mode=mode, fallback_reason="invalid_result")
            # DMSS enforcement is fail-closed: a provider must supply bounded,
            # independently meaningful confidence before it can influence policy.
            if field.confidence is None or field.confidence < float(profile.get("minimum_confidence", 1.0)):
                return SafetyCommandOutcome(True, mode=mode, fallback_reason="low_confidence")
            return SafetyCommandOutcome(
                True, mode=mode, decision=field.value, confidence=field.confidence,
                model_id=result.provider.model_id if result.provider else None,
                provider_key=result.provider.provider_key if result.provider else None,
                latency_ms=result.timing.total_ms, correlation_id=correlation_id,
            )
        except DecisionRuntimeError as exc:
            return SafetyCommandOutcome(True, mode=mode, fallback_reason=exc.code.value)
        except Exception:
            _LOG.warning("dmss.safety_command.failed", exc_info=True)
            return SafetyCommandOutcome(True, mode=mode, fallback_reason="runtime_error")


def get_safety_command_runner() -> SafetyCommandDecisionRunner:
    return SafetyCommandDecisionRunner()

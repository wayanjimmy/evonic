"""DMSS Decision Profile bridge regression coverage (no command execution)."""
from types import SimpleNamespace

from backend.dmss_decision_profile import (
    PROFILE_KEY, SafetyCommandDecisionRunner, SafetyCommandOutcome,
)
from backend.tools.lib.decision_models import (
    DecisionFieldResult, DecisionProfileRef, DecisionResult, ProviderMetadata,
    TimingMetadata,
)
from backend.tools.lib.safety_pipeline import SafetyPipeline


class Checker:
    def __init__(self, score=0):
        self.score = score

    def check(self, *_args):
        return {"score": self.score, "reasons": [], "blocked_patterns": [], "matched_patterns": []}


class ProfilePipeline(SafetyPipeline):
    def __init__(self, outcome):
        super().__init__(register_defaults=False)
        self.register(Checker())
        self.outcome = outcome

    def _safety_command_profile(self):
        return {"profile_key": "safety.command"}

    def _run_safety_command_profile(self, *_args):
        return self.outcome


def test_shadow_profile_never_changes_hmads_verdict():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="shadow", decision="block", confidence=.99))
    result = pipeline.check("echo safe", "bash", {})
    assert result["level"] == "safe"
    assert result["decision_source"] == "deterministic_shadow"


def test_enforce_profile_cannot_weaken_hmads_block():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", decision="allow", confidence=.99))
    pipeline._checkers = [Checker(15)]
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == "dangerous"
    assert result["decim_safety"]["final_reason"] == "hmads_floor"


def test_profile_failure_uses_exact_deterministic_fallback():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", fallback_reason="low_confidence"))
    expected = pipeline.check_deterministic("synthetic", "bash", {})
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == expected["level"]
    assert result["decision_source"] == "deterministic_fallback"
    assert result["decim_safety"]["fallback_reason"] == "low_confidence"


def test_profile_enforce_maps_review_without_executing_command():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", decision="review", confidence=.99))
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == "requires_approval"
    assert result["requires_approval"] is True


class _Runtime:
    def __init__(self, result):
        self.result = result

    def invoke(self, _request):
        return self.result


class _DB:
    def __init__(self):
        self.profile = {"enabled": True, "mode": "enforce", "minimum_confidence": .8}

    def get_decision_profile(self, key):
        return self.profile if key == PROFILE_KEY else None


def test_missing_confidence_cannot_enable_dmss_enforcement():
    profile = DecisionProfileRef(PROFILE_KEY, "decim/v1")
    result = DecisionResult(profile, "decision-schema/v1", (DecisionFieldResult("decision", "allow"),),
                            ProviderMetadata("systemone", "model"), TimingMetadata(1), 1)
    runner = SafetyCommandDecisionRunner(runtime_factory=lambda: _Runtime(result), db_provider=_DB)
    outcome = runner.decide("echo safe", "bash", "sandboxed_docker")
    assert outcome.decision is None
    assert outcome.fallback_reason == "low_confidence"

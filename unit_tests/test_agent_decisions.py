"""Regression coverage for built-in Decision Profile consumers and fallbacks."""
from unittest.mock import patch

import pytest

from backend.agent_decisions import (
    BUILTIN_DECISION_SPECS,
    GOAL_COMPLETION,
    PATH_BOUNDARY,
    TASK_COMPLEXITY,
    BuiltinDecisionRunner,
    goal_completion_input,
    path_boundary_input,
    task_complexity_input,
)
from backend.tools.lib.decision_models import DecisionError, DecisionErrorCode, DecisionFieldResult, DecisionResult, ProviderMetadata, TimingMetadata


class FakeDB:
    def __init__(self, profile):
        self.profile = profile

    def get_decision_profile(self, key):
        return self.profile if self.profile and key == self.profile["profile_key"] else None


def _profile(key, mode="enforce", enabled=True):
    return {"profile_key": key, "enabled": enabled, "mode": mode}


class Runtime:
    def __init__(self, fields=None, error=None, boom=False):
        self.fields, self.error, self.boom = fields or {}, error, boom
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        if self.boom:
            raise TimeoutError("simulated timeout")
        return DecisionResult(
            profile=type("Profile", (), {"key": request.profile_key, "version": "1"})(),
            schema_version="decision-schema/v1",
            fields=tuple(DecisionFieldResult(key, value, 0.95) for key, value in self.fields.items()),
            provider=ProviderMetadata("test", "test"), timing=TimingMetadata(1), attempts=1,
            error=self.error,
        )


@pytest.mark.parametrize("key,context,expected", [
    (TASK_COMPLEXITY, task_complexity_input("Small documentation change"), {"complexity": "trivial"}),
    (PATH_BOUNDARY, path_boundary_input("Add one more test", "Add tests", "Tests"), {"belongs_to_current": True}),
    (GOAL_COMPLETION, goal_completion_input("Ship feature", "Done", [], 0, 5), {"decision": "complete"}),
])
def test_enabled_profiles_return_only_expected_normalized_fields(key, context, expected):
    runtime = Runtime(expected)
    runner = BuiltinDecisionRunner(db_provider=lambda: FakeDB(_profile(key)), runtime_factory=lambda: runtime)

    assert runner.decide(key, context) == expected
    assert runtime.requests[0].input_context == context
    assert runtime.requests[0].cancellation.deadline_unix_ms is not None


@pytest.mark.parametrize("key,context", [
    (TASK_COMPLEXITY, task_complexity_input("Ambiguous request")),
    (PATH_BOUNDARY, path_boundary_input("Could you help?", "Current goal", "Current")),
    (GOAL_COMPLETION, goal_completion_input("Goal", "Candidate", [], 0, 5)),
])
@pytest.mark.parametrize("profile", [None, "disabled", "shadow"])
def test_disabled_or_missing_profiles_never_invoke_and_fall_back(key, context, profile):
    configured = None if profile is None else _profile(key, mode="shadow" if profile == "shadow" else "enforce", enabled=profile != "disabled")
    runtime = Runtime({next(iter(BUILTIN_DECISION_SPECS[key].fields))["key"]: True})
    runner = BuiltinDecisionRunner(db_provider=lambda: FakeDB(configured), runtime_factory=lambda: runtime)

    assert runner.decide(key, context) is None
    assert runtime.requests == []


@pytest.mark.parametrize("key,context,fields", [
    (TASK_COMPLEXITY, task_complexity_input("Ambiguous request"), {"wrong": "trivial"}),
    (PATH_BOUNDARY, path_boundary_input("Other task", "Current", "Current"), {"wrong": True}),
    (GOAL_COMPLETION, goal_completion_input("Goal", "Candidate", [], 0, 5), {"wrong": "complete"}),
])
def test_malformed_or_provider_failures_fall_back(key, context, fields):
    malformed = BuiltinDecisionRunner(db_provider=lambda: FakeDB(_profile(key)), runtime_factory=lambda: Runtime(fields))
    failed = BuiltinDecisionRunner(db_provider=lambda: FakeDB(_profile(key)), runtime_factory=lambda: Runtime(boom=True))
    unavailable = BuiltinDecisionRunner(
        db_provider=lambda: FakeDB(_profile(key)),
        runtime_factory=lambda: Runtime(error=DecisionError(DecisionErrorCode.PROVIDER_UNAVAILABLE, "unavailable")),
    )

    assert malformed.decide(key, context) is None
    assert failed.decide(key, context) is None
    assert unavailable.decide(key, context) is None


def test_task_complexity_uses_profile_then_legacy_fallback():
    class Runner:
        def decide(self, key, context):
            return {"complexity": "trivial"}
    with patch("backend.agent_decisions.get_builtin_decision_runner", return_value=Runner()):
        from backend.task_classifier import classify_task
        assert classify_task("ambiguous request") == "trivial"

    with patch("backend.agent_decisions.get_builtin_decision_runner", return_value=type("R", (), {"decide": lambda *_: None})()):
        with patch("backend.task_classifier._get_classifier_client") as client:
            client.return_value.chat_completion.return_value = {"success": True, "response": {"choices": [{"message": {"content": "TRIVIAL"}}]}}
            assert classify_task("ambiguous request") == "trivial"


def test_goal_completion_profile_and_fallback_preserve_fail_open():
    from backend.goal_evaluator import evaluate_goal
    class Runner:
        def decide(self, key, context):
            return {"decision": "continue"}
    with patch("backend.agent_decisions.get_builtin_decision_runner", return_value=Runner()):
        assert evaluate_goal({"summary": "Goal"}, "Candidate")["decision"] == "continue"

    with patch("backend.agent_decisions.get_builtin_decision_runner", return_value=type("R", (), {"decide": lambda *_: None})()):
        assert evaluate_goal({"summary": "Goal"}, "Candidate", client=type("C", (), {"chat_completion": lambda *_a, **_k: {"success": False}})())["failed"]


def test_path_boundary_profile_only_vetoes_switches():
    from backend.agent_runtime.cmp.detector import detect
    cmp = {"active_id": "A1", "paths": {"A1": {"goal": "Current", "title": "Current"}}}
    ms = type("MS", (), {})()
    class Runner:
        def decide(self, key, context):
            return {"belongs_to_current": True}
    with patch("backend.agent_decisions.get_builtin_decision_runner", return_value=Runner()):
        assert detect(cmp, ms, "Please continue the current work")["decision"] == "continue"

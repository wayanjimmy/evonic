"""Focused coverage for the LLM goal evaluator and its fail-open contract.

The evaluator decides whether a candidate final response satisfies the active
session goal. Parsing must be tolerant (models wrap JSON in prose and rename
fields), and any failure must fail open so a transient evaluator outage can
never trap the agent in a continuation loop.
"""

from backend.active_goal import create_active_goal
from backend.goal_evaluator import (
    DECISION_BLOCKED,
    DECISION_COMPLETE,
    DECISION_CONTINUE,
    MAX_NEXT_ACTION_CHARS,
    MAX_REASON_CHARS,
    _build_user_prompt,
    evaluate_goal,
    parse_goal_decision,
)

RAW = "Add request validation to the upload endpoint and cover it with tests"


def _goal():
    return create_active_goal(RAW)


class _FakeClient:
    """Minimal LLMClient stand-in for classifier-style calls."""

    def __init__(self, content: str = "", success: bool = True, boom: bool = False):
        self._content = content
        self._success = success
        self._boom = boom

    def chat_completion(self, messages, tools=None, temperature=None,
                        enable_thinking=None, max_tokens=None, **kwargs):
        if self._boom:
            raise RuntimeError("provider down")
        if not self._success:
            return {"success": False, "error_type": "timeout"}
        return {
            "success": True,
            "response": {"choices": [{"message": {"content": self._content}}]},
        }

    def extract_content(self, response, strip_thinking=True):
        return response["response"]["choices"][0]["message"]["content"]


def test_parse_goal_decision_accepts_every_valid_verdict():
    assert parse_goal_decision({"decision": "complete"})["decision"] == DECISION_COMPLETE
    assert parse_goal_decision({"decision": "CONTINUE"})["decision"] == DECISION_CONTINUE
    assert parse_goal_decision({"decision": "blocked"})["decision"] == DECISION_BLOCKED


def test_parse_goal_decision_is_tolerant_of_field_aliases():
    parsed = parse_goal_decision({
        "verdict": "continue",
        "why": "only the first half is done",
        "next": "add the tests",
    })

    assert parsed == {
        "decision": DECISION_CONTINUE,
        "reason": "only the first half is done",
        "next_action": "add the tests",
    }


def test_parse_goal_decision_returns_none_for_unusable_payloads():
    assert parse_goal_decision(None) is None
    assert parse_goal_decision({"reason": "no verdict here"}) is None
    assert parse_goal_decision({"decision": "maybe"}) is None


def test_parse_goal_decision_bounds_and_clears_next_action_on_complete():
    parsed = parse_goal_decision({
        "decision": "complete",
        "reason": "x" * (MAX_REASON_CHARS + 50),
        "next_action": "y" * (MAX_NEXT_ACTION_CHARS + 50),
    })

    assert parsed["next_action"] == ""
    assert len(parsed["reason"]) <= MAX_REASON_CHARS


def test_evaluate_goal_parses_a_successful_verdict():
    client = _FakeClient(
        'Reasoning first. {"decision": "continue", "reason": "validation missing", '
        '"next_action": "add schema validation"}'
    )

    result = evaluate_goal(
        goal=_goal(), candidate_response="Done, I updated the endpoint.",
        tasks=[{"text": "Add validation", "status": "in_progress"}],
        nudges_used=0, max_nudges=5, client=client,
    )

    assert result["failed"] is False
    assert result["decision"] == DECISION_CONTINUE
    assert result["next_action"] == "add schema validation"


def test_evaluate_goal_fails_open_on_client_error():
    result = evaluate_goal(
        goal=_goal(), candidate_response="ok", client=_FakeClient(success=False)
    )

    assert result["failed"] is True
    assert result["decision"] == DECISION_COMPLETE


def test_evaluate_goal_fails_open_when_the_client_raises():
    result = evaluate_goal(
        goal=_goal(), candidate_response="ok", client=_FakeClient(boom=True)
    )

    assert result["failed"] is True
    assert result["decision"] == DECISION_COMPLETE


def test_evaluate_goal_fails_open_without_a_goal():
    # No goal -> nothing to evaluate; must never raise or call the model.
    result = evaluate_goal(goal=None, candidate_response="ok")

    assert result["failed"] is True


def test_build_user_prompt_includes_goal_candidate_tasks_and_budget():
    prompt = _build_user_prompt(
        _goal(), "candidate answer",
        [{"text": "step one", "status": "done"}], nudges_used=2, max_nudges=5,
    )

    assert RAW in prompt
    assert "candidate answer" in prompt
    assert "- [done] step one" in prompt
    assert "NUDGES USED: 2 of 5" in prompt

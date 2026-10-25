"""Focused coverage for durable `/goal` propagation.

The `/goal` command must (a) persist a bounded, structured goal in session
state, (b) reach the agent as trusted system context on every turn, and
(c) survive session-summary compaction instead of relying on the raw
slash-command line staying in the conversation history.
"""

import threading

from backend.active_goal import (
    DEFAULT_MAX_GOAL_NUDGES,
    MAX_GOAL_NUDGE_CHARS,
    MAX_GOAL_SUMMARY_CHARS,
    MAX_GOAL_TITLE_CHARS,
    MAX_MAX_GOAL_NUDGES,
    bump_goal_nudges,
    create_active_goal,
    goal_nudges_exhausted,
    normalize_active_goal,
    normalize_max_goal_nudges,
    render_active_goal_event,
    render_goal_nudge,
)
from backend.agent_state import AgentState
from backend.agent_runtime.llm_response_parser import _emergency_compact_messages

RAW = "Keep session summaries within 2000 characters without losing key context"


def _goal():
    return create_active_goal(RAW)


def test_create_active_goal_is_bounded_and_auditable():
    goal = create_active_goal(RAW)

    assert goal["id"].startswith("goal_")
    assert len(goal["title"]) <= MAX_GOAL_TITLE_CHARS
    assert len(goal["summary"]) <= MAX_GOAL_SUMMARY_CHARS
    assert goal["raw_instruction"] == RAW
    assert goal["nudges_used"] == 0


def test_system_context_is_agent_visible_without_slash_literal():
    # The goal is rendered inside the per-turn "## Agent State" system message.
    content = AgentState(mode="execute", active_goal=_goal()).render()

    assert "## Active Goal" in content
    assert "[SYSTEM]" in content
    assert "/goal" not in content
    assert RAW[:40] in content


def test_legacy_text_goal_is_normalized():
    normalized = normalize_active_goal({"text": RAW})

    assert normalized is not None
    assert normalized["title"]
    assert normalized["summary"]
    assert normalized["raw_instruction"] == RAW


def test_agent_state_round_trip_preserves_active_goal():
    goal = _goal()
    restored = AgentState.deserialize(AgentState(active_goal=goal).serialize())

    assert restored.active_goal == goal


def test_agent_state_render_contains_active_goal_section():
    state = AgentState(mode="execute", active_goal=_goal())
    rendered = state.render()

    assert "## Active Goal" in rendered
    assert RAW[:40] in rendered
    assert "/goal" not in rendered


def test_summary_compaction_keeps_goal_system_context():
    """The goal lives in the leading system block, so compaction keeps it.

    This is the regression that motivated the change: relying on the raw
    conversation tail meant the objective disappeared from the model's context.
    """
    goal = _goal()
    messages = [
        {"role": "system", "content": "base system prompt"},
        {"role": "system", "content": "## Agent State\n**Mode**: execute"},
        {"role": "system", "content": AgentState(mode="execute", active_goal=goal).render()},
        {"role": "system", "content": "## Prior conversation summary\nold summary text"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "reply 2"},
    ]

    class _FakeLLM:
        def chat_completion(self, *args, **kwargs):
            return {
                "success": True,
                "response": {"choices": [{"message": {"content": "compacted summary"}}]},
            }

    compacted = _emergency_compact_messages(
        messages, _FakeLLM(), threading.Lock(), "session-1", "agent-1")

    assert compacted is not None
    texts = [m.get("content", "") for m in compacted]
    # The durable goal survives because it is part of the leading system block.
    assert any("## Active Goal" in text for text in texts)
    # The rewritten summary is rebuilt into the system block.
    assert any("compacted summary" in text for text in texts)


def test_goal_event_is_plain_system_text():
    event = render_active_goal_event(_goal())

    assert event.startswith("[SYSTEM] Active goal updated.")
    assert "/goal" not in event


def test_api_payload_never_exposes_raw_goal_instruction():
    """The Session State API exposes only the bounded title/summary fields."""
    from routes.agents import _normalize_active_goal

    payload = _normalize_active_goal({"text": RAW, "nudges_used": 0})

    assert payload is not None
    assert "raw_instruction" not in payload
    assert payload["title"]
    assert payload["summary"]


def test_api_payload_upgrades_legacy_text_goal():
    from routes.agents import _normalize_active_goal

    payload = _normalize_active_goal({"text": RAW})

    assert payload is not None
    assert payload["title"]
    assert "text" not in payload


def test_normalize_max_goal_nudges_clamps_and_defaults():
    assert normalize_max_goal_nudges(3) == 3
    assert normalize_max_goal_nudges(-5) == 0
    assert normalize_max_goal_nudges(999) == MAX_MAX_GOAL_NUDGES
    # Malformed input falls back to the default budget, never unbounded.
    assert normalize_max_goal_nudges("not-a-number") == DEFAULT_MAX_GOAL_NUDGES
    assert normalize_max_goal_nudges(None) == DEFAULT_MAX_GOAL_NUDGES


def test_bump_goal_nudges_advances_and_bounds_the_nudge_text():
    goal = bump_goal_nudges(_goal(), "keep going " * 200)

    assert goal is not None
    assert goal["nudges_used"] == 1
    assert goal["last_nudge"]
    assert len(goal["last_nudge"]) <= MAX_GOAL_NUDGE_CHARS
    assert goal["updated_at"]


def test_goal_nudges_exhausted_respects_the_budget():
    goal = _goal()

    assert goal_nudges_exhausted(goal, 1) is False
    goal["nudges_used"] = 1
    assert goal_nudges_exhausted(goal, 1) is True
    # A zero budget means the goal is never nudged.
    assert goal_nudges_exhausted(_goal(), 0) is True


def test_render_goal_nudge_is_a_visible_contextual_message():
    nudge = render_goal_nudge(_goal(), "Half of the request is still missing", "Add the missing tests")

    assert "[System/Goal]" in nudge
    assert "Half of the request is still missing" in nudge
    assert "Add the missing tests" in nudge
    # It is a normal session message, never a slash-command or prompt fragment.
    assert "/goal" not in nudge

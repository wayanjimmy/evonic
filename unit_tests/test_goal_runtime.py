"""Focused coverage for goal completion/continuation runtime plumbing.

These tests pin the observable contract the agent runtime relies on:

- ``evaluate_active_goal`` maps an evaluator verdict to a finalize/nudge action
  and decides whether the goal must be cleared.
- ``clear_active_goal`` removes the goal from session state and emits a state
  change so the Session State "Goal mode" indicator disappears.
- ``record_goal_nudge`` advances the nudge counter, persists the updated goal,
  and emits a *visible, session-persisted* continuation message.
"""

import json

import pytest

import backend.goal_runtime as goal_runtime
from backend.active_goal import create_active_goal
from backend.goal_evaluator import (
    DECISION_BLOCKED,
    DECISION_COMPLETE,
    DECISION_CONTINUE,
)

RAW = "Ship the goal completion mechanism with tests"


class _FakeAgentState:
    def __init__(self, goal=None, tasks=None):
        self.active_goal = goal
        self.tasks = tasks or []


class _FakeDB:
    def __init__(self):
        self.session_state = {}
        self.messages = []
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def get_session_state(self, session_id, agent_id=None):
        return self.session_state.get(session_id)

    def upsert_session_state(self, session_id, content, agent_id=None):
        self.session_state[session_id] = content

    def add_chat_message(self, session_id, role, content=None, agent_id=None,
                         metadata=None, **kwargs):
        self.messages.append({
            "session_id": session_id, "role": role, "content": content,
            "metadata": metadata,
        })
        return len(self.messages)


class _FakeChatlog:
    def __init__(self):
        self.entries = []

    def append(self, entry):
        self.entries.append(entry)


class _FakeChatlogManager:
    def __init__(self):
        self.log = _FakeChatlog()

    def get(self, agent_id, session_id):
        return self.log


@pytest.fixture
def wired(monkeypatch):
    fake_db = _FakeDB()
    fake_chatlog = _FakeChatlogManager()
    events = []
    monkeypatch.setattr(goal_runtime, "db", fake_db)

    import models.chatlog as chatlog_mod
    monkeypatch.setattr(chatlog_mod, "chatlog_manager", fake_chatlog)

    from backend.event_stream import event_stream
    monkeypatch.setattr(
        event_stream, "emit",
        lambda name, payload: events.append((name, payload)),
    )
    return fake_db, fake_chatlog, events


def _verdict(decision, reason="", next_action="", failed=False):
    def _evaluator(**_kwargs):
        return {
            "decision": decision, "reason": reason,
            "next_action": next_action, "failed": failed,
        }
    return _evaluator


def test_clear_active_goal_removes_state_and_emits_change(wired):
    fake_db, _chatlog, events = wired
    goal = create_active_goal(RAW)
    fake_db.session_state["s1"] = json.dumps({"mode": "execute", "active_goal": goal})
    ms = _FakeAgentState(goal=goal)

    cleared = goal_runtime.clear_active_goal(
        ms=ms, session_id="s1", agent_id="a1", db_agent_id="a1")

    assert cleared is True
    assert ms.active_goal is None
    assert json.loads(fake_db.session_state["s1"])["active_goal"] is None
    names = [name for name, _ in events]
    assert "state:changed" in names
    assert "evonic:agent-state-changed" in names
    state_payload = next(p for n, p in events if n == "state:changed")
    assert state_payload["active_goal"] is None


def test_clear_active_goal_is_a_noop_without_a_goal(wired):
    fake_db, _chatlog, _events = wired
    ms = _FakeAgentState()

    assert goal_runtime.clear_active_goal(
        ms=ms, session_id="s1", agent_id="a1", db_agent_id="a1") is False


def test_evaluate_without_a_goal_finalizes_without_clearing(wired):
    outcome = goal_runtime.evaluate_active_goal(ms=_FakeAgentState(), candidate="hi")

    assert outcome["has_goal"] is False
    assert outcome["action"] == goal_runtime.ACTION_FINALIZE
    assert outcome["clear"] is False


def test_evaluate_complete_finalizes_and_clears(wired):
    ms = _FakeAgentState(goal=create_active_goal(RAW))

    outcome = goal_runtime.evaluate_active_goal(
        ms=ms, candidate="done", evaluator=_verdict(DECISION_COMPLETE))

    assert outcome["action"] == goal_runtime.ACTION_FINALIZE
    assert outcome["clear"] is True


def test_evaluate_blocked_finalizes_and_clears(wired):
    ms = _FakeAgentState(goal=create_active_goal(RAW))

    outcome = goal_runtime.evaluate_active_goal(
        ms=ms, candidate="need credentials",
        evaluator=_verdict(DECISION_BLOCKED, reason="missing token"))

    assert outcome["action"] == goal_runtime.ACTION_FINALIZE
    assert outcome["clear"] is True


def test_evaluate_continue_within_budget_nudges(wired):
    ms = _FakeAgentState(goal=create_active_goal(RAW))

    outcome = goal_runtime.evaluate_active_goal(
        ms=ms, candidate="partial", max_nudges=5,
        evaluator=_verdict(DECISION_CONTINUE, reason="half done", next_action="add tests"))

    assert outcome["action"] == goal_runtime.ACTION_NUDGE
    assert outcome["clear"] is False


def test_evaluate_continue_with_exhausted_budget_clears(wired):
    goal = create_active_goal(RAW)
    goal["nudges_used"] = 5
    ms = _FakeAgentState(goal=goal)

    outcome = goal_runtime.evaluate_active_goal(
        ms=ms, candidate="still partial", max_nudges=5,
        evaluator=_verdict(DECISION_CONTINUE))

    assert outcome["action"] == goal_runtime.ACTION_FINALIZE
    assert outcome["clear"] is True


def test_evaluate_failure_fails_open_and_keeps_the_goal(wired):
    ms = _FakeAgentState(goal=create_active_goal(RAW))

    outcome = goal_runtime.evaluate_active_goal(
        ms=ms, candidate="whatever",
        evaluator=_verdict(DECISION_COMPLETE, failed=True))

    assert outcome["action"] == goal_runtime.ACTION_FINALIZE
    # A transient evaluator outage must not clear a still-open goal.
    assert outcome["clear"] is False


def test_record_goal_nudge_persists_visible_message_and_advances(wired):
    fake_db, fake_chatlog, events = wired
    goal = create_active_goal(RAW)
    ms = _FakeAgentState(goal=goal)
    outcome = {
        "goal": goal, "reason": "validation missing",
        "next_action": "add schema validation",
    }

    message = goal_runtime.record_goal_nudge(
        ms=ms, outcome=outcome, session_id="s1", agent_id="a1", db_agent_id="a1")

    assert message["role"] == "user"
    assert "[System/Goal]" in message["content"]
    assert "add schema validation" in message["content"]

    # The advanced counter is persisted on the goal and on the AgentState.
    assert ms.active_goal["nudges_used"] == 1
    assert json.loads(fake_db.session_state["s1"])["active_goal"]["nudges_used"] == 1

    # A visible user message is stored and announced.
    assert fake_db.messages[-1]["role"] == "user"
    assert fake_db.messages[-1]["metadata"]["goal_nudge"] is True
    assert fake_chatlog.log.entries[-1]["content"] == message["content"]
    assert any(name == "message_received" for name, _ in events)


def test_record_goal_nudge_without_a_goal_returns_none(wired):
    assert goal_runtime.record_goal_nudge(
        ms=_FakeAgentState(), outcome={}, session_id="s1", agent_id="a1",
        db_agent_id="a1") is None

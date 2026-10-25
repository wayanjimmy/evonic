"""Runtime plumbing for durable session goals.

Two responsibilities, kept out of ``llm_loop`` so they can be unit-tested:

1. **Completion** — :func:`evaluate_active_goal` runs the LLM goal evaluator on
   a candidate final response and returns a decision the loop can act on
   (finalize vs. nudge, and whether the goal must be cleared).
2. **Continuation** — :func:`record_goal_nudge` advances the nudge counter,
   persists the updated goal, and emits a *visible, session-persisted* nudge
   message (the same continuation-message pattern as Kanban stale-task
   detection). :func:`clear_active_goal` removes the goal and emits a state
   change so the Session State indicator disappears.

The nudge is a normal session message for both the agent and the user; nothing
is ever injected into the system prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Mapping, Optional

from models.db import db

from backend.active_goal import (
    DEFAULT_MAX_GOAL_NUDGES,
    bump_goal_nudges,
    goal_nudges_exhausted,
    normalize_active_goal,
    normalize_max_goal_nudges,
    render_goal_nudge,
)
from backend.goal_evaluator import (
    DECISION_BLOCKED,
    DECISION_COMPLETE,
    DECISION_CONTINUE,
    evaluate_goal,
)

_logger = logging.getLogger(__name__)

# Marker used on the persisted nudge so the message is recognizable in the
# transcript and by future tooling.
GOAL_NUDGE_METADATA = "goal_nudge"

GOAL_NUDGE_TAG = "System/Goal"

ACTION_FINALIZE = "finalize"
ACTION_NUDGE = "nudge"


def load_max_goal_nudges() -> int:
    """Return the globally configured goal-nudge budget (clamped)."""
    try:
        raw = db.get_setting("max_goal_nudges", str(DEFAULT_MAX_GOAL_NUDGES))
    except Exception:
        raw = DEFAULT_MAX_GOAL_NUDGES
    return normalize_max_goal_nudges(raw)


def _read_session_data(session_id: str, db_agent_id: str) -> Dict[str, Any]:
    try:
        raw = db.get_session_state(session_id, agent_id=db_agent_id)
    except Exception:
        _logger.debug("goal: could not read session state", exc_info=True)
        return {}
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _emit_goal_state_changed(agent_id: str, session_id: str, goal: Any) -> None:
    """Tell the browser the goal indicator changed so it refreshes live."""
    try:
        from backend.event_stream import event_stream

        event_stream.emit("state:changed", {
            "agent_id": agent_id,
            "session_id": session_id,
            "active_goal": goal,
        })
        event_stream.emit("evonic:agent-state-changed", {
            "agent_id": agent_id,
            "session_id": session_id,
        })
    except Exception:
        _logger.debug("goal: state change emit failed", exc_info=True)


def clear_active_goal(*, ms: Any, session_id: str, agent_id: str,
                      db_agent_id: Optional[str] = None) -> bool:
    """Remove the active goal from the session and notify the UI.

    Mutates ``ms.active_goal`` when an AgentState is supplied, drops the
    ``active_goal`` key from persisted session state, and emits a state change
    so the Session State "Goal mode" indicator disappears. Returns ``True`` when
    a goal was actually cleared.
    """
    db_agent_id = db_agent_id or agent_id
    had_goal = bool(getattr(ms, "active_goal", None)) if ms is not None else False

    data = _read_session_data(session_id, db_agent_id)
    if data.get("active_goal"):
        had_goal = True
    data["active_goal"] = None
    try:
        db.upsert_session_state(session_id, json.dumps(data), agent_id=db_agent_id)
    except Exception:
        _logger.warning("goal: failed to persist cleared goal", exc_info=True)

    if ms is not None:
        try:
            ms.active_goal = None
        except Exception:
            pass

    _emit_goal_state_changed(agent_id, session_id, None)
    return had_goal


def evaluate_active_goal(*, ms: Any, candidate: str,
                         max_nudges: Optional[int] = None,
                         evaluator: Callable[..., Dict[str, Any]] = evaluate_goal
                         ) -> Dict[str, Any]:
    """Evaluate the active goal for a candidate final response (no side effects).

    Returns a decision dict with:

    - ``has_goal``: whether the session currently has an active goal.
    - ``action``: ``finalize`` (commit the response) or ``nudge`` (keep working).
    - ``clear``: whether the runtime must clear the goal (only for ``finalize``).
    - ``decision``/``reason``/``next_action``: the evaluator verdict.
    - ``goal``: the normalized goal, when present.
    """
    goal = normalize_active_goal(getattr(ms, "active_goal", None))
    result: Dict[str, Any] = {
        "has_goal": bool(goal),
        "action": ACTION_FINALIZE,
        "clear": False,
        "decision": None,
        "reason": "",
        "next_action": "",
        "goal": goal,
        "max_nudges": None,
    }
    if not goal:
        return result

    budget = normalize_max_goal_nudges(
        max_nudges if max_nudges is not None else load_max_goal_nudges()
    )
    result["max_nudges"] = budget

    verdict = evaluator(
        goal=goal,
        candidate_response=candidate,
        tasks=list(getattr(ms, "tasks", []) or []),
        nudges_used=goal["nudges_used"],
        max_nudges=budget,
    )
    decision = verdict.get("decision")
    result["decision"] = decision
    result["reason"] = verdict.get("reason") or ""
    result["next_action"] = verdict.get("next_action") or ""

    if verdict.get("failed"):
        # Fail open: finalize normally, keep the goal for a later turn so a
        # transient evaluator outage never traps the agent in a loop.
        return result

    if decision in (DECISION_COMPLETE, DECISION_BLOCKED):
        result["clear"] = True
        return result

    # ``continue`` (or any unrecognized verdict that failed to parse) within the
    # budget keeps the agent working.
    if decision == DECISION_CONTINUE and not goal_nudges_exhausted(goal, budget):
        result["action"] = ACTION_NUDGE
        return result

    # Budget exhausted: finalize and abandon the goal.
    result["clear"] = True
    if decision != DECISION_CONTINUE:
        result["decision"] = DECISION_CONTINUE
    return result


def record_goal_nudge(*, ms: Any, outcome: Mapping[str, Any], session_id: str,
                      agent_id: str, db_agent_id: Optional[str] = None) \
        -> Optional[Dict[str, Any]]:
    """Persist a visible continuation nudge and advance the goal's budget.

    Returns the ``user`` message dict to append to the LLM messages list, or
    ``None`` when there is no goal to nudge. Even if persistence fails, an
    in-memory message is returned so the loop can still continue safely.
    """
    db_agent_id = db_agent_id or agent_id
    goal = outcome.get("goal") or normalize_active_goal(getattr(ms, "active_goal", None))
    if not goal:
        return None

    nudge_text = render_goal_nudge(goal, outcome.get("reason") or "",
                                   outcome.get("next_action") or "")
    updated = bump_goal_nudges(goal, nudge_text) or goal
    if ms is not None:
        try:
            ms.active_goal = updated
        except Exception:
            pass

    # Persist the advanced goal so the next evaluation sees the new count and a
    # later restart keeps the correct budget state.
    data = _read_session_data(session_id, db_agent_id)
    data["active_goal"] = updated
    try:
        db.upsert_session_state(session_id, json.dumps(data), agent_id=db_agent_id)
    except Exception:
        _logger.warning("goal: failed to persist nudge counter", exc_info=True)
    _emit_goal_state_changed(agent_id, session_id, updated)

    metadata = {GOAL_NUDGE_METADATA: True, "nudges_used": updated["nudges_used"]}
    try:
        message_id = db.add_chat_message(
            session_id, "user", nudge_text,
            agent_id=db_agent_id, metadata=metadata,
        )
        message_id = message_id if isinstance(message_id, (int, str)) else None
        from models.chatlog import chatlog_manager

        chatlog_manager.get(db_agent_id, session_id).append({
            "type": "user", "session_id": session_id, "content": nudge_text,
            "metadata": metadata, "message_id": message_id,
        })
        from backend.event_stream import event_stream

        event_stream.emit("message_received", {
            "agent_id": agent_id,
            "session_id": session_id,
            "message": nudge_text,
            "content": nudge_text,
            "metadata": metadata,
            "message_id": message_id,
            "role": "user",
        })
    except Exception:
        _logger.warning("goal: failed to persist visible nudge", exc_info=True)

    return {"role": "user", "content": nudge_text}

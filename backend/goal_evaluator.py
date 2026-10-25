"""LLM evaluator for active session goals.

After an agent produces a candidate *final* response (no tool calls) while a
session goal is active, the runtime asks this evaluator whether the objective
has actually been met. The verdict gates finalization:

- ``complete``  — the goal is achieved; finalize and clear the goal indicator.
- ``continue``  — progress was made but the objective is not met yet; the
  runtime nudges the agent to keep working (bounded by ``max_goal_nudges``).
- ``blocked``   — the agent cannot proceed without user input; finalize and
  clear so the user can respond.

The evaluator always *fails open*: any transport/parse failure returns
``complete`` with ``failed=True`` so a transient outage can never trap the
agent in a continuation loop.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

_logger = logging.getLogger(__name__)

DECISION_COMPLETE = "complete"
DECISION_CONTINUE = "continue"
DECISION_BLOCKED = "blocked"
VALID_DECISIONS = (DECISION_COMPLETE, DECISION_CONTINUE, DECISION_BLOCKED)

MAX_REASON_CHARS = 600
MAX_NEXT_ACTION_CHARS = 600
_WHITESPACE_RE = re.compile(r"\s+")


def _compact(value: Any, limit: int) -> str:
    """Collapse whitespace and bound a possibly non-string field."""
    text = _WHITESPACE_RE.sub(" ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{shortened or text[: limit - 1].rstrip()}\u2026"


def parse_goal_decision(payload: Any) -> Optional[Dict[str, str]]:
    """Normalize a decoded evaluator object into a decision dict.

    Pure function: accepts the decoded JSON object and returns
    ``{"decision", "reason", "next_action"}`` or ``None`` when the payload is
    unusable. Callers must treat ``None`` as a failure and fail open.
    """
    if not isinstance(payload, Mapping):
        return None
    raw = str(payload.get("decision") or payload.get("verdict") or "").strip().lower()
    if not raw:
        return None
    decision = next((c for c in VALID_DECISIONS if c in raw), None)
    if decision is None:
        return None
    next_action = _compact(
        payload.get("next_action") or payload.get("next"), MAX_NEXT_ACTION_CHARS
    )
    if decision == DECISION_COMPLETE:
        # A completed goal has no follow-up work; drop any stray next action.
        next_action = ""
    return {
        "decision": decision,
        "reason": _compact(payload.get("reason") or payload.get("why"), MAX_REASON_CHARS),
        "next_action": next_action,
    }


def _fail_open() -> Dict[str, Any]:
    return {
        "decision": DECISION_COMPLETE,
        "reason": "",
        "next_action": "",
        "failed": True,
    }


def _render_tasks(tasks: Any) -> str:
    if not isinstance(tasks, (list, tuple)) or not tasks:
        return "(no tracked tasks)"
    lines: List[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        status = str(task.get("status") or "pending")
        text = _compact(task.get("text"), 200)
        if text:
            lines.append(f"- [{status}] {text}")
    return "\n".join(lines) if lines else "(no tracked tasks)"


def _build_user_prompt(goal: Mapping[str, Any], candidate: str, tasks: Any,
                       nudges_used: int, max_nudges: int) -> str:
    objective = _compact(
        goal.get("summary") or goal.get("raw_instruction") or goal.get("title"),
        MAX_REASON_CHARS,
    )
    title = _compact(goal.get("title"), 200)
    return (
        f"ACTIVE GOAL\n"
        f"Title: {title}\n"
        f"Objective: {objective}\n\n"
        f"SESSION TASK STATUS\n{_render_tasks(tasks)}\n\n"
        f"CONTINUATION NUDGES USED: {nudges_used} of {max_nudges}\n\n"
        f"CANDIDATE FINAL RESPONSE\n{candidate}\n\n"
        f"Decide whether the ACTIVE GOAL is now achieved. Respond with a single JSON object:\n"
        f'{{"decision": "complete" | "continue" | "blocked", '
        f'"reason": "short justification", "next_action": "single best next step"}}\n'
        f"Guidance:\n"
        f"- complete: the objective is fully met (or no further agent action is needed).\n"
        f"- continue: the objective is not met and the agent can still make progress; "
        f"give the one best next_action.\n"
        f"- blocked: the agent cannot proceed without user input or an external change; "
        f"explain the blocker in reason.\n"
        f"When genuinely uncertain, prefer complete unless there is clear remaining work."
    )


def evaluate_goal(goal: Any, candidate_response: str, tasks: Any = None,
                  nudges_used: int = 0, max_nudges: int = 5,
                  client: Any = None) -> Dict[str, Any]:
    """LLM-evaluate whether ``candidate_response`` satisfies the active goal.

    Returns a dict with ``decision``, ``reason``, ``next_action`` and
    ``failed``. Never raises: any error fails open with ``complete``.
    """
    try:
        goal_map: Mapping[str, Any] = goal if isinstance(goal, Mapping) else {
            "summary": str(goal or ""),
            "title": str(goal or ""),
        }
        candidate = _compact(candidate_response, 4000)
        # An enforce-mode Decision Profile may replace the legacy evaluator only
        # after shared runtime validation. Every non-result preserves the
        # existing evaluator and, ultimately, its fail-open finalization.
        try:
            from backend.agent_decisions import get_builtin_decision_runner, goal_completion_input
            profile = get_builtin_decision_runner().decide(
                "agent.goal_completion", goal_completion_input(
                    goal_map.get("summary") or goal_map.get("title"), candidate,
                    tasks, nudges_used, max_nudges))
            if profile and profile.get("decision") in VALID_DECISIONS:
                return {"decision": profile["decision"], "reason": "",
                        "next_action": "", "failed": False}
        except Exception:
            _logger.debug("Decision Profile goal completion unavailable; using legacy evaluator", exc_info=True)
        if client is None:
            from backend.task_classifier import _get_classifier_client
            client = _get_classifier_client("goal_evaluator_model_id")
        from backend.agent_runtime.llm_json import extract_first_json
        from backend.task_classifier import classifier_chat

        messages = [
            {"role": "system", "content": _EVALUATOR_SYSTEM},
            {"role": "user", "content": _build_user_prompt(
                goal_map, candidate, tasks, int(nudges_used or 0), int(max_nudges or 0))},
        ]
        response = classifier_chat(
            client, messages, max_tokens=1024,
            log_label="goal evaluator", source="goal_evaluator",
        )
        if not response or not response.get("success"):
            _logger.warning("Goal evaluation call failed: %s",
                            (response or {}).get("error_type"))
            return _fail_open()
        content = client.extract_content(response, strip_thinking=True)
        payload = (extract_first_json(content, require_key="decision")
                   or extract_first_json(content))
        parsed = parse_goal_decision(payload)
        if not parsed:
            _logger.warning("Goal evaluator returned unparseable output: %r", content[:200])
            return _fail_open()
        parsed["failed"] = False
        return parsed
    except Exception:
        _logger.warning("Goal evaluation raised; finalizing normally", exc_info=True)
        return _fail_open()


_EVALUATOR_SYSTEM = """You evaluate whether an AI agent has achieved the active session goal.

You are strict about the GOAL but never invent requirements that are not implied by it.
You are given the goal, the session task status, how many continuation nudges were used,
and the agent's candidate final response.

Judge the candidate final response against the goal:
- "complete": the goal's objective is achieved. The agent may stop.
- "continue": the goal is not achieved but the agent can still make concrete progress.
  Provide exactly one best "next_action".
- "blocked": the agent genuinely cannot proceed without the user's input, an approval,
  credentials, or an external change. Do not use "blocked" merely because the task is hard.

Respond with a single JSON object and nothing else:
{"decision": "complete" | "continue" | "blocked", "reason": "...", "next_action": "..."}
"""

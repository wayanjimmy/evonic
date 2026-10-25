"""Structured, durable session goals and their agent-visible system context."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid
from typing import Any, Dict, Mapping

MAX_GOAL_TITLE_CHARS = 120
MAX_GOAL_SUMMARY_CHARS = 600
# Maximum characters retained for a nudge excerpt or an evaluated next action.
MAX_GOAL_NUDGE_CHARS = 600

# Bounded nudge budget for a session goal. The evaluator may ask the agent to
# keep working at most this many times before the goal is abandoned, so an
# impossible objective can never trap the agent in a continuation loop. The
# budget is global because it guards the runtime, not a single session.
DEFAULT_MAX_GOAL_NUDGES = 5
MIN_MAX_GOAL_NUDGES = 0
MAX_MAX_GOAL_NUDGES = 50


def _compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _bounded_sentence(value: str, limit: int) -> str:
    value = _compact_whitespace(value)
    if len(value) <= limit:
        return value
    shortened = value[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{shortened or value[:limit - 1].rstrip()}…"


def _title_from_instruction(instruction: str) -> str:
    """Return a compact, deterministic UI caption without an LLM dependency."""
    text = _compact_whitespace(instruction)
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return _bounded_sentence(first_sentence, MAX_GOAL_TITLE_CHARS)


def normalize_active_goal(goal: Any) -> Dict[str, Any] | None:
    """Return bounded goal fields and transparently upgrade legacy ``text`` data."""
    if not isinstance(goal, Mapping):
        return None
    raw_instruction = _compact_whitespace(
        str(goal.get("raw_instruction") or goal.get("text") or "")
    )
    summary = _bounded_sentence(
        str(goal.get("summary") or raw_instruction), MAX_GOAL_SUMMARY_CHARS
    )
    title = _bounded_sentence(
        str(goal.get("title") or _title_from_instruction(summary)), MAX_GOAL_TITLE_CHARS
    )
    if not title or not summary:
        return None
    return {
        "id": str(goal.get("id") or "legacy-goal"),
        "title": title,
        "summary": summary,
        "raw_instruction": raw_instruction,
        "created_at": str(goal.get("created_at") or ""),
        "updated_at": str(goal.get("updated_at") or ""),
        "nudges_used": int(goal.get("nudges_used") or 0),
        "last_nudge": goal.get("last_nudge"),
    }


def create_active_goal(instruction: str) -> Dict[str, Any]:
    """Create the session representation for a user-provided goal.

    ``raw_instruction`` is retained for audit/debugging only. Agent context and
    UI indicators consume the bounded ``title`` and ``summary`` fields instead.
    """
    raw_instruction = instruction.strip()
    summary = _bounded_sentence(raw_instruction, MAX_GOAL_SUMMARY_CHARS)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"goal_{uuid.uuid4().hex[:12]}",
        "title": _title_from_instruction(raw_instruction),
        "summary": summary,
        "raw_instruction": raw_instruction,
        "created_at": now,
        "updated_at": now,
        "nudges_used": 0,
        "last_nudge": None,
    }


def render_active_goal_system_message(goal: Any) -> str:
    """Render trusted agent context without exposing a slash-command literal.

    Embedded in the per-turn Agent State system message so the objective is
    re-injected on every LLM call, after history filtering and compaction.
    """
    normalized = normalize_active_goal(goal)
    if not normalized:
        return ""
    return (
        "## Active Goal\n"
        "[SYSTEM] The user has set the following active goal for this session. "
        "Treat it as durable operational context until it is replaced or cleared.\n"
        f"Title: {normalized['title']}\n"
        f"Instructions: {normalized['summary']}"
    )


def render_active_goal_event(goal: Any) -> str:
    """Render the auditable goal-update event stored in conversation history."""
    normalized = normalize_active_goal(goal)
    if not normalized:
        return "[SYSTEM] Active goal updated."
    return (
        "[SYSTEM] Active goal updated.\n"
        f"Goal: {normalized['title']}\n"
        f"Operational direction: {normalized['summary']}"
    )


def normalize_max_goal_nudges(value: Any) -> int:
    """Clamp a configured goal-nudge budget into the supported range.

    Used by both the settings API and the runtime so a malformed value can
    never disable the safety bound that keeps a goal from looping forever.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_GOAL_NUDGES
    return max(MIN_MAX_GOAL_NUDGES, min(MAX_MAX_GOAL_NUDGES, parsed))


def goal_nudges_exhausted(goal: Any, max_nudges: Any = DEFAULT_MAX_GOAL_NUDGES) -> bool:
    """Return whether the goal has used its entire continuation budget."""
    normalized = normalize_active_goal(goal)
    if not normalized:
        return False
    return normalized["nudges_used"] >= normalize_max_goal_nudges(max_nudges)


def bump_goal_nudges(goal: Any, nudge: str = "") -> Dict[str, Any] | None:
    """Return a copy of ``goal`` with the nudge counter advanced by one.

    Also records the (bounded) last nudge text so the runtime and UI can audit
    why the agent was asked to continue.
    """
    normalized = normalize_active_goal(goal)
    if not normalized:
        return None
    normalized["nudges_used"] = normalized["nudges_used"] + 1
    normalized["last_nudge"] = _bounded_sentence(nudge, MAX_GOAL_NUDGE_CHARS) or None
    normalized["updated_at"] = datetime.now(timezone.utc).isoformat()
    return normalized


def render_goal_nudge(goal: Any, judgement: str, next_action: str) -> str:
    """Render the visible continuation message that keeps the agent working.

    This is a session-persisted, user-visible message (never a system-prompt
    injection). It states why the goal is not yet met and the best next action
    the evaluator identified.
    """
    normalized = normalize_active_goal(goal)
    title = normalized["title"] if normalized else "active goal"
    reason = _bounded_sentence(judgement, MAX_GOAL_NUDGE_CHARS)
    action = _bounded_sentence(next_action, MAX_GOAL_NUDGE_CHARS)
    lines = [
        "[System/Goal] The active session goal is not complete yet.",
        f"Goal: {title}",
    ]
    if reason:
        lines.append(f"Why: {reason}")
    if action:
        lines.append(f"Next action: {action}")
    lines.append(
        "Continue working on the goal. Do not repeat work that is already "
        "done, and call the goal complete only when the objective is achieved."
    )
    return "\n".join(lines)

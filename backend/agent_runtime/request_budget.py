"""Deterministic, protocol-safe provider request budgeting.

This module never mutates the persisted transcript.  It plans a provider-bound
copy of messages, tools, and tool choice immediately before a request is sent.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from backend.agent_runtime.active_context import _collect_groups, _eligible, validate_tool_pairs
from backend.llm_usage_events import estimate_context_tokens, estimate_tokens

_DEFAULT_CONTEXT_WINDOW = 128000
_PROTOCOL_OVERHEAD = 64
_MULTIMODAL_OVERHEAD = 256

@dataclass(frozen=True)
class RequestBudgetPlan:
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]]
    tool_choice: Any
    capacity: int
    input_budget: int
    estimated_tokens: int
    output_reserve: int
    thinking_reserve: int
    protocol_reserve: int
    multimodal_reserve: int
    safety_margin: int
    stage: str
    fit: bool
    diagnostic: Optional[str]
    fingerprint: str

    def metrics(self) -> Dict[str, Any]:
        return {"capacity": self.capacity, "input_budget": self.input_budget,
                "estimated_tokens": self.estimated_tokens, "output_reserve": self.output_reserve,
                "thinking_reserve": self.thinking_reserve, "protocol_reserve": self.protocol_reserve,
                "multimodal_reserve": self.multimodal_reserve, "safety_margin": self.safety_margin,
                "stage": self.stage, "fit": self.fit, "diagnostic": self.diagnostic,
                "fingerprint": self.fingerprint, "message_count": len(self.messages),
                "tool_count": len(self.tools or [])}

def _positive(value: Any, default: int = 0) -> int:
    # Mocks and provider objects can implement surprising __int__ methods; only
    # concrete numeric/string configuration values are capacity declarations.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try: return max(0, int(value))
    except (TypeError, ValueError): return default

def _has_multimodal(messages: Sequence[Dict[str, Any]]) -> bool:
    return any(isinstance(m.get("content"), list) for m in messages if isinstance(m, dict))

def _fingerprint(messages, tools, tool_choice, capacity, stage) -> str:
    # Lengths/types only: telemetry fingerprints must not encode user content.
    shape = {"capacity": capacity, "stage": stage, "messages": [
        {"role": m.get("role"), "content_type": type(m.get("content")).__name__,
         "content_len": len(m.get("content", "")) if isinstance(m.get("content"), str) else len(m.get("content") or []),
         "calls": len(m.get("tool_calls") or [])} for m in messages],
        "tools": [((t.get("function") or {}).get("name"), len(json.dumps(t, sort_keys=True, default=str))) for t in (tools or [])],
        "tool_choice": bool(tool_choice)}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:24]

def _compact_tools(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep callable names and parameter types while removing descriptive bulk."""
    result = copy.deepcopy(list(tools))
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in {"description", "examples", "default"}}
        if isinstance(value, list): return [strip(v) for v in value]
        return value
    return [strip(tool) for tool in result]

def _remove_old_completed_group(messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    groups = _collect_groups(messages)
    for group in groups:
        if _eligible(group):
            return messages[:group.start] + messages[group.end:]
    return None

def _remove_old_history(messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Drop one oldest ordinary historical message, retaining all system and latest user."""
    latest_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for i, message in enumerate(messages):
        if i != latest_user and message.get("role") not in {"system", "tool"} and not message.get("tool_calls"):
            return messages[:i] + messages[i + 1:]
    return None

def plan_request(messages: Sequence[Dict[str, Any]], tools: Optional[Sequence[Dict[str, Any]]], *,
                 tool_choice: Any = None, context_window: Any = None, max_tokens: Any = None,
                 thinking_budget: Any = None, safety_margin: Any = 512,
                 enforce: bool = True, force_reduction: bool = False) -> RequestBudgetPlan:
    """Build a fit payload by monotonic reductions or return an irreducible plan."""
    copied_messages, copied_tools = copy.deepcopy(list(messages)), copy.deepcopy(list(tools or []))
    capacity = _positive(context_window, _DEFAULT_CONTEXT_WINDOW) or _DEFAULT_CONTEXT_WINDOW
    output = _positive(max_tokens, 4096) or 4096
    thinking = _positive(thinking_budget)
    protocol = _PROTOCOL_OVERHEAD + (32 if tool_choice else 0)
    multimodal = _MULTIMODAL_OVERHEAD if _has_multimodal(copied_messages) else 0
    safety = _positive(safety_margin, 512)
    budget = max(0, capacity - output - thinking - protocol - multimodal - safety)
    stage = "initial"
    diagnostic = None
    try:
        validate_tool_pairs(copied_messages, allow_unresolved=False)
    except ValueError as exc:
        diagnostic = "invalid_tool_protocol"
        estimated = estimate_context_tokens(copied_messages, copied_tools) + estimate_tokens(json.dumps(tool_choice, default=str))
        return RequestBudgetPlan(copied_messages, copied_tools or None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, False, diagnostic, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage))
    while True:
        estimated = estimate_context_tokens(copied_messages, copied_tools) + estimate_tokens(json.dumps(tool_choice, default=str))
        if not enforce or (estimated <= budget and not force_reduction):
            return RequestBudgetPlan(copied_messages, copied_tools or None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, True, None, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage))
        reduced = _remove_old_completed_group(copied_messages)
        if reduced is not None:
            copied_messages, stage = reduced, "completed_tool_groups"
            continue
        reduced = _remove_old_history(copied_messages)
        if reduced is not None:
            copied_messages, stage = reduced, "historical_messages"
            continue
        if copied_tools and stage != "compact_tool_schemas":
            copied_tools, stage = _compact_tools(copied_tools), "compact_tool_schemas"
            continue
        if copied_tools:
            copied_tools, tool_choice, stage = [], None, "disable_optional_tools"
            continue
        if force_reduction and estimated <= budget:
            return RequestBudgetPlan(copied_messages, copied_tools or None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, True, None, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage))
        diagnostic, stage = "irreducible_payload", "irreducible"
        return RequestBudgetPlan(copied_messages, None, tool_choice, capacity, budget, estimated, output, thinking, protocol, multimodal, safety, stage, False, diagnostic, _fingerprint(copied_messages, copied_tools, tool_choice, capacity, stage))

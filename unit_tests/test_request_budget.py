import copy

from backend.agent_runtime.active_context import validate_tool_pairs
from backend.agent_runtime.request_budget import plan_request


def _call(call_id, name="read_file"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _messages():
    return [
        {"role": "system", "content": "safety policy must remain"},
        {"role": "user", "content": "old request " * 300},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "x" * 1000},
        {"role": "tool", "tool_call_id": "b", "content": "x" * 1000},
        {"role": "user", "content": "latest intent must remain"},
    ]


def test_budget_reduces_completed_groups_without_mutating_or_orphaning():
    messages = _messages()
    original = copy.deepcopy(messages)
    plan = plan_request(messages, [], context_window=1000, max_tokens=200, safety_margin=100)
    assert plan.fit
    assert plan.estimated_tokens <= plan.input_budget
    assert plan.stage in {"completed_tool_groups", "historical_messages"}
    assert messages == original
    validate_tool_pairs(plan.messages)
    assert any(m.get("content") == "latest intent must remain" for m in plan.messages)
    assert any(m.get("role") == "system" for m in plan.messages)


def test_budget_accounts_for_tool_schema_and_can_disable_tools():
    tools = [{"type": "function", "function": {"name": "huge", "description": "z" * 6000,
             "parameters": {"type": "object", "description": "q" * 6000}}}]
    plan = plan_request([{"role": "system", "content": "safe"}, {"role": "user", "content": "latest"}],
                        tools, context_window=1000, max_tokens=100, safety_margin=100)
    assert plan.fit
    assert plan.stage in {"compact_tool_schemas", "disable_optional_tools"}
    assert plan.estimated_tokens <= plan.input_budget


def test_irreducible_system_payload_returns_diagnostic():
    plan = plan_request([{"role": "system", "content": "x" * 10000}], [],
                        context_window=1000, max_tokens=100, safety_margin=100)
    assert not plan.fit
    assert plan.diagnostic == "irreducible_payload"
    assert plan.fingerprint


def test_fingerprint_is_deterministic_and_capacity_specific():
    messages = [{"role": "system", "content": "safe"}, {"role": "user", "content": "hello 世界 ```json {\"a\": 1}```"}]
    first = plan_request(messages, [], context_window=3000, max_tokens=100)
    second = plan_request(messages, [], context_window=3000, max_tokens=100)
    smaller = plan_request(messages, [], context_window=1000, max_tokens=100)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != smaller.fingerprint

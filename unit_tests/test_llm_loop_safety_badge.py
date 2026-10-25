"""Plumbing tests for safety provenance and approval actors.

Covers the three seams the thinking-timeline badge depends on:
  1. tools annotate their result with `_safety` (bash / runpy),
  2. the LLM loop strips that annotation before the result is serialized,
  3. the approval registry records WHO decided (`resolved_by`).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


REQUIRES_APPROVAL = {
    'level': 'requires_approval',
    'score': 12,
    'reasons': ['Decim Safety requires manual approval.'],
    'blocked_patterns': ['decim_review'],
    'approval_info': {'risk_level': 'medium', 'description': 'Decim Safety',
                      'categories': ['decim_review'], 'pattern_count': 0},
    'decision_source': 'decim',
    'decim_safety': {'decision_source': 'decim', 'model_decision': 'review',
                     'model_confidence': 0.62, 'event_id': 'ev-42'},
}


class _StubPipeline:
    def __init__(self, verdict):
        self._verdict = verdict

    def check(self, code, tool_type='bash', agent_context=None):
        return dict(self._verdict)


# ---------------------------------------------------------------- tool annotation

def test_bash_attaches_safety_provenance(monkeypatch):
    from backend.tools import bash as bash_tool
    monkeypatch.setattr(bash_tool, 'get_safety_pipeline', lambda: _StubPipeline(REQUIRES_APPROVAL))
    monkeypatch.setattr(bash_tool, 'should_skip_safety', lambda agent: False)

    result = bash_tool.execute({'session_id': 'sid', 'safety_checker_enabled': 1,
                                'is_super': 0}, {'script': 'echo hello'})

    assert result['level'] == 'requires_approval'
    prov = result['_safety']
    assert prov['source'] == 'dmss'
    assert prov['decision'] == 'review'
    assert prov['confidence'] == 0.62
    assert 'provider_key' not in prov


def test_runpy_attaches_safety_provenance(monkeypatch):
    # runpy imports the pipeline lazily inside execute() to avoid a circular
    # import, so patch the source module instead of the tool module.
    from backend.tools.lib import safety_pipeline as sp
    from backend.tools import runpy as runpy_tool
    monkeypatch.setattr(sp, 'get_safety_pipeline', lambda: _StubPipeline(REQUIRES_APPROVAL))
    monkeypatch.setattr(sp, 'should_skip_safety', lambda agent: False)

    result = runpy_tool.execute({'session_id': 'sid', 'safety_checker_enabled': 1,
                                 'is_super': 0}, {'code': 'print(1)'})

    assert result['level'] == 'requires_approval'
    assert result['_safety']['source'] == 'dmss'


# ---------------------------------------------------------------- llm-loop stripping

def test_pop_safety_provenance_removes_the_field():
    from backend.agent_runtime.llm_loop import pop_safety_provenance

    payload = {'stdout': 'ok', '_safety': {'source': 'dmss', 'decision': 'allow'}}
    prov = pop_safety_provenance(payload)

    assert prov == {'source': 'dmss', 'decision': 'allow'}
    assert '_safety' not in payload
    # Idempotent — a second call is a no-op.
    assert pop_safety_provenance(payload) == {}


def test_pop_safety_provenance_tolerates_non_dicts():
    from backend.agent_runtime.llm_loop import pop_safety_provenance

    assert pop_safety_provenance(None) == {}
    assert pop_safety_provenance('text') == {}
    assert pop_safety_provenance([1, 2]) == {}
    assert pop_safety_provenance({'_safety': 'not-a-dict'}) == {}


def test_tool_executed_payload_carries_safety():
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'backend', 'agent_runtime', 'llm_loop.py'), encoding='utf-8').read()

    marker = "'tool_result': result_dict"
    idx = src.index(marker)
    block = src[idx:src.index('})', idx)]
    assert "'safety'" in block
    assert "'approval'" in block


def test_approval_resolved_payload_carries_decided_by():
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'backend', 'agent_runtime', 'llm_loop.py'), encoding='utf-8').read()

    assert src.count("'decided_by': _decided_by") >= 2
    assert "pending.resolved_by" in src
    # The tool output entry persists the provenance for replay.
    assert "'function': fn_name, **_timeline_extra" in src


# ---------------------------------------------------------------- approval actors

def test_approval_registry_records_who_decided():
    from backend.agent_runtime.approval import ApprovalRegistry

    registry = ApprovalRegistry()
    pending = registry.create(
        session_id='s1', agent_id='a1', tool_call_id='tc1', tool_name='bash',
        tool_args={'script': 'rm -rf /tmp/x'}, safety_result=dict(REQUIRES_APPROVAL),
    )
    assert pending.resolved_by is None

    assert registry.resolve(pending.approval_id, 'approve', resolved_by='user:telegram') is True
    assert pending.decision == 'approve'
    assert pending.resolved_by == 'user:telegram'

    # A second resolver must not overwrite the first actor.
    assert registry.resolve(pending.approval_id, 'reject', resolved_by='agent:other') is False
    assert pending.resolved_by == 'user:telegram'


def test_list_pending_exposes_verdict_provenance():
    from backend.agent_runtime.approval import ApprovalRegistry

    registry = ApprovalRegistry()
    payload = dict(REQUIRES_APPROVAL)
    payload['_safety'] = {'source': 'dmss', 'decision': 'review', 'confidence': 0.62}
    registry.create(session_id='s2', agent_id='a1', tool_call_id='tc2',
                    tool_name='bash', tool_args={}, safety_result=payload)

    pending = registry.list_pending()
    assert len(pending) == 1
    assert pending[0]['safety']['source'] == 'dmss'
    assert pending[0]['safety']['decision'] == 'review'

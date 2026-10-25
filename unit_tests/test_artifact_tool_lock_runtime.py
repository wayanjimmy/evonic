"""Regression tests: the ``artifacts_enabled`` tool lock is enforced at runtime.

Agents provisioned outside the create/update API (e.g. tenant agents written by
a plugin) had ``artifacts_enabled=1`` but no ``read_attachment`` row. The model
was never offered the tool, the executor would have refused it anyway, and the
agent fell back to opening ``data/attachments/...`` host paths that a sandboxed
or tunnelled workplace cannot reach, so inbound documents were never read.
"""

from backend.agent_factory import ARTIFACT_TOOLS
from backend.agent_runtime import context
from backend.agent_runtime import simulation_spec as sim_spec
from backend.agent_runtime.context import build_tools
from backend.tools.registry import ToolRegistry

# agent_tools rows of an affected production tenant agent
TENANT_ROWS = ['bash', 'calculator', 'patch', 'read_file', 'str_replace', 'write_file']


def _agent(**overrides):
    agent = {
        'id': 'tenant_agent',
        'is_super': False,
        'builtin_tools_enabled': False,
        'agent_messaging_enabled': 0,
        'vision_enabled': False,
        'artifacts_enabled': 1,
        'workplace_id': 'wp-tenant',
    }
    agent.update(overrides)
    return agent


def _patch_rows(monkeypatch, rows):
    monkeypatch.setattr(context.db, 'get_agent_tools', lambda agent_id: list(rows))
    monkeypatch.setattr(context.db, 'get_agent_skills', lambda agent_id: [])


def _tool_names(agent):
    return {t['function']['name'] for t in build_tools(agent) if t.get('function', {}).get('name')}


def test_enabled_flag_adds_missing_artifact_tools(monkeypatch):
    _patch_rows(monkeypatch, TENANT_ROWS)

    ids = sim_spec.tools(_agent())

    assert ids[:len(TENANT_ROWS)] == TENANT_ROWS
    assert ARTIFACT_TOOLS <= set(ids)
    assert len(ids) == len(set(ids))


def test_model_is_offered_read_attachment(monkeypatch):
    _patch_rows(monkeypatch, TENANT_ROWS)

    assert 'read_attachment' in _tool_names(_agent())


def test_executor_allowlist_accepts_read_attachment(monkeypatch):
    _patch_rows(monkeypatch, TENANT_ROWS)
    executor = ToolRegistry().get_real_executor({
        'agent_id': 'tenant_agent',
        'assigned_tool_ids': list(sim_spec.tools(_agent())),
        'is_super': False,
        'session_id': 'sess',
    })

    result = executor('read_attachment', {'attachment_id': 999999})

    assert result.get('blocked_by') != 'authorization'


def test_disabled_flag_strips_artifact_tools_even_if_assigned(monkeypatch):
    _patch_rows(monkeypatch, TENANT_ROWS + ['read_attachment', 'save_artifact'])

    ids = sim_spec.tools(_agent(artifacts_enabled=0))

    assert ids == TENANT_ROWS
    assert 'read_attachment' not in _tool_names(_agent(artifacts_enabled=0))


def test_simulation_spec_is_left_untouched(monkeypatch):
    _patch_rows(monkeypatch, ['should', 'not', 'be', 'read'])
    agent = _agent(is_simulation=True, _simulation_tool_ids=['bash'])

    assert sim_spec.tools(agent) == ['bash']

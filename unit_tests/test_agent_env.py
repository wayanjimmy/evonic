"""Tests for agent variable -> environment resolution (backend/tools/lib/agent_env.py)."""
from backend.tools.lib.agent_env import build_agent_env, coerce_env_source


def test_dict_form_values_are_stringified():
    assert build_agent_env({'variables': {'A': 'x', 'N': 5}}) == {'A': 'x', 'N': '5'}


def test_row_form_is_accepted():
    rows = [
        {'key': 'A', 'value': 'x', 'is_secret': 1},
        {'key': 'B', 'value': 'y', 'is_secret': 0},
    ]
    assert build_agent_env({'variables': rows}) == {'A': 'x', 'B': 'y'}


def test_invalid_keys_are_dropped():
    env = build_agent_env({'variables': {'GOOD': '1', 'BAD-KEY': '2', '': '3'}})
    assert env == {'GOOD': '1'}


def test_none_values_are_dropped():
    assert build_agent_env({'variables': {'A': None, 'B': 'x'}}) == {'B': 'x'}


def test_db_fallback_when_agent_context_has_no_variables(monkeypatch):
    monkeypatch.setattr('models.db.db.get_agent_variables', lambda aid: [
        {'key': 'T', 'value': 'tok', 'is_secret': 0},
    ])
    assert build_agent_env({'id': 'a1'}) == {'T': 'tok'}


def test_explicit_agent_id_wins(monkeypatch):
    seen = []

    def _rows(aid):
        seen.append(aid)
        return []

    monkeypatch.setattr('models.db.db.get_agent_variables', _rows)
    assert build_agent_env({}, 'override') == {}
    assert seen == ['override']


def test_lookup_failure_yields_empty_env(monkeypatch):
    def _boom(aid):
        raise RuntimeError('db down')

    monkeypatch.setattr('models.db.db.get_agent_variables', _boom)
    assert build_agent_env({'id': 'a1'}) == {}


def test_coerce_ignores_unknown_shapes():
    assert coerce_env_source(None) == {}
    assert coerce_env_source('nope') == {}
    assert coerce_env_source([{'nokey': 1}]) == {}


def test_non_dict_agent_is_tolerated(monkeypatch):
    monkeypatch.setattr('models.db.db.get_agent_variables', lambda aid: [])
    assert build_agent_env(None) == {}

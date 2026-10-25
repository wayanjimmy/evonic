"""Regression tests for the background-process live log endpoint."""

import pytest

from app import app
from backend.agent_runtime.background_jobs import background_jobs
from models.db import db


AGENT_ID = 'background_log_agent'
SESSION_ID = 'background-log-session'
LOG_PATH = '/tmp/evonic_build_background_log_test.log'


def _create_agent():
    db.create_agent({
        'id': AGENT_ID,
        'name': 'Background Log Agent',
        'system_prompt': '',
    })


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


@pytest.fixture(autouse=True)
def clean_background_jobs():
    background_jobs._jobs.clear()
    yield
    background_jobs._jobs.clear()


def test_reads_sandbox_log_with_spawn_time_backend_context(monkeypatch):
    """Live output must query the sandbox that started the tracked process."""
    _create_agent()
    background_jobs.register(
        session_id=SESSION_ID,
        session_name='background-log-test',
        log_file=LOG_PATH,
        pid_file='',
        command='sleep 60',
        backend_ctx={'workspace': '/sandbox/workspace', 'is_subagent': True},
    )

    calls = []

    class FakeBackend:
        def run_bash(self, command, timeout, env):
            calls.append((command, timeout, env))
            return {'exit_code': 0, 'stdout': 'first line\nsecond line\n'}

    backend = FakeBackend()
    monkeypatch.setattr(
        'backend.tools.lib.exec_backend.registry.get_backend',
        lambda session_id, context: calls.append(('backend', session_id, context)) or backend,
    )
    monkeypatch.setattr('routes.agents.os.path.isfile', lambda path: False)

    response = _client().get(
        f'/api/agents/{AGENT_ID}/bg-job/log',
        query_string={'file': LOG_PATH, 'session_id': SESSION_ID, 'lines': 200},
    )

    assert response.status_code == 200
    assert response.get_json()['content'] == ['first line', 'second line']
    _, resolved_session_id, context = calls[0]
    assert resolved_session_id == SESSION_ID
    assert context['agent_id'] == AGENT_ID
    assert context['workspace'] == '/sandbox/workspace'
    assert context['is_subagent'] is True


def test_missing_sandbox_log_has_clear_error(monkeypatch):
    """A stale/missing log remains actionable instead of a generic client error."""
    _create_agent()
    monkeypatch.setattr('routes.agents.os.path.isfile', lambda path: False)

    response = _client().get(
        f'/api/agents/{AGENT_ID}/bg-job/log',
        query_string={'file': LOG_PATH, 'session_id': SESSION_ID},
    )

    assert response.status_code == 404
    assert response.get_json() == {'error': 'Log file not found'}

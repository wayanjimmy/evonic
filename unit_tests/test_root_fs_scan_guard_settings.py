"""System -> Settings coverage for the root filesystem scan guard.

The guard is toggled by the `root_fs_scan_guard_enabled` DB setting (defaulting
to `config.ROOT_FS_SCAN_GUARD_ENABLED`); the RFS_GUARD_DISABLED=1 env var
force-disables it. This module exercises the HTTP surface only — the bash gate
itself is covered by test_root_fs_scan_guard.py.
"""

from unittest.mock import patch

import pytest

from app import app
from models.db import db


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_setting() caches in-process; the test DB is per-test, so clear it."""
    db.invalidate_settings_cache()
    yield
    db.invalidate_settings_cache()


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


def _general(client):
    return client.get('/api/settings/general').get_json()


def test_general_settings_exposes_guard_defaulting_on():
    """With no DB row, the setting defaults to the config flag (True)."""
    client = _client()
    assert _general(client)['root_fs_scan_guard_enabled'] is True
    assert client.get('/api/settings/root-fs-scan-guard').get_json() == {'enabled': True}


def test_general_settings_default_follows_env_force_disable(monkeypatch):
    """RFS_GUARD_DISABLED=1 (config flag False) makes the default '0'."""
    import config
    monkeypatch.setattr(config, 'ROOT_FS_SCAN_GUARD_ENABLED', False, raising=False)
    client = _client()
    assert _general(client)['root_fs_scan_guard_enabled'] is False
    assert client.get('/api/settings/root-fs-scan-guard').get_json() == {'enabled': False}


def test_batch_save_toggles_guard_off_and_on():
    client = _client()

    response = client.post('/api/settings/batch', json={
        'settings': {'root_fs_scan_guard_enabled': False},
    })
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        'root_fs_scan_guard_enabled': False,
    }}
    assert db.get_setting('root_fs_scan_guard_enabled') == '0'
    assert _general(client)['root_fs_scan_guard_enabled'] is False

    response = client.post('/api/settings/batch', json={
        'settings': {'root_fs_scan_guard_enabled': True},
    })
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        'root_fs_scan_guard_enabled': True,
    }}
    assert db.get_setting('root_fs_scan_guard_enabled') == '1'
    assert _general(client)['root_fs_scan_guard_enabled'] is True


def test_dedicated_endpoint_round_trips():
    client = _client()

    response = client.put('/api/settings/root-fs-scan-guard', json={'enabled': False})
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'enabled': False}
    assert db.get_setting('root_fs_scan_guard_enabled') == '0'
    assert client.get('/api/settings/root-fs-scan-guard').get_json() == {'enabled': False}

    response = client.put('/api/settings/root-fs-scan-guard', json={'enabled': True})
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'enabled': True}
    assert db.get_setting('root_fs_scan_guard_enabled') == '1'


def test_batch_save_audits_guard_change():
    client = _client()
    with patch('routes.settings.audit.log_setting_change') as logger:
        client.post('/api/settings/batch', json={
            'settings': {'root_fs_scan_guard_enabled': False},
        })

    logger.assert_called_once()
    kwargs = logger.call_args.kwargs
    assert kwargs['key'] == 'root_fs_scan_guard_enabled'
    assert kwargs['old_value'] == '1'
    assert kwargs['new_value'] == '0'


def test_dedicated_endpoint_audits_guard_change():
    client = _client()
    with patch('routes.settings.audit.log_setting_change') as logger:
        client.put('/api/settings/root-fs-scan-guard', json={'enabled': False})

    logger.assert_called_once()
    kwargs = logger.call_args.kwargs
    assert kwargs['key'] == 'root_fs_scan_guard_enabled'
    assert kwargs['old_value'] == '1'
    assert kwargs['new_value'] == '0'

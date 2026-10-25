"""Focused tests for save_artifact source retention and optional cleanup."""

import os

import backend.tools.save_artifact as save_artifact


def _agent(tmp_path):
    return {'id': 'artifact-test', 'session_id': 'artifact-test-session', 'artifacts_root': str(tmp_path / 'artifacts')}


def test_source_path_retains_source_by_default(tmp_path, monkeypatch):
    source = tmp_path / 'report.bin'
    source.write_bytes(b'payload')
    monkeypatch.setattr(save_artifact, 'shared_agents_dir', lambda agent: agent['artifacts_root'])

    result = save_artifact.execute(_agent(tmp_path), {'filename': 'report.bin', 'source_path': str(source)})

    assert result['result'] == 'Artifact saved successfully'
    assert source.exists()
    assert result['source_exists'] is True
    assert result['source_deleted'] is False
    assert 'delete_source=true' in result['source_cleanup_hint']
    assert open(result['filepath'], 'rb').read() == b'payload'


def test_source_path_deletes_source_only_when_requested(tmp_path, monkeypatch):
    source = tmp_path / 'temporary.bin'
    source.write_bytes(b'payload')
    monkeypatch.setattr(save_artifact, 'shared_agents_dir', lambda agent: agent['artifacts_root'])

    result = save_artifact.execute(_agent(tmp_path), {
        'filename': 'temporary.bin', 'source_path': str(source), 'delete_source': True,
    })

    assert result['result'] == 'Artifact saved successfully'
    assert not source.exists()
    assert result['source_exists'] is False
    assert result['source_deleted'] is True
    assert 'source_cleanup_hint' not in result


def test_content_mode_has_no_source_status(tmp_path, monkeypatch):
    monkeypatch.setattr(save_artifact, 'shared_agents_dir', lambda agent: agent['artifacts_root'])

    result = save_artifact.execute(_agent(tmp_path), {'filename': 'report.md', 'content': '# Report'})

    assert result['result'] == 'Artifact saved successfully'
    assert 'source_exists' not in result
    assert 'source_deleted' not in result

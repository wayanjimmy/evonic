"""Regression test: a file sent from a workplace/sandbox keeps its own name.

send_file stages remote bytes locally before handing them to the channel. It
used a NamedTemporaryFile, so users received e.g. ``tmpwoz1k9iw.xlsx`` instead
of ``Penilaian_Kinerja_v2.xlsx``.
"""

import os

from backend.tools import send_file


class _FakeWorkplaceBackend:
    def __init__(self, files):
        self.files = files

    def resolve_path(self, path):
        return path

    def file_stat(self, path):
        return {'exists': path in self.files, 'is_dir': False,
                'size': len(self.files.get(path, b''))}

    def cat_file_bytes(self, path):
        return {'bytes': self.files[path]}


def test_workplace_file_is_delivered_under_its_original_name(monkeypatch):
    from backend.agent_runtime import agent_runtime

    remote = '/workspace/Penilaian_Kinerja_v2.xlsx'
    content = b'PK\x03\x04 workbook bytes'
    delivered = []
    monkeypatch.setattr(send_file, '_get_workplace_backend',
                        lambda agent, session_id: _FakeWorkplaceBackend({remote: content}))
    monkeypatch.setattr('backend.tools._workspace.resolve_workspace_path',
                        lambda agent, path, root: path)
    monkeypatch.setattr(agent_runtime, 'send_file_as_bot',
                        lambda session_id, path, caption, mime: delivered.append(path) or True)

    result = send_file.execute(
        {'id': 'agent-wp', 'session_id': 'sess-wp', 'workplace_id': 'wp-1'},
        {'file_path': remote, 'caption': 'KPI v2'},
    )

    assert result['result'] == 'File sent successfully'
    assert result['file_name'] == 'Penilaian_Kinerja_v2.xlsx'
    assert os.path.basename(delivered[0]) == 'Penilaian_Kinerja_v2.xlsx'
    with open(delivered[0], 'rb') as fh:
        assert fh.read() == content


def test_two_workplace_files_with_same_name_do_not_clobber(monkeypatch):
    from backend.agent_runtime import agent_runtime

    files = {'/workspace/a/report.xlsx': b'first', '/workspace/b/report.xlsx': b'second'}
    delivered = []
    monkeypatch.setattr(send_file, '_get_workplace_backend',
                        lambda agent, session_id: _FakeWorkplaceBackend(files))
    monkeypatch.setattr('backend.tools._workspace.resolve_workspace_path',
                        lambda agent, path, root: path)
    monkeypatch.setattr(agent_runtime, 'send_file_as_bot',
                        lambda session_id, path, caption, mime: delivered.append(path) or True)

    agent = {'id': 'agent-wp', 'session_id': 'sess-wp', 'workplace_id': 'wp-1'}
    send_file.execute(agent, {'file_path': '/workspace/a/report.xlsx'})
    send_file.execute(agent, {'file_path': '/workspace/b/report.xlsx'})

    assert delivered[0] != delivered[1]
    assert [open(p, 'rb').read() for p in delivered] == [b'first', b'second']

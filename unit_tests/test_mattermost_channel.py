"""Tests for the Mattermost channel implementation."""

from backend.channels.mattermost import (
    MattermostChannel,
    _split_message,
    session_key_for,
    strip_bot_mention,
)


def test_get_channel_type():
    assert MattermostChannel.get_channel_type() == 'mattermost'


def test_registered_in_channel_types():
    from backend.channels.registry import CHANNEL_TYPES
    assert CHANNEL_TYPES.get('mattermost') is MattermostChannel


def test_session_keys_for_dm_and_thread():
    assert session_key_for('D', 'chan1', {'id': 'p1'}) == ('mm:dm:chan1', '')
    assert session_key_for('G', 'chan2', {'id': 'p2'}) == ('mm:dm:chan2', '')
    assert session_key_for('O', 'chan3', {'id': 'p3'}) == ('mm:thread:chan3:p3', 'p3')
    assert session_key_for('P', 'chan4', {'id': 'p4', 'root_id': 'root'}) == ('mm:thread:chan4:root', 'root')


def test_strip_bot_mention_uses_username_boundary():
    assert strip_bot_mention('@evonic hello', 'evonic') == 'hello'
    assert strip_bot_mention('hello @evonic', 'evonic') == 'hello'
    assert strip_bot_mention('@evonic-bot should stay', 'evonic') == '@evonic-bot should stay'


def test_split_message_hard_cut_on_unbreakable_text():
    chunks = list(_split_message('x' * 31000, limit=15000))
    assert len(chunks) == 3
    assert all(len(c) <= 15000 for c in chunks)
    assert ''.join(chunks) == 'x' * 31000


def _make_channel(db, mode='open'):
    agent_id = 'mm_agent'
    db.create_agent({'id': agent_id, 'name': 'Mattermost Agent', 'system_prompt': ''})
    chan_id = db.create_channel({
        'agent_id': agent_id,
        'type': 'mattermost',
        'name': 'Mattermost Test',
        'config': {'base_url': 'https://mm.example', 'bot_token': 'token', 'mode': mode},
    })
    chan = MattermostChannel(chan_id, agent_id, {'base_url': 'https://mm.example', 'bot_token': 'token', 'mode': mode})
    chan.bot_user_id = 'bot-user'
    chan.bot_username = 'evonic'
    chan.sent = []
    chan._post = lambda mm_channel_id, message, root_id=None, file_ids=None: chan.sent.append({
        'channel_id': mm_channel_id,
        'message': message,
        'root_id': root_id,
        'file_ids': file_ids,
    }) or {'id': f'post-{len(chan.sent)}'}
    return chan


class _FakeRuntime:
    def __init__(self):
        self.calls = []

    def handle_message(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {'response': 'agent response'}


def test_bot_authored_message_ignored():
    from models.db import db
    chan = _make_channel(db)
    chan._handle_post(
        {'id': 'p1', 'user_id': 'bot-user', 'channel_id': 'c1', 'message': 'loop'},
        {'channel_type': 'D'},
    )
    assert chan.sent == []


def test_public_top_level_requires_authoritative_mention(monkeypatch):
    from models.db import db
    import backend.agent_runtime as runtime_module

    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_module, 'agent_runtime', fake_runtime)
    chan = _make_channel(db)

    chan._handle_post(
        {'id': 'p1', 'user_id': 'user1', 'channel_id': 'c1', 'message': '@evonic hi'},
        {'channel_type': 'O', 'mentions': []},
    )
    assert fake_runtime.calls == []
    assert chan.sent == []

    chan._handle_post(
        {'id': 'p2', 'user_id': 'user1', 'channel_id': 'c1', 'message': '@evonic hi'},
        {'channel_type': 'O', 'mentions': ['bot-user']},
    )
    assert len(fake_runtime.calls) == 1
    assert fake_runtime.calls[0][0][1] == 'mm:thread:c1:p2'
    assert chan.sent[-1]['root_id'] == 'p2'
    assert chan.sent[-1]['message'] == 'agent response'


def test_owned_thread_continues_without_mention(monkeypatch):
    from models.db import db
    import backend.agent_runtime as runtime_module

    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_module, 'agent_runtime', fake_runtime)
    chan = _make_channel(db)

    db.upsert_mattermost_thread(chan.channel_id, chan.agent_id, 'c1', 'root1', 'user1')
    chan._handle_post(
        {'id': 'p3', 'root_id': 'root1', 'user_id': 'user1', 'channel_id': 'c1', 'message': 'continue'},
        {'channel_type': 'O', 'mentions': []},
    )
    assert len(fake_runtime.calls) == 1
    assert fake_runtime.calls[0][0][1] == 'mm:thread:c1:root1'
    assert chan.sent[-1]['root_id'] == 'root1'


def test_unapproved_user_gets_pairing_prompt():
    from models.db import db
    chan = _make_channel(db, mode='restricted')
    chan._handle_post(
        {'id': 'p1', 'user_id': 'user1', 'channel_id': 'c1', 'message': 'hi'},
        {'channel_type': 'D'},
    )
    pendings = db.get_pending_approvals(chan.channel_id)
    assert any(p['external_user_id'] == 'user1' for p in pendings)
    assert any('pairing code' in s['message'].lower() for s in chan.sent)


def test_approval_action_resolves(monkeypatch):
    from models.db import db
    import backend.agent_runtime.approval as approval_module

    chan = _make_channel(db)
    resolved = []
    monkeypatch.setattr(approval_module.approval_registry, 'resolve', lambda aid, decision: resolved.append((aid, decision)) or True)
    response, status = chan.handle_approval_action({
        'context': {
            'channel_id': chan.channel_id,
            'token': chan.channel_id,
            'approval_id': 'approval-1',
            'decision': 'approve',
        }
    })
    assert status == 200
    assert resolved == [('approval-1', 'approve')]
    assert response['update']['message'] == 'Approved by user.'


def test_progress_post_persisted_and_final_patches_existing(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    db.upsert_mattermost_thread(chan.channel_id, chan.agent_id, 'c1', 'root1', 'user1')
    chan._reply_roots['mm:thread:c1:root1'] = ('c1', 'root1')
    patches = []
    original_request = chan._request

    def fake_request(method, path, **kwargs):
        if method == 'PUT':
            patches.append((path, kwargs.get('json')))
            return {}
        return original_request(method, path, **kwargs)

    chan._request = fake_request
    chan.patch_progress('mm:thread:c1:root1', '⏳ Preparing context…')

    row = db.get_mattermost_thread(chan.channel_id, 'c1', 'root1')
    assert row['progress_post_id'] == 'post-1'

    chan._do_send('mm:thread:c1:root1', 'final answer')
    assert patches[-1] == ('/api/v4/posts/post-1/patch', {'message': 'final answer'})


def test_attachment_download_and_persistence(tmp_path, monkeypatch):
    from models.db import db
    monkeypatch.chdir(tmp_path)
    chan = _make_channel(db)
    session_id = db.get_or_create_session(chan.agent_id, 'mm:dm:c1', chan.channel_id, 'mattermost')
    monkeypatch.setattr(db, 'get_agent_attachment_config', lambda agent_id: {'enabled': True, 'max_size_mb': 20})
    monkeypatch.setattr(chan, '_download_file', lambda file_id: b'hello attachment')
    post = {'metadata': {'files': [{'id': 'file1', 'name': 'note.txt', 'mime_type': 'text/plain', 'size': 16}]}}

    image_url, video_url, info_lines = chan._ingest_attachments(
        post, chan.agent_id, session_id, 'mm:dm:c1', chan.channel_id, db,
    )

    assert image_url is None
    assert video_url is None
    assert len(info_lines) == 1
    assert '[Attached: note.txt' in info_lines[0]
    assert db.list_session_attachments(session_id, chan.agent_id)[0]['channel_type'] == 'mattermost'


def test_start_persists_random_action_token(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    chan._request = lambda method, path, **kwargs: {'id': 'bot-user', 'username': 'evonic'}
    chan._websocket_loop = lambda: None

    chan.start()
    saved = db.get_channel(chan.channel_id)['config']
    assert saved['action_token']
    assert saved['action_token'] != chan.channel_id
    chan.stop()

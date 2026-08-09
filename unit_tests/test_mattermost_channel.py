"""Tests for the Mattermost channel implementation."""

import uuid

import requests

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
    agent_id = f'mm_agent_{uuid.uuid4().hex}'
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


def test_progress_post_persisted_and_final_posts_fresh_reply(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    db.upsert_mattermost_thread(chan.channel_id, chan.agent_id, 'c1', 'root1', 'user1')
    chan._reply_roots['mm:thread:c1:root1'] = ('c1', 'root1')
    patches = []
    deletes = []
    original_request = chan._request

    def fake_request(method, path, **kwargs):
        if method == 'DELETE':
            deletes.append(path)
            return {}
        if method == 'PUT' and '/patch' in path:
            patches.append((path, kwargs.get('json', {}).get('message', '')))
            return {}
        return original_request(method, path, **kwargs)

    chan._request = fake_request
    chan.patch_progress('mm:thread:c1:root1', '⏳ Preparing context…')

    row = db.get_mattermost_thread(chan.channel_id, 'c1', 'root1')
    assert row['progress_post_id'] == 'post-1'

    chan._do_send('mm:thread:c1:root1', 'final answer')

    # Final answer is posted as a fresh reply
    assert chan.sent[-1]['message'] == 'final answer'
    # Progress post is patched to ✅ Done, never soft-deleted
    assert deletes == []
    assert len(patches) == 1
    assert patches[0][0] == '/api/v4/posts/post-1/patch'
    assert patches[0][1] == '✅ Done'
    # DB progress reference is cleared
    row = db.get_mattermost_thread(chan.channel_id, 'c1', 'root1')
    assert row['progress_post_id'] is None


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


def test_request_preserves_timeout_restricts_origin_and_retries_get_only(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    calls = []
    sleeps = []

    class Resp:
        def __init__(self, status, body=b'{"ok": true}', headers=None):
            self.status_code = status
            self.content = body
            self.headers = headers or {'content-type': 'application/json'}
            self.closed = False

        def close(self):
            self.closed = True

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'status {self.status_code}')

        def json(self):
            return {'ok': True}

    def fake_request(method, url, timeout=None, **kwargs):
        calls.append((method, url, timeout))
        resp = Resp(500 if len(calls) < 3 else 200)
        calls[-1] = (method, url, timeout, resp)
        return resp

    monkeypatch.setattr(chan._http, 'request', fake_request)
    monkeypatch.setattr('backend.channels.mattermost.time.sleep', lambda seconds: sleeps.append(seconds))
    assert chan._request('GET', '/api/v4/test', timeout=7) == {'ok': True}
    assert [c[2] for c in calls] == [7, 7, 7]
    assert calls[0][3].closed is True
    assert calls[1][3].closed is True
    assert sleeps == [0.5, 1.0]

    calls.clear()
    try:
        chan._request('GET', 'https://evil.example/api/v4/test')
        assert False, 'cross-origin absolute URL should fail'
    except ValueError:
        pass

    chan._request('GET', 'https://mm.example/api/v4/test')
    assert calls[-1][1] == 'https://mm.example/api/v4/test'

    calls.clear()
    monkeypatch.setattr(chan._http, 'request', lambda method, url, timeout=None, **kwargs: calls.append((method, url, timeout)) or Resp(500))
    try:
        chan._request('PATCH', '/api/v4/posts/p1/patch')
        assert False, '5xx should raise'
    except RuntimeError:
        pass
    assert len(calls) == 1


def test_request_retries_get_transport_errors_and_rejects_non_json(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    calls = []

    class Resp:
        status_code = 200
        content = b'plain text'
        headers = {'content-type': 'text/plain'}

        def raise_for_status(self):
            pass

    def flaky(method, url, timeout=None, **kwargs):
        calls.append(method)
        if len(calls) == 1:
            raise requests.Timeout('timeout')
        return Resp()

    monkeypatch.setattr(chan._http, 'request', flaky)
    monkeypatch.setattr('backend.channels.mattermost.time.sleep', lambda seconds: None)
    try:
        chan._request('GET', '/api/v4/test')
        assert False, 'non-json success should fail clearly'
    except ValueError as exc:
        assert 'non-JSON' in str(exc)
    assert calls == ['GET', 'GET']

    calls.clear()
    monkeypatch.setattr(chan._http, 'request', lambda method, url, timeout=None, **kwargs: calls.append(method) or (_ for _ in ()).throw(requests.ConnectionError('down')))
    try:
        chan._request('POST', '/api/v4/posts')
        assert False, 'POST transport error should fail without retry'
    except requests.ConnectionError:
        pass
    assert calls == ['POST']


def test_pair_code_bound_to_other_user_rejected():
    from models.db import db
    chan = _make_channel(db, mode='restricted')
    pair_code = db._generate_pair_code()
    db.create_pending_approval(chan.channel_id, 'other-user', 'Other', pair_code, '2999-01-01T00:00:00')

    chan._handle_post(
        {'id': 'p1', 'user_id': 'user1', 'channel_id': 'c1', 'message': pair_code},
        {'channel_type': 'D'},
    )
    assert not db.is_user_allowed(chan.channel_id, 'user1')
    assert any('not valid for this account' in s['message'] for s in chan.sent)


def test_pair_code_for_other_channel_rejected():
    from models.db import db
    chan = _make_channel(db, mode='restricted')
    other_chan_id = db.create_channel({
        'agent_id': chan.agent_id,
        'type': 'mattermost',
        'name': 'Other Mattermost',
        'config': {'mode': 'restricted'},
    })
    pair_code = db._generate_pair_code()
    db.create_pending_approval(other_chan_id, 'user1', 'User', pair_code, '2999-01-01T00:00:00')

    chan._handle_post(
        {'id': 'p1', 'user_id': 'user1', 'channel_id': 'c1', 'message': pair_code},
        {'channel_type': 'D'},
    )
    assert not db.is_user_allowed(chan.channel_id, 'user1')
    assert any('not valid for this channel' in s['message'] for s in chan.sent)


def test_disabled_bot_persists_attachment_only_placeholder(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    monkeypatch.setattr(chan, '_ingest_attachments', lambda *args, **kwargs: (None, None, []))
    session_id = db.get_or_create_session(chan.agent_id, 'mm:dm:c1', chan.channel_id, 'mattermost')
    db.set_session_bot_enabled(session_id, False, agent_id=chan.agent_id)

    chan._handle_post(
        {'id': 'p1', 'user_id': 'user1', 'channel_id': 'c1', 'message': '', 'metadata': {'files': [{'id': 'f1'}]}},
        {'channel_type': 'D'},
    )
    messages = db.get_session_messages(session_id)
    assert messages[-1]['role'] == 'user'
    assert messages[-1]['content'] == '[Attachment]'


def test_duplicate_post_id_processed_once(monkeypatch):
    from models.db import db
    import backend.agent_runtime as runtime_module

    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_module, 'agent_runtime', fake_runtime)
    chan = _make_channel(db)
    post = {'id': 'dup1', 'user_id': 'user1', 'channel_id': 'c1', 'message': 'hi'}
    chan._handle_post(dict(post), {'channel_type': 'D'})
    chan._handle_post(dict(post), {'channel_type': 'D'})
    assert len(fake_runtime.calls) == 1
    assert len(chan.sent) == 1


def test_post_validates_file_ids(monkeypatch):
    from models.db import db
    chan = _make_channel(db)
    payloads = []
    monkeypatch.setattr(chan, '_request', lambda method, path, **kwargs: payloads.append(kwargs['json']) or {'id': 'p1'})

    MattermostChannel._post(chan, 'c1', 'file', file_ids=(str(i) for i in [1, 2]))
    assert payloads[-1]['file_ids'] == ['1', '2']
    try:
        MattermostChannel._post(chan, 'c1', 'file', file_ids='abc')
        assert False, 'string file_ids should be rejected'
    except ValueError:
        pass

import base64

from PIL import Image

from backend.agent_runtime import native_images


class _FakeDb:
    def __init__(self, records):
        self.records = records

    def list_session_attachments(self, session_id, agent_id):
        return self.records


def _png(tmp_path, name='a.png', size=(40, 30)):
    path = tmp_path / name
    Image.new('RGB', size, (10, 120, 80)).save(path)
    return str(path)


def _msg(attachment_id):
    return {'role': 'user', 'content': (
        'recreate this\n\n[Attachment: a.png (image/png, 1 KB)]\n'
        f'Attachment ID: {attachment_id}\nFile path: /etc/passwd')}


def test_mode_needs_vision_and_respects_describe_override():
    assert native_images.native_mode_enabled({'vision_supported': True}, {})
    assert not native_images.native_mode_enabled({'vision_supported': False}, {})
    assert not native_images.native_mode_enabled(
        {'vision_supported': True}, {'image_input_mode': 'describe'})


def test_recent_image_attachment_is_attached_without_mutating_history(tmp_path):
    records = [{'id': 7, 'mime_type': 'image/png', 'file_path': _png(tmp_path)}]
    history = [{'role': 'system', 'content': 's'}, _msg(7)]
    out = native_images.attach_native_images(history, 'sess', 'agent', _FakeDb(records))
    parts = out[1]['content']
    assert parts[0]['type'] == 'text' and 'Attachment ID: 7' in parts[0]['text']
    assert parts[1]['type'] == 'image_url'
    url = parts[1]['image_url']['url']
    assert url.startswith('data:image/')
    assert base64.b64decode(url.split(',', 1)[1])
    assert isinstance(history[1]['content'], str)  # canonical history untouched


def test_only_database_owned_image_ids_are_read(tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_text('x')
    records = [{'id': 8, 'mime_type': 'text/plain', 'file_path': str(secret)}]
    out = native_images.attach_native_images([_msg(8), _msg(99)], 's', 'a', _FakeDb(records))
    assert all(isinstance(m['content'], str) for m in out)


def test_large_images_are_downscaled_and_old_turns_ignored(tmp_path):
    records = [{'id': 1, 'mime_type': 'image/png', 'file_path': _png(tmp_path, size=(3000, 2000))}]
    old = [_msg(1)] + [{'role': 'user', 'content': 'next'} for _ in range(3)]
    assert isinstance(native_images.attach_native_images(old, 's', 'a', _FakeDb(records))[0]['content'], str)
    out = native_images.attach_native_images([_msg(1)], 's', 'a', _FakeDb(records))
    raw = base64.b64decode(out[0]['content'][1]['image_url']['url'].split(',', 1)[1])
    import io
    assert max(Image.open(io.BytesIO(raw)).size) <= native_images.MAX_EDGE

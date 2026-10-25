from backend.agent_runtime import context as ctx


def test_every_attachment_gets_a_note_with_its_id():
    infos = [
        {'filename': f'image{i}.png', 'mime_type': 'image/png', 'size_bytes': 10,
         'attachment_id': 100 + i, 'file_path': f'/tmp/image{i}.png'}
        for i in range(3)
    ]
    note = ctx.build_attachment_notes(infos, has_describe_image=True)
    for i in range(3):
        assert f'Attachment ID: {100 + i}' in note
    assert '[Attachment #3:' in note

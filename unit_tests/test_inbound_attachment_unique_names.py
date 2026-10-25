"""Regression tests: inbound channel media received in the same instant must not
overwrite each other.

Inbound WhatsApp/Telegram/Discord attachments used to be named
``<int(time.time())>_<name>``. A burst of photos sent within one second all got
the same path, so every attachment row pointed at the file of the last photo.
"""

import os

import pytest

from backend.channels import base
from backend.channels.base import unique_attachment_path
from backend.channels.whatsapp import WhatsAppChannel


@pytest.fixture
def frozen_clock(monkeypatch):
    """Every clock read returns the same instant (a burst within one tick)."""
    monkeypatch.setattr(base.time, 'time_ns', lambda: 1_790_395_520_000_000_000)
    monkeypatch.setattr('time.time', lambda: 1_790_395_520.0)


def test_unique_attachment_path_never_repeats_within_one_instant(tmp_path, frozen_clock):
    paths = [unique_attachment_path(str(tmp_path / 'att'), 'whatsapp.jpg') for _ in range(50)]

    assert len(set(paths)) == 50
    assert all(os.path.basename(p).endswith('_whatsapp.jpg') for p in paths)
    assert (tmp_path / 'att').is_dir()


def test_unique_attachment_path_skips_existing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(base, '_last_attachment_stamp', 0)
    monkeypatch.setattr(base.time, 'time_ns', lambda: 1000)
    (tmp_path / '1000_doc.xlsx').write_bytes(b'from another process')

    path = unique_attachment_path(str(tmp_path), 'doc.xlsx')

    assert os.path.basename(path) == '1001_doc.xlsx'


def _whatsapp_channel():
    from models.db import db

    db.create_agent({'id': 'agent-burst', 'name': 'Burst Agent'})
    db.update_agent('agent-burst', {'attachments_enabled': True, 'attachment_max_size_mb': 20})
    channel_id = db.create_channel({
        'agent_id': 'agent-burst', 'type': 'whatsapp',
        'name': 'WhatsApp Burst', 'config': {'mode': 'open'},
    })
    return WhatsAppChannel(channel_id, 'agent-burst', {'mode': 'open'})


def test_whatsapp_photo_burst_keeps_every_image(tmp_path, monkeypatch, frozen_clock):
    monkeypatch.chdir(tmp_path)
    channel = _whatsapp_channel()
    images = [f'screenshot-{i}'.encode() * 50 for i in range(8)]

    infos = [channel._save_image_attachment('sess-burst', '628111', img, 'image/jpeg')
             for img in images]

    assert len({info['file_path'] for info in infos}) == 8
    for img, info in zip(images, infos):
        with open(info['file_path'], 'rb') as fh:
            assert fh.read() == img
        assert info['filename'] == os.path.basename(info['file_path'])


def test_whatsapp_documents_with_same_name_in_one_instant_are_kept(tmp_path, monkeypatch, frozen_clock):
    monkeypatch.chdir(tmp_path)
    channel = _whatsapp_channel()
    first = channel._save_document_attachment(
        'sess-doc', '628111', b'version one', 'application/pdf', 'report.pdf')
    second = channel._save_document_attachment(
        'sess-doc', '628111', b'version two', 'application/pdf', 'report.pdf')

    assert first['file_path'] != second['file_path']
    with open(first['file_path'], 'rb') as fh:
        assert fh.read() == b'version one'
    with open(second['file_path'], 'rb') as fh:
        assert fh.read() == b'version two'

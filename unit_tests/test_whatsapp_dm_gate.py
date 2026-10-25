"""Tests for the WhatsApp direct-message pairing gate.

A brand-new sender must always end up in the channel's Pending Approvals so an
admin can approve them from the channel modal — even when their first message
looks like a (stale) pairing code. Live EVN-XXXX codes still auto-approve.

Channels are instantiated but never start()ed — no Baileys bridge needed.
"""

import pytest

from backend.channels.whatsapp import WhatsAppChannel

SENDER = "628123"
JID = "628123@s.whatsapp.net"


@pytest.fixture
def wa_channel():
    """A dedicated (restricted) WhatsApp channel with an empty allowlist."""
    from models.db import db

    db.create_agent({"id": "agent-wa", "name": "WA Agent"})
    chan_id = db.create_channel({
        "agent_id": "agent-wa",
        "type": "whatsapp",
        "name": "Dedicated WA",
        "config": {"mode": "restricted", "allowed_users": []},
    })
    channel = db.get_channel(chan_id)
    ch = WhatsAppChannel(chan_id, "agent-wa", channel["config"])
    # No bridge in unit tests — record outbound sends instead of HTTP POSTs.
    sent = []
    ch._do_send = lambda user, text, *a, **kw: sent.append((user, text))
    ch._sent = sent
    return ch


def _dm(channel, text, sender=SENDER):
    return channel._gate_sender(sender, False, JID, text, "Andi", {})


def test_first_dm_with_ordinary_text_creates_pending(wa_channel):
    from models.db import db

    assert _dm(wa_channel, "Hi khodam") is False

    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1
    assert pend[0]["external_user_id"] == SENDER
    assert pend[0]["pair_code"].startswith("EVN-")
    # The user is guided instead of being answered with a pairing-code error.
    assert any("not yet approved" in t for _, t in wa_channel._sent)


def test_first_dm_with_unmatched_code_still_creates_pending(wa_channel):
    from models.db import db

    assert _dm(wa_channel, "EVN-ZZZZ") is False

    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1
    assert pend[0]["external_user_id"] == SENDER


def test_live_code_auto_approves_sender(wa_channel):
    from models.db import db

    # First contact creates the pending approval (and its EVN code).
    _dm(wa_channel, "halo")
    code = db.get_pending_approvals(wa_channel.channel_id)[0]["pair_code"]
    assert db.is_user_allowed(wa_channel.channel_id, SENDER) is False

    # Sending the live code back approves the sender.
    assert _dm(wa_channel, code) is False
    assert db.is_user_allowed(wa_channel.channel_id, SENDER) is True
    assert db.get_pending_approvals(wa_channel.channel_id) == []


def test_repeat_ordinary_dms_do_not_spam(wa_channel):
    from models.db import db

    _dm(wa_channel, "Hi khodam")
    _dm(wa_channel, "are you there?")

    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1
    # Only the first message triggered the guidance reply.
    guidance = [t for _, t in wa_channel._sent if "not yet approved" in t]
    assert len(guidance) == 1


def test_retried_unmatched_code_keeps_pending_and_warns(wa_channel):
    from models.db import db

    _dm(wa_channel, "hi")
    sent_before = len(wa_channel._sent)

    _dm(wa_channel, "EVN-ZZZZ")

    pend = db.get_pending_approvals(wa_channel.channel_id)
    assert len(pend) == 1
    assert any(
        "invalid or has expired" in t for _, t in wa_channel._sent[sent_before:]
    )

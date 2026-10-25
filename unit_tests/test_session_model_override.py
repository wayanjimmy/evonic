"""A chat session can pick its own model without touching the agent's config."""

import json


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _setup(db):
    db.create_provider({"id": "sm-prov", "name": "SM", "type": "remote",
                        "endpoint": "https://x.example.invalid/v1",
                        "adapter_key": "openai.chat_completions.v1"})
    for mid in ("sm-a", "sm-b"):
        db.create_model({"id": mid, "name": mid, "type": "remote", "provider": "sm-prov",
                         "model_name": mid, "enabled": True})
    db.create_agent({"id": "sm_agent", "name": "SM Agent", "model_id": "sm-a",
                     "fallback_model_id": "sm-b"})


def test_override_is_session_scoped_and_leaves_agent_untouched():
    from models.db import db

    _setup(db)
    c = _client()
    r = c.post("/api/agents/sm_agent/chat/model", json={"session_id": "s1", "model_id": "sm-b"})
    assert r.get_json()["success"] is True

    listing = c.get("/api/agents/sm_agent/chat/models?session_id=s1").get_json()
    assert listing["override_id"] == "sm-b"
    assert listing["primary_id"] == "sm-a" and listing["fallback_id"] == "sm-b"
    # another session is unaffected
    other = c.get("/api/agents/sm_agent/chat/models?session_id=s2").get_json()
    assert other["override_id"] is None

    agent = db.get_agent("sm_agent")
    assert agent["model_id"] == "sm-a" and agent["fallback_model_id"] == "sm-b"

    state = c.get("/api/agents/sm_agent/chat/state?session_id=s1").get_json()
    assert state["active_model"]["id"] == "sm-b"
    assert state["active_model"]["is_session_override"] is True

    c.post("/api/agents/sm_agent/chat/model", json={"session_id": "s1", "model_id": None})
    assert c.get("/api/agents/sm_agent/chat/models?session_id=s1").get_json()["override_id"] is None


def test_rejects_unknown_model():
    from models.db import db

    _setup(db)
    r = _client().post("/api/agents/sm_agent/chat/model", json={"session_id": "s1", "model_id": "nope"})
    assert r.status_code == 400

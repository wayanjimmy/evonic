"""Deleting a provider can take its models along, without leaving agents pointing at them."""


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _setup(db, default_model=None):
    db.create_provider({"id": "cd-prov", "name": "CD", "type": "remote",
                        "endpoint": "https://x.example.invalid/v1",
                        "adapter_key": "openai.chat_completions.v1"})
    for mid in ("cd-a", "cd-b"):
        db.create_model({"id": mid, "name": mid, "type": "remote", "provider": "cd-prov",
                         "model_name": mid, "enabled": True, "is_default": mid == default_model})
    if db.get_agent("cd_agent"):
        db.delete_agent("cd_agent")
    db.create_agent({"id": "cd_agent", "name": "CD Agent", "model_id": "cd-a", "fallback_model_id": "cd-b"})


def test_plain_delete_still_refuses_while_models_exist():
    from models.db import db

    _setup(db)
    r = _client().delete("/api/providers/cd-prov")
    assert r.status_code == 409 and db.get_provider("cd-prov")


def test_impact_lists_models_and_agents():
    from models.db import db

    _setup(db)
    impact = _client().get("/api/providers/cd-prov/delete-impact").get_json()
    assert {m["id"] for m in impact["models"]} == {"cd-a", "cd-b"}
    assert [a["id"] for a in impact["agents"]] == ["cd_agent"]
    assert impact["is_default"] is False


def test_cascade_removes_models_and_clears_agent_references():
    from models.db import db

    _setup(db)
    r = _client().delete("/api/providers/cd-prov?cascade=1").get_json()
    assert r == {"success": True, "models_removed": 2}
    assert db.get_provider("cd-prov") is None
    assert db.get_model_by_id("cd-a") is None
    agent = db.get_agent("cd_agent")
    assert not agent["model_id"] and not agent["fallback_model_id"]


def test_cascade_is_blocked_while_a_model_is_the_global_default():
    from models.db import db

    _setup(db, default_model="cd-a")
    r = _client().delete("/api/providers/cd-prov?cascade=1")
    assert r.status_code == 409
    assert db.get_provider("cd-prov") and db.get_model_by_id("cd-a")

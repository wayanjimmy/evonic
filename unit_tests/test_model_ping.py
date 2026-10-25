"""Ping sends a real prompt through the model and never borrows another model's answer."""

from unittest.mock import patch


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _model(db):
    db.create_provider({"id": "pg-prov", "name": "PG", "type": "remote",
                        "endpoint": "https://x.example.invalid/v1",
                        "adapter_key": "openai.chat_completions.v1"})
    db.create_model({"id": "pg-a", "name": "pg-a", "type": "remote", "provider": "pg-prov",
                     "model_name": "pg-a", "enabled": True})


class _Ok:
    def __init__(self, model_config=None, _allow_fallback=True):
        _Ok.kwargs = {"allow_fallback": _allow_fallback, "model": model_config["model_name"]}

    def chat_completion(self, **kw):
        _Ok.call = kw
        return {"success": True, "response": {"choices": [{"message": {"content": "pong"}}]}}


class _Fail:
    def __init__(self, model_config=None, _allow_fallback=True):
        pass

    def chat_completion(self, **kw):
        return {"success": False, "error_type": "auth_error", "error_detail": "401 bad key"}


def test_ping_success_uses_the_model_without_fallback():
    from models.db import db

    _model(db)
    with patch("backend.llm_client.LLMClient", _Ok):
        data = _client().post("/api/models/pg-a/ping").get_json()
    assert data["success"] is True and data["reply"] == "pong" and data["latency_ms"] >= 0
    assert _Ok.kwargs == {"allow_fallback": False, "model": "pg-a"}
    assert _Ok.call["enable_thinking"] is False


def test_ping_reports_the_failure_reason():
    from models.db import db

    _model(db)
    with patch("backend.llm_client.LLMClient", _Fail):
        data = _client().post("/api/models/pg-a/ping").get_json()
    assert data["success"] is False and "401 bad key" in data["error"]


def test_ping_unknown_model_is_404():
    assert _client().post("/api/models/nope/ping").status_code == 404

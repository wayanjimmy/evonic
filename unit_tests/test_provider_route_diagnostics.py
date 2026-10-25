"""Provider diagnostics use versioned route adapters and never send prompts."""

from unittest.mock import MagicMock, patch


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _response(payload):
    response = MagicMock(status_code=200)
    response.json.return_value = payload
    response.text = ""
    return response


def test_ollama_discovery_uses_adapter_key_not_legacy_format_or_locality():
    from models.db import db

    provider_id = db.create_provider({
        "id": "adapter-ollama", "name": "Adapter Ollama", "type": "remote",
        "endpoint": "https://cloud.example.invalid/api", "adapter_key": "ollama.chat.v1",
        # This conflicting legacy value proves routing does not select from it.
        "api_format": "openai",
    })
    with patch("routes.providers.requests.get", return_value=_response({"models": [{"name": "local-model"}]})) as get:
        result = _client().post(f"/api/providers/{provider_id}/fetch-models")

    assert result.status_code == 200
    assert result.get_json()["models"][0]["id"] == "local-model"
    assert get.call_args.args[0] == "https://cloud.example.invalid/api/tags"


def test_provider_test_is_a_prompt_free_adapter_discovery_probe():
    from models.db import db

    provider_id = db.create_provider({
        "id": "adapter-test", "name": "Adapter Test", "type": "remote",
        "endpoint": "http://127.0.0.1:11434", "adapter_key": "ollama.chat.v1",
    })
    with patch("routes.providers.requests.get", return_value=_response({"models": [{"name": "one"}]})) as get:
        result = _client().post(f"/api/providers/{provider_id}/test")

    assert result.status_code == 200
    assert result.get_json() == {"success": True, "message": "Connected (1 models available)"}
    assert get.call_args.args[0] == "http://127.0.0.1:11434/tags"
    assert "json" not in get.call_args.kwargs


def test_invalid_adapter_diagnostic_does_not_expose_endpoint_or_credentials():
    from models.db import db

    provider_id = db.create_provider({
        "id": "safe-diagnostic", "name": "Safe Diagnostic", "type": "remote",
        "endpoint": "https://private.example.invalid/v1", "adapter_key": "openai.chat_completions.v1",
        "api_key": "must-not-leak",
    })
    with db._connect() as conn:
        conn.execute("UPDATE providers SET adapter_key = ? WHERE id = ?", ("unknown.protocol.v1", provider_id))
        conn.commit()

    response = _client().post(f"/api/providers/{provider_id}/test")
    payload = response.get_json()
    assert response.status_code == 400
    assert payload["success"] is False
    assert "private.example.invalid" not in str(payload)
    assert "must-not-leak" not in str(payload)

"""Privacy, query, authorization, and failure-isolation coverage for Decision Activity."""
from dataclasses import dataclass

from app import app
from models.db import db
from backend.tools.lib.decision_models import DecisionFieldResult, DecisionResult, ProviderMetadata, TimingMetadata
from backend.tools.lib.decision_runtime import DecisionRuntime, RuntimeRequest


def _event(**overrides):
    event = {"source": "production", "profile_key": "application.example", "profile_version": "v1",
             "model_id": "test/model", "adapter_key": "test", "outcome_summary": {"approved": True},
             "confidence": 0.9, "latency_ms": 12, "attempts": 1, "created_at": 100.0}
    event.update(overrides)
    return event


def _client():
    client = app.test_client()
    with client.session_transaction() as session: session["authenticated"] = True
    return client


def test_activity_sanitizes_summary_bounds_and_supports_redaction_retention():
    event_id = db.record_decision_activity(_event(outcome_summary={"approved": True, "bad-key": "drop", "raw_response": "drop", "nested": {"raw": "drop"}, "note": "x" * 999, "comment": "token=drop"}, correlation_id="c" * 999))
    detail = db.get_decision_activity(event_id)
    assert detail["outcome_summary"] == {"approved": True, "note": "x" * 256}
    assert len(detail["correlation_id"]) == 256
    assert db.redact_decision_activity(event_id)
    assert db.get_decision_activity(event_id) is None
    old = db.record_decision_activity(_event(created_at=1.0))
    assert db.purge_decision_activity(2.0) == 1
    assert db.get_decision_activity(old) is None


def test_activity_query_is_deterministic_and_admin_authorized():
    first = db.record_decision_activity(_event(created_at=200.0, source="playground"))
    second = db.record_decision_activity(_event(created_at=200.0, source="profile_test", error_code="internal"))
    client = _client()
    page = client.get("/api/admin/decision-activity?limit=1")
    assert page.status_code == 200
    payload = page.get_json()
    assert len(payload["events"]) == 1 and payload["next_cursor"]
    next_page = client.get("/api/admin/decision-activity?limit=1&cursor=" + payload["next_cursor"])
    assert next_page.get_json()["events"][0]["id"] != payload["events"][0]["id"]
    filtered = client.get("/api/admin/decision-activity?source=profile_test&result_class=error")
    assert [item["id"] for item in filtered.get_json()["events"]] == [second]
    assert client.get("/api/admin/decision-activity/" + first).status_code == 200
    assert app.test_client().get("/api/admin/decision-activity").status_code == 401


class _RuntimeDB:
    def __init__(self): self.events = []
    def get_decision_profile(self, key):
        return {"profile_key": key, "enabled": True, "mode": "enforce", "policy_version": "v1", "schema_version": "decision-schema/v1", "schema": {"version": "decision-schema/v1", "fields": [{"key": "approved", "kind": "boolean"}]}, "primary_model_id": "test/model", "fallback_model_id": None, "timeout_ms": 1000, "retry_limit": 0, "minimum_confidence": 0}
    def validate_decision_profile(self, data, *, existing=None): return data
    def get_model_by_id(self, model_id): return {"id": model_id, "provider": "test", "model_name": "model", "capabilities": {"decision": {"enabled": True, "schema_versions": ["decision-schema/v1"], "structured_output": True, "schema_primitives": ["boolean"]}}}
    def record_decision_activity(self, event): raise OSError("telemetry unavailable")


class _Adapter:
    key = "test"
    def decide(self, invocation):
        return DecisionResult(invocation.profile, invocation.schema.version, (DecisionFieldResult("approved", True, .9),), ProviderMetadata("test", "test/model"), TimingMetadata(1), 1)


def test_activity_write_failure_never_changes_runtime_result():
    runtime = DecisionRuntime(_RuntimeDB(), adapter_factory=lambda model: _Adapter())
    result = runtime.invoke(RuntimeRequest("application.example", "v1", {"safe": "input"}, activity_source="playground"))
    assert result.error is None and result.fields[0].value is True

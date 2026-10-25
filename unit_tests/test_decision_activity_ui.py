"""Focused static coverage for the Decision Activity administration UI."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates/partials/settings/decision_models.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static/js/settings-decision-models.js").read_text(encoding="utf-8")


def test_activity_ui_exposes_all_observability_filters_and_states():
    for value in ("source", "profile_key", "model_id", "result_class", "error_code", "fallback_used", "correlation_id", 'name="from"', 'name="to"'):
        assert value in TEMPLATE
    for value in ("Loading activity", "No matching activity", "Could not load Decision Activity", "Load more", "dark:", "focus:ring-2"):
        assert value in TEMPLATE or value in SCRIPT
    assert "cursor" in SCRIPT and "next_cursor" in SCRIPT


def test_activity_detail_does_not_repurpose_outcome_as_rerun_input():
    assert "event.rerun && event.rerun.allowed" in SCRIPT
    assert "JSON.stringify(rerun.input_context" in SCRIPT
    assert "JSON.stringify(summary, null, 2)" not in SCRIPT.split("async rerunActivity", 1)[1]
    assert "event.source === \"production\"" in SCRIPT
    assert "it never runs automatically" in SCRIPT
    assert "Production inputs, raw provider data, and normalized outputs" in SCRIPT


def test_decision_profile_save_errors_use_global_toasts():
    save_method = SCRIPT.split("async save(event)", 1)[1].split("showError(message)", 1)[0]

    assert 'toast.show(message, "error")' in save_method
    assert 'toast.show("Could not save profile. Check the values and try again.", "error")' in save_method
    assert "if (!response.success) {\n                const message" in save_method
    assert "this.showError((response.error" not in save_method
    assert "this.showError(\"Could not save profile" not in save_method

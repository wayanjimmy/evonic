"""Run As User does not apply to Evonet agents — the UI must say so and disable it.

Evonet agents execute through a tunnel workplace, whose commands run as the OS
user running the Evonet process on the target host. ``run_as_user`` is only
honoured by the local backend (``sudo -u``), so the agent settings page must
disable the field for tunnel workplaces and explain why.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _detail_html():
    return (ROOT / "templates/agent_detail.html").read_text(encoding="utf-8")


def test_run_as_user_field_has_note_container_and_ids():
    html = _detail_html()
    assert 'id="agent-run-as-user"' in html
    assert 'id="run-as-user-label"' in html
    assert 'id="run-as-user-note"' in html
    start = html.index('id="run-as-user-note"')
    assert "ag-note" in html[start:start + 120]


def test_run_as_user_help_text_requires_no_workplace():
    html = _detail_html()
    start = html.index("<strong>no Workplace is assigned</strong>")
    helper = html[start - 200:start + 200]
    assert "Only applies when" in helper


def test_run_as_user_note_explains_evonet_tunnel_user():
    html = _detail_html()
    assert "Evonet (tunnel) workplace" in html
    assert "Evonet tunnel process" in html


def test_update_run_as_user_visibility_disables_only_tunnel():
    html = _detail_html()
    start = html.index("function updateRunAsUserVisibility()")
    end = html.index("async function loadWorkplaceSelector()", start)
    fn = html[start:end]
    assert "homeType === 'tunnel'" in fn
    assert "input.disabled = true" in fn
    assert "input.disabled = false" in fn
    assert "run-as-user-note" in fn
    # The stored value must survive: disabling only, never clearing.
    assert "input.value" not in fn


def test_update_workspace_visibility_syncs_run_as_user():
    html = _detail_html()
    start = html.index("function updateWorkspaceVisibility()")
    end = html.index("// ==================== Workplace chip", start)
    fn = html[start:end]
    assert "updateRunAsUserVisibility();" in fn


def test_system_prompt_skips_run_as_user_claim_when_workplace_assigned():
    src = (ROOT / "backend/agent_runtime/context.py").read_text(encoding="utf-8")
    idx = src.index("Run-as-user awareness")
    block = src[idx:idx + 900]
    cond = [ln for ln in block.splitlines()
            if "run_as_user and not agent.get('sandbox_enabled')" in ln]
    assert cond, block
    assert "not agent.get('workplace_id')" in cond[0]
    assert block.index("workplace_id") < block.index("You are run as the")

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_initial_agent_state_refresh_resolves_session_before_requesting_state():
    detail = (ROOT / "templates/agent_detail.html").read_text(encoding="utf-8")
    start = detail.index("const _refreshSessionStateImpl = async function()")
    end = detail.index("// Debounced wrapper", start)
    refresh = detail[start:end]

    assert "const sessionId = await _getChatSessionId();" in refresh
    assert "const sid = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '';" in refresh
    assert refresh.index("await _getChatSessionId()") < refresh.index("/chat/state?user_id=web_test${sid}&include=summary")
    assert "StateUI.renderAgentState(AGENT_ID, sessionId" in refresh


def test_standalone_agent_state_refresh_uses_resolved_session_id():
    detail = (ROOT / "templates/agent_detail.html").read_text(encoding="utf-8")
    start = detail.index("async function refreshAgentState()")
    end = detail.index("document.addEventListener('evonic:agent-state-changed'", start)
    refresh = detail[start:end]

    assert "const sessionId = await _getChatSessionId();" in refresh
    assert "const sid = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '';" in refresh
    assert refresh.index("await _getChatSessionId()") < refresh.index("/chat/state?user_id=web_test${sid}")
    assert "StateUI.renderAgentState(AGENT_ID, sessionId" in refresh

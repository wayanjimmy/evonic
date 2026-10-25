"""Opt-in `switch_workplace` tool: gating, allowlist, and live effect."""

import json


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _setup(db, **agent_extra):
    for wid in ("ws-a", "ws-b"):
        try:
            db.create_workplace({"id": wid, "name": f"WP {wid}", "type": "remote",
                                 "config": {"host": "h", "username": "u"}})
        except Exception:
            pass
    spec = {"id": "ws_agent", "name": "WS Agent", "workplace_id": "ws-a"}
    spec.update(agent_extra)
    if db.get_agent("ws_agent"):
        db.delete_agent("ws_agent")
    db.create_agent(spec)


def _tools(ctx):
    from backend.tools import tool_registry
    return {t["function"]["name"] for t in tool_registry.get_builtin_tools(ctx)}


def test_tool_is_off_by_default_and_gated_on_the_flag():
    assert "switch_workplace" not in _tools({"id": "x"})
    assert "switch_workplace" in _tools({"id": "x", "workplace_switch_enabled": True})


def test_switch_moves_agent_and_updates_live_context():
    from backend.tools import tool_registry
    from models.db import db

    _setup(db, workplace_switch_enabled=1)
    ctx = {"id": "ws_agent", "workplace_switch_enabled": True, "workplace_id": "ws-a"}
    run = tool_registry.get_builtin_executor(ctx)
    listing = run("switch_workplace", {"action": "list"})
    assert {w["id"] for w in listing["workplaces"]} >= {"ws-a", "ws-b", "local"}
    assert [w for w in listing["workplaces"] if w["current"]][0]["id"] == "ws-a"

    res = run("switch_workplace", {"action": "switch", "workplace": "ws-b"})
    assert "error" not in res
    # the chat draws its divider from this, at the point of the call
    assert res["workplace_switch"]["from"]["id"] == "ws-a" and res["workplace_switch"]["to"]["id"] == "ws-b"
    assert db.get_agent("ws_agent")["workplace_id"] == "ws-b"
    assert ctx["workplace_id"] == "ws-b"      # later calls this turn follow the move

    back = run("switch_workplace", {"action": "switch", "workplace": "local"})
    assert not db.get_agent("ws_agent")["workplace_id"]
    assert back["workplace_switch"]["from"]["id"] == "ws-b" and back["workplace_switch"]["to"] is None


def test_allowlist_blocks_other_workplaces():
    from backend.workplaces import agent_switch
    from models.db import db

    _setup(db, workplace_switch_enabled=1, workplace_switch_allowed=json.dumps(["ws-b"]))
    assert agent_switch.switch("ws_agent", "ws-b")["ok"] is True
    denied = agent_switch.switch("ws_agent", "ws-a")
    assert denied["ok"] is False and db.get_agent("ws_agent")["workplace_id"] == "ws-b"
    # the local workspace can never be taken away, even though it is not in the list
    assert "local" in {o["id"] for o in agent_switch.list_options(db.get_agent("ws_agent"))}
    assert agent_switch.switch("ws_agent", "local")["ok"] is True


def test_disabled_agent_cannot_switch_and_sandbox_agent_cannot_leave_local():
    from backend.workplaces import agent_switch
    from models.db import db

    _setup(db)
    assert agent_switch.switch("ws_agent", "ws-b")["ok"] is False
    _setup(db, workplace_switch_enabled=1, sandbox_enabled=1, workplace_id=None)
    assert agent_switch.switch("ws_agent", "ws-b")["ok"] is False


def test_settings_api_round_trip():
    from models.db import db

    _setup(db)
    c = _client()
    r = c.put("/api/agents/ws_agent", json={"workplace_switch_enabled": 1,
                                            "workplace_switch_allowed": ["ws-a", "local"]})
    assert r.get_json()["success"] is True
    agent = c.get("/api/agents/ws_agent").get_json()
    assert agent["workplace_switch_enabled"] == 1
    assert json.loads(agent["workplace_switch_allowed"]) == ["ws-a", "local"]
    c.put("/api/agents/ws_agent", json={"workplace_switch_allowed": []})
    assert not c.get("/api/agents/ws_agent").get_json()["workplace_switch_allowed"]


def test_tools_tab_lists_switch_workplace_only_when_enabled():
    from models.db import db

    _setup(db)
    c = _client()

    def names():
        return {t["name"] for t in c.get("/api/settings/tools?agent_id=ws_agent").get_json()["tools"]}

    assert "switch_workplace" not in names()
    c.put("/api/agents/ws_agent", json={"workplace_switch_enabled": 1})
    assert "switch_workplace" in names()


def test_workplace_endpoint_follows_a_switch_the_agent_made():
    from backend.workplaces import agent_switch
    from models.db import db

    _setup(db, workplace_switch_enabled=1)
    c = _client()
    assert c.get("/api/agents/ws_agent/workplace").get_json() == {"workplace_id": "ws-a"}
    agent_switch.switch("ws_agent", "ws-b")
    assert c.get("/api/agents/ws_agent/workplace").get_json() == {"workplace_id": "ws-b"}
    agent_switch.switch("ws_agent", "local")
    assert c.get("/api/agents/ws_agent/workplace").get_json() == {"workplace_id": None}
    assert c.get("/api/agents/nope/workplace").status_code == 404


def test_tool_switch_writes_no_system_note_but_the_manual_path_still_does():
    from unittest.mock import patch
    from backend.tools import tool_registry
    from backend.workplaces import agent_switch
    from models.db import db

    _setup(db, workplace_switch_enabled=1)
    ctx = {"id": "ws_agent", "workplace_switch_enabled": True, "workplace_id": "ws-a"}
    with patch("backend.workplaces.switch_note.note_workplace_switch") as note:
        tool_registry.get_builtin_executor(ctx)("switch_workplace", {"action": "switch", "workplace": "ws-b"})
        # a note written mid-turn would only surface after the answer; the tool result carries the divider instead
        note.assert_not_called()
        agent_switch.switch("ws_agent", "ws-a")
        note.assert_called_once()

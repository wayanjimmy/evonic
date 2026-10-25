"""Tell an agent, inside its conversation, that its workplace was switched.

The system prompt already follows the agent's current workplace, but the
history keeps tool results from the previous one. A ``[SYSTEM]`` note at the
switch point tells the agent where it moved and that those results are stale.
"""

import logging
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)

# The web chat in templates/agent_detail.html always talks as this user.
WEB_CHAT_USER_ID = 'web_test'

_TYPE_LABELS = {'tunnel': 'Evonet tunnel', 'remote': 'SSH', 'bwrap': 'Bwrap sandbox', 'local': 'local'}


def describe_workplace(workplace: Optional[Dict[str, Any]], workspace: str,
                       connector: Optional[Dict[str, Any]] = None) -> str:
    """One-line description of where an agent's tools run.

    Network addresses (device IPs, SSH host/user/port) are deliberately left
    out: the agent does not need them, and they would sit in its history.
    """
    if not workplace:
        return f"the local workspace (working directory {workspace})"
    details = [_TYPE_LABELS.get(workplace.get('type'), workplace.get('type') or 'workplace')]
    if workplace.get('type') == 'tunnel' and connector and connector.get('device_name'):
        details.append(f"device {connector['device_name']}")
    if workspace:
        details.append(f"working directory {workspace}")
    return f"\"{workplace.get('name', workplace.get('id'))}\" ({' · '.join(details)})"


def _side(agent: Dict[str, Any]) -> Dict[str, Any]:
    """Description + UI metadata for the workplace an agent record points at."""
    from models.db import db
    from backend.agent_runtime.context import _resolve_workspace

    wp_id = agent.get('workplace_id') or None
    workplace = db.get_workplace(wp_id) if wp_id else None
    connector = db.get_connector_by_workplace(wp_id) if workplace and workplace.get('type') == 'tunnel' else None
    return {
        'text': describe_workplace(workplace, _resolve_workspace(agent), connector),
        'short': f"\"{workplace.get('name')}\"" if workplace else 'the local workspace',
        'meta': {'id': workplace['id'], 'name': workplace.get('name'), 'type': workplace.get('type')} if workplace else None,
    }


def build_switch_note(old_text: str, new_text: str, new_short: str) -> str:
    return (
        f"[SYSTEM] Workplace switched from {old_text} to {new_text}. "
        f"From now on your tools (bash, runpy, file operations) run on {new_short}. "
        "Tool results earlier in this conversation came from the previous workplace "
        "and may no longer apply."
    )


def note_workplace_switch(agent_before: Dict[str, Any], agent_after: Dict[str, Any]) -> bool:
    """Add the switch note to the agent's web chat session.

    Returns False when there is no web chat session yet (no history to correct).
    """
    from models.db import db
    from backend.agent_runtime import agent_runtime

    agent_id = agent_after['id']
    session_id = db.get_session_id(agent_id, WEB_CHAT_USER_ID)
    if not session_id:
        return False
    old, new = _side(agent_before), _side(agent_after)
    agent_runtime.add_session_note(
        agent_id, session_id,
        build_switch_note(old['text'], new['text'], new['short']),
        {'workplace_switch': {'from': old['meta'], 'to': new['meta']}},
        external_user_id=WEB_CHAT_USER_ID,
    )
    return True

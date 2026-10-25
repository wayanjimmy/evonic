"""Let an agent move itself between workplaces (opt-in `switch_workplace` tool).

The agent's ``workplace_id`` is agent-wide, exactly like the manual switcher in
the chat header, so a switch here is the same operation: it updates the agent
row and leaves a ``[SYSTEM]`` note in the web chat. The per-agent allowlist in
``workplace_switch_allowed`` (JSON array of workplace ids) limits where the agent
may go; an empty list means anywhere. The local workspace is always allowed.
"""

import json
import logging
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

LOCAL_ID = 'local'


def parse_allowed(raw: Any) -> List[str]:
    """Normalise the stored allowlist (JSON string, list or None) to a list of ids."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def _status(workplace_id: str) -> str:
    try:
        from backend.workplaces.manager import workplace_manager
        return workplace_manager.get_status(workplace_id).get('status') or 'unknown'
    except Exception:
        return 'unknown'


def list_options(agent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Workplaces this agent may switch to, flagged with the current one."""
    from models.db import db

    allowed = set(parse_allowed(agent.get('workplace_switch_allowed')))
    current = agent.get('workplace_id') or None
    # The local workspace is always available, whatever the allowlist says.
    options = [{'id': LOCAL_ID, 'name': 'Local workspace', 'type': 'local',
                'status': 'connected', 'current': current is None}]
    for wp in db.get_workplace_summaries():
        if allowed and wp['id'] not in allowed:
            continue
        options.append({
            'id': wp['id'], 'name': wp.get('name'), 'type': wp.get('type'),
            'status': _status(wp['id']), 'current': wp['id'] == current,
            'workspace_path': wp.get('workspace_path'),
            'device': (wp.get('connector') or {}).get('device_name'),
        })
    return options


def _resolve(options: List[Dict[str, Any]], target: str) -> Optional[Dict[str, Any]]:
    t = (target or '').strip().lower()
    for o in options:
        if o['id'].lower() == t:
            return o
    named = [o for o in options if (o.get('name') or '').lower() == t]
    return named[0] if len(named) == 1 else None


def switch(agent_id: str, target: str, note: bool = True) -> Dict[str, Any]:
    """Switch ``agent_id`` to ``target`` (workplace id or exact name, or ``local``).

    ``note=False`` skips the ``[SYSTEM]`` chat note: the switch_workplace tool uses it because
    its own result already tells the agent, and a note written mid-turn would only show up after
    the answer. The result then carries ``transition`` so the chat can draw the divider in place.

    Returns ``{'ok': True, ...}`` or ``{'ok': False, 'error': ...}``.
    """
    from models.db import db

    agent = db.get_agent(agent_id)
    if not agent:
        return {'ok': False, 'error': 'Agent not found.'}
    if not agent.get('workplace_switch_enabled'):
        return {'ok': False, 'error': 'Workplace switching is not enabled for this agent.'}

    options = list_options(agent)
    option = _resolve(options, target)
    if not option:
        return {'ok': False, 'error': f"Workplace '{target}' is not available to you. "
                                      "Call switch_workplace with action='list' to see the choices."}
    if option['current']:
        return {'ok': True, 'changed': False, 'workplace': option,
                'message': f"Already on \"{option['name']}\"."}
    if option['type'] == 'tunnel' and option['status'] != 'connected':
        return {'ok': False, 'error': f"\"{option['name']}\" is not connected right now "
                                      f"(status: {option['status']}); pick another workplace or try later."}
    # Docker sandbox only runs locally. The manual switcher turns the sandbox off
    # for remote targets; an agent must not be able to drop its own isolation.
    if agent.get('sandbox_enabled') and option['type'] in ('remote', 'tunnel', 'bwrap'):
        return {'ok': False, 'error': f"\"{option['name']}\" cannot be used while this agent's Docker "
                                      "sandbox is enabled. Ask the user to change that setting."}

    new_id = None if option['id'] == LOCAL_ID else option['id']
    if not db.update_agent(agent_id, {'workplace_id': new_id}):
        return {'ok': False, 'error': 'Failed to update the agent workplace.'}
    after = db.get_agent(agent_id)
    transition = None
    try:
        from backend.workplaces import switch_note
        if note:
            switch_note.note_workplace_switch(agent, after)
        transition = {'from': switch_note._side(agent)['meta'], 'to': switch_note._side(after)['meta']}
    except Exception:
        _logger.warning("Failed to describe the workplace switch for agent %s", agent_id, exc_info=True)
    return {'ok': True, 'changed': True, 'workplace': option, 'workplace_id': new_id, 'transition': transition,
            'message': f"Switched to \"{option['name']}\". Your tools now run there; "
                       "earlier tool results came from the previous workplace."}

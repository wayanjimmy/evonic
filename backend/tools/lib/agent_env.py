"""Resolution of an agent's declared variables into a process environment map.

Interactive tools (bash, runpy) hold a runtime agent context that carries
`variables`, while scheduler-driven paths (monitors, background jobs) rebuild
the agent from `db.get_agent`, which does not select that column. Both go
through :func:`build_agent_env` so a tool and the monitor watching it always
see the same environment.
"""

import logging
import re

logger = logging.getLogger(__name__)

_ENV_KEY_PATTERN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def coerce_env_source(raw) -> dict:
    """Normalize an agent variable source into a {key: value} dict.

    Accepts the runtime map form ({key: value}) and the DB row form
    ([{key, value, is_secret}, ...]). Keys that are not valid environment
    identifiers are dropped, and values are coerced to str.
    """
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        items = [
            (row.get('key'), row.get('value'))
            for row in raw if isinstance(row, dict)
        ]
    else:
        return {}
    env = {}
    for key, value in items:
        if not isinstance(key, str) or not _ENV_KEY_PATTERN_RE.match(key):
            continue
        if value is None:
            continue
        env[key] = str(value)
    return env


def build_agent_env(agent, agent_id: str = '') -> dict:
    """Resolve the agent's declared variables into a validated {KEY: str} map.

    Uses ``agent['variables']`` when the caller holds a runtime agent context;
    otherwise falls back to the DB (monitor/scheduler paths). Never raises: an
    unreadable variable store yields an empty env so a caller such as a monitor
    poll continues instead of aborting the tick.
    """
    agent = agent if isinstance(agent, dict) else {}
    raw = agent.get('variables')
    if not raw:
        raw = _db_agent_variables(agent, agent_id or agent.get('id') or '')
    return coerce_env_source(raw)


def _db_agent_variables(agent: dict, agent_id: str) -> dict:
    """DB-backed fallback for :func:`build_agent_env` (simulation-aware)."""
    try:
        from backend.agent_runtime.simulation_spec import variable_dict
        return variable_dict(agent, agent_id or None) or {}
    except Exception as e:
        logger.warning('[agent_env] agent variables lookup failed: %s', e)
        return {}

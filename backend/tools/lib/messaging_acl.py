"""
messaging_acl — the single source of truth for the inter-agent messaging ACL.

Both :mod:`backend.tools.agent_messaging` (``send_agent_message``) and the
``artifact-share`` ``share_artifact`` tool enforce exactly this policy, so a
given sender/recipient pair is allowed or denied identically no matter which
delivery mechanism is used.

Policy
------
Super agents bypass the ACL entirely.  For everyone else the agent record's
``messaging_acl`` field is consulted:

* empty / absent            -> no restriction
* ``messaging_acl_mode`` == ``whitelist`` (default) -> target must be listed
* ``messaging_acl_mode`` == ``blacklist``           -> target must not be listed

The function is deliberately side-effect free (no logging) so callers can log
in their own module's style; it returns the exact error string to surface.
"""

from __future__ import annotations

import json
from typing import Optional

__all__ = ["check_messaging_acl", "parse_messaging_acl"]


def parse_messaging_acl(raw) -> list:
    """Return the ACL as a list, tolerating a JSON string or an already-parsed list.

    Malformed input (invalid JSON, wrong type) is treated as "no entries"
    (an empty list), matching the historical behaviour of ``send_agent_message``.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def check_messaging_acl(agent_context: Optional[dict], target_id: str) -> Optional[str]:
    """Return an error message when the sender may not message *target_id*.

    Returns ``None`` when the message is allowed.  Super agents always pass.
    ``agent_context`` is the tool execution context (agent record + session
    fields); only ``is_super``, ``id``, ``messaging_acl`` and
    ``messaging_acl_mode`` are read.
    """
    if not agent_context:
        return None
    if agent_context.get("is_super"):
        return None

    acl_list = parse_messaging_acl(agent_context.get("messaging_acl"))
    if not acl_list:
        return None

    sender_id = agent_context.get("id", "")
    acl_mode = agent_context.get("messaging_acl_mode", "whitelist")

    if acl_mode == "whitelist" and target_id not in acl_list:
        return (
            f"Agent '{sender_id}' is not allowed to message '{target_id}' "
            f"(not in messaging whitelist)."
        )
    if acl_mode == "blacklist" and target_id in acl_list:
        return (
            f"Agent '{sender_id}' is not allowed to message '{target_id}' "
            f"(in messaging blacklist)."
        )
    return None

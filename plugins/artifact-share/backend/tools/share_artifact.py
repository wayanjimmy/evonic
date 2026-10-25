"""
share_artifact — sender-side agent tool for cross-agent binary artifacts.

Shares one of the acting agent's own artifact files with another agent.  The
platform selects the shortest *safe* transport automatically:

* ``local_path`` — returned only when BOTH the sender and the recipient are
  conclusively running directly on this Evonic host.  The tool returns the
  owner's absolute artifact path so the recipient can copy it straight from the
  shared filesystem.
* ``token_url`` — returned for every other case (sandbox, bwrap, remote,
  tunnel, simulation, unknown/malformed context).  A 256-bit token is minted,
  only its SHA-256 digest is persisted, and an immutable opaque snapshot of the
  artifact is written into the server-side spool before the URL is issued.  The
  download is single-use and expires after the configured TTL (default 300 s).

Messages never carry binary data.  Neither branch ever exposes an unrelated
path, and the raw token / download URL is never logged (audit records carry the
safe filename, size and checksum only).

Recipient contract (also stated in the tool description):

* ``local_path`` — copy the file into your configured local workspace
  immediately, verify the SHA-256, and never forward the path to anyone else.
* ``token_url`` — download the URL exactly once (it is single-use), verify the
  SHA-256, then use the local copy.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

_PLUGIN_ID = "artifact-share"
_DB_MODULE_NAME = "artifact_share_plugin_db"
_AGENT_ID_RE = re.compile(r"^[a-z0-9_]+$")


def _plugin_root() -> str:
    """Return the plugin root directory (``<plugin>/backend/tools/x.py`` -> ``<plugin>``)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _share_db():
    """Return the plugin's ArtifactShareDB singleton, loading ``db.py`` once.

    The plugin directory name contains a hyphen (``artifact-share``) so it is
    not importable as a dotted Python package; the module is therefore loaded
    by path under a stable, process-wide module name so the sender tool and the
    HTTP routes share a single DB instance.
    """
    module = sys.modules.get(_DB_MODULE_NAME)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            _DB_MODULE_NAME, os.path.join(_plugin_root(), "db.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_DB_MODULE_NAME] = module
        spec.loader.exec_module(module)
    return module.db


def _iso(timestamp: float) -> str:
    """Return an ISO-8601 UTC timestamp for *timestamp* (epoch seconds)."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def execute(agent: dict, args: dict) -> dict:
    from backend.audit_logger import audit
    from backend.tools._workspace import effective_agent_id
    from backend.tools.lib import artifact_delivery as delivery
    from backend.tools.lib.messaging_acl import check_messaging_acl
    from models.db import db

    agent = agent or {}
    args = args or {}

    if not delivery.is_share_enabled():
        return {"error": "Artifact sharing is disabled by the administrator."}

    sender_id = str(agent.get("id") or "")
    owner_id = effective_agent_id(agent)
    if not sender_id or not owner_id:
        return {"error": "Agent ID not found in context."}

    # ---- filename validation -------------------------------------------------
    name = delivery.safe_basename(args.get("filename"))
    if not name:
        return {
            "error": (
                'Invalid filename: provide a plain basename such as "report.pdf" '
                '(no "/", "\\", "..", or leading dot).'
            )
        }
    type_error = delivery.validate_artifact_type(name)
    if type_error:
        return {"error": type_error}

    # ---- recipient validation ------------------------------------------------
    recipient_id = str(args.get("recipient_agent_id") or "").strip().lower()
    if not recipient_id or not _AGENT_ID_RE.match(recipient_id):
        return {
            "error": (
                "recipient_agent_id is required and must be lowercase snake_case "
                "(letters, digits and underscores)."
            )
        }
    if recipient_id == sender_id:
        return {"error": "An agent cannot share an artifact with itself."}

    # Sub-agents may only share with their parent — mirrors messaging.
    if agent.get("is_subagent"):
        parent_id = str(agent.get("parent_id") or "")
        if recipient_id != parent_id:
            return {
                "error": (
                    f"Sub-agents can only share artifacts with their parent agent "
                    f"('{parent_id}')."
                )
            }

    # Same messaging-ACL policy used by send_agent_message.
    acl_error = check_messaging_acl(agent, recipient_id)
    if acl_error:
        return {"error": acl_error}

    # ---- recipient existence / enabled state ---------------------------------
    recipient = db.get_agent(recipient_id)
    if not recipient:
        try:
            from backend.subagent_manager import subagent_manager

            recipient = subagent_manager.get(recipient_id)
        except Exception:
            recipient = None
    if not recipient:
        return {"error": f"Agent '{recipient_id}' not found."}
    if not recipient.get("is_super") and not recipient.get("enabled", True):
        return {
            "error": f"Agent '{recipient.get('name', recipient_id)}' is currently disabled."
        }

    # ---- locate the owned artifact ------------------------------------------
    source_path, resolve_error = delivery.resolve_owned_artifact(agent, name)
    if resolve_error:
        return {"error": resolve_error}

    try:
        sha256, size = delivery.sha256_and_size(source_path)
    except OSError as exc:
        return {"error": f"Failed to read artifact '{name}': {exc}"}

    max_bytes = delivery.get_max_share_bytes()
    if size > max_bytes:
        return {
            "error": (
                f"Artifact '{name}' is {size} bytes, which exceeds the "
                f"{delivery.get_max_share_mb()} MB share limit."
            )
        }

    # ---- transport selection (fail-closed to token_url) ----------------------
    sender_ctx = delivery.resolve_agent_context(agent)
    recipient_ctx = delivery.resolve_agent_context(recipient)
    selection = delivery.select_transport(sender_ctx, recipient_ctx)
    transport = selection["transport"]
    reason = selection["reason"]

    if transport == delivery.TRANSPORT_LOCAL_PATH:
        audit.log_artifact_share(
            sender_agent_id=sender_id,
            owner_agent_id=owner_id,
            recipient_agent_id=recipient_id,
            transport=transport,
            reason=reason,
            filename=name,
            size_bytes=size,
            sha256=sha256,
        )
        return {
            "transport": "local_path",
            "artifact_path": source_path,
            "filename": name,
            "size_bytes": size,
            "sha256": sha256,
        }

    # ---- token_url branch ----------------------------------------------------
    if delivery.spool_usage_bytes() >= delivery.get_spool_quota_bytes():
        return {"error": "The artifact spool is full; please try again later."}

    try:
        delivery.ensure_spool_dir()
    except OSError as exc:
        return {"error": f"The artifact spool is not writable: {exc}"}

    token = delivery.mint_token()
    token_hash = delivery.hash_token(token)
    staging_path = os.path.join(delivery.get_spool_dir(), delivery.new_staging_name())

    # Snapshot BEFORE issuing the URL so the download is immutable and the
    # source artifact can change freely afterwards.
    try:
        snap_sha, snap_size = delivery.snapshot_and_digest(source_path, staging_path)
    except OSError as exc:
        delivery.safe_remove_spool_file(staging_path)
        return {"error": f"Failed to snapshot artifact '{name}': {exc}"}

    if snap_size != size or snap_sha != sha256:
        delivery.safe_remove_spool_file(staging_path)
        return {"error": "The artifact changed while it was being shared; please retry."}

    now = time.time()
    ttl = delivery.get_token_ttl_seconds()
    expires_at = now + ttl

    try:
        _share_db().create_share(
            token_hash=token_hash,
            owner_agent_id=owner_id,
            sender_agent_id=sender_id,
            recipient_agent_id=recipient_id,
            display_filename=name,
            size_bytes=size,
            sha256=sha256,
            staging_path=staging_path,
            created_at=now,
            expires_at=expires_at,
            single_use=True,
            max_uses=1,
        )
    except Exception as exc:
        delivery.safe_remove_spool_file(staging_path)
        return {"error": f"Failed to register the artifact share: {exc}"}

    base_url, _base_source = delivery.get_public_base_url()
    url = f"{base_url}/plugin/artifact-share/dl/{token}"

    # Never log the raw token or URL — audit only the safe metadata.
    audit.log_artifact_share(
        sender_agent_id=sender_id,
        owner_agent_id=owner_id,
        recipient_agent_id=recipient_id,
        transport=transport,
        reason=reason,
        filename=name,
        size_bytes=size,
        sha256=sha256,
        extra={"ttl_seconds": ttl, "expires_at": _iso(expires_at)},
    )

    return {
        "transport": "token_url",
        "url": url,
        "expires_at": _iso(expires_at),
        "ttl_seconds": ttl,
        "filename": name,
        "size_bytes": size,
        "sha256": sha256,
        "single_use": True,
    }

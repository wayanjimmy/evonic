"""
Security audit logger for evonic.

Writes structured JSON entries to logs/audit.log with rotation
(max 100 MB, 10 backups). Each entry includes: timestamp, event_type,
user_id, ip_address, resource, action, result, detail.

Usage:
    from backend.audit_logger import audit

    audit.log_login(ip="1.2.3.4", email="admin@example.com",
                    result="success")
    audit.log_agent_crud(user_id="admin", agent_id="my_agent",
                         action="create", ip="1.2.3.4")
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)
_AUDIT_LOG_FILE = os.path.join(_LOG_DIR, "audit.log")
_AUDIT_MAX_BYTES = 100 * 1024 * 1024   # 100 MB
_AUDIT_BACKUP_COUNT = 10

# ---------------------------------------------------------------------------
# Custom JSON formatter
# ---------------------------------------------------------------------------

class JsonAuditFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": getattr(record, "event_type", ""),
            "user_id": getattr(record, "user_id", ""),
            "ip_address": getattr(record, "ip_address", ""),
            "resource": getattr(record, "resource", ""),
            "action": getattr(record, "action", ""),
            "result": getattr(record, "result", ""),
            "detail": getattr(record, "detail", ""),
        }
        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# Singleton logger setup
# ---------------------------------------------------------------------------

_audit_logger: Optional[logging.Logger] = None


def _get_audit_logger() -> logging.Logger:
    """Lazily initialise and return the audit logger singleton."""
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    logger = logging.getLogger("audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't bubble up to root

    # Remove any pre-existing handlers (idempotent)
    logger.handlers.clear()

    os.makedirs(_LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        _AUDIT_LOG_FILE,
        maxBytes=_AUDIT_MAX_BYTES,
        backupCount=_AUDIT_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(JsonAuditFormatter())
    logger.addHandler(handler)

    _audit_logger = logger
    return logger


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class AuditLogger:
    """Public interface for recording security events."""

    @staticmethod
    def _emit(
        event_type: str,
        user_id: str = "",
        ip_address: str = "",
        resource: str = "",
        action: str = "",
        result: str = "",
        detail: str = "",
    ) -> None:
        """Emit a single audit log entry in JSON format."""
        logger = _get_audit_logger()
        logger.info(
            "",
            extra={
                "event_type": event_type,
                "user_id": user_id,
                "ip_address": ip_address,
                "resource": resource,
                "action": action,
                "result": result,
                "detail": detail,
            },
        )

    # -- Login -----------------------------------------------------------

    @staticmethod
    def log_login(
        ip: str = "",
        email: str = "",
        result: str = "",
        reason: str = "",
    ) -> None:
        detail = {"email": email}
        if reason:
            detail["reason"] = reason
        AuditLogger._emit(
            event_type="login",
            ip_address=ip,
            resource="auth",
            action="login",
            result=result,
            detail=json.dumps(detail),
        )

    # -- Agent CRUD -----------------------------------------------------

    @staticmethod
    def log_agent_crud(
        user_id: str = "",
        agent_id: str = "",
        action: str = "",
        ip: str = "",
        detail: str = "",
    ) -> None:
        AuditLogger._emit(
            event_type="agent_crud",
            user_id=user_id,
            ip_address=ip,
            resource=f"agent/{agent_id}",
            action=action,
            result="success",
            detail=detail,
        )

    # -- Plugin ----------------------------------------------------------

    @staticmethod
    def log_plugin(
        user_id: str = "",
        plugin_id: str = "",
        action: str = "",
        ip: str = "",
    ) -> None:
        AuditLogger._emit(
            event_type="plugin",
            user_id=user_id,
            ip_address=ip,
            resource=f"plugin/{plugin_id}",
            action=action,
            result="success",
        )

    # -- Skill -----------------------------------------------------------

    @staticmethod
    def log_skill(
        user_id: str = "",
        skill_id: str = "",
        action: str = "",
        ip: str = "",
    ) -> None:
        AuditLogger._emit(
            event_type="skill",
            user_id=user_id,
            ip_address=ip,
            resource=f"skill/{skill_id}",
            action=action,
            result="success",
        )

    # -- Settings --------------------------------------------------------

    @staticmethod
    def log_setting_change(
        user_id: str = "",
        key: str = "",
        old_value: str = "",
        new_value: str = "",
        ip: str = "",
    ) -> None:
        AuditLogger._emit(
            event_type="setting_change",
            user_id=user_id,
            ip_address=ip,
            resource=f"setting/{key}",
            action="update",
            result="success",
            detail=json.dumps({
                "key": key,
                "old": old_value,
                "new": new_value,
            }),
        )

    # -- Session ---------------------------------------------------------

    @staticmethod
    def log_session(
        user_id: str = "",
        session_id: str = "",
        action: str = "",
        ip: str = "",
    ) -> None:
        AuditLogger._emit(
            event_type="session",
            user_id=user_id,
            ip_address=ip,
            resource=f"session/{session_id}",
            action=action,
            result="success",
        )

    # -- User management -------------------------------------------------

    @staticmethod
    def log_user_management(
        user_id: str = "",
        target_user: str = "",
        action: str = "",
        ip: str = "",
        detail: str = "",
    ) -> None:
        AuditLogger._emit(
            event_type="user_management",
            user_id=user_id,
            ip_address=ip,
            resource=f"user/{target_user}",
            action=action,
            result="success",
            detail=detail,
        )


    # -- Artifact sharing ------------------------------------------------

    @staticmethod
    def log_artifact_share(
        sender_agent_id: str = "",
        owner_agent_id: str = "",
        recipient_agent_id: str = "",
        transport: str = "",
        reason: str = "",
        filename: str = "",
        size_bytes: int = 0,
        sha256: str = "",
        result: str = "success",
        extra: dict = None,
    ) -> None:
        """Record a cross-agent artifact share.

        The raw download token and URL are deliberately never recorded.  The
        acting *sender* is preserved separately from the artifact *owner*:
        for a sub-agent the owner is its parent (where the artifact registry
        actually lives), so both identities remain auditable.
        """
        detail = {
            "sender_agent_id": sender_agent_id,
            "owner_agent_id": owner_agent_id,
            "recipient_agent_id": recipient_agent_id,
            "transport": transport,
            "reason": reason,
            "filename": filename,
            "size_bytes": size_bytes,
            "sha256": sha256,
        }
        if extra:
            detail.update(extra)
        AuditLogger._emit(
            event_type="artifact_share",
            user_id=sender_agent_id,
            resource=f"agent/{owner_agent_id}",
            action="share_artifact",
            result=result,
            detail=json.dumps(detail, default=str),
        )


# Module-level singleton for convenient imports
audit = AuditLogger()

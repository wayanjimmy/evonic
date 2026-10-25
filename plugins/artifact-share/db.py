"""
ArtifactShareDB \u2014 SQLite storage for the Artifact Share plugin.

Each row records a *tokenized* artifact share: the SHA-256 digest of the bearer
token (never the token itself), the owner/sender/recipient agents, the safe
display filename, byte size, content SHA-256, the opaque server-side staging
path, creation/expiry timestamps, single-use state/count and revocation state.

The database lives at ``data/db/plugins/artifact_share.db`` (or
``shared/data/db/plugins/artifact_share.db`` when that root exists), matching the
other plugin databases, in WAL mode.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_shared_data = os.path.join(BASE_DIR, "shared", "data")
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, "data")
PLUGIN_DB_DIR = os.path.join(_data_root, "db", "plugins")
DB_PATH = os.path.join(PLUGIN_DB_DIR, "artifact_share.db")


class ArtifactShareDB:
    """CRUD + atomic single-use consumption for tokenized artifact shares."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_share_tokens (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash          TEXT NOT NULL UNIQUE,
                    owner_agent_id      TEXT NOT NULL,
                    sender_agent_id     TEXT NOT NULL,
                    recipient_agent_id  TEXT NOT NULL,
                    display_filename    TEXT NOT NULL,
                    size_bytes          INTEGER NOT NULL,
                    sha256              TEXT NOT NULL,
                    staging_path        TEXT NOT NULL,
                    created_at          REAL NOT NULL,
                    expires_at          REAL NOT NULL,
                    single_use          INTEGER NOT NULL DEFAULT 1,
                    max_uses            INTEGER NOT NULL DEFAULT 1,
                    used_count          INTEGER NOT NULL DEFAULT 0,
                    revoked_at          REAL,
                    last_used_at        REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifact_share_expires "
                "ON artifact_share_tokens (expires_at)"
            )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_share(
        self,
        *,
        token_hash: str,
        owner_agent_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        display_filename: str,
        size_bytes: int,
        sha256: str,
        staging_path: str,
        created_at: float,
        expires_at: float,
        single_use: bool = True,
        max_uses: int = 1,
    ) -> int:
        """Insert a new share row and return its id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO artifact_share_tokens (
                    token_hash, owner_agent_id, sender_agent_id, recipient_agent_id,
                    display_filename, size_bytes, sha256, staging_path,
                    created_at, expires_at, single_use, max_uses, used_count,
                    revoked_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
                """,
                (
                    token_hash, owner_agent_id, sender_agent_id, recipient_agent_id,
                    display_filename, int(size_bytes), sha256, staging_path,
                    float(created_at), float(expires_at), 1 if single_use else 0,
                    int(max_uses),
                ),
            )
            return int(cursor.lastrowid)

    def consume(self, token_hash: str, now: float) -> Optional[Dict[str, Any]]:
        """Atomically consume one use of a live token.

        Returns the row (as a dict, with ``used_count`` already incremented) on
        success, or ``None`` when the token is unknown, expired, revoked or
        exhausted.  The read-modify-write happens inside a single IMMEDIATE
        transaction so two concurrent downloads can never both succeed on a
        single-use token.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM artifact_share_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            now = float(now)
            if record["revoked_at"] is not None:
                return None
            if float(record["expires_at"]) <= now:
                return None
            if int(record["used_count"]) >= int(record["max_uses"]):
                return None
            conn.execute(
                "UPDATE artifact_share_tokens "
                "SET used_count = used_count + 1, last_used_at = ? "
                "WHERE token_hash = ? AND used_count < max_uses",
                (now, token_hash),
            )
            record["used_count"] = int(record["used_count"]) + 1
            record["last_used_at"] = now
            return record

    def revoke(self, token_hash: str, now: float) -> bool:
        """Mark a token revoked. Returns True when a live row was updated."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE artifact_share_tokens SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (float(now), token_hash),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_share_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            return dict(row) if row else None

    def count_active(self, now: float) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM artifact_share_tokens "
                "WHERE revoked_at IS NULL AND expires_at > ?",
                (float(now),),
            ).fetchone()
            return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def delete_expired(self, now: float) -> List[str]:
        """Delete expired/revoked rows and return their staging paths.

        Callers are responsible for unlinking the returned staging files (via
        ``artifact_delivery.safe_remove_spool_file`` so only spool paths are
        ever touched).
        """
        now = float(now)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT staging_path FROM artifact_share_tokens "
                "WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now,),
            ).fetchall()
            paths = [r["staging_path"] for r in rows if r["staging_path"]]
            conn.execute(
                "DELETE FROM artifact_share_tokens "
                "WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now,),
            )
            return paths

    def delete_by_hash(self, token_hash: str) -> Optional[str]:
        """Delete a single row and return its staging path (or None)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT staging_path FROM artifact_share_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM artifact_share_tokens WHERE token_hash = ?",
                (token_hash,),
            )
            return row["staging_path"]


#: Process-wide default store, shared by the routes and the sender tool.
db = ArtifactShareDB()

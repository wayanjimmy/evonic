"""
SQLite-backed tiered API rate limiter.

Replaces the missing per-endpoint rate limiting (FINDING-004) with a persistent
SQLite store that survives restarts. Tracks requests per-user (authenticated)
or per-IP (anonymous).

Tiers
-----
- chat:    10 req/min  — /api/agents/<id>/chat/*
- upload:   5 req/min  — POST /api/agents/<id>/artifacts, /avatar, /kb; /api/plugins
- crud:   120 req/min  — /api/agents* (excluding chat/upload sub-paths)
- general: 300 req/min — all other /api/* endpoints
- static: 300 req/min  — /static/* (or unlimited, configurable)
- download: 60 req/min — /plugin/artifact-share/* (tokenized binary downloads)
- sse:     max 5 concurrent connections per user/IP

Schema
------
api_rate_limit(key TEXT PRIMARY KEY, tier TEXT NOT NULL, count INTEGER NOT NULL,
               window_start REAL NOT NULL, reset_at REAL NOT NULL)

  key          — "user:<id>" or "ip:<addr>"
  tier         — one of: chat, upload, crud, general, static
  count        — requests within the current window
  window_start — time.time() when the current window began
  reset_at     — time.time() when the window expires

sse_connections(key TEXT PRIMARY KEY, count INTEGER NOT NULL)

  key   — "user:<id>" or "ip:<addr>"
  count — current concurrent SSE connections
"""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional, Tuple

import config

# ---------------------------------------------------------------------------
# Tier configuration: (requests_per_window, window_seconds)
# ---------------------------------------------------------------------------
TIERS = {
    "chat":     (10,  60),   # 10 req/min — actual LLM message sends only
    "poll":     (120, 60),   # 120 req/min — cheap chat reads / 1s SSE-fallback poll
    "upload":   (5,   60),   #  5 req/min
    "crud":     (120, 60),   # 120 req/min
    "general":  (300, 60),   # 300 req/min
    "static":   (300, 60),   # 300 req/min (effectively unlimited for normal use)
    # Bounded tier for the tokenized artifact-share download endpoint
    # (/plugin/artifact-share/dl/<token>). Deliberately small: each request
    # streams up to 100 MB, so a bearer token must not be brutable in bulk.
    "download": (60,  60),   # 60 req/min
}

# Max concurrent SSE connections per user/IP. A single page legitimately opens
# multiple streams (unified status/approvals/update + a per-turn chat stream), and
# the client reconnects faster than the server detects dropped connections (one
# heartbeat interval), so orphaned connections briefly hold slots. Keep this well
# above the app's own connection pattern; the global WORKER_CONNECTIONS guard
# bounds true DoS. (SSE endpoints are auth-gated, so this only ever applies to the
# logged-in admin.)
SSE_MAX_CONCURRENT = 50

# ---------------------------------------------------------------------------
# Database path
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(config.APP_ROOT, "shared", "db", "api_rate_limit.db")

# ---------------------------------------------------------------------------
# Thread-local connection management (WAL mode)
# ---------------------------------------------------------------------------
_tls = threading.local()


@contextmanager
def _connect():
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except Exception:
            conn = None

    if conn is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(
            f"file:{_DB_PATH}?mode=rwc&busy_timeout=5000", uri=True
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _init_tables(conn)
        _tls.conn = conn

    with conn:
        yield conn


def close():
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _tls.conn = None


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_rate_limit (
            key         TEXT NOT NULL,
            tier        TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            window_start REAL NOT NULL,
            reset_at    REAL NOT NULL,
            PRIMARY KEY (key, tier)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sse_connections (
            key   TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
    """)


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------

def _make_key(identifier: str) -> str:
    """Build a rate-limit key from user ID or IP."""
    return identifier  # already formatted as "user:<id>" or "ip:<addr>"


# ---------------------------------------------------------------------------
# Route → tier classification
# ---------------------------------------------------------------------------

def classify_request(path: str, method: str = "GET") -> str:
    """Return the rate-limit tier for a given request path and method.

    Classification rules (first match wins):
      0. GET /api/evaluator/log_poll, /api/evaluator/test_matrix,
         /api/v1/history/*, /api/dashboard*,
         /api/models*, /api/config*, /api/system* → None (exempt reads;
         high-frequency evaluator polling / dashboard / config reads). Non-GET
         (mutations) fall through to normal classification below.
      1. /static/*                     → static
      1b. /plugin/artifact-share/*      → download (tokenized binary download)
      2. POST /api/agents/<id>/chat and /chat/approve → chat (LLM sends)
      2b. any other /api/agents/<id>/chat* (GET reads, polls, stream) → poll
      3. POST /api/agents/<id>/artifacts* → upload
      4. POST /api/agents/<id>/avatar    → upload
      5. POST /api/agents/<id>/kb        → upload
      6. /api/plugins*                   → upload
      7. GET /api/agents/<id>/avatar     → static (image serving, not CRUD)
      8. GET /api/agents/<id>/artifacts/* → static (file serving, not CRUD)
      9. /api/agents*                    → crud
      10. /api/*                         → general
      11. everything else                → None (no limit)
    """
    # Static assets
    if path.startswith("/static/"):
        return "static"

    # Tokenized cross-agent artifact-share downloads. Each request streams a
    # binary snapshot behind a short-lived bearer token, so it gets its own
    # small bounded tier instead of the shared "general" budget.
    if path.startswith("/plugin/artifact-share/"):
        return "download"

    # High-frequency evaluator polling / dashboard endpoints — exempt READS only
    # from rate limiting. The evaluator hammers these progress/history/config
    # reads far faster than the "general" tier (60 req/min) allows, so GET is
    # cap-free. Mutations (POST/PUT/DELETE) fall through to normal limiting.
    if method == "GET" and (
        path.startswith("/api/evaluator/log_poll")
        or path.startswith("/api/evaluator/test_matrix")
        or path.startswith("/api/v1/history/")
        or path.startswith("/api/dashboard")
        or path.startswith("/api/models")
        or path.startswith("/api/config")
        or path.startswith("/api/system")
    ):
        return None

    # Chat endpoints. Only the expensive LLM-send POSTs consume the small "chat"
    # budget; cheap GET reads/polls (history, poll, state, session, summary,
    # events, stream, llm-preview) and POST /chat/clear ride the high-ceiling
    # "poll" tier so normal chatting + the 1s SSE-fallback poll never exhaust it.
    if "/api/agents/" in path and "/chat" in path:
        p = path.rstrip("/")
        if method == "POST" and (p.endswith("/chat") or p.endswith("/chat/approve")):
            return "chat"
        return "poll"

    # File/Plugin Upload endpoints (POST only)
    if method == "POST":
        if "/api/agents/" in path and (
            "/artifacts" in path or "/avatar" in path
        ):
            # Only POST to artifacts or avatar is an upload
            if path.rstrip("/").endswith("/artifacts") or path.rstrip("/").endswith("/avatar"):
                return "upload"
        if "/api/agents/" in path and "/kb" in path:
            if path.rstrip("/").endswith("/kb"):
                return "upload"
        if path.startswith("/api/plugins"):
            return "upload"

    # Static asset serving (avatar images, artifact files) — GET only.
    # These serve static binary content (images, etc.) and should not
    # consume the CRUD budget alongside agent list/create/edit operations.
    if method == "GET":
        if "/api/agents/" in path and "/avatar" in path:
            return "static"
        if "/api/agents/" in path and "/artifacts/" in path:
            return "static"

    # Agent CRUD
    if path.startswith("/api/agents"):
        return "crud"

    # All other API endpoints
    if path.startswith("/api/"):
        return "general"

    # Non-API routes (login, dashboard pages, etc.) — no rate limit
    return None


# ---------------------------------------------------------------------------
# SSE connection tracking
# ---------------------------------------------------------------------------

def sse_register(identifier: str) -> Tuple[bool, int]:
    """Register a new SSE connection. Returns (allowed, current_count)."""
    key = _make_key(identifier)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO sse_connections (key, count)
               VALUES (?, 1)
               ON CONFLICT(key) DO UPDATE SET count = count + 1""",
            (key,),
        )
        row = conn.execute(
            "SELECT count FROM sse_connections WHERE key = ?", (key,)
        ).fetchone()
        current = row[0] if row else 0
        allowed = current <= SSE_MAX_CONCURRENT
        if not allowed:
            # Roll back — don't count rejected connections
            conn.execute(
                "UPDATE sse_connections SET count = count - 1 WHERE key = ?",
                (key,),
            )
    return allowed, current


def sse_unregister(identifier: str) -> None:
    """Unregister an SSE connection when it closes."""
    key = _make_key(identifier)
    with _connect() as conn:
        conn.execute(
            """UPDATE sse_connections SET count = MAX(0, count - 1)
               WHERE key = ?""",
            (key,),
        )
        # Clean up zero-count rows
        conn.execute(
            "DELETE FROM sse_connections WHERE key = ? AND count <= 0", (key,)
        )


def reset_sse_connections() -> None:
    """Clear all SSE connection counts.

    SSE connection counts are process-bound in-memory state that we happen to
    persist in SQLite. On a non-graceful shutdown (or when long-lived SSE
    generators are force-killed at restart) the generator `finally` that calls
    sse_unregister never runs, so counts leak and survive the restart — eventually
    pegging a user at SSE_MAX_CONCURRENT and rejecting every new connection with
    429 even when nothing is connected. At startup there are zero live
    connections, so the persisted counts are always stale; clear them.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM sse_connections")


# ---------------------------------------------------------------------------
# Core rate-limit check
# ---------------------------------------------------------------------------

def check_rate_limit(identifier: str, tier: str) -> Tuple[bool, int, int, int]:
    """Check if a request is allowed under the given tier.

    Args:
        identifier: "user:<id>" or "ip:<addr>"
        tier: one of the TIERS keys

    Returns:
        (allowed, remaining, limit, retry_after_seconds)
    """
    if tier not in TIERS:
        return True, -1, -1, 0

    limit, window = TIERS[tier]
    key = _make_key(identifier)
    now = time.time()

    reset_at = now + window

    with _connect() as conn:
        # Atomic insert-or-update. Doing the read-modify-write as a single
        # statement avoids a race where two concurrent requests with the same
        # (key, tier) both see no row and both INSERT — which raised
        # sqlite3.IntegrityError: UNIQUE constraint failed (key, tier).
        # On conflict: reset the window if expired, otherwise increment.
        conn.execute(
            """
            INSERT INTO api_rate_limit (key, tier, count, window_start, reset_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(key, tier) DO UPDATE SET
                count        = CASE WHEN ? >= reset_at THEN 1 ELSE count + 1 END,
                window_start = CASE WHEN ? >= reset_at THEN ? ELSE window_start END,
                reset_at     = CASE WHEN ? >= reset_at THEN ? ELSE reset_at END
            """,
            (key, tier, now, reset_at,
             now,            # count reset check
             now, now,       # window_start reset check + new value
             now, reset_at), # reset_at reset check + new value
        )
        row = conn.execute(
            "SELECT count, reset_at FROM api_rate_limit WHERE key = ? AND tier = ?",
            (key, tier),
        ).fetchone()

    count, reset_at = row
    if count > limit:
        # Over the limit for this window — block until it resets.
        retry_after = int(reset_at - now) + 1
        return False, 0, limit, retry_after

    return True, limit - count, limit, 0


# ---------------------------------------------------------------------------
# Periodic cleanup
# ---------------------------------------------------------------------------

def cleanup_expired() -> int:
    """Delete expired rate-limit rows. Returns number of rows deleted."""
    now = time.time()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM api_rate_limit WHERE reset_at <= ?", (now,)
        )
        return cursor.rowcount


def _cleanup_loop(interval: float = 300.0) -> None:
    while True:
        time.sleep(interval)
        try:
            cleanup_expired()
        except Exception:
            pass


def start_periodic_cleanup(interval: float = 300.0) -> threading.Thread:
    t = threading.Thread(target=_cleanup_loop, args=(interval,), daemon=True)
    t.start()
    return t

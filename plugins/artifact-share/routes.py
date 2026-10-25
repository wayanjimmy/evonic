"""
artifact-share plugin — Flask route handlers.

Provides the public, token-authenticated download endpoint used by the
``token_url`` transport:

  GET /plugin/artifact-share/dl/<token>   download a shared artifact snapshot
  GET /plugin/artifact-share/             small JSON status probe

The endpoint is deliberately self-contained: it validates the bearer token
syntax, hashes it, looks up the *digest only* (the raw token is never stored),
atomically consumes a single-use token, streams the opaque server-side snapshot
with unbuffered ``send_file``, and deletes the snapshot once the response has
been sent.

Security properties
-------------------
* identical 404 for unknown / expired / revoked / exhausted tokens (no oracle);
* constant-time digest comparison;
* strict token syntax validation before any database access;
* ``Cache-Control: no-store`` + ``Pragma: no-cache`` + ``X-Content-Type-Options:
  nosniff`` so intermediaries never retain the bearer token or the payload;
* the raw token is never logged.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from flask import Blueprint, jsonify, send_file

from backend.tools.lib import artifact_delivery as delivery

from .db import db as share_db

_logger = logging.getLogger(__name__)

#: Periodic cleanup cadence for expired/revoked rows + their staging bytes.
CLEANUP_INTERVAL_SECONDS = 300.0

_cleanup_started = False
_cleanup_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_expired(now: float = None) -> int:
    """Delete expired/revoked share rows and their staging files.

    Returns the number of rows removed.  Staging paths are only unlinked when
    they resolve inside the configured spool directory.
    """
    now = time.time() if now is None else float(now)
    try:
        paths = share_db.delete_expired(now)
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning("artifact-share cleanup failed to prune rows: %s", exc)
        return 0
    for path in paths:
        delivery.safe_remove_spool_file(path)
    return len(paths)


def _cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            cleanup_expired()
        except Exception:  # pragma: no cover - never let the thread die loudly
            pass


def start_cleanup_thread() -> None:
    """Start the periodic cleanup thread once per process."""
    global _cleanup_started
    with _cleanup_lock:
        if _cleanup_started:
            return
        thread = threading.Thread(
            target=_cleanup_loop,
            name="artifact-share-cleanup",
            daemon=True,
        )
        thread.start()
        _cleanup_started = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _not_found(**_kwargs):
    """Uniform failure response — never reveals why a token was rejected.

    ``**_kwargs`` lets this double as the view for the ``<token>``-shaped and
    catch-all rules without leaking the reason.
    """
    return jsonify({"error": "not_found"}), 404


def _resolve_live_row(token: str):
    """Return a dict describing the token, or ``None`` when it is not usable."""
    if not delivery.is_valid_token_format(token):
        return None
    token_hash = delivery.hash_token(token)
    row = share_db.get_by_hash(token_hash)
    if not row:
        return None
    # Constant-time comparison guards against an index-timing side channel.
    if not delivery.constant_time_equal(str(row.get("token_hash", "")), token_hash):
        return None
    now = time.time()
    if row.get("revoked_at") is not None:
        return None
    if float(row.get("expires_at") or 0) <= now:
        # Opportunistic cleanup for the expired token's staging bytes.
        cleanup_expired(now)
        return None
    return row


def _stream_and_remove(app_iter, staging_path):
    """Yield *app_iter* and unlink the spool snapshot once iteration ends.

    Using an explicit iterator (rather than a ``call_on_close`` callback) makes
    the cleanup fire reliably on both the success path (iterator exhausted) and
    the abort path (the WSGI server closes the iterator), so spool bytes can
    never outlive the request they were issued for.
    """
    try:
        for chunk in app_iter:
            yield chunk
    finally:
        try:
            if hasattr(app_iter, "close"):
                app_iter.close()
        finally:
            delivery.safe_remove_spool_file(staging_path)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _download(token: str):
    if not delivery.is_share_enabled():
        return _not_found()

    row = _resolve_live_row(token)
    if row is None:
        return _not_found()

    staging = row.get("staging_path") or ""
    if not staging or not os.path.isfile(staging):
        # Snapshot vanished (e.g. external cleanup) — treat as an unknown token.
        return _not_found()

    # Atomically consume one use. Two concurrent downloads cannot both win.
    consumed = share_db.consume(row["token_hash"], time.time())
    if consumed is None:
        return _not_found()

    display_name = delivery.sanitize_download_name(consumed.get("display_filename"))
    try:
        response = send_file(
            staging,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=display_name,
            conditional=False,
            etag=False,
            last_modified=None,
            max_age=0,
        )
    except Exception as exc:
        _logger.warning("artifact-share download failed to open snapshot: %s", exc)
        delivery.safe_remove_spool_file(staging)
        return _not_found()

    # Advertise the exact length of the bytes actually stored on disk.
    try:
        stored_size = os.path.getsize(staging)
    except OSError:
        stored_size = int(consumed.get("size_bytes") or 0)
    response.headers["Content-Length"] = str(stored_size)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Stream the snapshot unbuffered (send_file builds the response envelope:
    # headers, attachment filename, content length) and guarantee the spool
    # bytes are deleted once streaming finishes or the client disconnects.
    response.response = _stream_and_remove(response.response, staging)
    response.direct_passthrough = True
    return response


def _status():
    return jsonify({
        "plugin": "artifact-share",
        "enabled": delivery.is_share_enabled(),
        "ttl_seconds": delivery.get_token_ttl_seconds(),
        "max_mb": delivery.get_max_share_mb(),
    })


def create_blueprint() -> Blueprint:
    bp = Blueprint("artifact_share", __name__)

    bp.add_url_rule(
        "/plugin/artifact-share/dl/<token>",
        "download",
        _download,
        methods=["GET"],
    )
    bp.add_url_rule(
        "/plugin/artifact-share/",
        "status",
        _status,
        methods=["GET"],
    )

    # Uniform JSON 404 for malformed / traversal-shaped download paths so the
    # failure body is identical to every other rejection (never an HTML error
    # page, which would be an observable difference).
    bp.add_url_rule(
        "/plugin/artifact-share/dl/",
        "download_missing_token",
        _not_found,
        methods=["GET", "HEAD"],
    )
    bp.add_url_rule(
        "/plugin/artifact-share/<path:rest>",
        "unmatched",
        _not_found,
        methods=["GET", "HEAD", "POST"],
    )

    # Startup cleanup + opportunistic periodic cleanup.
    try:
        delivery.ensure_spool_dir()
        cleanup_expired()
        start_cleanup_thread()
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning("artifact-share startup cleanup skipped: %s", exc)

    return bp

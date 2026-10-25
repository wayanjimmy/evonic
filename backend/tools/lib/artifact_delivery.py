"""
artifact_delivery — central execution-context classification and transport
selection for cross-agent binary artifact sharing.

This module is the single, conservative source of truth used by every piece of
artifact-sharing code (the ``share_artifact`` sender tool and the
``artifact-share`` plugin).  It answers two questions:

1. **Where does an agent actually execute?**  (:func:`resolve_agent_context` /
   :func:`resolve_execution_context`).  The classification is derived from the
   agent record (``workplace_id`` + ``sandbox_enabled``), the workplace type,
   and the simulation state — never from an SSH host name, a loopback address,
   a workspace name, or any user-supplied host value.

2. **Which transport is safe?**  (:func:`select_transport`).  A direct
   host-local filesystem path is only returned when *both* the sender and the
   recipient are conclusively ``host_local``.  Every other combination —
   including ``sandbox``, ``bwrap``, ``remote``, ``tunnel`` and ``unknown`` —
   fails closed to a short-lived tokenized HTTPS download URL.

Execution contexts
------------------
==================  =====================================================
context             meaning
==================  =====================================================
``host_local``      runs directly on the Evonic host filesystem
``sandbox``         no/`local` workplace + ``sandbox_enabled`` => docker
``bwrap``           bubblewrap-isolated workplace
``remote``          SSH remote workplace (executes on another host)
``tunnel``          tunnel/connector workplace
``unknown``         simulation / missing or stale workplace / malformed
==================  =====================================================

Only ``host_local`` agents can read a path written by another ``host_local``
agent.  Remote/tunnel/bwrap backends keep their own filesystems and must never
be treated as direct-host-local even when ``sandbox_enabled`` is forced off by
policy (see ``routes/agents.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Callable, Optional, Tuple

try:
    import config as _config
except Exception:  # pragma: no cover - config is importable in practice
    _config = None


# ---------------------------------------------------------------------------
# Execution contexts
# ---------------------------------------------------------------------------

CTX_HOST_LOCAL = "host_local"
CTX_SANDBOX = "sandbox"
CTX_BWRAP = "bwrap"
CTX_REMOTE = "remote"
CTX_TUNNEL = "tunnel"
CTX_UNKNOWN = "unknown"

#: Contexts that are NOT direct-host-local: never expose a host path to these.
ISOLATED_CONTEXTS = frozenset(
    {CTX_SANDBOX, CTX_BWRAP, CTX_REMOTE, CTX_TUNNEL, CTX_UNKNOWN}
)

# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

TRANSPORT_LOCAL_PATH = "local_path"
TRANSPORT_TOKEN_URL = "token_url"

# ---------------------------------------------------------------------------
# Safe defaults (all overridable via system setting -> env -> default)
# ---------------------------------------------------------------------------

DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_TOKEN_TTL_SECONDS = 300          # 5 minutes
DEFAULT_MAX_SHARE_MB = 100               # 100 MB
DEFAULT_SPOOL_QUOTA_MB = 2048            # 2 GB
DEFAULT_SPOOL_SUBDIR = os.path.join("data", "spool", "artifact_share")

#: Bounded streaming chunk size (1 MiB) — keeps snapshot/hash memory-bounded.
STREAM_CHUNK_SIZE = 1024 * 1024

#: 32 bytes of entropy -> 43 char urlsafe token.
TOKEN_BYTES = 32
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,128}$")


# ---------------------------------------------------------------------------
# Config resolution: system setting -> env var -> safe default
# ---------------------------------------------------------------------------

def _db_setting(key: str) -> Optional[str]:
    """Read a system setting from the app settings table (None when absent)."""
    try:
        from models.db import db

        value = db.get_setting(key)
    except Exception:
        return None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _env(name: str) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _plugin_config(plugin_id: str, name: str) -> Optional[str]:
    """Read a plugin config variable (``plugin_config:<id>:<NAME>``), if set."""
    try:
        from models.db import db

        value = db.get_setting(f"plugin_config:{plugin_id}:{name}")
    except Exception:
        return None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _resolve_str(
    setting_key: str,
    env_name: str,
    default: str = "",
    plugin_var: Optional[str] = None,
    plugin_id: str = "artifact-share",
) -> str:
    """Resolve a string config with setting -> plugin config -> env -> default."""
    value = _db_setting(setting_key)
    if value:
        return value
    if plugin_var:
        value = _plugin_config(plugin_id, plugin_var)
        if value:
            return value
    value = _env(env_name)
    if value:
        return value
    return default


def _resolve_int(
    setting_key: str,
    env_name: str,
    default: int,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
    plugin_var: Optional[str] = None,
) -> int:
    """Resolve an int config value, clamping to [min_val, max_val] when given."""
    raw = _resolve_str(setting_key, env_name, "", plugin_var=plugin_var)
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    if min_val is not None and value < min_val:
        return min_val
    if max_val is not None and value > max_val:
        return max_val
    return value


def _resolve_bool(
    setting_key: str, env_name: str, default: bool, plugin_var: Optional[str] = None
) -> bool:
    raw = _resolve_str(setting_key, env_name, "", plugin_var=plugin_var)
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _to_bool(value) -> bool:
    """Normalize a DB/env boolean-ish value (``0``/``1``/``true``/...)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Public configuration accessors
# ---------------------------------------------------------------------------

def is_share_enabled() -> bool:
    """Feature kill switch. Defaults to enabled."""
    return _resolve_bool(
        "artifact_share_enabled", "ARTIFACT_SHARE_ENABLED", True,
        plugin_var="ENABLED",
    )


def get_public_base_url() -> Tuple[str, str]:
    """Return ``(base_url, source)`` for building download URLs.

    Precedence: system setting ``artifact_share_public_base_url`` -> plugin
    config ``PUBLIC_BASE_URL`` -> env ``EVONIC_PUBLIC_URL`` -> generic default.
    The default is deliberately deployment-agnostic; nothing deployment-specific
    is hard-coded.
    """
    value = _db_setting("artifact_share_public_base_url")
    if value:
        return value.rstrip("/"), "setting"
    value = _plugin_config("artifact-share", "PUBLIC_BASE_URL")
    if value:
        return value.rstrip("/"), "plugin_config"
    value = _env("EVONIC_PUBLIC_URL")
    if value:
        return value.rstrip("/"), "env"
    return DEFAULT_PUBLIC_BASE_URL, "default"


def get_token_ttl_seconds() -> int:
    """Token URL lifetime in seconds (default 300, hard-required 5 minutes)."""
    return _resolve_int(
        "artifact_share_token_ttl", "ARTIFACT_SHARE_TOKEN_TTL",
        DEFAULT_TOKEN_TTL_SECONDS, min_val=30, max_val=3600,
        plugin_var="TOKEN_TTL_SECONDS",
    )


def get_max_share_mb() -> int:
    """Maximum artifact size, in MB, permitted for sharing."""
    return _resolve_int(
        "artifact_share_max_mb", "ARTIFACT_SHARE_MAX_MB",
        DEFAULT_MAX_SHARE_MB, min_val=1, max_val=10240,
        plugin_var="MAX_MB",
    )


def get_max_share_bytes() -> int:
    return get_max_share_mb() * 1024 * 1024


def get_spool_quota_mb() -> int:
    return _resolve_int(
        "artifact_share_spool_quota_mb", "ARTIFACT_SHARE_SPOOL_QUOTA_MB",
        DEFAULT_SPOOL_QUOTA_MB, min_val=16, max_val=1048576,
        plugin_var="SPOOL_QUOTA_MB",
    )


def get_spool_quota_bytes() -> int:
    return get_spool_quota_mb() * 1024 * 1024


def get_spool_dir() -> str:
    """Controlled, opaque server-side staging directory for URL shares."""
    configured = _resolve_str(
        "artifact_share_spool_dir", "ARTIFACT_SHARE_SPOOL_DIR", "",
        plugin_var="SPOOL_DIR",
    )
    if configured:
        return configured
    base = getattr(_config, "APP_ROOT", None) or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return os.path.join(base, DEFAULT_SPOOL_SUBDIR)


def get_allowed_extensions() -> Optional[frozenset]:
    """Optional artifact-type allowlist. ``None`` means "all types allowed"."""
    raw = _resolve_str(
        "artifact_share_allowed_extensions", "ARTIFACT_SHARE_ALLOWED_EXTENSIONS", "",
        plugin_var="ALLOWED_EXTENSIONS",
    )
    if not raw:
        return None
    exts = {("." + e.strip().lower().lstrip(".")) for e in raw.split(",") if e.strip()}
    return frozenset(exts) or None


def validate_artifact_type(filename: str) -> Optional[str]:
    """Return an error string when *filename*'s type is not permitted, else None."""
    allowed = get_allowed_extensions()
    if allowed is None:
        return None
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in allowed:
        return None
    return (
        f"Artifact type '{ext or '(none)'}' is not permitted for sharing "
        f"(allowed: {', '.join(sorted(allowed))})."
    )


# ---------------------------------------------------------------------------
# Execution-context classification
# ---------------------------------------------------------------------------

def _default_workplace_lookup(workplace_id: str) -> Optional[dict]:
    try:
        from models.db import db

        return db.get_workplace(workplace_id)
    except Exception:
        return None


def resolve_execution_context(
    agent_record: Optional[dict],
    *,
    is_simulation: bool = False,
    workplace_lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Classify where *agent_record* executes.

    Returns ``{"context": <ctx>, "reason": <str>}``.  Fails closed to
    ``unknown`` for simulations, missing/stale workplaces, and unrecognized
    workplace types.
    """
    if is_simulation:
        return {"context": CTX_UNKNOWN, "reason": "simulation"}

    record = agent_record or {}
    workplace_id = str(record.get("workplace_id") or "").strip()
    sandbox = _to_bool(record.get("sandbox_enabled"))

    # No workplace: the sandbox flag alone decides.
    if not workplace_id:
        if sandbox:
            return {"context": CTX_SANDBOX, "reason": "no_workplace+sandbox"}
        return {"context": CTX_HOST_LOCAL, "reason": "no_workplace+no_sandbox"}

    lookup = workplace_lookup or _default_workplace_lookup
    workplace = lookup(workplace_id)
    if not workplace:
        # Missing / stale / deleted workplace -> cannot prove localness.
        return {"context": CTX_UNKNOWN, "reason": f"workplace_missing:{workplace_id}"}

    wp_type = str(workplace.get("type") or "").strip().lower()
    if wp_type == "local":
        if sandbox:
            return {"context": CTX_SANDBOX, "reason": "local_workplace+sandbox"}
        return {"context": CTX_HOST_LOCAL, "reason": "local_workplace+no_sandbox"}
    if wp_type == "bwrap":
        # bwrap supplies its own isolation; sandbox_enabled is irrelevant.
        return {"context": CTX_BWRAP, "reason": "bwrap_workplace"}
    if wp_type == "remote":
        return {"context": CTX_REMOTE, "reason": "remote_workplace"}
    if wp_type == "tunnel":
        return {"context": CTX_TUNNEL, "reason": "tunnel_workplace"}
    return {"context": CTX_UNKNOWN, "reason": f"unrecognized_workplace_type:{wp_type}"}


def resolve_agent_context(
    agent_record: Optional[dict],
    *,
    workplace_lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Like :func:`resolve_execution_context`, auto-detecting simulation state."""
    record = agent_record or {}
    is_sim = bool(record.get("is_simulation"))
    if not is_sim:
        try:
            from backend.tools.lib.simulation_scope import is_simulation as _is_sim

            is_sim = bool(_is_sim(record))
        except Exception:
            is_sim = False
    return resolve_execution_context(
        record, is_simulation=is_sim, workplace_lookup=workplace_lookup
    )


def select_transport(sender_ctx: Optional[dict], recipient_ctx: Optional[dict]) -> dict:
    """Choose the shortest safe transport for a sender/recipient pair.

    ``local_path`` is returned **only** when both sides are conclusively
    ``host_local``.  Anything else fails closed to ``token_url``.
    """
    sender = (sender_ctx or {}).get("context")
    recipient = (recipient_ctx or {}).get("context")
    if sender == CTX_HOST_LOCAL and recipient == CTX_HOST_LOCAL:
        return {"transport": TRANSPORT_LOCAL_PATH, "reason": "both_host_local"}
    return {
        "transport": TRANSPORT_TOKEN_URL,
        "reason": f"sender={sender or CTX_UNKNOWN},recipient={recipient or CTX_UNKNOWN}",
    }


# ---------------------------------------------------------------------------
# Owned-artifact resolution (simulation aware)
# ---------------------------------------------------------------------------

def resolve_owned_artifact(agent: Optional[dict], filename: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve *filename* inside the acting agent's artifact directory.

    Returns ``(absolute_path, error)``.  The path is always a regular file
    directly inside ``shared/agents/<effective-agent-id>/artifacts`` — the
    simulation-aware root is used, so a simulated agent can never be pointed
    at the live registry.  Only one of the two tuple members is ever set.
    """
    name = safe_basename(filename)
    if not name:
        return None, (
            "Invalid filename: provide a plain basename such as \"chart.png\" "
            "(no \"/\", \"\\\\\", \"..\" or leading dot)."
        )

    from backend.tools._workspace import effective_agent_id
    from backend.tools.lib.simulation_scope import shared_agents_dir

    record = agent or {}
    agent_id = effective_agent_id(record)
    if not agent_id:
        return None, "Agent ID not found in context"

    artifacts_dir = os.path.join(shared_agents_dir(record), agent_id, "artifacts")
    candidate = os.path.join(artifacts_dir, name)

    # Defence in depth: the resolved path must stay directly inside the dir.
    try:
        real_dir = os.path.realpath(artifacts_dir)
        real_path = os.path.realpath(candidate)
    except OSError:
        return None, "Artifact directory is not accessible."
    if real_path != os.path.join(real_dir, name):
        return None, "Invalid filename: resolves outside the artifact directory."

    if not os.path.isfile(real_path):
        return None, f'Artifact not found: "{name}". Use list_artifacts to see available files.'

    return real_path, None


# ---------------------------------------------------------------------------
# Filename / hashing / streaming helpers
# ---------------------------------------------------------------------------

def safe_basename(filename) -> Optional[str]:
    """Validate and return a safe plain basename, or ``None`` when unsafe.

    Rejects path separators, traversal, hidden/relative names, control
    characters and over-long names.  The result is always a plain file name
    that cannot escape the artifact directory.
    """
    if not isinstance(filename, str):
        return None
    name = filename.strip()
    if not name or len(name) > 255:
        return None
    if name != os.path.basename(name):
        return None
    if "/" in name or "\\" in name:
        return None
    if name in (".", "..") or name.startswith("."):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return None
    return name


def sanitize_download_name(name, fallback: str = "artifact.bin") -> str:
    """Return a safe attachment filename, degrading to *fallback* when unusable.

    Unlike :func:`safe_basename` (which *rejects* unsafe input so a sender
    cannot smuggle a path), this *repairs* display names: any directory
    component is stripped (so ``"../evil path.pdf"`` becomes
    ``"evil path.pdf"``), control characters are dropped, and an empty /
    hidden / over-long result falls back to *fallback*.  Used for the
    ``Content-Disposition`` filename on download.
    """
    if not isinstance(name, str):
        return fallback
    candidate = name.replace("\\", "/").split("/")[-1].strip()
    candidate = "".join(ch for ch in candidate if ord(ch) >= 32 and ord(ch) != 127)
    candidate = candidate.strip()
    if not candidate or candidate in (".", "..") or candidate.startswith("."):
        return fallback
    return candidate[:255]


def sha256_and_size(path: str, chunk_size: int = STREAM_CHUNK_SIZE) -> Tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` using bounded-memory streaming."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def snapshot_and_digest(
    source_path: str, dest_path: str, chunk_size: int = STREAM_CHUNK_SIZE
) -> Tuple[str, int]:
    """Copy *source_path* to *dest_path* while hashing, in one bounded pass.

    Returns ``(sha256_hex, size_bytes)``.  The destination is an immutable
    snapshot: later changes to the source artifact never affect a download.
    """
    digest = hashlib.sha256()
    size = 0
    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(source_path, "rb") as src, open(dest_path, "wb") as dst:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def mint_token() -> str:
    """Mint a 256-bit urlsafe download token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of *token* (only the digest is persisted)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    """Constant-time string comparison (never short-circuits)."""
    if left is None or right is None:
        return False
    try:
        return hmac.compare_digest(str(left), str(right))
    except (TypeError, ValueError):
        return False


def is_valid_token_format(token) -> bool:
    """Strict token syntax check performed before any lookup."""
    return bool(isinstance(token, str) and TOKEN_RE.match(token))


def new_staging_name() -> str:
    """Opaque staging file name — never derived from the token."""
    return f"{secrets.token_hex(16)}.blob"


# ---------------------------------------------------------------------------
# Spool helpers
# ---------------------------------------------------------------------------

def ensure_spool_dir() -> str:
    path = get_spool_dir()
    os.makedirs(path, exist_ok=True)
    return path


def is_under_spool(path: str) -> bool:
    """True when *path* resolves inside the configured spool directory."""
    if not path:
        return False
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(get_spool_dir())
    except OSError:
        return False
    return real == base or real.startswith(base + os.sep)


def safe_remove_spool_file(path: str) -> bool:
    """Delete a spool file, refusing any path outside the spool directory."""
    if not path or not is_under_spool(path):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def spool_usage_bytes() -> int:
    """Current total bytes held in the spool directory."""
    total = 0
    spool = get_spool_dir()
    try:
        with os.scandir(spool) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total

# Artifact Share

Cross-agent binary artifact sharing for Evonic.

`artifact-share` lets one agent hand a binary artifact (PDF, image, archive, dataset,
build output, and so on) to another agent **without ever placing binary data or base64
into a message**. It adds a single agent tool, `share_artifact`, plus a tokenized
download endpoint served by the host application.

---

## 1. Problem this plugin solves

Agents frequently need to exchange files: reports, screenshots, archives, generated
code bundles. The messaging channel is meant for text. Pasting bytes or base64 into a
message:

- inflates the payload (base64 adds ~33%),
- burns token/context budget on both sides,
- is brittle (truncation, encoding corruption),
- and leaks content into conversation history and logs.

`artifact-share` replaces that with a filesystem-level handoff. The sender exposes a
file it already owns; the recipient copies it into its own workspace and verifies a
SHA-256 checksum. The message itself carries only metadata.

---

## 2. Transports

The platform picks the **shortest safe transport** automatically. Selection is derived
from each agent's execution context — never from a host name, a loopback address, a
workspace name, or any user-supplied value.

### 2.1 Execution contexts

| Context     | Meaning                                                        |
|-------------|----------------------------------------------------------------|
| `host_local`| Runs directly on the Evonic host filesystem                    |
| `sandbox`   | No/`local` workplace with `sandbox_enabled` → Docker isolation |
| `bwrap`     | Bubblewrap-isolated workplace                                  |
| `remote`    | SSH remote workplace (executes on another host)                |
| `tunnel`    | Tunnel / connector workplace                                    |
| `unknown`   | Simulation, missing/stale workplace, or malformed context      |

Only `host_local` agents can read a path written by another `host_local` agent.
Remote, tunnel, and bwrap backends own their own filesystems and are never treated as
host-local.

### 2.2 `local_path`

Returned **only** when both the sender and the recipient are conclusively `host_local`.
The sender receives the owner's absolute artifact path and can copy the file straight
from the shared filesystem. This is the fastest path — no bytes move over HTTP.

### 2.3 `token_url`

Returned for **every other case**: `sandbox`, `bwrap`, `remote`, `tunnel`, `unknown`, or
simulation. The plugin:

1. snapshots the artifact into an opaque server-side spool file,
2. mints a 256-bit random bearer token and persists only its SHA-256 digest,
3. returns a short-lived, single-use HTTPS download URL.

If the execution context cannot be *proven* local, the selection fails closed to
`token_url`. It never optimistically exposes a filesystem path.

---

## 3. Using the tool: `share_artifact`

### 3.1 Parameters

| Parameter            | Required | Description                                                                 |
|----------------------|----------|-----------------------------------------------------------------------------|
| `filename`           | yes      | Exact artifact basename as it appears in your artifact directory (e.g. `report.pdf`). A plain basename — no directory separators. |
| `recipient_agent_id` | yes      | ID of the recipient agent, lowercase snake_case (e.g. `linus`).              |

### 3.2 Validation performed before any share is created

1. Plugin kill switch is checked (`ENABLED`).
2. Filename is validated as a safe plain basename.
3. The extension allowlist is applied (`ALLOWED_EXTENSIONS`, when configured).
4. Recipient ID is validated (`^[a-z0-9_]+$`).
5. Self-sharing is rejected.
6. Sub-agents may only share with their parent agent (mirrors messaging rules).
7. The same messaging ACL used by `send_agent_message` is enforced.
8. The recipient must exist and be enabled (super agents are exempt from the enabled check).
9. The artifact is resolved **only** inside the acting agent's own artifact directory,
   with a real-path check that rejects anything escaping that directory.
10. Size and SHA-256 are computed with bounded-memory streaming.
11. The maximum share size (`MAX_MB`) is enforced — for both transports.

### 3.3 `local_path` response

```json
{
  "transport": "local_path",
  "artifact_path": "/abs/path/to/artifacts/report.pdf",
  "filename": "report.pdf",
  "size_bytes": 123456,
  "sha256": "e3b0c44298fc1c149afbf4c8996fb924..."
}
```

### 3.4 `token_url` response

```json
{
  "transport": "token_url",
  "url": "https://agents.example.com/plugin/artifact-share/dl/<token>",
  "expires_at": "2026-10-16T07:35:00+00:00",
  "ttl_seconds": 300,
  "filename": "report.pdf",
  "size_bytes": 123456,
  "sha256": "e3b0c44298fc1c149afbf4c8996fb924...",
  "single_use": true
}
```

`expires_at` is an ISO-8601 UTC timestamp.

### 3.5 Recipient contract

The recipient is expected to:

- **`local_path`** — copy the file into its configured workspace immediately, verify the
  SHA-256, and never forward the path to a third party.
- **`token_url`** — download the URL exactly once (it is single-use), verify the
  SHA-256, then work from the local copy.

Verification (any host):

```bash
sha256sum report.pdf        # Linux
shasum -a 256 report.pdf    # macOS
```

The reported `sha256` must match the downloaded file exactly. Only then should the file
be used.

---

## 4. HTTP endpoints

### 4.1 `GET /plugin/artifact-share/dl/<token>`

Downloads a tokenized artifact snapshot.

- Only `GET` with a valid token succeeds.
- The token is **atomically consumed**: two concurrent requests cannot both download the
  same single-use share.
- The snapshot is streamed and then deleted — on completion **and** on client disconnect.
- Unknown, expired, revoked, exhausted, malformed, or vanished-snapshot tokens all return
  the **same** response: `404` with `{"error": "not_found"}`.
- Response headers:

  | Header                    | Value                    |
  |---------------------------|--------------------------|
  | `Content-Type`            | `application/octet-stream` |
  | `Content-Disposition`     | `attachment; filename="<sanitized name>"` |
  | `Cache-Control`           | `no-store`               |
  | `Pragma`                  | `no-cache`               |
  | `X-Content-Type-Options`  | `nosniff`                |

  No `ETag`, no `Last-Modified`, no conditional caching.

### 4.2 `GET /plugin/artifact-share/` — status probe

```json
{
  "plugin": "artifact-share",
  "enabled": true,
  "ttl_seconds": 300,
  "max_mb": 100
}
```

This endpoint intentionally reveals only effective TTL and size limits — never tokens,
paths, or share records.

### 4.3 Uniform failure for other paths

- `/plugin/artifact-share/dl/` without a token → uniform JSON `404`.
- Any other unmatched path under `/plugin/artifact-share/` → uniform JSON `404`.

The same body is used everywhere so an attacker cannot distinguish "wrong token" from
"no token" from "no such route".

---

## 5. Configuration

Configuration is resolved with this precedence:

> system setting → plugin config variable → environment variable → built-in default

Plugin variables are editable from the plugin configuration UI and are stored as
`plugin_config:artifact-share:<NAME>`.

| Plugin variable      | System setting key                | Environment variable               | Type    | Default                                  |
|----------------------|-----------------------------------|------------------------------------|---------|------------------------------------------|
| `ENABLED`            | `artifact_share_enabled`          | `ARTIFACT_SHARE_ENABLED`           | boolean | `true`                                   |
| `PUBLIC_BASE_URL`    | `artifact_share_public_base_url`  | `EVONIC_PUBLIC_URL`                | string  | *(empty)* → `http://127.0.0.1:8080`      |
| `TOKEN_TTL_SECONDS`  | `artifact_share_token_ttl`        | `ARTIFACT_SHARE_TOKEN_TTL`         | number  | `300` (clamped 30–3600)                  |
| `MAX_MB`             | `artifact_share_max_mb`           | `ARTIFACT_SHARE_MAX_MB`            | number  | `100` (clamped 1–10240)                  |
| `SPOOL_DIR`          | `artifact_share_spool_dir`        | `ARTIFACT_SHARE_SPOOL_DIR`         | string  | `<APP_ROOT>/data/spool/artifact_share`   |
| `SPOOL_QUOTA_MB`     | `artifact_share_spool_quota_mb`   | `ARTIFACT_SHARE_SPOOL_QUOTA_MB`    | number  | `2048` (clamped 16–1048576)              |
| `ALLOWED_EXTENSIONS` | `artifact_share_allowed_extensions` | `ARTIFACT_SHARE_ALLOWED_EXTENSIONS` | string | *(empty)* → all types allowed          |

### 5.1 Variable details

- **`ENABLED`** — kill switch. When disabled, both `share_artifact` and the download
  endpoint fail closed (the endpoint returns the uniform 404).
- **`PUBLIC_BASE_URL`** — externally reachable origin used to build download URLs, with
  **no trailing slash** (a trailing slash is stripped automatically). If empty, the
  plugin falls back to `EVONIC_PUBLIC_URL`, then to the deployment-agnostic default
  `http://127.0.0.1:8080`. In production this must be the HTTPS origin that Nginx serves.
- **`TOKEN_TTL_SECONDS`** — token lifetime. Default and expected value is `300` seconds.
  Values are clamped to `[30, 3600]`.
- **`MAX_MB`** — largest artifact that may be shared. Applies equally to `local_path`
  and `token_url` shares. Clamped to `[1, 10240]`.
- **`SPOOL_DIR`** — directory holding opaque snapshots until they are downloaded or
  expire. Defaults to `<APP_ROOT>/data/spool/artifact_share`.
- **`SPOOL_QUOTA_MB`** — maximum total spool size. When the spool is at or above quota,
  new `token_url` shares are refused with "The artifact spool is full". Clamped to
  `[16, 1048576]`.
- **`ALLOWED_EXTENSIONS`** — optional comma-separated allowlist such as `.pdf,.png,.zip`.
  Empty allows any file type. Matching is case-insensitive and tolerant of entries with
  or without a leading dot.

### 5.2 Token database

Token records are stored in SQLite in WAL mode:

- `shared/data/db/plugins/artifact_share.db` when the shared data root exists, otherwise
- `data/db/plugins/artifact_share.db`.

Each row stores the token **digest** (never the token), owner/sender/recipient agent IDs,
the safe display filename, byte size, content SHA-256, the opaque staging path, and
creation/expiry/usage/revocation state.

### 5.3 Recommended production profile

| Variable             | Recommended               | Why                                                        |
|----------------------|---------------------------|------------------------------------------------------------|
| `ENABLED`            | `true`                    | Only disable during maintenance.                            |
| `PUBLIC_BASE_URL`    | `https://<your-public-host>` | Tokens must travel over TLS, never plaintext.            |
| `TOKEN_TTL_SECONDS`  | `300`                     | Short enough to limit exposure, long enough to download.    |
| `MAX_MB`             | `100` (or lower)          | Caps spool pressure and download time.                      |
| `SPOOL_DIR`          | Dedicated volume with free space | Isolates snapshot bytes from application data.      |
| `SPOOL_QUOTA_MB`     | `2048` or sized to volume  | Prevents unbounded disk growth.                            |
| `ALLOWED_EXTENSIONS` | Restrict if sharing is narrow | Defence in depth for unintended file types.            |

Example production configuration:

```ini
ENABLED=true
PUBLIC_BASE_URL=https://agents.example.com
TOKEN_TTL_SECONDS=300
MAX_MB=100
SPOOL_DIR=/var/lib/evonic/spool/artifact_share
SPOOL_QUOTA_MB=2048
ALLOWED_EXTENSIONS=.pdf,.png,.jpg,.zip,.csv,.json
```

---

## 6. Nginx deployment

The token lives in the URL path, so the reverse proxy must not retain or cache it. The
plugin ships `nginx-location.conf` for exactly this purpose.

### 6.1 Install the location block

Copy the `location` block from `plugins/artifact-share/nginx-location.conf` into **every
relevant server block** — both the HTTP:80 and the HTTPS:443 virtual hosts of the Evonic
site (for example `/etc/nginx/sites-available/<your-site>.conf`), then validate and
reload:

```bash
nginx -t && systemctl reload nginx
```

The block itself:

```nginx
location /plugin/artifact-share/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_cache off;
    gzip off;
    chunked_transfer_encoding on;

    proxy_connect_timeout 30s;
    proxy_send_timeout    120s;
    proxy_read_timeout    120s;

    access_log off;
}
```

### 6.2 Why each directive matters

| Directive                       | Reason                                                                                     |
|---------------------------------|--------------------------------------------------------------------------------------------|
| `proxy_pass http://127.0.0.1:8080` | Backend address of the Evonic application. **Change this if your app listens elsewhere.** |
| `proxy_http_version 1.1`        | Required for chunked transfer/streaming to the client.                                    |
| `proxy_set_header Host/X-Real-IP/X-Forwarded-*` | Preserves client context for the backend.                                 |
| `proxy_buffering off`           | Streams the download straight through instead of buffering the whole file in Nginx.        |
| `proxy_cache off`               | Prevents a bearer-authorized payload from being stored in any proxy cache.                 |
| `gzip off`                      | Avoids compressing/buffering the stream.                                                   |
| `chunked_transfer_encoding on`  | Enables streaming to clients of unknown-length responses.                                  |
| `proxy_connect_timeout 30s`     | Bounds how long Nginx waits to reach the backend.                                          |
| `proxy_send_timeout` / `proxy_read_timeout 120s` | Bounds how long a slow client can occupy a proxy worker while still allowing large downloads. |
| `access_log off`                | **Critical:** the URL path contains a bearer token; this keeps it out of Nginx access logs. |

### 6.3 Important notes

- The sample uses `http://127.0.0.1:8080` as the backend. Adjust it to match your
  deployment. The *public* URL (`PUBLIC_BASE_URL`) should normally be HTTPS.
- If the block is omitted, downloads may still work locally, but tokens will then be at
  risk of landing in Nginx access logs and buffers. Add the block in production.
- If you terminate TLS elsewhere (load balancer, ingress), apply the same
  `access_log off` / no-cache / no-buffering rules at that layer too.

---

## 7. Security model

### 7.1 Authorization

- `local_path` is returned only for proven same-host execution; every uncertain case
  fails closed to `token_url`.
- Agents cannot share with themselves.
- Sub-agents can only share with their parent agent.
- The messaging ACL is enforced identically to `send_agent_message`.
- The recipient must exist and be enabled.
- Only artifacts owned by the acting agent can be resolved.

### 7.2 Filename and path safety

- Input must be a plain basename: no `/`, no `\`, no `..`, no leading dot, no control
  characters, max 255 characters.
- The resolved artifact path must stay directly inside the agent's artifact directory
  (real-path check) — anything else is rejected.
- Download attachment names are sanitized (directory components stripped, control
  characters removed, fallback applied) before being sent in `Content-Disposition`.
- Spool files are removed only through a helper that refuses any path outside the
  configured spool directory.

### 7.3 Snapshot immutability

The artifact is snapshotted **and hashed in a single bounded pass** before the URL is
issued. If the source changes between the initial hash and the snapshot, the share is
rejected ("The artifact changed while it was being shared"). A download therefore always
returns the exact bytes that were hashed and reported.

### 7.4 Token protection

- 256 bits of entropy (`secrets.token_urlsafe(32)` → 43-character URL-safe string).
- Only the SHA-256 digest is persisted; the raw token never touches the database or logs.
- Token syntax is validated (regex) **before** any database lookup.
- Digest comparison is constant-time.
- Consumption is an atomic SQLite transaction (`BEGIN IMMEDIATE`) with `max_uses = 1`.
- Unknown, expired, revoked, exhausted, malformed, and vanished-snapshot tokens are
  deliberately indistinguishable (uniform `404 {"error": "not_found"}`).
- Downloads are sent with `no-store`, `no-cache`, `nosniff`, no ETag, and no conditional
  caching.
- Audit records store only safe metadata: transport, reason, filename, size, SHA-256, TTL.

### 7.5 Cleanup

- Expired and revoked rows are pruned from the database.
- Their staging files are removed only when they resolve inside the spool directory.
- Cleanup runs at blueprint startup and periodically in a daemon thread every
  **300 seconds**.
- A consumed download snapshot is deleted when streaming completes or the WSGI iterator
  closes, so spool bytes cannot outlive their request.
- An expired or externally removed snapshot is treated as an ordinary 404.

---

## 8. Operational limits

| Limit                                  | Value / Source                                   |
|----------------------------------------|--------------------------------------------------|
| Max artifact size                      | `MAX_MB` (default 100 MB)                        |
| Token TTL                              | `TOKEN_TTL_SECONDS` (default 300 s, clamp 30–3600) |
| Token uses                             | 1 (single-use)                                   |
| Spool quota                            | `SPOOL_QUOTA_MB` (default 2048 MB)               |
| Cleanup interval                       | 300 s                                            |
| Streaming chunk size                   | 1 MiB                                            |
| Filename length                        | 255 characters                                   |

---

## 9. Troubleshooting

| Symptom                                                   | Likely cause / remedy                                                                 |
|-----------------------------------------------------------|---------------------------------------------------------------------------------------|
| `Artifact sharing is disabled by the administrator.`      | `ENABLED` is false. Re-enable it in the plugin configuration.                          |
| `Invalid filename: ...`                                   | You passed a path instead of a plain basename. Use only the file name.                 |
| `Artifact type '<ext>' is not permitted for sharing`      | The extension is not in `ALLOWED_EXTENSIONS`. Add it or clear the allowlist.           |
| `Agent '<id>' not found.` / `... is currently disabled.`  | The recipient does not exist or is disabled.                                           |
| `An agent cannot share an artifact with itself.`          | `recipient_agent_id` equals the sender.                                                |
| `Sub-agents can only share artifacts with their parent agent` | A sub-agent tried to target a non-parent recipient.                              |
| `Artifact '...' is N bytes, which exceeds the X MB share limit.` | Raise `MAX_MB` or share a smaller file.                                        |
| `Artifact not found: "..."`                               | The file is not in your artifact directory. Check with `list_artifacts`.               |
| `The artifact spool is full; please try again later.`     | Spool usage ≥ `SPOOL_QUOTA_MB`. Wait for cleanup or raise the quota.                   |
| `The artifact spool is not writable: ...`                 | `SPOOL_DIR` is missing or lacks permissions for the app user.                          |
| `Failed to snapshot artifact '...'`                       | I/O error while copying into the spool. Check disk space and permissions.              |
| `The artifact changed while it was being shared; please retry.` | The source file was modified mid-share. Retry when the file is stable.            |
| Download returns `404 {"error": "not_found"}`             | Token unknown, expired (past TTL), already used, revoked, malformed, or the snapshot was removed. Request a new share. |
| Download URL points at `127.0.0.1`                        | `PUBLIC_BASE_URL` is unset (and `EVONIC_PUBLIC_URL` is unset). Set the public HTTPS origin. |
| Tokens appear in web-server logs                           | The Nginx `location` block (or an upstream TLS terminator) is missing `access_log off`. See section 6. |
| Download stalls or truncates for large files               | Verify `proxy_buffering off`, `chunked_transfer_encoding on`, and the 120 s proxy timeouts in the Nginx block. |

---

## 10. Files in this plugin

| File                              | Purpose                                                                  |
|-----------------------------------|--------------------------------------------------------------------------|
| `plugin.json`                     | Plugin metadata and configuration variables.                              |
| `tools.json`                      | Declares the `share_artifact` agent tool.                                 |
| `db.py`                           | SQLite store for tokenized shares (WAL, atomic single-use consumption).    |
| `routes.py`                       | Flask blueprint: download endpoint, status probe, uniform 404s, cleanup.   |
| `backend/tools/share_artifact.py` | Sender-side tool implementation and transport selection.                  |
| `nginx-location.conf`             | Nginx location block for the download endpoint.                           |

Shared helper: `backend/tools/lib/artifact_delivery.py` is the single source of truth for
execution-context classification, transport selection, config resolution, token helpers,
path safety, and spool management.

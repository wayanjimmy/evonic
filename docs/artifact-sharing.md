# Cross-Agent Artifact Sharing

Evonic agents can hand **binary artifacts** (PDFs, images, archives, datasets)
to one another without ever putting bytes into a message. The platform picks the
shortest *safe* transport automatically:

* a **direct host-local filesystem path** when both agents provably run directly
  on the same Evonic host, or
* a **short-lived, single-use tokenized HTTPS download URL** in every other case.

Binary payloads, base64 blobs, arbitrary workspace paths and storage keys are
**never** sent through `send_agent_message`.

---

## 1. The `share_artifact` tool (sender side)

```jsonc
{
  "filename": "report.pdf",          // exact basename in YOUR artifact directory
  "recipient_agent_id": "linus"      // lowercase snake_case agent id
}
```

Before doing anything the tool validates, in order:

1. artifact sharing is enabled (kill switch);
2. `filename` is a safe plain basename (no `/`, `\`, `..`, leading dot, control
   chars) and passes the optional extension allowlist;
3. `recipient_agent_id` is syntactically valid and is not the sender;
4. sub-agents may only share with their **parent** agent;
5. the recipient passes the **same messaging ACL** used by `send_agent_message`
   (whitelist / blacklist, super agents bypass);
6. the recipient exists and is enabled;
7. the file is a regular file inside the sender's own artifact directory
   (`shared/agents/<agent-id>/artifacts/`, simulation-aware);
8. the file size is within the configured limit;
9. SHA-256 and byte size are computed by bounded-memory streaming.

The tool then resolves the execution context of both agents and selects the
transport. Every share is written to the audit log with the acting sender, the
artifact owner (a sub-agent's parent), the recipient, the chosen
transport/reason, the safe filename, size and checksum — never the raw token or
URL.

### Result: `local_path`

```json
{"transport":"local_path","artifact_path":"/abs/path/to/report.pdf",
 "filename":"report.pdf","size_bytes":123,"sha256":"<hex>"}
```

Only returned when **both** agents are conclusively `host_local`. It exposes the
owner's artifact path and nothing else.

### Result: `token_url`

```json
{"transport":"token_url",
 "url":"https://agents.example.com/plugin/artifact-share/dl/<token>",
 "expires_at":"2026-01-01T00:05:00+00:00","ttl_seconds":300,
 "filename":"report.pdf","size_bytes":123,"sha256":"<hex>","single_use":true}
```

---

## 2. Decision matrix

The transport is chosen conservatively from the agent record
(`workplace_id`, `sandbox_enabled`), the workplace **type** and the simulation
state. Localness is never inferred from an SSH host name, a loopback address, a
workspace name, or any user-supplied host value.

| Sender context | Recipient context | Transport |
|---|---|---|
| `host_local` | `host_local` | **`local_path`** |
| any | any other combination | **`token_url`** |

Execution-context resolution:

| Agent configuration | Context |
|---|---|
| no workplace + `sandbox_enabled=0` | `host_local` |
| `local` workplace + `sandbox_enabled=0` | `host_local` |
| no workplace + `sandbox_enabled=1` | `sandbox` |
| `local` workplace + `sandbox_enabled=1` | `sandbox` |
| `bwrap` workplace | `bwrap` |
| `remote` workplace | `remote` |
| `tunnel` workplace | `tunnel` |
| simulation, missing/stale workplace, malformed type | `unknown` |

`remote`, `tunnel` and `bwrap` are **never** treated as host-local, even though
policy forces `sandbox_enabled=0` for them. `unknown` fails closed to
`token_url`, so no host path is ever leaked.

---

## 3. Recipient procedures

### A. Direct host-local copy (`local_path`)

1. Read the `artifact_path` from the tool result.
2. **Never forward the path** to a third party or paste it into a message.
3. Copy the file into your own configured workspace (e.g. `bash`/`runpy`:
   `cp "<artifact_path>" <your-workspace>/`).
4. Verify the copy:
   ```bash
   sha256sum <your-workspace>/report.pdf   # must equal the result's "sha256"
   ```
5. Only use the file after the digest matches.

### B. Tokenized URL download (`token_url`)

1. Download the URL **once** — the token is single-use and expires after the
   advertised `ttl_seconds` (default 300 s / 5 minutes):
   ```bash
   curl -fSL "<url>" -o report.pdf
   ```
2. Verify the digest before use:
   ```bash
   sha256sum report.pdf   # must equal the result's "sha256"
   ```
3. If you get a `404`, the token was already used, expired, revoked, or is
   otherwise invalid. Ask the sender to issue a new share — do not retry the
   same URL.
4. The URL is a bearer credential: treat it as a secret and do not log or
   forward it.

Neither `send_agent_message` nor this tool accepts binary content. To reply,
send a short textual acknowledgement referencing the filename and checksum.

---

## 4. Download endpoint

```
GET /plugin/artifact-share/dl/<token>
```

* The token is 256 bits (`secrets.token_urlsafe(32)`); **only its SHA-256
  digest is stored**, so a database leak never reveals a usable credential.
* An **immutable opaque snapshot** of the artifact is written to the server-side
  spool *before* the URL is issued, so later edits to the original artifact do
  not affect the download.
* Downloads are streamed unbuffered with bounded memory and deleted as soon as
  the response is sent (or the client disconnects).
* Every response carries `Cache-Control: no-store`, `Pragma: no-cache` and
  `X-Content-Type-Options: nosniff`.
* Malformed, unknown, expired, revoked and already-consumed tokens, missing
  staging bytes, and the disabled kill switch all return an **identical bare
  `404`** — the endpoint cannot be used to probe which tokens exist.
* Consumption is atomic: two concurrent requests for the same token cannot both
  succeed.

---

## 5. Configuration

Every value is resolved at runtime in this order:
**plugin/system setting → environment variable → built-in default.**

| Plugin variable | Environment variable | Default | Purpose |
|---|---|---|---|
| `ENABLED` | `ARTIFACT_SHARE_ENABLED` | `true` | kill switch for the tool and the endpoint |
| `PUBLIC_BASE_URL` | `EVONIC_PUBLIC_URL` | `http://127.0.0.1:8080` | public origin used to build download URLs |
| `TOKEN_TTL_SECONDS` | `ARTIFACT_SHARE_TOKEN_TTL` | `300` | download-URL lifetime (seconds) |
| `MAX_MB` | `ARTIFACT_SHARE_MAX_MB` | `100` | maximum share size (MB) |
| `SPOOL_DIR` | `ARTIFACT_SHARE_SPOOL_DIR` | `<APP_ROOT>/data/spool/artifact_share` | opaque snapshot store |
| `SPOOL_QUOTA_MB` | `ARTIFACT_SHARE_SPOOL_QUOTA_MB` | `2048` | spool size cap before new shares are refused |
| `ALLOWED_EXTENSIONS` | `ARTIFACT_SHARE_ALLOWED_EXTENSIONS` | *(empty = any)* | optional extension allowlist |

Set the plugin variables in **Plugins → Artifact Share → Settings**, or the
values above in `.env`. In production `PUBLIC_BASE_URL` / `EVONIC_PUBLIC_URL`
must be the externally reachable origin so that recipients on other hosts can
resolve the download URL.

The equivalent advanced settings keys are
`artifact_share_enabled`, `artifact_share_token_ttl`,
`artifact_share_max_mb`, `artifact_share_spool_dir`,
`artifact_share_spool_quota_mb`, `artifact_share_allowed_extensions` and
`artifact_share_public_base_url`.

---

## 6. Operations

* **Enabling.** The `Artifact Share` plugin must be enabled and the
  `share_artifact` tool assigned to the sending agent (Plugins → agent tools).
* **Rate limiting.** `/plugin/artifact-share/*` uses a dedicated bounded
  `download` tier (60 req/min per user/IP) in `models/api_rate_limit.py`.
* **Reverse proxy.** A dedicated nginx `location /plugin/artifact-share/` block
  streams unbuffered, disables caching, uses bounded proxy timeouts, and sets
  `access_log off` so the bearer token is never written to access logs. Apply it
  to both the HTTP and HTTPS server blocks.
* **Cleanup.** Expired/revoked shares are purged opportunistically on issue and
  download, at plugin startup, and by a periodic background sweep; their spool
  bytes are removed at the same time.
* **Audit.** Each share emits an `artifact_share` audit event (sender, owner,
  recipient, transport/reason, filename, size, SHA-256). Raw tokens/URLs are
  never recorded.
* **Simulations.** Inside an agent-template simulation `share_artifact` is
  intercepted and never executes for real.

---

## 7. Tests

| File | Coverage |
|---|---|
| `unit_tests/test_artifact_delivery.py` | context classifier + decision matrix, `select_transport`, basename/type guards, streaming hash, token helpers |
| `unit_tests/test_artifact_share_tool.py` | sender/recipient validation, ACL, no path disclosure outside local↔local, token-hash-only persistence, snapshot immutability |
| `unit_tests/test_artifact_share_routes.py` | uniform 404, single-use, expiry, revocation, safe headers, sanitized filename, cleanup, hash-only persistence |
| `unit_tests/test_artifact_share_config.py` | public-base-URL / TTL / size / kill-switch precedence and defaults |
| `unit_tests/test_api_rate_limit_classify.py` | `download` tier classification |

Run them with:

```bash
python -m pytest unit_tests/test_artifact_delivery.py \
                 unit_tests/test_artifact_share_tool.py \
                 unit_tests/test_artifact_share_routes.py \
                 unit_tests/test_artifact_share_config.py \
                 unit_tests/test_api_rate_limit_classify.py -q
```

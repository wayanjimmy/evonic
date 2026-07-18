## Final implementation plan: Mattermost gateway

### 1. Add Mattermost as a channel type

Create:

```text
backend/channels/mattermost.py
```

Register it in:

```text
backend/channels/registry.py
```

```python
CHANNEL_TYPES["mattermost"] = MattermostChannel
```

The adapter will implement the existing `BaseChannel` contract:

- `start()` / `stop()`
- `_do_send()`
- `_do_send_file()`
- `get_channel_type()`
- `get_system_instructions()`
- progress-aware intermediate delivery
- optional best-effort typing/status behavior

---

### 2. Channel configuration and UI

Add **Mattermost** in the agent channel creation UI.

Config:

```json
{
  "base_url": "https://mattermost.example.com",
  "bot_token": "…",
  "mode": "restricted"
}
```

The UI will collect:

- Mattermost server URL
- bot token
- existing restricted/open access setting

It will include setup guidance: bot must be able to read the intended channels, post replies, edit its own posts, and upload/download files when attachment support is enabled.

Add an `MM` session badge in `templates/sessions.html`.

---

### 3. Connection architecture

Use the current Mattermost APIs:

| Direction | Mechanism |
|---|---|
| Inbound messages | Mattermost WebSocket `/api/v4/websocket` |
| Outbound messages | REST API v4 |
| Bot verification | `GET /api/v4/users/me` |
| Thread replies | `POST /api/v4/posts` with `root_id` |
| Progress updates | `PUT /api/v4/posts/{post_id}/patch` |
| Incoming file bytes | `GET /api/v4/files/{file_id}` |
| Outgoing files | Mattermost upload endpoint followed by a post with file IDs |

At startup, the adapter will verify credentials and dynamically load:

- bot user ID;
- bot username.

This allows reliable self-message filtering and mention removal without hardcoded identity values.

The WebSocket runs in a managed background thread, with bounded exponential reconnect backoff. REST transient failures get limited retry/backoff; ambiguous post timeouts do not blindly retry to avoid duplicates.

---

### 4. Inbound activation and thread policy

#### DMs and group DMs

Both are supported:

| Mattermost type | Behavior |
|---|---|
| `D` direct message | Handle any message from an authorized user |
| `G` group DM | Handle any message from an authorized user |

No mention required.

#### Public and private channels

| Situation | Behavior |
|---|---|
| New top-level message | Requires `@bot` mention |
| Message in an Evonic-owned thread | Handle without mention |
| Message in unowned thread | Ignore unless explicitly `@bot` mentioned |
| Bot-authored message | Always ignore |

This gives the requested behavior: after the bot has been invoked in a thread, users can continue replying naturally without repeating the mention.

Mention detection will use Mattermost’s authoritative `event.data.mentions` user-ID list, not text matching. The dynamically resolved `@botUsername` is then removed from agent input.

---

### 5. Durable session and owned-thread model

Mattermost needs thread-scoped conversations rather than generic per-user sessions.

Session identifiers:

```text
DM / group DM:
mm:dm:<mattermost_channel_id>

Channel thread:
mm:thread:<mattermost_channel_id>:<root_post_id>
```

For a top-level mention:

```text
root_post_id = incoming post ID
```

For a thread reply:

```text
root_post_id = incoming post.root_id
```

Add durable storage for Evonic-owned Mattermost threads, approximately:

```text
mattermost_threads
- id
- evonic_channel_id
- agent_id
- mattermost_channel_id
- root_post_id
- started_by_user_id
- progress_post_id
- created_at
- updated_at
```

This provides:

- no-repeat-mention thread continuation;
- persistence across restarts;
- correct thread reply routing;
- durable progress-post tracking;
- safe rejection of unrelated thread replies.

Authorization remains per real Mattermost **author user ID**, consistent with existing restricted-mode pairing. Thread keys are only used for conversation history and delivery routing.

---

### 6. Access control and identities

Reuse existing Evonic pairing/allowlist behavior:

1. Unknown Mattermost user contacts the bot.
2. Restricted mode creates a pending approval/pairing record.
3. Approved user is recorded in the channel allowlist.
4. Mattermost display name is saved when available.

The implementation will maintain this distinction:

```text
Mattermost author ID → authorization, display name, pairing
Mattermost channel/thread key → session history and reply location
```

This prevents a group thread/session identity from accidentally becoming the access-control identity.

---

### 7. Progress UX

Mattermost will use **one editable progress reply per agent turn**, rather than posting every intermediate response as a separate message.

Example lifecycle:

```text
⏳ Preparing context…

• Preparing context
• Calling model
• Running tool: search_docs
• Reviewing tool results
• Preparing response
```

Rules:

- update only every roughly 1–2 seconds;
- expose high-level lifecycle states and safe tool names;
- do not expose chain-of-thought, full tool arguments, secrets, command output, private prompt data, or sensitive tool results;
- safely truncate any status detail;
- patch that post into the final answer at completion;
- if creation/patching fails, fall back to a normal final thread reply.

I’ll connect this to existing runtime events where they already exist. If they do not provide sufficiently structured lifecycle data, I’ll add a small provider-neutral progress event layer instead of making Mattermost depend directly on LLM-loop implementation details.

---

### 8. Messages, Markdown, and replies

Mattermost supports Markdown, so model instructions will allow it.

Outbound behavior:

- split responses conservatively to avoid deployment-specific post-size limits;
- reply inside the triggering Mattermost thread;
- preserve a single thread root for all progress and final messages;
- emit Evonic’s usual `message_sent` event;
- support bot-initiated sends when the external session key identifies an existing Mattermost destination.

---

### 9. Attachments

#### Incoming

For `post.metadata.files`:

1. parse and validate metadata in the inbound handler;
2. apply configured attachment enablement, MIME, and size limits;
3. download lazily from the Mattermost file endpoint;
4. save to Evonic’s existing attachment storage;
5. persist with:
   ```python
   channel_type="mattermost"
   ```
6. attach the normal `[Attached: ...]` prompt metadata;
7. supply vision/video data URLs only when agent capabilities are enabled;
8. keep audio attachment-only for the existing transcription-tool flow.

#### Outgoing

`send_file` will:

1. validate local file availability and size;
2. upload into the Mattermost destination channel;
3. create a root-thread reply referencing the returned file ID;
4. emit the normal outbound event.

---

### 10. Interactive tool approvals

Because the deployment is self-hosted and current, I’ll implement Mattermost-native approval actions as a supported integration path.

Approval behavior:

- create a Mattermost interactive post with **Approve** and **Reject** actions;
- action callback reaches a dedicated Evonic HTTPS route;
- validate Mattermost callback authenticity;
- bind the approval to the expected Mattermost user/session;
- resolve idempotently: expired/already-resolved actions display an appropriate result;
- patch the original approval post with the final status.

Fallback:

- if Mattermost cannot reach the Evonic callback URL, do not show unusable approval buttons;
- approval remains available through Evonic’s web UI.

---

### 11. Tests

Add:

```text
unit_tests/test_mattermost_channel.py
```

Test coverage will include:

- registry/type registration;
- config and startup token validation;
- nested `posted` event parsing;
- bot self-message loop prevention;
- mention detection from `event.data.mentions`;
- dynamic mention stripping;
- DM/group-DM routing;
- public/private top-level mention gating;
- owned-thread continuation without mention;
- unowned-thread rejection;
- durable owned-thread state behavior;
- thread session-key generation;
- restricted pairing and user display-name handling;
- outbound `root_id` routing;
- response splitting;
- progress-post create/patch/final fallback;
- attachment validation/download/persistence;
- outbound file upload/posting;
- reconnect and retry behavior with mocked REST/WebSocket clients;
- interactive approval callback validation and idempotency.

---

### 12. Expected files

```text
backend/channels/mattermost.py          new
backend/channels/registry.py            register implementation
backend/channels/base.py                only if a small generic progress hook is needed
models/schema.py                        Mattermost owned-thread/progress persistence
models/mixins/channels.py or new mixin  CRUD helpers for Mattermost thread state
routes/agents.py or new route module    secure Mattermost action callback endpoint
templates/agent_detail.html             Mattermost channel form/config/badge behavior
templates/sessions.html                 MM session badge
requirements.txt                        WebSocket client dependency
unit_tests/test_mattermost_channel.py   new
```

Implementation order:

1. schema + persistence helpers;
2. REST/WebSocket clients and lifecycle;
3. inbound routing, pairing, sessions, owned threads;
4. outbound threaded replies;
5. progress updates;
6. attachments/files;
7. native approvals and secure callbacks;
8. UI and comprehensive tests.

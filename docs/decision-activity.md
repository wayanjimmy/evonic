# Decision Activity API and Privacy Policy

Decision Activity provides bounded diagnostics for Decision Runtime invocations. It is an administrative observability feed, not a prompt, provider-response, or general logging store.

## Recorded event fields

Each event has an opaque ID, timestamp, source (`production`, `playground`, or `profile_test`), profile key/version, selected model ID and adapter/provider key, result class, typed error code, normalized confidence, total latency, attempt count, fallback status/reason, and optional correlation ID.

A detail event may include `outcome_summary`: a **strict allowlist snapshot** of normalized result field values only. The field name must match the Decision Schema-style lowercase key pattern, there are at most 32 fields, scalar strings are capped at 256 characters, and aggregate stored rows are capped at 12 KiB. Nested objects, arrays, provider candidates/logits, arbitrary payloads, and invalid keys are dropped. Keys resembling secret names are also dropped.

The store never accepts or records raw `input_context`, policy metadata, full prompts, provider responses, credentials, free-form provider error text, or arbitrary rerun payloads. Decision Activity is intentionally insufficient to reconstruct an invocation.

## Administration UI and safe reruns

**System Settings → Decision Models → Activity** offers the same source, profile, model, result/error class, fallback, correlation ID, and time-range filters as the API. It uses opaque cursor pagination and presents loading, empty, error, redaction, and retention-aware states. The detail panel links to the selected profile's Playground configuration and displays only normalized outcome and diagnostic fields.

The UI must never use `outcome_summary` as an input payload. It shows **Rerun in Playground** only if a future authorized source exposes a separately retained allowlisted `rerun.input_context` draft; selecting it creates a draft and still requires the administrator to explicitly select **Run safe test**. The current Activity repository never produces this field, so production events and normal retained events explain that rerun is unavailable instead of reconstructing sensitive or unavailable data.

## Runtime emission

`RuntimeRequest.activity_source` identifies the operational origin and defaults to `production`. Playground calls must set `playground`; saved profile-test callers must set `profile_test`.

The runtime emits an event for returned successes, failures, and terminal cancellations. Event persistence is wrapped in a broad best-effort boundary: database/telemetry failure is logged with only the profile key and cannot change, fail, or otherwise modify the decision returned to the consumer.

## Administrative API

All endpoints are protected by Evonic's global authenticated administrative middleware.

- `GET /api/admin/decision-activity`: list events. Supports `source`, `profile_key`, `model_id`, `result_class`, `error_code`, `fallback_used`, `correlation_id`, `from`, and `to` filters. Results are deterministically sorted by `(created_at DESC, id DESC)` and use opaque `cursor` pagination with a maximum page size of 100.
- `GET /api/admin/decision-activity/<event_id>`: retrieve the bounded detail record.
- `POST /api/admin/decision-activity/<event_id>/redact`: irreversibly clears the stored summary/correlation ID and hides the row from observability queries.
- `POST /api/admin/decision-activity/purge` with `{"before": <Unix timestamp>}`: deletes all older records and reports the delete count.

## Retention operations

Retention is an explicit, operationally invocable purge by timestamp so deployments can schedule it according to their policy. Redaction is available for an individual record. Indexes cover the primary administration queries: global time order, profile/time, model/time, and correlation/time.

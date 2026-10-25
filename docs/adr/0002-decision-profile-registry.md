---
title: Decision Profile registry and management API (ADR-002)
description: Durable, validated configuration for reusable Decision Model profiles.
sidebar:
  order: 3
---

# ADR-002: Decision Profile registry and management API

**Status:** Accepted

**Date:** 2026-10-11

**Decision:** Store reusable Decision Profile configuration in SQLite and manage it through an authenticated, CSRF-protected admin API.

## Context

ADR-001 defines the provider-neutral Decision Models contract and schema DSL. Consumers need durable, operator-managed settings without embedding model selection or policy knobs in their code. A Decision Profile is the stable lookup key for that configuration; runtime invocation remains out of scope for this registry.

## Stored configuration

Each `decision_profiles` row has an immutable `profile_key`, display metadata, `enabled`, `mode`, schema/policy versions, canonical Decision Schema, primary/fallback model IDs, timeout, retry limit, minimum confidence, timestamps, and an optimistic-concurrency `revision`.

- Profile keys must be lowercase, dot-separated identifiers, such as `application.release_gate`.
- `schema_version` is currently `decision-schema/v1`; the stored schema is validated and canonicalized with the ADR-001 contract parser.
- `mode` is one of `off`, `shadow`, or `enforce`.
- `timeout_ms` is 1–300,000, `retry_limit` is 0–10, and `minimum_confidence` is a finite value from 0 through 1.
- Model references are validated against configured models at write time, including legacy model-ID aliases. They are intentionally soft references: a later model deletion does not silently rewrite the profile. Operators must repair or disable affected profiles before a future runtime consumer uses them.

The repository never returns model credentials; the public profile payload contains only model identifiers.

## Built-in profile keys and lifecycle

The registry reserves these stable consumer keys:

- `safety.command`
- `agent.task_complexity`
- `agent.path_boundary`
- `agent.goal_completion`

Built-in keys are created and configured using the same validation as application-defined keys. Keys cannot be renamed. Built-in rows cannot be deleted because deletion would break their consumer contract; instead update them with `enabled: false` and typically `mode: "off"`. Application-defined profiles may be deleted when no consumer reference policy is in place.

This is deliberately safe before consumers are migrated: management can disable a built-in profile without breaking a stable key, and later consumer integration can treat a disabled/missing profile according to its own conservative fallback policy.

## Management API

All routes are protected by Evonic's global authenticated-session middleware. State-changing requests also use its global CSRF middleware.

| Method | Endpoint | Meaning |
| --- | --- | --- |
| `GET` | `/api/admin/decision-profiles` | List profiles. Supply `?enabled_only=1` to omit disabled profiles. |
| `POST` | `/api/admin/decision-profiles` | Create a profile. |
| `GET` | `/api/admin/decision-profiles/<profile_key>` | Read one profile. |
| `PUT` | `/api/admin/decision-profiles/<profile_key>` | Update using the current `revision`. Payload fields merge with the current profile, but the key is immutable. |
| `DELETE` | `/api/admin/decision-profiles/<profile_key>` | Delete an application-defined profile. Built-ins return a conflict. |

Creation returns `201`; reads and updates return `200`. Invalid input returns `400` with a safe error object:

```json
{
  "success": false,
  "error": {
    "code": "unknown_model",
    "path": "primary_model_id",
    "message": "does not reference a configured model"
  }
}
```

Duplicate keys and stale revisions return `409`; unknown keys return `404`. A successful update increments `revision`; clients must send that value back on subsequent `PUT` operations to avoid overwriting a concurrent administrator's change.

## Compatibility and migration

The table and index use idempotent `CREATE ... IF NOT EXISTS` migration statements within Evonic's standard database initialization path. Existing installations start with an empty registry and retain all prior behavior because runtime consumer migration is separate work.

---
title: Provider-neutral model routing contracts (ADR-003)
description: Endpoint-based provider routing, versioned protocol adapters, and canonical model capabilities.
sidebar:
  order: 4
---

# ADR-003: Provider-neutral model routing and capability contracts

- **Status:** Accepted
- **Date:** 2026-10-11
- **Decision:** Evonic uses one configured provider per endpoint/account connection, versioned protocol adapter keys, and the `model-capabilities/v1` contract defined in `backend/tools/lib/model_routing_contracts.py`.

## Context

The registry must support cloud and local endpoints using the same native protocol without inferring behavior from a hostname, a `remote`/`local` label, or a workload-specific provider name. It must also make model assignment and fallback decisions from explicit, composable capability declarations.

This ADR defines public data/validation boundaries only. It does **not** introduce a database migration, provider invocation, credential storage, API/UI changes, or DMSS policy changes.

## Decision

### Resource ownership

| Resource | Owns | Does not own |
| --- | --- | --- |
| **Provider** | One endpoint/account connection, enabled state, protocol adapter key, authentication configuration, non-secret connection options | Workload policy or individual model endpoint/auth overrides |
| **Model** | Provider reference, provider-native model name, model capabilities and operation options | A second endpoint or credential source |
| **Profile** | Workload requirements, primary/fallback model choice, timeout, confidence threshold, and failure policy | Provider wire protocol or authentication |
| **Protocol adapter** | Request lowering, response normalization, authentication strategy, supported operations, retry/error semantics | Profile policy or hostname classification |

There is intentionally **no `provider_deployments` resource**. A second endpoint or account is a second provider. A model references exactly one provider.

### Versioned protocol adapters

Adapter keys describe native wire protocols, not a generic vendor, a locality label, or a workload:

- `openai.chat_completions.v1`
- `openai.responses.v1`
- `anthropic.messages.v1`
- `ollama.chat.v1`
- `codex.responses.v1`
- `systemone.questions.v1`

An endpoint must be an absolute HTTP(S) URL but never selects an adapter by hostname. `remote`/`local` is, at most, descriptive UI metadata and cannot affect routing, credentials, compatibility, or fallback.

### Capabilities and compatibility

`model-capabilities/v1` contains explicit operation evidence (`unsupported`, `advertised`, or `verified`), supported output-schema versions, confidence provenance, and approved data-egress classifications. Every request supplies an `OperationRequirements` set; all requested operations, schema, confidence, and egress requirements must pass for compatibility.

Unknown capability keys and values are rejected. They never silently change routing. Capability evidence is not an entitlement to infer unadvertised behavior.

Confidence has provenance: an unavailable provider confidence remains `null`; it must never be manufactured as `1.0`.

### Resolution, exposure, and fallback

A resolver may create an internal `ResolvedModelRoute` containing model/provider identity, native model name, endpoint, adapter key, effective capabilities, enabled state, credentials, and legacy provenance. Credentials and endpoint internals are never returned by `to_public_dict`, logs, telemetry, or public exceptions.

Fallback is eligible only when the source failure is typed and retryable and the candidate independently satisfies the complete requirement set, including egress restrictions. A fallback does not bypass profile/workload policy.

Provider endpoint/adapter are the forward-looking source of truth. Existing model-level endpoint/auth fields or legacy format fields are compatibility inputs only during an explicitly bounded migration window; no new model-level endpoint/auth override is permitted.

### Extension and diagnostic rules

To add a provider protocol, add a new versioned `AdapterKey` and a `RouteAdapter` that declares its operations, credential strategy, prompt-free discovery path, and response normalization. Do not infer a protocol from an endpoint hostname or add a provider-deployment layer: **there is no provider-deployments resource**. A different endpoint or account is a different provider.

Provider connection tests and model discovery are synthetic health operations: they use the registered adapter's discovery endpoint and must never submit production prompts. Public diagnostics and comparison telemetry may report adapter keys, capability/compatibility codes, and credential *state*, but must redact endpoints, credentials, request bodies, and upstream response bodies.

Before retiring legacy `remote`/`local`, `api_format`, or model endpoint/credential compatibility reads, maintain a measured equivalence window and verify reference-preserving, resumable migrations (row counts, canonical IDs and aliases, defaults, agent/profile/fallback references, and disabled/stale rows). Column removal, if justified, requires a separately verified SQLite table rebuild.

## Consequences

- Registry, profile assignment, runtime, and fallback have one canonical capability vocabulary.
- The same adapter may serve local or remote endpoints without branching on hostnames.
- A disabled provider invalidates every dependent route.
- Future tasks implement persistence, resolver/adapters, migrations, and workload integration against these contracts.
- This ADR does not relax DMSS deterministic safety fallback or approval/write guards.

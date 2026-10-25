---
title: Decision Models contracts (ADR-001)
description: Provider-neutral contract, schema, and result boundaries for structured decisions.
sidebar:
  order: 2
---

# ADR-001: Versioned Decision Models contracts

- **Status:** Accepted
- **Date:** 2026-10-11
- **Decision:** Decision Models use a small, versioned, provider-neutral public contract defined in `backend/tools/lib/decision_models.py`.

## Context

Evonic consumers need structured decisions (for example, an agent choosing a task classification) without coupling their application contracts to a provider's response shape. A decision request must state what result it needs, while a selected Model/provider adapter may vary independently over time.

This ADR establishes data and validation boundaries only. It does **not** introduce persistence, database migrations, provider invocation, credentials, management UI, or consumer policy enforcement.

## Terminology and ownership

| Term | Meaning | Owned by |
| --- | --- | --- |
| **Model** | A configured model capable of producing a decision. Its operational configuration and credentials are outside this contract. | Model/provider subsystem |
| **Provider adapter** | An implementation of `DecisionAdapter` that maps a `DecisionInvocation` to a normalized `DecisionResult`. | Provider integration subsystem |
| **Decision Profile** | A durable, named configuration that identifies a decision purpose, its profile version, and later may select Models/fallbacks. | Future profile registry/persistence subsystem |
| **Consumer** | An agent or service requesting a structured result and interpreting it for its own policy. | Calling subsystem |
| **Contract** | Versioned request, schema, normalized result, validation, and typed error vocabulary. | `decision_models.py` |

The contract neither selects a Model nor contains credentials. Adapters do not own profile policy; consumers do not parse provider-native payloads.

## Public versions and compatibility

- `CONTRACT_VERSION` is `decision-models/v1` and applies to invocation/result envelopes.
- `SCHEMA_VERSION` is `decision-schema/v1` and applies to the schema DSL.
- A request references a profile `{key, version}`. Profile versions are opaque non-empty strings, allowing a later registry to use semver or another documented scheme.
- New optional fields may be added only in a compatible version. A semantic or required-field change requires a new contract/schema version. Validators reject unknown DSL properties and unknown versions rather than silently changing meaning.
- Serialization is canonical: schema fields and returned field results are ordered lexicographically by field key. JSON object context keys are also normalized, supporting stable logging, hashing, and tests.

## Stable built-in profile keys

Built-ins use lowercase dotted namespaces (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`). These keys name a decision purpose, not a provider or a safety policy:

| Key | Intended decision purpose |
| --- | --- |
| `safety.command` | Classify a command-related safety decision. The consuming safety system separately owns all concrete safety policy and enforcement. |
| `agent.task_complexity` | Assess the complexity classification of an agent task. |
| `agent.path_boundary` | Determine whether a proposed path/action is within a consumer-defined boundary. |
| `agent.goal_completion` | Assess whether consumer-supplied goal-completion evidence satisfies its requested outcome. |

Additional keys must remain generic purpose names, not vendor/model identifiers. A stable key can gain a newer profile version; it must not silently change the meanings of existing versions.

## Invocation contract

A `DecisionInvocation` has:

- `profile`: profile key and requested profile version;
- `schema`: the expected versioned `DecisionSchema`;
- `input_context`: JSON-compatible consumer input, sorted during serialization;
- `policy_metadata`: optional JSON-compatible, non-secret consumer metadata that describes request handling rather than provider configuration;
- `correlation_id`: optional bounded tracing identifier; and
- `cancellation`: local, cooperative cancellation/deadline data. It is intentionally not serialized as provider policy.

Consumers must minimize input context and keep credentials, environment values, and unrelated conversation data out of it. The contract itself does not define domain-specific safety rules.

## Schema DSL

A `DecisionSchema` is a non-empty field list with `decision-schema/v1`. Every field has a key, kind, optional description (maximum 2,000 characters), `required` flag, `nullable` flag, and optional default.

| Kind | Required shape | Value semantics |
| --- | --- | --- |
| `choice` | At least two unique non-empty string `choices` | Value/default must be a declared choice (or `null` only when nullable). |
| `boolean` | No choices or bounds | Value/default is `true` or `false` (or nullable `null`). This represents Boolean/Null. |
| `score` | Finite `minimum` and `maximum`, with minimum < maximum | Value/default is a finite number in the inclusive bounds. |

An optional, non-nullable field must supply a default. The DSL deliberately excludes arbitrary JSON Schema, nested result objects, provider settings, and executable expressions in v1.

## Normalized result envelope

A `DecisionResult` reports the profile/schema version, canonical field results, provider metadata, timings, number of attempts, warnings, fallback metadata, and an optional typed `DecisionError`.

Per-field results can include:

- validated field `value`;
- optional `[0, 1]` `confidence` for the chosen value;
- optional candidates with a finite `score` and/or normalized `[0, 1]` `probability`; and
- optional `provider_logits`.

**Scores, probabilities, confidence, and logits are distinct:** a score is a provider/adapter-normalized finite ranking value and has no implied scale; probability and confidence are each normalized inclusively from 0 to 1 but describe different concepts; raw logits are provider-native, unnormalized numeric values. `provider_logits` is omitted unless the provider genuinely supplied raw logits. An adapter must never place a normalized score or probability in `provider_logits`, nor label a derived value as a raw logit.

Typed errors include cancellation, deadline exceeded, invalid request, provider unavailable, invalid provider response, no compatible model, and internal failure. A successful result must contain every required field. An error result may omit field values.

## Consequences

- The model registry may mark an existing model as decision-capable without changing its primary `type`; a model may therefore serve conversation, evaluation, vision, decision, or future uses concurrently.
- Consumers get a predictable validation and error vocabulary.
- Implementations must retain provider-native details only where semantics are explicit, bounded, and safe to expose.
- This ADR intentionally leaves fallback selection and policy enforcement to future owners.

## Registry capability and adapter extension

The registry persists optional additive metadata in `llm_models.capabilities`. Legacy
models have `{}` and retain their pre-existing behavior. A decision-capable model is
currently declared as:

```json
{
  "decision": {
    "structured_output": true,
    "schema_primitives": ["choice", "boolean", "score"],
    "candidate_scores": false,
    "provider_logits": false
  }
}
```

`validate_decision_compatibility` in `backend/tools/lib/decision_adapters.py` is the
server-side guard for both a future Decision Profile assignment and a Direct Model
Test. It checks that the model exists, is enabled, declares structured output,
supports every requested schema primitive, and has a registered adapter format. It
must run before provider invocation and returns an actionable incompatibility error.

The first implementation, `OpenAIStructuredDecisionAdapter`, supports `api_format`
`openai` and sends the v1 DSL as a strict JSON schema to `/chat/completions`. It
maps the returned object to `DecisionResult`, validates it against the requested
schema, applies the configured timeout bounded by the cooperative invocation
deadline, and reports cancellation/deadline/provider/invalid-response failures
through the normalized `DecisionError` vocabulary.

Adapters must never log or expose API keys, resolved credentials, or request
authorization headers. Public capability reporting is intentionally limited to
enabled state and decision capability metadata. Candidate scores and raw provider
logits are optional: an adapter may populate them only when the provider actually
returns values with those semantics. The OpenAI structured-output adapter does not
synthesize either; absent raw logits remain omitted from field results.

To add an adapter, implement the `DecisionAdapter` protocol, add a deterministic
format branch to `adapter_for_model`, preserve the compatibility guard, map provider
usage/timing metadata where available, and add a network-mocked contract test for
success, unsupported schema, cancellation/deadline, and malformed provider output.

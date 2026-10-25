# Decision Runtime API (internal)

`backend.tools.lib.decision_runtime.DecisionRuntime` is the single domain-neutral entry point for invoking a persisted Decision Profile. It resolves the enabled profile and compatible model, invokes a structured-output adapter, validates the normalized response, and performs bounded retry/fallback.

## Calling the runtime

```python
from backend.tools.lib.decision_runtime import DecisionRuntime, RuntimeRequest

runtime = DecisionRuntime(db)
result = runtime.invoke(RuntimeRequest(
    profile_key="application.example",
    profile_version="1",
    input_context={"subject": "opaque application data"},
    channel="internal",
    rate_limit_key="consumer-id",
))
```

`profile_version` must equal the profile's `policy_version`. The returned value is a `DecisionResult` from `decision_models`; consumers must interpret its fields themselves. The runtime does not convert fields into allow/review/block or any other domain-specific policy.

## Invocation contract and limits

- Only enabled, non-`off` profiles are callable. Persisted profile configuration is revalidated for every invocation.
- The profile schema is used directly; requests cannot provide a different schema.
- Requests are limited to 64 KiB serialized JSON; results are limited to 64 KiB.
- Schemas are limited to 32 fields, 32 choices per field, and 2,000 description characters per field.
- A profile timeout is capped at 120 seconds. The adapter receives cancellation and deadline data from `RuntimeRequest.cancellation`.
- Attempts are capped at three and include the primary attempt. A configured fallback is selected after primary failure; configuration retry limits cannot make invocation unbounded.
- Low-confidence successful outputs are treated as failed attempts when any supplied field confidence is lower than `minimum_confidence`.

## Safety and observability

Generic invocation context and policy metadata are rejected when they contain secret-like keys or values (`authorization`, `api_key`, `password`, `secret`, or `token`). Provider credentials remain only in the resolved model mapping and are never included in a returned result or runtime log.

Runtime errors raised before invocation are `DecisionRuntimeError` with a `DecisionRuntimeErrorCode` (`profile_not_found`, `profile_disabled`, `profile_invalid`, `rate_limited`, `payload_too_large`, `secret_detected`, `response_too_large`). Provider/result failures use the established `DecisionErrorCode` in the returned `DecisionResult`. Error messages are fixed safe text and do not echo input or provider responses.

The optional metrics sink receives `decision_runtime.success`, `failure`, `fallback`, `low_confidence`, and `latency_ms` events. Tags contain the profile key and error class only. Logs similarly contain profile key, attempt count, and fallback flag—never context, fields, raw provider content, or credentials.

Decision Activity separately persists a bounded diagnostic record for each returned runtime result. Set `RuntimeRequest.activity_source` to `production` (default), `playground`, or `profile_test` to distinguish callers. Activity writes are best-effort and cannot affect the returned decision. See [Decision Activity API and Privacy Policy](decision-activity.md) for its schema, snapshot allowlist, administrative API, redaction, and retention operations.

## Integration guidance

Pass the application database object and, in production, use the default `OpenAIStructuredDecisionAdapter`. Tests and future provider formats should inject `adapter_factory(model) -> DecisionAdapter`. Consumers should provide a stable channel/rate-limit key appropriate to their own request boundary. Do not use the safety-specific Decim runtime as a substitute for this service.

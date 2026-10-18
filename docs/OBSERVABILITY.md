# Observability

Operational logs, security audit events and OpenTelemetry traces for Vouch.

Everything here rides on the existing `vouch.logger` pipeline. There is no
second logging configuration and no second log pipeline.

## Log schema

`src/vouch/logger.py` emits one JSON object per line to `logs/vouch.log`.

| Field | Source | Notes |
|---|---|---|
| `schema_version` | constant | `1`. Bump on any shape change. |
| `timestamp` | record | UTC, ISO-8601, microseconds, `Z` suffix. |
| `level` | record | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `logger` | record | Dotted module name. |
| `module`, `lineno` | record | Source location. |
| `message` | record | Control characters stripped, length bounded. |
| `exception` | record | Traceback, when `exc_info` is set. Separate from `message`. |
| `stack` | record | Only when `stack_info` is set. |
| `event` | call site via `extra` | Stable dotted event name. |
| `component` | call site via `extra` | Owning module. |
| `investigation_id`, `job_id` | record factory | From `ContextVar`s. See below. |
| `trace_id`, `span_id`, `trace_sampled` | record factory | Absent when no span is active — never `"0"`. |
| everything else | call site via `extra` | Sanitised, bounded, redacted by key. |

Call sites keep using the `event` / `component` / `reason_code` /
`error_category` convention already in `router.py`. Extras are additive: the
formatter never drops a field a call site passed.

### Correlation

`vouch.observability.context.bind_correlation()` sets identifiers for a block.
Values are snapshotted onto the record by `vouch.observability.records` **at
record creation time**, on the emitting thread. The JSON formatter runs later on
the `QueueListener` thread, which has no access to the emitting task's context —
reading them there would silently yield `null`.

These names, and the trace fields, are **reserved**: `logging.Logger.makeRecord`
raises `KeyError` if a call site passes one through `extra=`, so use
`bind_correlation` instead.

### Sanitisation and redaction

`vouch.observability.sanitize` bounds strings (512), collections (20) and
nesting depth (3), strips CR/LF and other control characters so a value cannot
forge a log line, and replaces values whose key matches a credential or raw
content fragment with `[REDACTED]`.

**This is a last line of defence, not the data-protection strategy.** Truncating
a secret does not make it safe to log. Never pass these in the first place:
API keys, tokens, cookies, auth headers, raw prompts, system instructions, model
completions, conversation history, uploaded artifacts, unrestricted tool
arguments or results, and threat-intelligence response bodies.

### Log levels

`VOUCH_LOG_LEVEL` (default `INFO`) sets the root level.

`vouch.audit` is pinned at `INFO` and the queue handler is set to `NOTSET`. These
are a **coupled pair**: a logger's effective level is checked before any handler
sees a record, so root at `WARNING` will not suppress `vouch.audit` — but the
handler's own level would. Removing either half makes audit records disappear
without an error.

## Audit events

Security-relevant, attributable actions travel on the `vouch.audit` logger —
same formatter, same file, same pipeline, separately routable.

| Event | Severity | `dispatched` |
|---|---|---|
| `tool_call.requested` | INFO | not applicable |
| `tool_call.authorization_decided` | INFO | not applicable |
| `tool_call.execution_started` | INFO | must be `true` |
| `tool_call.completed` | INFO | must be `true` |
| `tool_call.failed` | WARNING | required, `true` or `false` |
| `tool_call.timed_out` | WARNING | must be `true` |
| `tool_call.cancelled` | WARNING | must be `true` |

Severity, outcome and required fields come from `EVENT_POLICIES`. Callers cannot
restate them. `dispatched` reflects whether the tool handler was actually
invoked, which is why a denial produces `tool_call.failed` with
`dispatched=false` and no `tool_call.execution_started`.

Fields: `audit_event_id`, `occurred_at`, `tool_call_id`, `tool_name`,
`tool_side_effects`, `tool_version`, `actor_type`, `actor_id`,
`authorization_decision`, `policy_id`, `policy_version`, `reason_code`,
`error_category`, `dispatched`, `attempt_no`, `duration_ms`, `outcome`, plus any
correlation and trace fields the record factory attaches.

### API

```python
auditor.emit(
    AuditEvent.TOOL_CALL_COMPLETED,
    tool_call_id=call_id,
    tool_name="lookup_rdap",
    dispatched=True,
    duration_ms=elapsed_ms,
)
```

One entry point, typed keyword parameters, no `**kwargs`. `emit` never raises:
any failure returns `AuditEmitStatus.FAILED`. A tool call must not behave
differently because an audit write failed.

### Delivery guarantees

`AuditEmitStatus` is named for what actually happened:

| Status | Meaning |
|---|---|
| `ENQUEUED` | Accepted by the logging API and queued for the listener. |
| `EMBEDDED` | Delivered to whatever handlers exist, without the queue. |
| `SKIPPED` | Filtered by level before construction. |
| `FAILED` | Constructed and validated, but delivery raised. |

`ENQUEUED` is **not** a durability guarantee. A `QueueHandler` enqueue says
nothing about the record being written, transmitted or persisted; a process
crash loses whatever is still queued.

**The audit trail is not tamper-proof, append-only, or non-repudiable.** It is a
JSON line in a rotated file.

## Spans

`vouch.observability.spans.span()` starts a span as the current one and never
swallows an exception. It does not record exceptions itself — the OTel context
manager already calls `record_exception` and sets `ERROR` status, and doing both
would double every exception in the span.

| Span | Where |
|---|---|
| `vouch.investigation.fanout` | `router.route` |
| `vouch.tool.execute` | `tools.gateway` dispatch |
| `vouch.llm.generate` | `agents.llm.LLMClient.ainvoke` |
| `vouch.fact.lookup` | `tools.facts.lookup` |
| `vouch.job.process` | `worker.Worker.process` |

No HTTP, database or Redis spans are added by hand: `opentelemetry-instrument`
already emits those in the api container.

### Attributes

`vouch.`-prefixed because there is no semantic convention for an investigation
engine. Defined in `vouch.observability.spans` as constants. Only identifiers
and enums — never prompts, completions, arguments or results.

## Trace propagation

The relay publishes in a different process from the webhook that created the
event, so the relay's own span must not be used.

1. `api/server.create_entry` captures the active span's W3C carrier into the new
   nullable `outbox_events.trace_context` column.
2. `api/outbox.publish_outbox` replays **that** carrier via
   `EventStream.produce(..., trace_context=...)`.
3. `queue/stream.produce` merges it inside the `data` JSON body — not as a
   second Redis field, so the broker contract is unchanged and
   `set(fields) == {"data"}` still holds. Rows with no carrier are published
   byte-identically to before.
4. `worker.Worker.process` extracts it and starts `vouch.job.process` as a child
   of the originating request.

Legacy events and events published without a trace start a fresh root span. No
baggage is carried: it is arbitrary application state and does not belong in a
broker message.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VOUCH_LOG_LEVEL` | `INFO` | Root level. Does not affect `vouch.audit`. |
| `VOUCH_LOG_DIR` | `logs` | Log directory. |
| `OTEL_SERVICE_NAME` | `vouch.ai` | Resource service name, per process in compose. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Collector endpoint. |
| `PYROSCOPE_SERVER_ADDRESS` | `http://localhost:4040` | Continuous profiling. |

## Known limitations

1. **17 pre-existing `test_api_server.py` failures.** API feature gaps (missing
   verify endpoint, `create_entry` arity, digest stored as bytes, payload stored
   as a dict into a `Text` column, hardcoded channel, 500 where 400 is
   expected). Surfaced by the import repair; fixing them means changing HTTP
   status codes.
2. **CI runs zero tests.** With `tests/integration/conftest.py` importable, its
   session-scoped autouse fixture calls `pytest.exit()` when PostgreSQL and
   Redis are absent, so `pytest tests/ -q` collects nothing and exits 0. Run
   `pytest -m "not integration"`.
3. **The fact layer is non-functional.** `@tool` returns a `StructuredTool`, and
   `_bind` awaits it, so every lookup ends in `'StructuredTool' object is not
   callable`, swallowed by `_guard` into `status=MISS`. The new logs make this
   visible; they do not fix it.
4. **`pipeline.run` cannot execute.** `route()` is never awaited at
   `pipeline.py:28`, and `ledger`, `flatten`, `execute_branch`, `investigate`,
   `get_modality_skills`, `select_required_tools` and `update_state` are
   undefined. There is no caller (`WorkerPool(handler=None)`). Not instrumented.
5. **`agents/agents.py` is empty.** No agent phase transitions exist to log.
6. **No ledger writer.** `ledger.append` is undefined, so evidence-persistence
   logging has no target.
7. **The outbox insert cannot succeed.** `OutboxEvent.published_at` is NOT NULL
   and `channel` is required but `create_entry` omits it;
   `tests/integration/test_db_constraints.py` pins this as a strict xfail.
   Trace propagation is unit-verified only, not end-to-end.
8. **`setup_telemetry()` has no call site.** Pyroscope is never configured.
9. **Three gateway paths can end `tool_call.requested` with no terminal event**:
   `model_validate` raising a non-`ValidationError`, `authorize`, and
   `rate_limiter.allow` all propagate unexpected exceptions today. Adding
   handlers would change error semantics.
10. **Gateway `attempt_no` is always 1.** The gateway has no retry loop; the
    real retries are in `utils.retry` and `tools.facts._shared.call`.
11. **Backend delivery is unverified.** Nothing here has been observed reaching
    Tempo, Prometheus or Grafana.
12. **No linter or type checker is configured** in this repository.
13. `router.py` and `pipeline.py` call `get_logger(__file__)`, so those logger
    names are filesystem paths. The formatter never derives a field from the
    logger name.
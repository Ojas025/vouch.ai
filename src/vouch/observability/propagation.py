"""W3C trace-context capture and replay across the Redis stream boundary.

The outbox relay publishes in a different process from the webhook that created
the event, so the relay's own span is not the request's. The originating context
is therefore captured when the outbox row is written and replayed at publish
time, rather than injecting whatever the publisher happens to be doing.

The carrier travels *inside* the existing ``data`` JSON rather than as a second
Redis field, so the broker message contract is unchanged: ``set(fields)`` is
still exactly ``{"data"}``.

Injection is skipped when there is no valid span, which keeps legacy and
non-instrumented publishes byte-identical to what they were before.
"""

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACEPARENT_KEY = "traceparent"
TRACESTATE_KEY = "tracestate"

#: Keys owned by this module inside the message body.
CARRIER_KEYS = frozenset({TRACEPARENT_KEY, TRACESTATE_KEY})


def _propagator() -> TraceContextTextMapPropagator:
    return TraceContextTextMapPropagator()


def capture_trace_context() -> dict[str, str]:
    """W3C carrier for the active span, or ``{}`` when there is not one.

    Never fabricates identifiers: an event created outside any trace simply
    carries none, and its consumer starts a fresh root span.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}

    carrier: dict[str, str] = {}
    _propagator().inject(carrier)
    return carrier


def inject_trace_context(body: dict[str, Any], carrier: dict[str, str]) -> dict[str, Any]:
    """Return ``body`` with the carrier merged in. Mutates and returns ``body``.

    Only W3C trace-context keys are carried. Baggage is deliberately excluded:
    it is arbitrary application state and does not belong in a broker message.
    """
    for key in CARRIER_KEYS:
        if key in carrier:
            body[key] = carrier[key]
    return body


def extract_trace_context(body: dict[str, Any]) -> Context:
    """Rebuild the originating context, or an empty one for legacy events."""
    carrier = {key: body[key] for key in CARRIER_KEYS if key in body}
    if TRACEPARENT_KEY not in carrier:
        return otel_context.Context()
    return _propagator().extract(carrier=carrier)